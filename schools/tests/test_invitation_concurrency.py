"""What happens when two people act on one invitation at the same time.

`TransactionTestCase` rather than `TestCase`, and real threads: every test here
needs two database connections whose commits are visible to each other, which a
test wrapped in a single rolled-back transaction cannot give. The interleaving is
driven by `threading.Event`, not by sleeps, so the window each test opens is the
exact one the bug lived in rather than one that happens to be wide enough today.

Two separate concerns, and they failed for the same underlying reason:

`AcceptUnderConcurrencyTests` pins that `Invitation.accept()` decides on state it
has locked. It used to read `self.status` and `self.membership.status` off
objects `validate_token()` had loaded in an earlier transaction, and only then
lock the `User` — so every guard was answering a question about the past.

`LockScopeTests` pins that the invitation path locks only the rows it writes.
`Membership.Meta.ordering` sorts by `school__name` and `user__full_name`, and
Postgres locks a row in *every* joined table when `FOR UPDATE` meets a join, so
the default ordering quietly put an exclusive lock on the School row into every
grant — and two admins inviting two different teachers at one school queued
behind a row neither of them was touching.
"""

import threading

from django.db import connection, connections, transaction
from django.db.utils import OperationalError
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from accounts.deletion import deactivate_user
from accounts.models import Membership, MembershipStatus, Role, User
from accounts.services import grant_membership
from schools import invitations as invitation_service
from schools.models import Invitation, InvitationError, InvitationStatus
from schools.tests.test_invitations import PASSWORD, RecordingChannel, make_school


def run_in_thread(fn):
    """Run `fn` on its own connection, returning a started `Thread`.

    Django's connections are thread-local, so a thread that opens one must close
    it or `TransactionTestCase`'s teardown blocks trying to truncate tables the
    abandoned connection still holds locks on.
    """

    def wrapped():
        try:
            fn()
        finally:
            connections.close_all()

    thread = threading.Thread(target=wrapped)
    thread.start()
    return thread


@override_settings(INVITATION_CHANNEL="schools.tests.test_invitations.RecordingChannel")
class AcceptUnderConcurrencyTests(TransactionTestCase):
    def setUp(self):
        RecordingChannel.sent = []
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.admin = User.objects.create_user(
            "admin@st-marys.school", PASSWORD, full_name="Ada Admin",
            email="admin@st-marys.school",
        )
        grant_membership(self.admin, self.school, Role.ADMIN)
        self.invitation, self.raw_token = invitation_service.invite_staff(
            self.admin,
            self.school,
            Role.TEACHER,
            email="new.teacher@example.com",
            full_name="New Teacher",
            accept_url_for=lambda token: f"https://portal/i/{token}/",
        )
        self.membership = self.invitation.membership

    def test_an_accept_in_flight_cannot_resurrect_an_ended_membership(self):
        """The admin's `end()` wins, even though the accept started first.

        The invitee loads the link, the admin ends the membership while they are
        deciding, and the invitee then submits. The submission must be refused:
        an ended relationship is not revivable by redeeming a link minted before
        it ended. This used to overwrite the ENDED with ACTIVE and leave the
        `ended_on` date sitting on a live row.
        """
        loaded = threading.Event()
        ended = threading.Event()
        outcome = {}

        def accept():
            # Exactly what the endpoint does: validate, then accept.
            invitation = Invitation.validate_token(self.raw_token)
            loaded.set()
            ended.wait(15)
            try:
                invitation.accept(password=PASSWORD)
                outcome["accepted"] = True
            except InvitationError as exc:
                outcome["refused"] = str(exc)

        thread = run_in_thread(accept)
        self.assertTrue(loaded.wait(15), "the accepting thread never started")

        Membership.objects.get(pk=self.membership.pk).end()
        ended.set()
        thread.join(20)

        self.assertNotIn("accepted", outcome, "a spent relationship was revived")
        self.assertIn("not invited", outcome["refused"])

        membership = Membership.objects.get(pk=self.membership.pk)
        self.assertEqual(membership.status, MembershipStatus.ENDED)
        self.assertIsNotNone(membership.ended_on)
        self.assertEqual(
            Invitation.objects.get(pk=self.invitation.pk).status,
            InvitationStatus.PENDING,
        )

    def test_two_simultaneous_clicks_accept_once(self):
        """Both requests read the invitation before either writes to it."""
        both_loaded = threading.Barrier(2, timeout=15)
        results = []
        guard = threading.Lock()

        def accept(label):
            invitation = Invitation.validate_token(self.raw_token)
            both_loaded.wait()
            try:
                invitation.accept(password=PASSWORD)
                with guard:
                    results.append((label, "accepted"))
            except InvitationError as exc:
                with guard:
                    results.append((label, f"refused: {exc}"))

        threads = [run_in_thread(lambda i=i: accept(f"click-{i}")) for i in (1, 2)]
        for thread in threads:
            thread.join(20)

        accepted = [label for label, result in results if result == "accepted"]
        self.assertEqual(len(results), 2, f"a click never finished: {results}")
        self.assertEqual(len(accepted), 1, f"the token was spent twice: {results}")

        invitation = Invitation.objects.get(pk=self.invitation.pk)
        self.assertEqual(invitation.status, InvitationStatus.ACCEPTED)
        self.assertEqual(
            Membership.objects.get(pk=self.membership.pk).status,
            MembershipStatus.ACTIVE,
        )

    def test_deactivating_the_invitee_kills_an_accept_in_flight(self):
        """Deactivation is how access is taken away; a live link must not undo it."""
        loaded = threading.Event()
        deactivated = threading.Event()
        outcome = {}

        def accept():
            invitation = Invitation.validate_token(self.raw_token)
            loaded.set()
            deactivated.wait(15)
            try:
                invitation.accept(password=PASSWORD)
                outcome["accepted"] = True
            except InvitationError as exc:
                outcome["refused"] = str(exc)

        thread = run_in_thread(accept)
        self.assertTrue(loaded.wait(15), "the accepting thread never started")

        deactivate_user(User.objects.get(pk=self.membership.user_id))
        deactivated.set()
        thread.join(20)

        self.assertNotIn("accepted", outcome, "a disabled account was activated")
        self.assertIn("deactivated", outcome["refused"])
        self.assertEqual(
            Membership.objects.get(pk=self.membership.pk).status,
            MembershipStatus.INVITED,
        )


@override_settings(INVITATION_CHANNEL="schools.tests.test_invitations.RecordingChannel")
class LockScopeTests(TransactionTestCase):
    def setUp(self):
        RecordingChannel.sent = []
        self.school = make_school("St Mary's", "st-marys", "st_marys")
        self.admin = User.objects.create_user(
            "admin@st-marys.school", PASSWORD, full_name="Ada Admin",
            email="admin@st-marys.school",
        )
        grant_membership(self.admin, self.school, Role.ADMIN)

    def invite(self, email):
        return invitation_service.invite_staff(
            self.admin,
            self.school,
            Role.TEACHER,
            email=email,
            full_name="A Teacher",
            accept_url_for=lambda token: f"https://portal/i/{token}/",
        )

    def test_no_row_lock_reaches_a_joined_table(self):
        """Every `FOR UPDATE` the invite path takes reads a single table.

        A joined `SELECT ... FOR UPDATE` locks a row in each joined table, so a
        query that only *reads* `schools_school` to satisfy an ORDER BY still
        locks it. Asserting on the SQL keeps that from creeping back when
        somebody adds an ordering or a `select_related` to one of these lookups.
        """
        with CaptureQueriesContext(connection) as captured:
            self.invite("first.teacher@example.com")

        locking = [q["sql"] for q in captured.captured_queries if "FOR UPDATE" in q["sql"]]
        self.assertTrue(locking, "the invite path took no row locks at all")
        for sql in locking:
            self.assertNotIn("JOIN", sql, f"this lock reaches a joined table:\n{sql}")

    def test_no_lock_accept_takes_reaches_a_joined_table(self):
        """The same rule for the other side of the flow.

        `accept()` locks three rows — membership, invitation, user — and the
        membership lookup is the one that inherits a joined `Meta.ordering`.
        """
        _invitation, raw_token = self.invite("accepting.teacher@example.com")
        invitation = Invitation.validate_token(raw_token)

        with CaptureQueriesContext(connection) as captured:
            invitation.accept(password=PASSWORD)

        locking = [q["sql"] for q in captured.captured_queries if "FOR UPDATE" in q["sql"]]
        self.assertEqual(len(locking), 3, f"expected three row locks, got:\n{locking}")
        for sql in locking:
            self.assertNotIn("JOIN", sql, f"this lock reaches a joined table:\n{sql}")

    def test_two_invites_at_one_school_do_not_serialise(self):
        """Two admins, two different teachers, one school: neither waits."""
        # A membership for the lock to actually find. Against zero rows
        # `FOR UPDATE` locks nothing and this test would pass vacuously.
        teacher = User.objects.create_user(
            "held@example.com", None, email="held@example.com"
        )
        grant_membership(
            teacher, self.school, Role.TEACHER, status=MembershipStatus.INVITED
        )

        holding = threading.Event()
        release = threading.Event()

        def hold_a_grant():
            with transaction.atomic():
                grant_membership(
                    teacher, self.school, Role.TEACHER, status=MembershipStatus.INVITED
                )
                holding.set()
                release.wait(15)

        thread = run_in_thread(hold_a_grant)
        self.assertTrue(holding.wait(15), "the holding thread never started")

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    # NOWAIT so contention is an error rather than a hang.
                    cursor.execute(
                        "SELECT id FROM schools_school WHERE id = %s FOR UPDATE NOWAIT",
                        [self.school.pk],
                    )
                    cursor.fetchall()
        except OperationalError as exc:
            self.fail(f"a grant locked the School row it never writes: {exc}")
        finally:
            release.set()
            thread.join(15)
