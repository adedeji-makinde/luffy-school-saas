"""The transfer handshake: two schools, two signatures, one transaction.

The thing under test is not really "does a child end up at the right school" —
`transfer_student()` already did that. It is *who was allowed to make it happen*,
and what the record says afterwards. So most of these tests are about authority
and about the row, and the ones that check the child's school are checking that
the two-sided path reaches the same end state the both-ends-at-once path does.

The case worth watching is `test_the_requesting_school_cannot_answer_itself` and
its platform-staff sibling. A handshake that one party can complete alone is not
a handshake, and the failure would be invisible in the data: the transfer would
be correct, and only the record would quietly be worth less than it claims.
"""

from django.test import TestCase

from accounts import services, transfers
from accounts.models import (
    Guardianship,
    Membership,
    MembershipStatus,
    Relationship,
    Role,
    TransferRequest,
    TransferRequestStatus,
    TransferSide,
    User,
)
from schools.models import School

PASSWORD = "correct-horse-battery"


def make_school(name, slug, schema_name):
    school = School(name=name, slug=slug, schema_name=schema_name)
    school.auto_create_schema = False
    school.save()
    return school


def make_user(username, full_name, **extra):
    return User.objects.create_user(username, PASSWORD, full_name=full_name, **extra)


class HandshakeSetUp(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.hillside = make_school("Hillside", "hillside", "hillside")

        self.stmarys_admin = make_user("admin@stmarys.ng", "Stella Admin")
        services.grant_membership(self.stmarys_admin, self.stmarys, Role.ADMIN)
        self.grace_admin = make_user("admin@grace.ng", "Gbenga Admin")
        services.grant_membership(self.grace_admin, self.grace, Role.ADMIN)
        self.hillside_admin = make_user("admin@hillside.ng", "Hilda Admin")
        services.grant_membership(self.hillside_admin, self.hillside, Role.ADMIN)

        self.parent = make_user("08031234567", "Bisi Ade", phone="08031234567")
        self.child = services.enroll_student(
            make_user("STM/1", "Ada Ade"), self.stmarys, reference="STM/1"
        )
        services.link_guardian(
            self.parent, self.child, relationship=Relationship.MOTHER,
            is_primary_contact=True,
        )


class EitherSideMayAskTests(HandshakeSetUp):
    def test_the_releasing_school_asks_and_the_receiving_school_accepts(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        self.assertEqual(request.requested_side, TransferSide.RELEASING)
        self.assertEqual(request.status, TransferRequestStatus.PENDING)
        # Nothing has moved yet: a proposal is not a transfer.
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, MembershipStatus.ACTIVE)

        moved = transfers.accept_transfer_as(
            self.grace_admin, request, reference="GA/77"
        )

        self.assertEqual(moved.school, self.grace)
        self.assertEqual(moved.reference, "GA/77")
        self.assertEqual(self.child.user.student_membership(), moved)

    def test_the_receiving_school_asks_and_the_releasing_school_accepts(self):
        """The same proposal from the other end of the table."""
        request = transfers.request_transfer_as(
            self.grace_admin, self.child, self.grace, reference="GA/77"
        )
        self.assertEqual(request.requested_side, TransferSide.RECEIVING)

        moved = transfers.accept_transfer_as(self.stmarys_admin, request)

        self.assertEqual(moved.school, self.grace)
        self.assertEqual(moved.reference, "GA/77", "the reference offered up front is kept")

    def test_a_stranger_school_can_act_for_neither_side(self):
        with self.assertRaises(services.NotPermitted):
            transfers.request_transfer_as(self.hillside_admin, self.child, self.grace)
        self.assertEqual(TransferRequest.objects.count(), 0)


class TheRecordTests(HandshakeSetUp):
    def test_both_signatures_are_recorded(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace, note="Family relocating."
        )
        transfers.accept_transfer_as(self.grace_admin, request)

        request.refresh_from_db()
        self.assertEqual(request.requested_by, self.stmarys_admin)
        self.assertEqual(request.requested_side, TransferSide.RELEASING)
        self.assertIsNotNone(request.requested_at)
        self.assertEqual(request.answered_by, self.grace_admin)
        self.assertIsNotNone(request.answered_at)
        self.assertEqual(request.status, TransferRequestStatus.ACCEPTED)
        self.assertEqual(request.note, "Family relocating.")

    def test_the_record_survives_the_transfer_it_describes(self):
        """It points at the *old* membership, which is what makes it evidence."""
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        transfers.accept_transfer_as(self.grace_admin, request)

        request.refresh_from_db()
        self.assertEqual(request.student, self.child)
        self.assertEqual(request.from_school, self.stmarys)
        self.assertEqual(request.to_school, self.grace)
        self.assertEqual(request.student.status, MembershipStatus.ENDED)

    def test_who_said_no_is_recorded_too(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        transfers.decline_transfer_as(self.grace_admin, request)

        request.refresh_from_db()
        self.assertEqual(request.status, TransferRequestStatus.DECLINED)
        self.assertEqual(request.answered_by, self.grace_admin)
        self.assertIsNotNone(request.answered_at)


class TwoSignaturesOrNothingTests(HandshakeSetUp):
    def test_the_requesting_school_cannot_answer_itself(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        with self.assertRaises(services.NotPermitted):
            transfers.accept_transfer_as(self.stmarys_admin, request)

        self.child.refresh_from_db()
        self.assertEqual(self.child.status, MembershipStatus.ACTIVE)

    def test_platform_staff_cannot_sign_both_halves(self):
        """Authority at both ends is not the same as two parties agreeing.

        This is the test that stops the handshake being decorative. Platform
        staff pass every authority check, so nothing but an explicit rule keeps
        one person from producing a row that claims two schools agreed.
        """
        operator = make_user("ops", "Ops Person", is_platform_staff=True)
        request = transfers.request_transfer_as(operator, self.child, self.grace)

        with self.assertRaises(transfers.SameSignatory):
            transfers.accept_transfer_as(operator, request)
        with self.assertRaises(transfers.SameSignatory):
            transfers.decline_transfer_as(operator, request)

        self.child.refresh_from_db()
        self.assertEqual(self.child.status, MembershipStatus.ACTIVE)
        # ...and the one-caller path is still there for them.
        moved = services.transfer_student_as(operator, self.child, self.grace)
        self.assertEqual(moved.school, self.grace)

    def test_a_third_school_cannot_answer(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        with self.assertRaises(services.NotPermitted):
            transfers.accept_transfer_as(self.hillside_admin, request)


class ClosingTheWindowTests(HandshakeSetUp):
    def test_the_child_is_never_between_schools(self):
        """The whole reason the handshake exists.

        Contrast with the two-act path below: there the child provably belongs
        nowhere between the release and the admission. Here there is no moment
        to observe, because one transaction does both halves.
        """
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        self.assertIsNotNone(self.child.user.student_membership())

        transfers.accept_transfer_as(self.grace_admin, request)

        membership = self.child.user.student_membership()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.school, self.grace)

    def test_the_two_act_path_still_leaves_the_gap_it_always_did(self):
        """Pinned as the contrast, not as a defect of this module."""
        services.release_student_as(self.stmarys_admin, self.child)
        self.assertIsNone(self.child.user.student_membership())

    def test_the_guardians_come_across_without_being_re_linked(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        moved = transfers.accept_transfer_as(self.grace_admin, request)

        self.assertEqual([c.pk for c in self.parent.children()], [moved.pk])
        self.assertTrue(self.parent.has_access_to(self.grace))
        self.assertFalse(self.parent.has_access_to(self.stmarys))
        self.assertTrue(
            Guardianship.objects.get(
                guardian=self.parent, student=moved
            ).is_primary_contact
        )


class OneOpenRequestTests(HandshakeSetUp):
    def test_a_second_destination_is_refused_while_one_is_open(self):
        transfers.request_transfer_as(self.stmarys_admin, self.child, self.grace)

        with self.assertRaises(transfers.TransferAlreadyPending) as caught:
            transfers.request_transfer_as(
                self.stmarys_admin, self.child, self.hillside
            )
        self.assertIn("Grace Academy", str(caught.exception))
        self.assertEqual(TransferRequest.objects.count(), 1)

    def test_declining_frees_the_child_to_be_asked_for_again(self):
        first = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        transfers.decline_transfer_as(self.grace_admin, first)

        second = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.hillside
        )
        self.assertEqual(second.to_school, self.hillside)
        self.assertEqual(TransferRequest.objects.count(), 2)

    def test_withdrawing_frees_it_too_and_needs_the_asking_school(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        # The answering school withdraws nothing; that is a decline.
        with self.assertRaises(services.NotPermitted):
            transfers.withdraw_transfer_as(self.grace_admin, request)

        transfers.withdraw_transfer_as(self.stmarys_admin, request)
        request.refresh_from_db()
        self.assertEqual(request.status, TransferRequestStatus.WITHDRAWN)

        transfers.request_transfer_as(self.stmarys_admin, self.child, self.hillside)

    def test_any_admin_at_the_asking_school_may_withdraw(self):
        """People change jobs; a school must be able to retract its own proposal."""
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        colleague = make_user("admin2@stmarys.ng", "Second Admin")
        services.grant_membership(colleague, self.stmarys, Role.ADMIN)

        transfers.withdraw_transfer_as(colleague, request)
        request.refresh_from_db()
        self.assertEqual(request.answered_by, colleague)


class AnsweringTwiceTests(HandshakeSetUp):
    def test_a_resolved_request_cannot_be_answered_again(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        transfers.accept_transfer_as(self.grace_admin, request)

        stale = TransferRequest.objects.get(pk=request.pk)
        with self.assertRaises(transfers.TransferAlreadyResolved):
            transfers.accept_transfer_as(self.grace_admin, stale)
        with self.assertRaises(transfers.TransferAlreadyResolved):
            transfers.decline_transfer_as(self.grace_admin, stale)

        self.assertEqual(
            Membership.objects.filter(
                user=self.child.user, role=Role.STUDENT
            ).count(),
            2,
            "a second accept must not open a third enrolment",
        )


class TheEnrolmentMovedOnTests(HandshakeSetUp):
    def test_a_request_cannot_be_accepted_after_the_child_leaves_another_way(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        # St Mary's releases with no destination while the proposal sits.
        services.release_student_as(self.stmarys_admin, self.child)

        with self.assertRaises(transfers.EnrolmentMovedOn):
            transfers.accept_transfer_as(self.grace_admin, request)

        # The request keeps its honest status: nobody declined it.
        request.refresh_from_db()
        self.assertEqual(request.status, TransferRequestStatus.PENDING)
        self.assertIsNone(request.answered_by)

    def test_a_stale_request_drops_out_of_the_queue_it_can_never_be_answered_from(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        self.assertIn(request, transfers.transfers_awaiting(self.grace))

        services.release_student_as(self.stmarys_admin, self.child)

        self.assertNotIn(request, transfers.transfers_awaiting(self.grace))
        self.assertEqual(
            TransferRequest.objects.get(pk=request.pk).status,
            TransferRequestStatus.PENDING,
            "dropping out of the queue must not rewrite the record",
        )


class TheQueueTests(HandshakeSetUp):
    def test_each_school_sees_only_what_it_must_answer(self):
        outgoing = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )

        other_child = services.enroll_student(
            make_user("GA/9", "Chidi Eze"), self.grace
        )
        incoming = transfers.request_transfer_as(
            self.stmarys_admin, other_child, self.stmarys
        )

        # St Mary's asked for both, so it is waiting on nobody.
        self.assertEqual(list(transfers.transfers_awaiting(self.stmarys)), [])
        self.assertEqual(
            {r.pk for r in transfers.transfers_awaiting(self.grace)},
            {outgoing.pk, incoming.pk},
        )

    def test_an_answered_request_leaves_the_queue(self):
        request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )
        transfers.decline_transfer_as(self.grace_admin, request)
        self.assertEqual(list(transfers.transfers_awaiting(self.grace)), [])


class WhatCannotBeTransferredTests(HandshakeSetUp):
    def test_only_a_student_membership(self):
        teacher = services.grant_membership(
            make_user("ada", "Ada Obi"), self.stmarys, Role.TEACHER
        )
        with self.assertRaises(services.NotAStudent):
            transfers.request_transfer_as(self.stmarys_admin, teacher, self.grace)

    def test_not_to_the_school_the_child_is_already_at(self):
        with self.assertRaises(transfers.AlreadyAtThatSchool):
            transfers.request_transfer_as(
                self.stmarys_admin, self.child, self.stmarys
            )

    def test_not_an_enrolment_that_has_already_ended(self):
        services.release_student_as(self.stmarys_admin, self.child)
        with self.assertRaises(transfers.EnrolmentMovedOn):
            transfers.request_transfer_as(
                self.stmarys_admin, self.child, self.grace
            )
