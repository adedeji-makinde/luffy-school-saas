"""What a real tenant→shared ForeignKey actually does, measured end to end.

docs/tenancy.md carries a hard blocker: the next tenant-scoped model must not
add a ForeignKey back to `accounts` until the policy is decided. That blocker
was originally written from hand-authored DDL matching what `sqlmigrate`
prints, which confirmed the *mechanism* but left the ORM ergonomics inferred
from the deletion collector's design rather than observed. This file closes
that gap with real Django models, a real ForeignKey and a real `.delete()`.

Every `user.delete()` below runs inside `_sanctioned_delete()`. That is not the
subject of these tests — it is how they reach past `accounts.deletion`'s
`pre_delete` guard, which now refuses an unsanctioned delete before any of this
can happen. What is measured here is what Django and Postgres do underneath that
policy, and it is the reason the policy exists; lifting the guard deliberately,
in the one file that documents the failure, keeps that evidence readable.

Why the probe models are defined here instead of in academics/models.py:
shipping a tenant→shared ForeignKey is exactly what the blocker forbids, so
these must not exist in any migration or reach any real schema. They are
registered in the real app registry for the life of this test class only —
which is what makes `User._meta.related_objects` see them and the collector
behave as it would in production — and their tables are built directly with
`schema_editor` inside the test transaction. Nothing survives the class.
"""

from django.apps import apps
from django.db import IntegrityError, connection, models, transaction
from django.db.models.deletion import Collector, ProtectedError
from django.test import TestCase

from accounts.deletion import _sanctioned_delete
from accounts.models import User
from schools.tests.test_tenant_isolation import (
    PASSWORD,
    connected_to,
    make_school,
    query,
)

PROBE_APP = "academics"


def _define_probe_models():
    """Two real tenant-scoped models, one per on_delete policy worth testing."""

    class ProbeCascade(models.Model):
        # What an attendance or fee row would look like: tenant-scoped, with a
        # ForeignKey pointing back out to a shared identity row in public.
        student = models.ForeignKey(
            User, on_delete=models.CASCADE, related_name="probe_cascade_rows"
        )

        class Meta:
            app_label = PROBE_APP
            managed = False  # never migrated; the table is made by hand below

    class ProbeProtect(models.Model):
        student = models.ForeignKey(
            User, on_delete=models.PROTECT, related_name="probe_protect_rows"
        )

        class Meta:
            app_label = PROBE_APP
            managed = False

    return ProbeCascade, ProbeProtect


class CrossSchemaForeignKeyTests(TestCase):
    """Deleting a shared row referenced from more than one tenant schema."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Defining the classes registers them and clears the _meta caches, so
        # User._meta.related_objects picks them up exactly as it would a real
        # model. Undone in tearDownClass so no other test sees them.
        cls.ProbeCascade, cls.ProbeProtect = _define_probe_models()

    @classmethod
    def tearDownClass(cls):
        for model in (cls.ProbeCascade, cls.ProbeProtect):
            del apps.all_models[PROBE_APP][model._meta.model_name]
        apps.clear_cache()
        super().tearDownClass()

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        # Build the probe tables inside each school's schema, the way a real
        # migrate_schemas run would have.
        for school in (self.stmarys, self.grace):
            with connected_to(school):
                with connection.schema_editor() as editor:
                    editor.create_model(self.ProbeCascade)
                    editor.create_model(self.ProbeProtect)

    def _rows_in(self, schema, table):
        return query(f"select count(*) from {schema}.{table}")[0][0]

    # -- what Postgres was actually handed -----------------------------------

    def test_the_foreign_key_binds_to_public_with_no_on_delete_action(self):
        """The DDL claim the blocker rests on, now from a real Django model."""
        rows = query(
            "select confrelid::regclass::text, confdeltype, condeferrable, condeferred "
            "from pg_constraint "
            "where connamespace = 'st_marys'::regnamespace and contype = 'f' "
            "order by 1"
        )
        self.assertTrue(rows, "expected foreign keys in the tenant schema")
        for referenced_table, on_delete, deferrable, deferred in rows:
            # Resolves to the shared table, from inside the tenant schema.
            self.assertEqual(referenced_table, "accounts_user")
            # 'a' is NO ACTION: Django never emits an ON DELETE clause, so the
            # database will not clean up or refuse on its own behalf.
            self.assertEqual(on_delete, "a")
            # ...and the check is deferred to COMMIT, which is what hides it.
            self.assertTrue(deferrable)
            self.assertTrue(deferred)

    # -- what Django's collector can see -------------------------------------

    def test_the_collector_sees_the_relation_but_only_one_schemas_rows(self):
        user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
        for school in (self.stmarys, self.grace):
            with connected_to(school):
                self.ProbeCascade.objects.create(student=user)

        self.assertEqual(self._rows_in("st_marys", "academics_probecascade"), 1)
        self.assertEqual(self._rows_in("grace", "academics_probecascade"), 1)

        with connected_to(self.stmarys):
            collector = Collector(using="default")
            collector.collect([user])
            seen = {model._meta.label for model in collector.data}
            gathered = collector.data[self.ProbeCascade]

        # The relation IS known to Django -- this is not the ORM being unaware
        # of the model. It knows, and still resolves it against one schema.
        self.assertIn("academics.ProbeCascade", seen)
        # Two rows reference this user. The collector gathered St Mary's one.
        self.assertEqual(len(gathered), 1)

    def test_cascade_cleans_the_connected_schema_and_orphans_every_other(self):
        """The heart of it: one schema is tidied, the rest are left behind."""
        user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
        for school in (self.stmarys, self.grace):
            with connected_to(school):
                self.ProbeCascade.objects.create(student=user)

        with connected_to(self.stmarys):
            with transaction.atomic():
                with _sanctioned_delete():
                    user.delete()
                # St Mary's rows were cascaded away...
                self.assertEqual(
                    self._rows_in("st_marys", "academics_probecascade"), 0
                )
                # ...and Grace Academy's were never looked at.
                self.assertEqual(self._rows_in("grace", "academics_probecascade"), 1)
                transaction.set_rollback(True)

    def test_the_delete_appears_to_succeed_and_then_fails_at_commit(self):
        """The failure mode the blocker is about, reproduced through the ORM.

        `.delete()` returns cleanly. Nothing is raised at the point of the
        mistake. The transaction only comes apart when the deferred constraint
        is finally checked, naming a table in a schema this connection was
        never pointed at.
        """
        user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
        with connected_to(self.grace):
            self.ProbeCascade.objects.create(student=user)

        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError) as caught:
                with transaction.atomic():
                    # No exception here. This is the whole problem.
                    with _sanctioned_delete():
                        user.delete()
                    # Stand-in for COMMIT: SET CONSTRAINTS ALL IMMEDIATE, which
                    # is exactly what Postgres does when the transaction ends.
                    connection.check_constraints()

        message = str(caught.exception)
        self.assertIn("academics_probecascade", message)
        # Names a schema the caller never touched.
        self.assertIn("not present in table", message)

    def test_protect_does_not_protect_across_schemas(self):
        """`on_delete=PROTECT` is not a safety net here, which is worse.

        PROTECT works by querying the referencing table; from St Mary's that
        query finds nothing, so the delete is allowed to proceed even though
        Grace Academy is holding a reference. The guarantee people would most
        expect to save them is the one that quietly does not apply.
        """
        user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
        with connected_to(self.grace):
            self.ProbeProtect.objects.create(student=user)

        # Sanity: PROTECT does work when the row is in the connected schema.
        with connected_to(self.grace):
            with self.assertRaises(ProtectedError):
                with transaction.atomic():
                    with _sanctioned_delete():
                        user.delete()

        # But from the other school it raises nothing, and only the deferred
        # constraint catches it -- as an IntegrityError, not a ProtectedError.
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    with _sanctioned_delete():
                        user.delete()
                    connection.check_constraints()

    def test_a_single_tenant_deployment_would_never_reveal_any_of_this(self):
        """Why this is worth a blocker rather than a code review comment.

        With one school, the connected schema is the only schema, so cascade
        tidies everything and PROTECT protects. Every one of these tests passes
        the moment the second school exists, and not before.
        """
        user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
        with connected_to(self.stmarys):
            self.ProbeCascade.objects.create(student=user)

            with transaction.atomic():
                with _sanctioned_delete():
                    user.delete()
                connection.check_constraints()  # no complaint
                self.assertEqual(
                    self._rows_in("st_marys", "academics_probecascade"), 0
                )
                transaction.set_rollback(True)
