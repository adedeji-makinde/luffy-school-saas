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

from django.apps import apps
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.test import TestCase

from accounts.deletion import deactivate_user, find_references, hard_delete_user, reactivate_user
from accounts.models import Membership, Role, User
from schools.tests.test_tenant_isolation import (
    PASSWORD,
    connected_to,
    make_school,
    query,
)

PROBE_APP = "academics"


def _define_probe_model():
    """A tenant-scoped model holding a PROTECT reference to a shared user.

    This is the shape of a future attendance or fee row: it lives in one
    school's schema and points back out to `public.accounts_user`.
    """

    class DeletionProbe(models.Model):
        student = models.ForeignKey(
            User, on_delete=models.PROTECT, related_name="deletion_probe_rows"
        )

        class Meta:
            app_label = PROBE_APP
            managed = False  # never migrated; the table is made by hand below

    return DeletionProbe


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

    def test_a_hard_delete_is_not_reachable_by_chaining(self):
        """The reason it is a function and not a manager method.

        If this ever grows a queryset method, `User.objects.filter(...)` becomes
        a way to erase people without passing the guard below.
        """
        self.assertFalse(hasattr(User.objects, "hard_delete_user"))
        self.assertFalse(hasattr(User.objects.all(), "hard_delete_user"))


class HardDeleteGuardTests(TestCase):
    """What `hard_delete_user()` refuses, and what it lets through."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Registering the model clears the _meta caches, so it shows up in
        # User._meta.related_objects exactly as a migrated model would — which
        # is what the scan reads.
        cls.DeletionProbe = _define_probe_model()

    @classmethod
    def tearDownClass(cls):
        del apps.all_models[PROBE_APP][cls.DeletionProbe._meta.model_name]
        apps.clear_cache()
        super().tearDownClass()

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        for school in (self.stmarys, self.grace):
            with connected_to(school):
                with connection.schema_editor() as editor:
                    editor.create_model(self.DeletionProbe)
        self.user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")

    # -- the case no constraint catches --------------------------------------

    def test_protect_alone_does_not_catch_a_reference_in_another_schema(self):
        """The premise. Without the guard, this delete looks like it worked."""
        with connected_to(self.grace):
            self.DeletionProbe.objects.create(student=self.user)

        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    self.user.delete()  # raises nothing at all
                    connection.check_constraints()  # stands in for COMMIT

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
