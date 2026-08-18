"""Proof that a User cannot be hard-deleted out from under a reference.

The interesting case is the one no database constraint catches. A tenant-scoped
row in Grace Academy's schema referencing a shared user is invisible to a
connection pointed at St Mary's, so `on_delete=PROTECT` raises nothing and the
delete only comes apart at `COMMIT`. That was measured in PR #3
(`schools/tests/test_cross_schema_fk.py`); this file shows `hard_delete_user()`
catching what `PROTECT` misses, and does it with the same probe-model pattern —
real Django models with a real ForeignKey into `public`, registered for the life
of the test class only, with their tables built by `schema_editor` inside the
test transaction. They must not reach a migration: shipping a tenant→shared
ForeignKey is still the thing `docs/tenancy.md` forbids.
"""

from unittest import mock

from django.apps import apps
from django.contrib.auth import authenticate
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.test import TestCase

from accounts import deletion
from accounts.deletion import (
    _sanctioned_delete,
    deactivate_user,
    find_references,
    hard_delete_user,
    reactivate_user,
)
from accounts.models import Membership, Role, User
from schools.tests.test_tenant_isolation import (
    PASSWORD,
    connected_to,
    make_school,
    query,
)

PROBE_APP = "academics"


def _define_probe_models():
    """Tenant-scoped models holding a PROTECT reference to a shared user.

    This is the shape of a future attendance or fee row: it lives in one
    school's schema and points back out to `public.accounts_user`.

    The second one differs by a single field option, `related_name="+"`, and
    that option is the whole reason it is here. It makes the relation *hidden*,
    which drops it from `User._meta.related_objects` while leaving it in
    `get_fields(include_hidden=True)` — the set Django's deletion collector
    actually reads. A scan built on the former would not see this model at all.
    """

    class DeletionProbe(models.Model):
        student = models.ForeignKey(
            User, on_delete=models.PROTECT, related_name="deletion_probe_rows"
        )

        class Meta:
            app_label = PROBE_APP
            managed = False  # never migrated; the table is made by hand below

    class HiddenProbe(models.Model):
        # No reverse accessor. An ordinary thing to write, and it used to make
        # this row invisible to the guard.
        student = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")

        class Meta:
            app_label = PROBE_APP
            managed = False

    return DeletionProbe, HiddenProbe


class DeactivationIsTheStandardPathTests(TestCase):
    """`is_active` already does the job, and does it without erasing history."""

    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
        self.membership = Membership.objects.create(
            user=self.user, school=self.school, role=Role.TEACHER
        )

    def test_deactivating_refuses_the_sign_in(self):
        self.assertIsNotNone(authenticate(username="ada", password=PASSWORD))
        deactivate_user(self.user)
        # IdentifierBackend inherits user_can_authenticate(), so this is the
        # existing Django behaviour rather than anything new.
        self.assertIsNone(authenticate(username="ada", password=PASSWORD))

    def test_deactivating_keeps_every_record_intact(self):
        deactivate_user(self.user)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.user_id, self.user.pk)
        self.assertEqual(Membership.objects.filter(user=self.user).count(), 1)

    def test_it_is_reversible(self):
        deactivate_user(self.user)
        reactivate_user(self.user)
        self.assertIsNotNone(authenticate(username="ada", password=PASSWORD))

    def test_calling_delete_on_the_instance_is_refused(self):
        """The policy is enforced, not merely written down.

        Being a module-level function keeps a hard delete off the end of a
        queryset chain. It does not stop `.delete()` existing, so something has
        to actually say no — otherwise any view or shell session erases a person
        and cascades their memberships without the scan ever running.
        """
        # The refusal is raised from inside the collector's own atomic block, so
        # it marks the surrounding transaction for rollback exactly as any other
        # failure mid-delete would. A savepoint keeps the assertions below
        # runnable — the same idiom test_tenant_isolation.py uses.
        with self.assertRaises(ValidationError) as caught:
            with transaction.atomic():
                self.user.delete()
        self.assertIn("deactivate_user", " ".join(caught.exception.messages))
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertEqual(Membership.objects.filter(user=self.user).count(), 1)

    def test_a_bulk_queryset_delete_is_refused_too(self):
        """The route that would otherwise skip per-object signals entirely.

        `Collector.can_fast_delete()` returns False once a model has
        `pre_delete` listeners, so registering the receiver also takes away the
        fast path that would have deleted these rows without asking anyone.
        """
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                User.objects.filter(pk=self.user.pk).delete()
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_the_sentinel_does_not_stay_open_after_a_delete(self):
        """A leaked sentinel would disarm the guard for the rest of the thread."""
        loose = User.objects.create_user("kemi", PASSWORD, full_name="Kemi Bello")
        hard_delete_user(loose)  # succeeds, and closes the door behind it

        with self.assertRaises(ValidationError):
            with transaction.atomic():
                self.user.delete()


class HardDeleteGuardTests(TestCase):
    """What `hard_delete_user()` refuses, and what it lets through."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Registering the models clears the _meta caches, so they show up in
        # User._meta.get_fields() exactly as migrated models would — which is
        # what the scan reads.
        cls.DeletionProbe, cls.HiddenProbe = _define_probe_models()

    @classmethod
    def tearDownClass(cls):
        for model in (cls.DeletionProbe, cls.HiddenProbe):
            del apps.all_models[PROBE_APP][model._meta.model_name]
        apps.clear_cache()
        super().tearDownClass()

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        for school in (self.stmarys, self.grace):
            with connected_to(school):
                with connection.schema_editor() as editor:
                    editor.create_model(self.DeletionProbe)
                    editor.create_model(self.HiddenProbe)
        self.user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")

    # -- the case no constraint catches --------------------------------------

    def test_protect_alone_does_not_catch_a_reference_in_another_schema(self):
        """The premise, with the guard deliberately lifted.

        `_sanctioned_delete()` here is not the code under test — it is how this
        test reaches the raw Django behaviour the guard was written to stop.
        Without it the `pre_delete` receiver refuses the delete and this stops
        demonstrating anything.
        """
        with connected_to(self.grace):
            self.DeletionProbe.objects.create(student=self.user)

        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    with _sanctioned_delete():
                        self.user.delete()  # raises nothing at all
                    connection.check_constraints()  # stands in for COMMIT

    def test_a_hidden_relation_is_scanned_too(self):
        """`related_name="+"` must not be a way past the guard.

        The collector reads `get_fields(include_hidden=True)` and would happily
        delete against this relation; `_meta.related_objects` — the obvious
        thing for the scan to use — filters it out. Building the scan on the
        latter left a one-word field option that silently disabled it.
        """
        with connected_to(self.grace):
            self.HiddenProbe.objects.create(student=self.user)

        self.assertEqual(
            [reference.model for reference in find_references(self.user)],
            [self.HiddenProbe],
        )
        with self.assertRaises(ValidationError) as caught:
            hard_delete_user(self.user)
        self.assertIn("grace", " ".join(caught.exception.messages))
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_hard_delete_refuses_when_another_schema_holds_a_reference(self):
        with connected_to(self.grace):
            self.DeletionProbe.objects.create(student=self.user)

        with connected_to(self.stmarys):
            with self.assertRaises(ValidationError) as caught:
                hard_delete_user(self.user)

        message = str(caught.exception)
        # Names the schema the caller was never connected to...
        self.assertIn("grace", message)
        self.assertIn("academics.DeletionProbe", message)
        # ...and points at the supported alternative.
        self.assertIn("deactivate_user", message)
        # The user is still there, which is the whole point.
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_it_names_every_offending_schema_not_just_the_first(self):
        for school in (self.stmarys, self.grace):
            with connected_to(school):
                self.DeletionProbe.objects.create(student=self.user)

        with self.assertRaises(ValidationError) as caught:
            hard_delete_user(self.user)

        message = str(caught.exception)
        self.assertIn("st_marys", message)
        self.assertIn("grace", message)

    # -- shared references, counted once -------------------------------------

    def test_a_membership_blocks_the_delete_and_is_reported_against_public(self):
        """`Membership` is shared, so it must be reported once, in `public`.

        There is one `accounts_membership` table and it lives in `public`.
        Because `schema_context` leaves `public` on the `search_path`, querying
        it from inside a tenant reads that same table again — so scanning every
        schema for it would name every school on the platform as an offender
        for a user who holds a single membership at one of them.
        """
        Membership.objects.create(user=self.user, school=self.stmarys, role=Role.TEACHER)

        with self.assertRaises(ValidationError) as caught:
            hard_delete_user(self.user)

        message = str(caught.exception)
        self.assertIn("accounts.Membership", message)
        self.assertIn("public", message)
        self.assertNotIn("grace", message)

        schemas = [
            reference.schema_name
            for reference in find_references(self.user)
            if reference.model is Membership
        ]
        self.assertEqual(schemas, ["public"])

    def test_an_ended_membership_still_blocks(self):
        """History is a reference too. Ending is not the same as unlinking."""
        membership = Membership.objects.create(
            user=self.user, school=self.stmarys, role=Role.TEACHER
        )
        membership.end()

        with self.assertRaises(ValidationError):
            hard_delete_user(self.user)

    # -- and the delete that is genuinely safe --------------------------------

    def test_it_deletes_cleanly_when_nothing_references_the_user(self):
        self.assertEqual(find_references(self.user), [])

        hard_delete_user(self.user)

        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        # Really gone from the shared table, not merely filtered out.
        self.assertEqual(
            query("select count(*) from public.accounts_user where username = 'ada'")[0][0],
            0,
        )

    def test_it_deletes_cleanly_once_the_blocking_rows_are_removed(self):
        with connected_to(self.grace):
            probe = self.DeletionProbe.objects.create(student=self.user)
        with self.assertRaises(ValidationError):
            hard_delete_user(self.user)

        with connected_to(self.grace):
            probe.delete()

        hard_delete_user(self.user)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        # ...and no deferred constraint is waiting to fire at COMMIT.
        connection.check_constraints()

    def test_it_leaves_the_connection_where_it_found_it(self):
        """The scan walks every schema; it must not strand the caller in one."""
        with connected_to(self.stmarys):
            hard_delete_user(self.user)
            self.assertEqual(connection.schema_name, "st_marys")

    # -- how the refusal reads -----------------------------------------------

    def test_the_refusal_survives_being_logged(self):
        """One message per line, not one string with newlines inside it.

        `ValidationError` renders as `repr(self.messages)`, so a message built
        with embedded newlines comes back out as literal backslash-n inside a
        list repr the moment anything logs `str(exc)`.
        """
        for school in (self.stmarys, self.grace):
            with connected_to(school):
                self.DeletionProbe.objects.create(student=self.user)

        with self.assertRaises(ValidationError) as caught:
            hard_delete_user(self.user)

        messages = caught.exception.messages
        self.assertEqual(len(messages), 4)  # header, two schemas, remedy
        self.assertNotIn("\n", "".join(messages))
        self.assertIn("grace: academics.DeletionProbe (1 row)", messages)
        self.assertIn("st_marys: academics.DeletionProbe (1 row)", messages)

    # -- an app the scan cannot place ----------------------------------------

    def test_an_app_in_neither_setting_is_an_error_not_an_empty_result(self):
        """Fail closed. A relation nobody scans is the one thing to never allow.

        Installing an app by appending it to INSTALLED_APPS rather than to
        SHARED_APPS is ordinary, and it used to drop that app's references out
        of both buckets silently — so the scan reported nothing and the delete
        went ahead.
        """
        with mock.patch.object(deletion, "TENANT_APP_LABELS", frozenset()):
            with self.assertRaises(ImproperlyConfigured) as caught:
                find_references(self.user)

        message = str(caught.exception)
        self.assertIn("academics", message)
        self.assertIn("SHARED_APPS", message)
