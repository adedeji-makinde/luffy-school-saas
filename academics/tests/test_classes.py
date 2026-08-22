"""Which class a child sits in, and the two numbers that depend on it.

`ClassGroup` and `ClassPlacement` exist so that "position in class" and "class
average" have a denominator. Everything here is therefore about one property or
another of that denominator being trustworthy:

- a group belongs to one school, and two schools may both have a "JSS 1A";
- a child sits in exactly **one** group per term, enforced by the database and
  not only by the service;
- a child at another school cannot be placed here at all, which is the check
  that earns the bare `student_membership_id`;
- carrying a term forward does not overwrite work already done by hand.

**Two schools in every fixture, never one.** A single-tenant test cannot fail
for any of the reasons this table is most likely to be got wrong: a uniqueness
constraint that should be per-schema, a roster query missing its `term` filter,
a placement written on the wrong connection. St Mary's and Grace Academy are
both real schemas, created the production way.
"""

import contextlib
import threading
from datetime import date

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django_tenants.utils import schema_context

from academics import services
from academics.models import ClassGroup, ClassPlacement, Term, TermName
from accounts.models import MembershipStatus, Role, User
from accounts.services import enroll_student, grant_membership
from schools.models import School

PASSWORD = "correct-horse-battery"


def make_school(name, slug, schema_name):
    school = School(name=name, slug=slug, schema_name=schema_name)
    school.save()
    return school


@contextlib.contextmanager
def connected_to(school):
    with schema_context(school.schema_name):
        yield


def a_term(session="2025/2026", name=TermName.FIRST, starts=None, ends=None):
    """A term in whichever schema the connection is on."""
    return Term.objects.create(
        session=session,
        name=name,
        starts_on=starts or date(2025, 9, 15),
        ends_on=ends or date(2025, 12, 12),
    )


class TwoSchoolsSetUp(TestCase):
    """St Mary's and Grace Academy, each with a real schema of its own."""

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

        self.ada = enroll_student(
            User.objects.create_user("ada", PASSWORD, full_name="Ada Obi"),
            self.stmarys,
        )
        self.emeka = enroll_student(
            User.objects.create_user("emeka", PASSWORD, full_name="Emeka Nwosu"),
            self.stmarys,
        )
        # Grace Academy's own child. The one a St Mary's placement must refuse.
        self.chidi = enroll_student(
            User.objects.create_user("chidi", PASSWORD, full_name="Chidi Okafor"),
            self.grace,
        )

        self.head = grant_membership(
            User.objects.create_user("ngozi", PASSWORD, full_name="Ngozi Eze"),
            self.stmarys,
            Role.ADMIN,
        )
        self.teacher = grant_membership(
            User.objects.create_user("kemi", PASSWORD, full_name="Kemi Bello"),
            self.stmarys,
            Role.TEACHER,
        )

        with connected_to(self.stmarys):
            self.term_id = a_term().pk
            self.second_term_id = a_term(
                name=TermName.SECOND,
                starts=date(2026, 1, 12),
                ends=date(2026, 4, 2),
            ).pk
            self.jss1a_id = ClassGroup.objects.create(name="JSS 1A", level=1).pk
            self.jss1b_id = ClassGroup.objects.create(name="JSS 1B", level=1).pk

        with connected_to(self.grace):
            self.grace_term_id = a_term().pk
            self.grace_jss1a_id = ClassGroup.objects.create(name="JSS 1A", level=1).pk

    def tearDown(self):
        # `schema_context` leaves the connection wherever it was last set, and
        # the next test's `School.save()` refuses to run outside `public`. The
        # same tearDown `gradebook/tests/test_api.py` carries, for the same
        # reason.
        connection.set_schema_to_public()
        super().tearDown()

    # Re-read inside the schema under test rather than held across the boundary:
    # a tenant model instance fetched on one connection and used on another is
    # the mistake these helpers exist to make impossible to write by accident.
    def term(self):
        return Term.objects.get(pk=self.term_id)

    def second_term(self):
        return Term.objects.get(pk=self.second_term_id)

    def jss1a(self):
        return ClassGroup.objects.get(pk=self.jss1a_id)

    def jss1b(self):
        return ClassGroup.objects.get(pk=self.jss1b_id)


class TwoSchoolsKeepTheirOwnGroupsTests(TwoSchoolsSetUp):
    def test_both_schools_may_have_a_jss_1a(self):
        """The uniqueness is per schema, which is the whole of the isolation.

        A platform-wide unique name would mean the second school to open could
        not call its class what it calls it.
        """
        with connected_to(self.stmarys):
            self.assertEqual(ClassGroup.objects.filter(name="JSS 1A").count(), 1)
        with connected_to(self.grace):
            self.assertEqual(ClassGroup.objects.filter(name="JSS 1A").count(), 1)

    def test_the_two_jss_1as_have_the_same_primary_key(self):
        """Asserted on purpose, because it is the trap under every bare id.

        Each schema has its own sequence, so St Mary's first class group and
        Grace Academy's first class group are *both* id 1 — two unrelated rows
        wearing the same number. That is why docs/tenancy.md forbids a foreign
        key across the boundary and why `student_membership_id` is checked in
        code: an id carried from one schema to another does not fail to
        resolve, it resolves to somebody else's row.
        """
        self.assertEqual(self.jss1a_id, self.grace_jss1a_id)

        with connected_to(self.stmarys):
            self.assertEqual(ClassGroup.objects.get(pk=self.jss1a_id).name, "JSS 1A")
            ours = ClassGroup.objects.get(pk=self.jss1a_id).created_at
        with connected_to(self.grace):
            theirs = ClassGroup.objects.get(pk=self.grace_jss1a_id).created_at

        # Same number, different rows: they were written at different moments.
        self.assertNotEqual(ours, theirs)

    def test_one_school_cannot_name_two_groups_the_same(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ClassGroup.objects.create(name="JSS 1A", level=1)

    def test_a_placement_is_invisible_to_the_other_school(self):
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            self.assertEqual(ClassPlacement.objects.count(), 1)

        with connected_to(self.grace):
            self.assertEqual(ClassPlacement.objects.count(), 0)

    def test_the_roster_is_scoped_to_one_group_and_one_term(self):
        """Both filters matter, and a missing `term` is the silent one.

        A roster missing its term filter still returns children, still looks
        like a class, and quietly ranks last term's leavers alongside this
        term's arrivals.
        """
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            services.place_student(self.jss1b(), self.term(), self.emeka)
            services.place_student(self.jss1a(), self.second_term(), self.ada)

            first = ClassPlacement.objects.student_ids(self.jss1a(), self.term())
            second = ClassPlacement.objects.student_ids(
                self.jss1a(), self.second_term()
            )
            other_arm = ClassPlacement.objects.student_ids(self.jss1b(), self.term())

        self.assertEqual(first, [self.ada.pk])
        self.assertEqual(second, [self.ada.pk])
        self.assertEqual(other_arm, [self.emeka.pk])


class PlacingSomebodyElsesChildTests(TwoSchoolsSetUp):
    """The check that earns the bare `student_membership_id`.

    The column takes any integer, and a foreign key could not have helped:
    `Membership` is shared, so a key into it would constrain only that the row
    exists. Every school's children are in that one table.
    """

    def test_another_schools_student_is_refused(self):
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.place_student(self.jss1a(), self.term(), self.chidi)

            self.assertEqual(ClassPlacement.objects.count(), 0)

    def test_the_refusal_names_the_school_the_child_actually_attends(self):
        """Right for a log. `gradebook/tests/test_api.py` proves the HTTP layer
        never repeats it back — a name and a school are another tenant's data."""
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotThisSchoolsStudent) as caught:
                services.place_student(self.jss1a(), self.term(), self.chidi)

        self.assertIn("Grace Academy", str(caught.exception))

    def test_a_teachers_membership_is_not_a_student(self):
        """A placement is keyed on the STUDENT membership, which pins the school.

        Passing a teacher's membership is not a near miss; it is a row about the
        wrong person entirely.
        """
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.place_student(self.jss1a(), self.term(), self.teacher)

    def test_removing_another_schools_child_is_refused_too(self):
        """The read paths are guarded as well as the writes.

        A `remove_placement()` that skipped the check would be a delete keyed on
        an id from another school — which finds nothing today, and would find
        something the moment two schools' ids happened to coincide.
        """
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.remove_placement(self.term(), self.chidi)


class OneGroupPerChildPerTermTests(TwoSchoolsSetUp):
    def test_a_second_placement_is_refused_and_names_the_group(self):
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)

            with self.assertRaises(services.AlreadyPlaced) as caught:
                services.place_student(self.jss1b(), self.term(), self.ada)

            self.assertEqual(ClassPlacement.objects.count(), 1)
        self.assertIn("JSS 1A", str(caught.exception))
        self.assertEqual(caught.exception.current.class_group_id, self.jss1a_id)

    def test_placing_into_the_same_group_twice_is_refused_as_well(self):
        """Looks like it should be a no-op, and is not.

        Two administrators each believing they made the placement is a real
        disagreement about who did what, and `placed_by_id` would name whichever
        of them lost.
        """
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)

            with self.assertRaises(services.AlreadyPlaced):
                services.place_student(self.jss1a(), self.term(), self.ada)

    def test_the_same_child_may_sit_in_a_group_in_each_term(self):
        """The constraint is per term. A child has a place in all three."""
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            services.place_student(self.jss1b(), self.second_term(), self.ada)

            self.assertEqual(ClassPlacement.objects.count(), 2)

    def test_the_database_refuses_it_even_when_the_service_is_bypassed(self):
        """The rule is a constraint, not a convention in a service module.

        An import, a data migration or a shell session writes rows directly.
        If this only held in `place_student()` it would not hold for them.
        """
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)

            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ClassPlacement.objects.create(
                        class_group_id=self.jss1b_id,
                        term_id=self.term_id,
                        student_membership_id=self.ada.pk,
                    )

    def test_a_missing_class_group_is_not_reported_as_a_collision(self):
        """Measured, not assumed — and it did not behave the way it reads.

        The obvious expectation is that placing into a `ClassGroup` that does
        not exist raises `IntegrityError` inside `place_student()`, where
        `_is_the_placement_colliding()` has to keep it from being relabelled
        `AlreadyPlaced`. It does not: Django declares PostgreSQL foreign keys
        `DEFERRABLE INITIALLY DEFERRED`, so the insert *succeeds* and the
        violation is raised at commit instead — which is why this test forces
        the check by hand rather than waiting for one.

        What matters either way is the same, and is what is asserted: the caller
        is never told "this child is already placed" about a row that was never
        refused for that reason.
        """
        with connected_to(self.stmarys):
            missing = ClassGroup(pk=999_999, name="Nowhere")

            # No AlreadyPlaced, and no IntegrityError either — nothing has been
            # checked yet at this point.
            services.place_student(missing, self.term(), self.ada)

            with self.assertRaises(IntegrityError):
                connection.check_constraints()

    def test_a_real_non_collision_integrity_error_is_not_swallowed(self):
        """The guard is asked of a genuine error, not a fabricated one.

        A duplicate `ClassGroup` name raises an immediate `IntegrityError`
        carrying real psycopg2 diagnostics naming a different constraint.
        `_is_the_placement_colliding()` must say no to it — otherwise every
        integrity failure on the placement path would be reported as a conflict
        and retried forever, which is the bug `gradebook.services` guards
        against by name.
        """
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError) as caught:
                with transaction.atomic():
                    ClassGroup.objects.create(name="JSS 1A", level=1)

            self.assertFalse(services._is_the_placement_colliding(caught.exception))


class MovingTests(TwoSchoolsSetUp):
    def test_a_move_changes_the_group_and_keeps_one_row(self):
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada, by=self.head)

            moved = services.move_student(
                self.jss1b(), self.term(), self.ada, by=self.head
            )

            self.assertEqual(moved.class_group_id, self.jss1b_id)
            self.assertEqual(ClassPlacement.objects.count(), 1)

    def test_a_move_records_who_made_it(self):
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            services.move_student(self.jss1b(), self.term(), self.ada, by=self.head)

            self.assertEqual(
                ClassPlacement.objects.get().placed_by_id, self.head.pk
            )

    def test_moving_into_the_group_they_are_already_in_is_a_no_op(self):
        """A retried request must not fail because it succeeded the first time."""
        with connected_to(self.stmarys):
            placed = services.place_student(self.jss1a(), self.term(), self.ada)

            again = services.move_student(self.jss1a(), self.term(), self.ada)

            self.assertEqual(again.pk, placed.pk)
            self.assertEqual(again.class_group_id, self.jss1a_id)

    def test_moving_a_child_who_is_not_placed_is_refused(self):
        """Not a silent create. "Move" and "place" are different acts, and a
        move that quietly placed would hide a roster nobody had filled in."""
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotPlaced):
                services.move_student(self.jss1b(), self.term(), self.ada)

            self.assertEqual(ClassPlacement.objects.count(), 0)


class CarryForwardTests(TwoSchoolsSetUp):
    def test_it_copies_a_terms_placements_into_the_next(self):
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            services.place_student(self.jss1b(), self.term(), self.emeka)

            made = services.carry_forward_placements(
                self.term(), self.second_term(), by=self.head
            )

            self.assertEqual(made, 2)
            self.assertEqual(
                ClassPlacement.objects.student_ids(self.jss1a(), self.second_term()),
                [self.ada.pk],
            )
            self.assertEqual(
                ClassPlacement.objects.student_ids(self.jss1b(), self.second_term()),
                [self.emeka.pk],
            )

    def test_running_it_twice_makes_nothing_and_says_so(self):
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            services.carry_forward_placements(self.term(), self.second_term())

            again = services.carry_forward_placements(self.term(), self.second_term())

            self.assertEqual(again, 0)
            self.assertEqual(
                ClassPlacement.objects.for_term(self.second_term()).count(), 1
            )

    def test_it_does_not_overwrite_a_placement_already_made_by_hand(self):
        """The load-bearing one.

        A school that has already moved one child into next term's correct group
        must not have that undone by somebody running the carry-forward
        afterwards — which is the ordinary order these two things happen in.
        """
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            services.place_student(self.jss1b(), self.second_term(), self.ada)

            made = services.carry_forward_placements(self.term(), self.second_term())

            self.assertEqual(made, 0)
            self.assertEqual(
                ClassPlacement.objects.get(term_id=self.second_term_id).class_group_id,
                self.jss1b_id,
            )

    def test_it_does_not_promote(self):
        """Carrying forward keeps the same group on purpose.

        Moving JSS 1A into JSS 2A is a decision about who passed, and this
        function does not get to make it.
        """
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            services.carry_forward_placements(self.term(), self.second_term())

            self.assertEqual(
                ClassPlacement.objects.get(term_id=self.second_term_id).class_group_id,
                self.jss1a_id,
            )

    def test_carrying_forward_an_empty_term_is_not_an_error(self):
        with connected_to(self.stmarys):
            self.assertEqual(
                services.carry_forward_placements(self.term(), self.second_term()), 0
            )

    def test_a_child_who_has_left_is_not_carried_forward(self):
        """The one that silently corrupts a class average.

        A child who graduates or transfers in December would otherwise appear on
        January's roster, be counted in the class size, and pull the class
        average towards a mark nobody was ever going to give them. Nothing about
        that looks wrong in the data — it is an ordinary placement row.
        """
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)
            services.place_student(self.jss1a(), self.term(), self.emeka)

        self.emeka.status = MembershipStatus.ENDED
        self.emeka.save(update_fields=["status"])

        with connected_to(self.stmarys):
            made = services.carry_forward_placements(self.term(), self.second_term())

            self.assertEqual(made, 1)
            self.assertEqual(
                ClassPlacement.objects.student_ids(self.jss1a(), self.second_term()),
                [self.ada.pk],
            )

    def test_the_departed_childs_own_term_is_left_exactly_as_it_was(self):
        """They *were* in that class that term, and its report card says so.

        Leaving is a fact about the future, not a reason to rewrite the past —
        and this is the phase where rewriting the past is the thing to fear.
        """
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.emeka)

        self.emeka.status = MembershipStatus.ENDED
        self.emeka.save(update_fields=["status"])

        with connected_to(self.stmarys):
            services.carry_forward_placements(self.term(), self.second_term())

            self.assertEqual(
                ClassPlacement.objects.student_ids(self.jss1a(), self.term()),
                [self.emeka.pk],
            )

    def test_a_suspended_child_is_still_carried_forward(self):
        """`LIVE_STATUSES`, not `ACCESS_STATUSES`.

        The question is whether the child is still enrolled, not whether they
        can sign in. A suspended student is still in the class and still has a
        report card coming.
        """
        with connected_to(self.stmarys):
            services.place_student(self.jss1a(), self.term(), self.ada)

        self.ada.status = MembershipStatus.SUSPENDED
        self.ada.save(update_fields=["status"])

        with connected_to(self.stmarys):
            self.assertEqual(
                services.carry_forward_placements(self.term(), self.second_term()), 1
            )

    def test_another_schools_placement_row_could_not_be_carried_here(self):
        """The school is re-checked even though the ids came from this schema.

        A guard that trusts its own input stops guarding the day something else
        writes that input. Grace Academy's child is given a St Mary's placement
        row directly — the state a bad import would leave — and must not be
        carried forward into a St Mary's term.
        """
        with connected_to(self.stmarys):
            ClassPlacement.objects.create(
                class_group_id=self.jss1a_id,
                term_id=self.term_id,
                student_membership_id=self.chidi.pk,
            )

            made = services.carry_forward_placements(self.term(), self.second_term())

            self.assertEqual(made, 0)
            self.assertEqual(
                ClassPlacement.objects.for_term(self.second_term()).count(), 0
            )


class WhoMayPlaceTests(TwoSchoolsSetUp):
    def test_an_administrator_may(self):
        with connected_to(self.stmarys):
            placed = services.place_student_as(
                self.head.user, self.jss1a(), self.term(), self.ada
            )
            self.assertEqual(placed.placed_by_id, self.head.user.pk)

    def test_a_teacher_may_not(self):
        """Narrower than marking, deliberately.

        A teacher enters marks for the children in front of them; which children
        those are is not their decision. Moving a child between arms changes
        whose average they count towards and whose position they displace.
        """
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotAllowedToPlace):
                services.place_student_as(
                    self.teacher.user, self.jss1a(), self.term(), self.ada
                )

            self.assertEqual(ClassPlacement.objects.count(), 0)

    def test_a_suspended_administrator_may_not(self):
        """Authority is access-scoped: a membership is not the same as a role."""
        self.head.status = MembershipStatus.SUSPENDED
        self.head.save(update_fields=["status"])

        with connected_to(self.stmarys):
            with self.assertRaises(services.NotAllowedToPlace):
                services.place_student_as(
                    self.head.user, self.jss1a(), self.term(), self.ada
                )

    def test_an_administrator_at_the_other_school_may_not(self):
        """Authority is asked at the child's school, not at any school."""
        theirs = grant_membership(
            User.objects.create_user("bola", PASSWORD, full_name="Bola Ade"),
            self.grace,
            Role.ADMIN,
        )

        with connected_to(self.stmarys):
            with self.assertRaises(services.NotAllowedToPlace):
                services.place_student_as(
                    theirs.user, self.jss1a(), self.term(), self.ada
                )

    def test_platform_staff_may_not(self):
        """Support staff repairing plumbing is one thing; deciding which class a
        child sits in is the school's own act, and `placed_by_id` would name a
        platform operator on the row."""
        operator = User.objects.create_superuser(
            "ops@luffy.school", PASSWORD, full_name="Ope Rator"
        )

        with connected_to(self.stmarys):
            with self.assertRaises(services.NotAllowedToPlace):
                services.place_student_as(
                    operator, self.jss1a(), self.term(), self.ada
                )

    def test_an_anonymous_caller_may_not(self):
        with connected_to(self.stmarys):
            self.assertFalse(services.can_place_students(None, self.stmarys))


class PlacementUnderConcurrencyTests(TransactionTestCase):
    """Two administrators placing the same child into different arms at once.

    `place_student()` reads nothing before it inserts, so there is no lost
    update to find here — the question is the other one: when the unique
    constraint refuses the second insert, is that reported as "already placed",
    or does it escape as a 500?

    Real threads and `TransactionTestCase`, on the reasoning
    `accounts/tests/test_signin_concurrency.py` sets out: two connections whose
    commits are visible to each other, released together by a barrier rather
    than interleaved with sleeps, so both inserts are provably in flight.
    """

    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.ada = enroll_student(
            User.objects.create_user("ada", PASSWORD, full_name="Ada Obi"),
            self.school,
        )
        with connected_to(self.school):
            self.term_id = a_term().pk
            self.group_ids = [
                ClassGroup.objects.create(name="JSS 1A", level=1).pk,
                ClassGroup.objects.create(name="JSS 1B", level=1).pk,
            ]

    def tearDown(self):
        connection.set_schema_to_public()
        super().tearDown()

    def test_exactly_one_placement_survives_and_the_loser_is_told(self):
        ready = threading.Barrier(2, timeout=15)
        refusals = []
        unexpected = []

        def place(group_id):
            try:
                with connected_to(self.school):
                    term = Term.objects.get(pk=self.term_id)
                    group = ClassGroup.objects.get(pk=group_id)
                    ready.wait()
                    services.place_student(group, term, self.ada)
            except services.AlreadyPlaced as refused:
                refusals.append(refused)
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                unexpected.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=place, args=(group_id,))
            for group_id in self.group_ids
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)

        self.assertEqual(unexpected, [], f"a thread failed: {unexpected}")
        with connected_to(self.school):
            self.assertEqual(ClassPlacement.objects.count(), 1)
        # Exactly one of the two was refused, and refused as a placement
        # conflict rather than as an unhandled IntegrityError.
        self.assertEqual(len(refusals), 1)
        self.assertIsNotNone(refusals[0].current)
