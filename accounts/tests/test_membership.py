"""Proof that logins are not staff-only and that families have the right shape."""

import contextlib

from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase

from accounts import services
from accounts.middleware import SchoolAccessMiddleware
from accounts.models import (
    FAMILY_ROLES,
    STAFF_ROLES,
    Guardianship,
    Membership,
    MembershipStatus,
    Relationship,
    Role,
    User,
)
from schools.models import School

PASSWORD = "correct-horse-battery"


def make_school(name, slug, schema_name):
    school = School(name=name, slug=slug, schema_name=schema_name)
    # These tests only touch the public schema, so skip CREATE SCHEMA.
    school.auto_create_schema = False
    school.save()
    return school


def make_user(username, full_name, **extra):
    return User.objects.create_user(username, PASSWORD, full_name=full_name, **extra)


@contextlib.contextmanager
def on_host_of(school):
    """Pretend the request arrived on `school`'s domain (or the portal if None).

    Only sets the marker TenantMainMiddleware would set; the search_path is
    untouched, which is fine because every model here is public-schema.
    """
    previous = getattr(connection, "tenant", None)
    connection.tenant = school
    try:
        yield
    finally:
        connection.tenant = previous


class EveryRoleGetsALoginTests(TestCase):
    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")

    def test_all_six_roles_can_sign_in_and_are_scoped_to_the_school(self):
        for role in Role:
            with self.subTest(role=role.value):
                user = make_user(f"user-{role.value}", f"Person {role.value}")
                services.grant_membership(user, self.school, role)

                self.assertEqual(
                    authenticate(username=f"user-{role.value}", password=PASSWORD), user
                )
                self.assertTrue(user.has_access_to(self.school))
                self.assertEqual(user.roles_at(self.school), {role.value})

    def test_roles_cover_staff_and_family_with_nothing_left_over(self):
        self.assertEqual(STAFF_ROLES | FAMILY_ROLES, set(Role.values))
        self.assertEqual(len(Role.values), 6)

    def test_no_role_confers_platform_staff(self):
        principal = make_user("head", "Head Teacher")
        services.grant_membership(principal, self.school, Role.PRINCIPAL)
        self.assertFalse(principal.is_platform_staff)
        self.assertFalse(principal.is_staff)

    def test_role_groupings_match_both_db_strings_and_enum_members(self):
        """TextChoices mixes in str, so members compare and hash by value."""
        membership = services.grant_membership(
            make_user("bursar-1", "Bursar"), self.school, Role.BURSAR
        )
        membership.refresh_from_db()
        self.assertIsInstance(membership.role, str)  # plain string off the wire
        self.assertTrue(membership.is_staff_role)

        self.assertIn("admin", STAFF_ROLES)
        self.assertIn(Role.ADMIN, STAFF_ROLES)
        self.assertIn("admin", {Role.ADMIN})


class SignInIdentifierTests(TestCase):
    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")

    def test_staff_parent_and_student_identifiers_all_resolve(self):
        teacher = make_user("ada@stmarys.ng", "Ada Obi", email="Ada@Stmarys.NG")
        parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        student = make_user("STM/2026/0042", "Tunde Ade")

        self.assertEqual(authenticate(username="ada@stmarys.ng", password=PASSWORD), teacher)
        self.assertEqual(authenticate(username="ADA@STMARYS.NG", password=PASSWORD), teacher)
        self.assertEqual(authenticate(username="08031234567", password=PASSWORD), parent)
        self.assertEqual(authenticate(username="STM/2026/0042", password=PASSWORD), student)

    def test_a_student_needs_neither_email_nor_phone(self):
        first = make_user("STM/2026/0001", "Child One")
        second = make_user("STM/2026/0002", "Child Two")
        self.assertIsNone(first.email)
        self.assertIsNone(second.phone)  # blanks stored as NULL, so no collision

    def test_wrong_password_and_unknown_identifier_both_fail(self):
        make_user("ada@stmarys.ng", "Ada Obi", email="ada@stmarys.ng")
        self.assertIsNone(authenticate(username="ada@stmarys.ng", password="wrong"))
        self.assertIsNone(authenticate(username="nobody@nowhere.ng", password=PASSWORD))

    def test_inactive_user_cannot_sign_in(self):
        user = make_user("suspended", "Suspended Person")
        User.objects.filter(pk=user.pk).update(is_active=False)
        self.assertIsNone(authenticate(username="suspended", password=PASSWORD))


class StudentBelongsToOneSchoolTests(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.child = make_user("STM/2026/0042", "Tunde Ade")

    def test_a_second_live_student_membership_is_rejected_by_the_database(self):
        services.enroll_student(self.child, self.stmarys)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(
                user=self.child, school=self.grace, role=Role.STUDENT
            )

    def test_enrolling_twice_elsewhere_raises_a_readable_error(self):
        services.enroll_student(self.child, self.stmarys)
        with self.assertRaises(services.AlreadyEnrolled) as caught:
            services.enroll_student(self.child, self.grace)
        self.assertIn("already enrolled", str(caught.exception))

    def test_re_enrolling_at_the_same_school_is_idempotent(self):
        first = services.enroll_student(self.child, self.stmarys, reference="0042")
        again = services.enroll_student(self.child, self.stmarys)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(self.child.memberships.students().count(), 1)

    def test_ended_membership_frees_the_constraint_and_keeps_history(self):
        old = services.enroll_student(self.child, self.stmarys)
        old.end()
        new = services.enroll_student(self.child, self.grace)

        self.assertNotEqual(old.pk, new.pk)
        self.assertEqual(self.child.memberships.students().count(), 2)  # history kept
        self.assertEqual(self.child.student_membership(), new)  # only one is live

    def test_staff_may_hold_the_same_role_at_several_schools(self):
        """The one-school rule is for students only."""
        teacher = make_user("ada", "Ada Obi")
        services.grant_membership(teacher, self.stmarys, Role.TEACHER)
        services.grant_membership(teacher, self.grace, Role.TEACHER)
        self.assertEqual(teacher.schools().count(), 2)


class ParentAcrossSchoolsTests(TestCase):
    """The headline case: one login, three children, two schools."""

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

        self.parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        self.ada = services.enroll_student(make_user("STM/1", "Ada Ade"), self.stmarys)
        self.tunde = services.enroll_student(make_user("STM/2", "Tunde Ade"), self.stmarys)
        self.zainab = services.enroll_student(make_user("GA/1", "Zainab Ade"), self.grace)

        for child in (self.ada, self.tunde, self.zainab):
            services.link_guardian(self.parent, child, relationship=Relationship.MOTHER)

    def test_one_login_sees_every_child_at_every_school(self):
        self.assertEqual(
            [c.user.full_name for c in self.parent.children()],
            ["Zainab Ade", "Ada Ade", "Tunde Ade"],  # grouped by school name
        )

    def test_linking_children_granted_a_parent_membership_at_each_school(self):
        self.assertEqual(
            set(self.parent.schools().values_list("slug", flat=True)), {"st-marys", "grace"}
        )
        self.assertEqual(self.parent.memberships.parents().live().count(), 2)
        self.assertEqual(self.parent.roles_at(self.grace), {Role.PARENT.value})

    def test_dashboard_groups_children_by_school(self):
        dashboard = services.parent_dashboard(self.parent)
        self.assertEqual([school.name for school, _ in dashboard], ["Grace Academy", "St Mary's"])
        self.assertEqual([len(children) for _, children in dashboard], [1, 2])

    def test_a_parent_reaches_only_the_schools_their_children_attend(self):
        other = make_school("Kings College", "kings", "kings")
        self.assertTrue(self.parent.has_access_to(self.stmarys))
        self.assertFalse(self.parent.has_access_to(other))

    def test_children_see_only_themselves(self):
        self.assertEqual(self.ada.user.children().count(), 0)
        self.assertEqual(self.ada.user.student_membership(), self.ada)
        self.assertFalse(self.ada.user.has_access_to(self.grace))

    def test_both_parents_can_guard_the_same_child(self):
        father = make_user("08099999999", "Femi Ade", phone="08099999999")
        services.link_guardian(father, self.ada, relationship=Relationship.FATHER)
        self.assertEqual(self.ada.guardians().count(), 2)

    def test_only_one_primary_contact_per_child(self):
        father = make_user("08099999999", "Femi Ade", phone="08099999999")
        services.link_guardian(self.parent, self.ada, is_primary_contact=True)
        services.link_guardian(father, self.ada, is_primary_contact=True)

        primaries = Guardianship.objects.filter(student=self.ada, is_primary_contact=True)
        self.assertEqual(primaries.count(), 1)
        self.assertEqual(primaries.get().guardian, father)

    def test_unlinking_the_last_child_at_a_school_ends_access_there(self):
        services.unlink_guardian(self.parent, self.zainab)

        self.assertFalse(self.parent.has_access_to(self.grace))
        self.assertTrue(self.parent.has_access_to(self.stmarys))  # two children remain
        self.assertEqual(self.parent.children().count(), 2)

    def test_unlinking_one_of_two_children_keeps_access(self):
        services.unlink_guardian(self.parent, self.ada)
        self.assertTrue(self.parent.has_access_to(self.stmarys))
        self.assertEqual(self.parent.roles_at(self.stmarys), {Role.PARENT.value})


class TransferCarriesTheFamilyTests(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        self.child = services.enroll_student(make_user("STM/1", "Ada Ade"), self.stmarys)
        services.link_guardian(
            self.parent, self.child, relationship=Relationship.MOTHER, is_primary_contact=True
        )

    def test_transfer_moves_child_and_guardian_and_drops_the_old_school(self):
        moved = services.transfer_student(self.child, self.grace, reference="GA/77")

        self.child.refresh_from_db()
        self.assertEqual(self.child.status, MembershipStatus.ENDED)
        self.assertIsNotNone(self.child.ended_on)

        self.assertEqual(moved.school, self.grace)
        self.assertEqual(moved.reference, "GA/77")
        self.assertEqual(moved.status, MembershipStatus.ACTIVE)

        self.assertEqual([c.pk for c in self.parent.children()], [moved.pk])
        self.assertTrue(self.parent.has_access_to(self.grace))
        self.assertFalse(self.parent.has_access_to(self.stmarys))
        self.assertTrue(
            Guardianship.objects.get(guardian=self.parent, student=moved).is_primary_contact
        )

    def test_transfer_keeps_the_parent_at_the_old_school_for_a_sibling(self):
        sibling = services.enroll_student(make_user("STM/2", "Tunde Ade"), self.stmarys)
        services.link_guardian(self.parent, sibling)

        services.transfer_student(self.child, self.grace)

        self.assertTrue(self.parent.has_access_to(self.stmarys))
        self.assertEqual(self.parent.schools().count(), 2)
        self.assertEqual(self.parent.children().count(), 2)

    def test_only_a_student_membership_can_be_transferred(self):
        teacher = services.grant_membership(
            make_user("ada", "Ada Obi"), self.stmarys, Role.TEACHER
        )
        with self.assertRaises(services.NotAStudent):
            services.transfer_student(teacher, self.grace)


class OnePersonManyRolesTests(TestCase):
    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.teacher = make_user("ada@stmarys.ng", "Ada Obi", email="ada@stmarys.ng")

    def test_a_teacher_can_also_be_a_parent_at_the_same_school(self):
        services.grant_membership(self.teacher, self.school, Role.TEACHER)
        child = services.enroll_student(make_user("STM/1", "Chidi Obi"), self.school)
        services.link_guardian(self.teacher, child, relationship=Relationship.MOTHER)

        self.assertEqual(
            self.teacher.roles_at(self.school), {Role.TEACHER.value, Role.PARENT.value}
        )
        self.assertEqual(self.teacher.memberships.live().count(), 2)

    def test_losing_the_parent_role_leaves_the_teaching_role_intact(self):
        services.grant_membership(self.teacher, self.school, Role.TEACHER)
        child = services.enroll_student(make_user("STM/1", "Chidi Obi"), self.school)
        services.link_guardian(self.teacher, child)

        services.unlink_guardian(self.teacher, child)

        self.assertEqual(self.teacher.roles_at(self.school), {Role.TEACHER.value})
        self.assertTrue(self.teacher.has_access_to(self.school))

    def test_the_same_role_twice_at_one_school_is_rejected(self):
        services.grant_membership(self.teacher, self.school, Role.TEACHER)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(
                user=self.teacher, school=self.school, role=Role.TEACHER
            )

    def test_grant_membership_revives_an_ended_one(self):
        membership = services.grant_membership(self.teacher, self.school, Role.BURSAR)
        membership.end()

        revived = services.grant_membership(self.teacher, self.school, Role.BURSAR)
        self.assertEqual(revived.pk, membership.pk)
        self.assertEqual(revived.status, MembershipStatus.ACTIVE)
        self.assertIsNone(revived.ended_on)


class GuardianshipRulesTests(TestCase):
    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        self.child = services.enroll_student(make_user("STM/1", "Ada Ade"), self.school)

    def test_a_guardianship_must_point_at_a_student(self):
        bursar = services.grant_membership(
            make_user("bursar", "Bursar Person"), self.school, Role.BURSAR
        )
        with self.assertRaises(services.NotAStudent):
            services.link_guardian(self.parent, bursar)

        with self.assertRaises(ValidationError):
            Guardianship(guardian=self.parent, student=bursar).full_clean()

    def test_nobody_guards_themselves(self):
        with self.assertRaises(services.MembershipError):
            services.link_guardian(self.child.user, self.child)

        with self.assertRaises(ValidationError):
            Guardianship(guardian=self.child.user, student=self.child).full_clean()

    def test_linking_the_same_pair_twice_is_idempotent(self):
        first = services.link_guardian(self.parent, self.child)
        again = services.link_guardian(self.parent, self.child)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Guardianship.objects.count(), 1)

    def test_a_suspended_child_still_belongs_to_the_school(self):
        services.link_guardian(self.parent, self.child)
        Membership.objects.filter(pk=self.child.pk).update(
            status=MembershipStatus.SUSPENDED
        )
        self.assertEqual(self.parent.children().count(), 1)


class GrantAuthorityStopsAtOwnSchoolTests(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

        self.admin = make_user("admin@stmarys.ng", "Office Admin")
        services.grant_membership(self.admin, self.stmarys, Role.ADMIN)
        self.newcomer = make_user("STM/9", "New Child")

    def test_an_admin_grants_memberships_at_their_own_school(self):
        self.assertTrue(services.can_grant_memberships(self.admin, self.stmarys))
        membership = services.enroll_student_as(self.admin, self.newcomer, self.stmarys)
        self.assertEqual(membership.school, self.stmarys)

    def test_an_admin_cannot_reach_another_school(self):
        self.assertFalse(services.can_grant_memberships(self.admin, self.grace))
        with self.assertRaises(services.NotPermitted):
            services.enroll_student_as(self.admin, self.newcomer, self.grace)
        with self.assertRaises(services.NotPermitted):
            services.grant_membership_as(
                self.admin, self.newcomer, self.grace, Role.TEACHER
            )
        self.assertEqual(Membership.objects.filter(school=self.grace).count(), 0)

    def test_platform_staff_reach_every_school(self):
        operator = make_user("ops", "Ops Person", is_platform_staff=True)
        self.assertTrue(services.can_grant_memberships(operator, self.grace))
        services.enroll_student_as(operator, self.newcomer, self.grace)
        self.assertEqual(self.newcomer.student_membership().school, self.grace)

    def test_other_roles_cannot_grant_even_at_their_own_school(self):
        for role in (Role.PRINCIPAL, Role.TEACHER, Role.BURSAR, Role.PARENT):
            with self.subTest(role=role.value):
                person = make_user(f"person-{role.value}", f"Person {role.value}")
                services.grant_membership(person, self.stmarys, role)
                self.assertFalse(services.can_grant_memberships(person, self.stmarys))
                with self.assertRaises(services.NotPermitted):
                    services.grant_membership_as(
                        person, self.newcomer, self.stmarys, Role.STUDENT
                    )

    def test_linking_a_guardian_needs_authority_at_the_childs_school(self):
        child = services.enroll_student(make_user("GA/1", "Grace Child"), self.grace)
        parent = make_user("08031234567", "Bisi Ade", phone="08031234567")

        with self.assertRaises(services.NotPermitted):
            services.link_guardian_as(self.admin, parent, child)
        self.assertEqual(parent.children().count(), 0)

    def test_a_transfer_needs_authority_at_both_ends(self):
        child = services.enroll_student(make_user("STM/1", "Ada Ade"), self.stmarys)

        # Admin holds St Mary's only, so a move to Grace is refused.
        with self.assertRaises(services.NotPermitted):
            services.transfer_student_as(self.admin, child, self.grace)
        child.refresh_from_db()
        self.assertEqual(child.status, MembershipStatus.ACTIVE)

        services.grant_membership(self.admin, self.grace, Role.ADMIN)
        moved = services.transfer_student_as(self.admin, child, self.grace)
        self.assertEqual(moved.school, self.grace)

    def test_an_admin_who_is_not_active_loses_the_authority(self):
        for status in (
            MembershipStatus.INVITED,
            MembershipStatus.SUSPENDED,
            MembershipStatus.ENDED,
        ):
            with self.subTest(status=status.value):
                Membership.objects.filter(
                    user=self.admin, school=self.stmarys
                ).update(status=status)
                self.assertFalse(
                    services.can_grant_memberships(self.admin, self.stmarys)
                )


class AccessRequiresActiveStatusTests(TestCase):
    """Invited is an offer, not access. Suspended withdraws it.

    Both still occupy the relationship, which is a different question from
    whether the person can sign in.
    """

    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.factory = RequestFactory()
        self.middleware = SchoolAccessMiddleware(lambda request: "ok")
        self.teacher = make_user("ada@stmarys.ng", "Ada Obi", email="ada@stmarys.ng")

    def set_status(self, user, status):
        Membership.objects.filter(user=user, school=self.school).update(status=status)

    def test_only_an_active_membership_grants_access(self):
        services.grant_membership(self.teacher, self.school, Role.TEACHER)
        expected = {
            MembershipStatus.ACTIVE: True,
            MembershipStatus.INVITED: False,
            MembershipStatus.SUSPENDED: False,
            MembershipStatus.ENDED: False,
        }
        for status, allowed in expected.items():
            with self.subTest(status=status.value):
                self.set_status(self.teacher, status)
                self.assertEqual(self.teacher.has_access_to(self.school), allowed)
                self.assertEqual(
                    self.teacher.roles_at(self.school),
                    {Role.TEACHER.value} if allowed else set(),
                )
                self.assertEqual(self.teacher.schools().count(), 1 if allowed else 0)

    def test_an_invited_person_is_refused_at_the_school_door(self):
        services.grant_membership(
            self.teacher, self.school, Role.TEACHER, status=MembershipStatus.INVITED
        )
        request = self.factory.get("/")
        request.user = self.teacher
        with on_host_of(self.school):
            with self.assertRaises(PermissionDenied):
                self.middleware(request)

    def test_an_existing_parent_invited_to_a_second_school_waits_at_the_door(self):
        """The case this rule exists for: they can already sign in elsewhere."""
        child = services.enroll_student(make_user("STM/1", "Ada Ade"), self.school)
        parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        services.link_guardian(parent, child)

        Membership.objects.create(
            user=parent,
            school=self.grace,
            role=Role.PARENT,
            status=MembershipStatus.INVITED,
        )

        self.assertTrue(parent.has_access_to(self.school))
        self.assertFalse(parent.has_access_to(self.grace))
        self.assertEqual(parent.live_memberships().count(), 2)  # relationship exists

    def test_a_parent_sees_an_invited_child_before_that_child_can_sign_in(self):
        parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        child = services.enroll_student(
            make_user("STM/1", "Ada Ade"), self.school, status=MembershipStatus.INVITED
        )
        services.link_guardian(parent, child)

        self.assertEqual(parent.children().count(), 1)
        self.assertEqual(
            [c.user.full_name for _, kids in services.parent_dashboard(parent) for c in kids],
            ["Ada Ade"],
        )
        self.assertFalse(child.user.has_access_to(self.school))
        self.assertEqual(child.user.student_membership(), child)  # still their school

    def test_an_invited_student_still_occupies_their_one_school(self):
        child = make_user("STM/1", "Ada Ade")
        services.enroll_student(child, self.school, status=MembershipStatus.INVITED)
        with self.assertRaises(services.AlreadyEnrolled):
            services.enroll_student(child, self.grace)

    def test_the_two_predicates_are_distinct_on_the_membership(self):
        membership = services.grant_membership(
            self.teacher, self.school, Role.TEACHER, status=MembershipStatus.SUSPENDED
        )
        self.assertTrue(membership.is_live)  # relationship exists
        self.assertFalse(membership.grants_access)  # but cannot act


class DeletingASchoolIsProtectedTests(TestCase):
    """Family history must not disappear as a side effect of an unrelated delete."""

    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        self.child = services.enroll_student(make_user("STM/1", "Ada Ade"), self.school)
        services.link_guardian(self.parent, self.child)

    def test_a_school_with_memberships_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.school.delete()

        self.assertTrue(School.objects.filter(pk=self.school.pk).exists())
        self.assertEqual(Membership.objects.filter(school=self.school).count(), 2)
        self.assertEqual(Guardianship.objects.count(), 1)

    def test_ending_memberships_does_not_unlock_the_delete(self):
        """Ended rows *are* the history, so they keep protecting the school."""
        for membership in Membership.objects.filter(school=self.school):
            membership.end()

        with self.assertRaises(ProtectedError), transaction.atomic():
            self.school.delete()
        self.assertTrue(School.objects.filter(pk=self.school.pk).exists())

    def test_a_school_nobody_ever_joined_can_be_deleted(self):
        empty = make_school("Kings College", "kings", "kings")
        empty.delete()
        self.assertFalse(School.objects.filter(slug="kings").exists())

    def test_a_guardian_cannot_be_deleted_while_a_link_remains(self):
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.parent.delete()
        self.assertTrue(User.objects.filter(pk=self.parent.pk).exists())
        self.assertEqual(Guardianship.objects.count(), 1)

    def test_a_child_cannot_be_deleted_while_a_guardian_is_linked(self):
        """Their membership would cascade, and the guardianship protects it."""
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.child.user.delete()
        self.assertEqual(Guardianship.objects.count(), 1)

    def test_unlinking_first_makes_deletion_possible(self):
        services.unlink_guardian(self.parent, self.child)
        self.assertEqual(Guardianship.objects.count(), 0)

        self.parent.delete()
        self.assertFalse(User.objects.filter(username="08031234567").exists())
        self.assertTrue(Membership.objects.filter(pk=self.child.pk).exists())


class StudentsDoNotSeeSiblingsTests(TestCase):
    """Sibling visibility is a parent-only view, for now."""

    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        self.ada = services.enroll_student(make_user("STM/1", "Ada Ade"), self.school)
        self.tunde = services.enroll_student(make_user("STM/2", "Tunde Ade"), self.school)
        for child in (self.ada, self.tunde):
            services.link_guardian(self.parent, child)

    def test_a_student_sees_no_siblings_even_though_the_parent_does(self):
        self.assertEqual(self.parent.children().count(), 2)

        self.assertEqual(self.ada.user.children().count(), 0)
        self.assertEqual(services.parent_dashboard(self.ada.user), [])

    def test_a_student_sees_only_their_own_membership(self):
        self.assertEqual(self.ada.user.student_membership(), self.ada)
        self.assertEqual(list(self.ada.user.live_memberships()), [self.ada])
        self.assertEqual(self.ada.user.roles_at(self.school), {Role.STUDENT.value})

    def test_a_student_is_never_a_guardian(self):
        self.assertEqual(
            Guardianship.objects.filter(guardian=self.ada.user).count(), 0
        )
        self.assertNotIn(self.ada.user, self.tunde.guardians())


class SchoolAccessMiddlewareTests(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.factory = RequestFactory()
        self.middleware = SchoolAccessMiddleware(lambda request: "ok")

        self.parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        child = services.enroll_student(make_user("STM/1", "Ada Ade"), self.stmarys)
        services.link_guardian(self.parent, child)

    def request_as(self, user):
        request = self.factory.get("/")
        request.user = user
        return request

    def test_a_member_reaches_their_school(self):
        with on_host_of(self.stmarys):
            request = self.request_as(self.parent)
            self.assertEqual(self.middleware(request), "ok")
            self.assertEqual(request.school, self.stmarys)
            self.assertEqual(request.school_roles, {Role.PARENT.value})

    def test_a_non_member_is_refused_another_school(self):
        with on_host_of(self.grace):
            with self.assertRaises(PermissionDenied):
                self.middleware(self.request_as(self.parent))

    def test_the_portal_host_needs_no_membership(self):
        """Where a parent's cross-school list is served."""
        with on_host_of(None):
            request = self.request_as(self.parent)
            self.assertEqual(self.middleware(request), "ok")
            self.assertIsNone(request.school)
            self.assertEqual(request.school_roles, frozenset())

    def test_platform_staff_reach_any_school(self):
        operator = make_user("ops", "Ops Person", is_platform_staff=True)
        with on_host_of(self.grace):
            request = self.request_as(operator)
            self.assertEqual(self.middleware(request), "ok")
            self.assertEqual(request.school_roles, frozenset())

    def test_an_anonymous_visitor_is_not_refused(self):
        from django.contrib.auth.models import AnonymousUser

        with on_host_of(self.stmarys):
            self.assertEqual(self.middleware(self.request_as(AnonymousUser())), "ok")
