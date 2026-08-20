"""Two people answering one transfer request at the same moment.

`TransferRequest._lock_pending()` exists because of what the invitation flow
found the expensive way: a guard that reads an in-memory copy is answering a
question about the past, so two answers arriving together both pass "is this
still pending?" before either writes. There it meant one token accepted twice.
Here it would mean one child enrolled at a third school — `transfer_student()`
run twice against the same request, the second call ending the enrolment the
first had just opened.

`TransactionTestCase` and real threads, because this needs two connections whose
commits are visible to each other. The interleaving is driven by a barrier, not
by sleeps, so both answers are provably in flight together.
"""

import threading

from django.db import connections
from django.test import TransactionTestCase

from accounts import services, transfers
from accounts.models import (
    Membership,
    MembershipStatus,
    Role,
    TransferError,
    TransferRequest,
    TransferRequestStatus,
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


class TwoAnswersAtOnceTests(TransactionTestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

        self.stmarys_admin = make_user("admin@stmarys.ng", "Stella Admin")
        services.grant_membership(self.stmarys_admin, self.stmarys, Role.ADMIN)
        self.first_grace_admin = make_user("admin1@grace.ng", "Gbenga Admin")
        services.grant_membership(self.first_grace_admin, self.grace, Role.ADMIN)
        self.second_grace_admin = make_user("admin2@grace.ng", "Grace Second")
        services.grant_membership(self.second_grace_admin, self.grace, Role.ADMIN)

        self.child = services.enroll_student(
            make_user("STM/1", "Ada Ade"), self.stmarys, reference="STM/1"
        )
        self.request = transfers.request_transfer_as(
            self.stmarys_admin, self.child, self.grace
        )

    def _answer_together(self, actors, answer):
        """Both actors reach `answer` before either commits."""
        ready = threading.Barrier(len(actors), timeout=15)
        results = []
        guard = threading.Lock()

        def run(actor):
            try:
                # Each thread loads its own copy, as two web requests would.
                request = TransferRequest.objects.get(pk=self.request.pk)
                ready.wait()
                answer(actor, request)
                with guard:
                    results.append((actor.username, "answered"))
            except TransferError as exc:
                with guard:
                    results.append((actor.username, f"refused: {type(exc).__name__}"))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(a,)) for a in actors]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        return results

    def test_only_one_of_two_simultaneous_accepts_takes_effect(self):
        results = self._answer_together(
            [self.first_grace_admin, self.second_grace_admin],
            lambda actor, request: transfers.accept_transfer_as(actor, request),
        )

        self.assertEqual(len(results), 2, f"an answer never finished: {results}")
        answered = [name for name, outcome in results if outcome == "answered"]
        self.assertEqual(len(answered), 1, f"the request was answered twice: {results}")
        self.assertIn(
            "refused: TransferAlreadyResolved", [outcome for _, outcome in results]
        )

        # The decisive assertion: a second transfer would have ended the
        # enrolment the first one opened and left a third row behind.
        self.assertEqual(
            Membership.objects.filter(
                user=self.child.user, role=Role.STUDENT
            ).count(),
            2,
        )
        live = Membership.objects.get(
            user=self.child.user, role=Role.STUDENT, status=MembershipStatus.ACTIVE
        )
        self.assertEqual(live.school, self.grace)

    def test_an_accept_and_a_decline_arriving_together_do_not_both_land(self):
        """The two answers disagree, so exactly one of them must win outright."""

        def answer(actor, request):
            if actor == self.first_grace_admin:
                transfers.accept_transfer_as(actor, request)
            else:
                transfers.decline_transfer_as(actor, request)

        results = self._answer_together(
            [self.first_grace_admin, self.second_grace_admin], answer
        )

        answered = [name for name, outcome in results if outcome == "answered"]
        self.assertEqual(len(answered), 1, f"both answers landed: {results}")

        self.request.refresh_from_db()
        self.assertIn(
            self.request.status,
            {TransferRequestStatus.ACCEPTED, TransferRequestStatus.DECLINED},
        )

        # Whichever won, the enrolment and the record agree with each other.
        live = Membership.objects.filter(
            user=self.child.user, role=Role.STUDENT, status__in=["active", "invited"]
        ).get()
        if self.request.status == TransferRequestStatus.ACCEPTED:
            self.assertEqual(live.school, self.grace)
        else:
            self.assertEqual(live.school, self.stmarys)
