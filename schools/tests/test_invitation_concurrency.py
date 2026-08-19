"""What the invitation flow locks, and what it must not.

`TransactionTestCase` rather than `TestCase`, and real threads: these need two
database connections whose commits are visible to each other, which a test
wrapped in a single rolled-back transaction cannot give. Interleaving is driven
by `threading.Event`, not by sleeps, so the window each test opens is the exact
one rather than one that happens to be wide enough today.

`Membership.Meta.ordering` sorts by `school__name` and `user__full_name`, and
Postgres locks a row in *every* joined table when `FOR UPDATE` meets a join, so
the default ordering quietly put an exclusive lock on the School row into every
membership lookup that took one — and two admins inviting two different teachers
at one school queued behind a row neither of them was touching.
"""

import threading

from django.db import connection, connections, transaction
from django.db.utils import OperationalError
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from accounts.models import MembershipStatus, Role, User
from accounts.services import grant_membership
from schools import invitations as invitation_service
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
