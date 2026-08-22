"""Two people pressing approve at the same instant, and what each lock reaches.

`approve()` is read-modify-write on one row: read the state, decide the move is
legal, write the transition and the new state. Both requests read `checked`,
both find the move legal, and both proceed.

**What each defence actually does was measured, and it is not what it looks
like.** Removing `select_for_update()` and re-running these tests gives:

    IntegrityError: duplicate key value violates unique constraint
    "one_transition_to_each_state_per_cycle"
    DETAIL: Key (sheet_id, cycle, to_state)=(1, 0, approved) already exists.

So the *constraint* is what prevents the double approval — even unlocked, the
audit never gains a second approver. The *lock* is what turns the loser's
outcome from an unhandled `IntegrityError` — a 500 on a principal's screen,
with nothing said about what happened — into a `WrongState` naming the state
the sheet is now in. Two layers, two different jobs, and it would have been easy
to write the lock's docstring claiming the constraint's job.

That distinction is why `test_only_one_of_two_simultaneous_approvals_succeeds`
asserts on **both** halves: one row in the audit, *and* the loser holding a
refusal it can act on.

`LockScopeTests` at the foot asks the other question about the same lock — not
"does it serialise the right pair" but **"what else does it stop?"** A joined
`SELECT ... FOR UPDATE` locks a row in every joined table, so an ordering that
walks a foreign key silently puts an exclusive lock on rows the transition never
writes. This codebase has fixed that bug three times in other apps. It is *not*
present here — `QuerySet.get()` clears ordering itself — and those tests carry
the measurement that settled it, along with the one-line rewrite that brings the
bug back and fails them both.

`TransactionTestCase` and real threads, on the reasoning
`accounts/tests/test_signin_concurrency.py` sets out: two connections whose
commits are visible to each other, released together by a barrier rather than
interleaved with sleeps, so both attempts are provably in flight.

**Two schools, both real schemas**, as everywhere else in this project. The
race itself is between two connections on one row and needs only one school —
that was the argument for building one here, and it was wrong, because it is not
the only thing these tests can catch. Each thread resolves its own school from
`connection.schema_name`, so a thread whose `search_path` is wrong would
authorise against one school and write into another's schema, and no
single-tenant test can tell that apart from working correctly.
"""

import contextlib
import threading
from datetime import date

from django.db import OperationalError, connection, connections, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django_tenants.utils import schema_context

from academics.models import ClassGroup, Term, TermName
from accounts.models import Role, User
from accounts.services import grant_membership
from results import services
from results.models import ResultSheet, ResultSheetTransition, SheetState
from schools.models import School

PASSWORD = "correct-horse-battery"


@contextlib.contextmanager
def connected_to(school):
    with schema_context(school.schema_name):
        yield


class TwoSchoolsSetUp(TransactionTestCase):
    """St Mary's and Grace Academy, each with a sheet standing at `checked`."""

    def setUp(self):
        self.school = self._school("St Mary's", "st-marys", "st_marys")
        self.other_school = self._school("Grace Academy", "grace", "grace")

        self.teacher = self._staff("kemi", "Kemi Bello", self.school, Role.TEACHER)
        self.vp = self._staff(
            "ngozi", "Ngozi Eze", self.school, Role.VICE_PRINCIPAL_ACADEMIC
        )
        # Two principals, because the interesting race is two *different*
        # people approving at once. One person twice would be refused by the
        # same-signatory rule instead, which is a different test.
        self.principal = self._staff(
            "tunde", "Tunde Alabi", self.school, Role.PRINCIPAL
        )
        self.other_principal = self._staff(
            "amaka", "Amaka Obi", self.school, Role.PRINCIPAL
        )

        # Grace Academy's own staff. Authority is resolved per connection, so
        # these are the people who prove a thread wrote where it authorised.
        self.their_teacher = self._staff(
            "chidi", "Chidi Okafor", self.other_school, Role.TEACHER
        )
        self.their_vp = self._staff(
            "isioma", "Isioma Nwosu", self.other_school, Role.VICE_PRINCIPAL_ACADEMIC
        )
        self.their_principal = self._staff(
            "bisi", "Bisi Ojo", self.other_school, Role.PRINCIPAL
        )

        self.sheet_id, self.term_id, self.group_id = self._checked_sheet(
            self.school, self.teacher, self.vp
        )
        self.their_sheet_id, self.their_term_id, self.their_group_id = (
            self._checked_sheet(
                self.other_school, self.their_teacher, self.their_vp
            )
        )

    def _school(self, name, slug, schema_name):
        school = School(name=name, slug=slug, schema_name=schema_name)
        school.save()
        return school

    def _staff(self, username, full_name, school, role):
        user = User.objects.create_user(username, PASSWORD, full_name=full_name)
        grant_membership(user, school, role)
        return user

    def _checked_sheet(self, school, teacher, vp):
        """A sheet at this school walked as far as `checked`, the ordinary way."""
        with connected_to(school):
            term = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            )
            group = ClassGroup.objects.create(name="JSS 1A", level=1)
            sheet = services.open_sheet(group, term, teacher)
            services.submit(sheet, teacher)
            services.check(sheet, vp)
            return sheet.pk, term.pk, group.pk

    def tearDown(self):
        connection.set_schema_to_public()
        # Drop the schemas, not just the rows. `TransactionTestCase` flushes the
        # *public* tables between tests, which removes the `School` rows — but a
        # tenant schema is not a table and survives, so the next test's
        # `School.save()` finds `st_marys` already there, skips `CREATE SCHEMA`,
        # and inherits the previous test's Term. That surfaced here as a
        # `uniq_term_session_name` violation in `setUp`, which reads like a
        # fixture bug and is really a schema that outlived its test.
        #
        # `TestCase` elsewhere in this codebase does not need it: its rollback
        # covers tenant tables too, because they are in the same transaction.
        #
        # Dropped with SQL rather than `School.delete(force_drop=True)`, which
        # cannot run here: `Membership.school` is PROTECT, so deleting the row
        # is refused while this test's staff memberships point at it. The schema
        # is the thing that has to go; the row is flushed for us.
        with connection.cursor() as cursor:
            for school in (self.school, self.other_school):
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{school.schema_name}" CASCADE'
                )
        super().tearDown()


class ApproveUnderConcurrencyTests(TwoSchoolsSetUp):
    def _approve_together(self, actors, school=None, sheet_id=None):
        """Both actors call approve() on one sheet, released at once."""
        school = school or self.school
        sheet_id = sheet_id or self.sheet_id
        ready = threading.Barrier(len(actors), timeout=15)
        refusals = []
        unexpected = []

        def run(actor):
            try:
                with connected_to(school):
                    sheet = ResultSheet.objects.get(pk=sheet_id)
                    ready.wait()
                    services.approve(sheet, actor)
            except services.ResultsError as refused:
                refusals.append(refused)
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                unexpected.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(actor,)) for actor in actors]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        return refusals, unexpected

    def test_only_one_of_two_simultaneous_approvals_succeeds(self):
        refusals, unexpected = self._approve_together(
            [self.principal, self.other_principal]
        )

        self.assertEqual(unexpected, [], f"a thread failed: {unexpected}")

        with connected_to(self.school):
            sheet = ResultSheet.objects.get(pk=self.sheet_id)
            approvals = ResultSheetTransition.objects.filter(
                sheet=sheet, to_state=SheetState.APPROVED
            )

            # The whole point: one decision, one row, one name against it.
            self.assertEqual(approvals.count(), 1)
            self.assertEqual(sheet.state, SheetState.APPROVED)

        # And the loser was told, as a refusal it can act on rather than a 500.
        self.assertEqual(len(refusals), 1)
        self.assertIsInstance(refusals[0], services.WrongState)
        self.assertEqual(refusals[0].state, SheetState.APPROVED)

    def test_the_audit_never_shows_two_approvers_for_one_decision(self):
        """Stated separately because it is the consequence that matters.

        A count of rows is a proxy; this asserts the thing a school would
        actually be harmed by — an approval history naming two people for a
        decision only one of them took.
        """
        self._approve_together([self.principal, self.other_principal])

        with connected_to(self.school):
            approvers = list(
                ResultSheetTransition.objects.filter(
                    sheet_id=self.sheet_id, to_state=SheetState.APPROVED
                ).values_list("actor_id", flat=True)
            )

        self.assertEqual(len(approvers), 1)
        self.assertIn(approvers[0], {self.principal.pk, self.other_principal.pk})

    def test_four_at_once_still_leaves_one(self):
        """Two threads can pass by luck. Four is harder to be lucky with."""
        extra = [
            self._staff(f"head-{n}", f"Head {n}", self.school, Role.PRINCIPAL)
            for n in range(2)
        ]
        refusals, unexpected = self._approve_together(
            [self.principal, self.other_principal, *extra]
        )

        self.assertEqual(unexpected, [], f"a thread failed: {unexpected}")
        with connected_to(self.school):
            self.assertEqual(
                ResultSheetTransition.objects.filter(
                    sheet_id=self.sheet_id, to_state=SheetState.APPROVED
                ).count(),
                1,
            )
        self.assertEqual(len(refusals), 3)

    def test_a_race_at_one_school_leaves_the_other_school_untouched(self):
        """Four threads fighting over St Mary's sheet; Grace's does not move.

        The single-tenant version of this file could not have asked it. Each
        thread resolves its school from `connection.schema_name`, so the failure
        this guards against is not exotic — a thread whose `search_path` leaked
        would approve the wrong school's results, and the only visible symptom
        would be at the school that never pressed anything.
        """
        extra = [
            self._staff(f"head-{n}", f"Head {n}", self.school, Role.PRINCIPAL)
            for n in range(2)
        ]
        self._approve_together([self.principal, self.other_principal, *extra])

        with connected_to(self.other_school):
            theirs = ResultSheet.objects.get(pk=self.their_sheet_id)
            self.assertEqual(theirs.state, SheetState.CHECKED)
            self.assertEqual(
                ResultSheetTransition.objects.filter(
                    sheet_id=self.their_sheet_id, to_state=SheetState.APPROVED
                ).count(),
                0,
            )

    def test_two_schools_approving_at_once_do_not_serialise(self):
        """Both succeed. Neither school's chain is a queue for the other's.

        The sheets are different rows in different schemas, so nothing here
        should contend — but "should" is the word that got the joined lock into
        this module in the first place. Both approvals landing is the assertion;
        a lock reaching wider than its row would show up as one of them being
        refused or timing out.
        """
        ready = threading.Barrier(2, timeout=15)
        unexpected = []

        def run(school, sheet_id, actor):
            try:
                with connected_to(school):
                    sheet = ResultSheet.objects.get(pk=sheet_id)
                    ready.wait()
                    services.approve(sheet, actor)
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                unexpected.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(
                target=run, args=(self.school, self.sheet_id, self.principal)
            ),
            threading.Thread(
                target=run,
                args=(self.other_school, self.their_sheet_id, self.their_principal),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)

        self.assertEqual(unexpected, [], f"a thread failed: {unexpected}")
        for school, sheet_id in (
            (self.school, self.sheet_id),
            (self.other_school, self.their_sheet_id),
        ):
            with connected_to(school):
                self.assertEqual(
                    ResultSheet.objects.get(pk=sheet_id).state, SheetState.APPROVED
                )


class LockScopeTests(TwoSchoolsSetUp):
    """What the transition lock reaches, which is not the same as what it guards.

    `ResultSheet.Meta.ordering` is `["term", "class_group"]` — two relations. A
    `select_for_update()` that inherits it compiles to a three-table join, and
    Postgres locks a row in *each* joined table unless told `OF`. Every submit,
    check, approve, release and send-back would then take an exclusive lock on
    the term row and the class row, neither of which a transition writes: two
    principals approving two *different* classes in one term would serialise on
    the shared `academics_term` row, and a transition would contend with
    anything else touching that term — flipping `Term.is_current`, for instance.

    **That is not the state of the code, and these tests were written after
    believing it was.** `QuerySet.get()` clears ordering itself in Django 5.2,
    so `_locked()` compiles to a single-table `FOR UPDATE` whether or not it
    says `.order_by()`. Both tests here pass with that call removed — which is
    the honest reason they exist as a *pair* and are kept: they pin the
    property, not the spelling.

    What they do catch was measured rather than argued. Rewriting `_locked()` as
    `.filter(pk=...).first()` — which does not clear ordering, and is the sort
    of change nobody would think of as touching locking — fails both:

        AssertionError: 'JOIN' unexpectedly found in 'SELECT ... INNER JOIN
        "academics_term" ... FOR UPDATE'

        AssertionError: a transition locked a row it never writes: could not
        obtain lock on row in relation "academics_term"

    `accounts/models.py` states the habit these exist to keep: "the habit is
    what keeps the next person from adding a joined sort and locking two more
    tables without noticing."
    """

    def test_no_row_lock_a_transition_takes_reaches_a_joined_table(self):
        """Asserted on the SQL, so an ordering added later fails here first.

        Cheaper and more specific than the contention test below — it names the
        query rather than the symptom — and the two are kept as a pair because
        this one would still pass if `FOR UPDATE` were dropped altogether.
        """
        with connected_to(self.school):
            sheet = ResultSheet.objects.get(pk=self.sheet_id)
            with CaptureQueriesContext(connection) as captured:
                services.approve(sheet, self.principal)

        locking = [
            query["sql"] for query in captured.captured_queries
            if "FOR UPDATE" in query["sql"]
        ]
        self.assertTrue(locking, "approve() took no row lock at all")
        for sql in locking:
            self.assertNotIn("JOIN", sql, f"this lock reaches a joined table:\n{sql}")

    def test_approving_one_class_does_not_lock_the_term(self):
        """The consequence, proved by contention rather than by reading SQL.

        A thread holds an approval's transaction open; the test then asks
        Postgres for the term row `FOR UPDATE NOWAIT`. NOWAIT so contention is
        an immediate error rather than a hang the suite would sit through.
        """
        holding = threading.Event()
        release = threading.Event()
        failed = []

        def hold_an_approval():
            try:
                with connected_to(self.school):
                    with transaction.atomic():
                        sheet = ResultSheet.objects.get(pk=self.sheet_id)
                        services.approve(sheet, self.principal)
                        holding.set()
                        release.wait(15)
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                failed.append(exc)
                holding.set()
            finally:
                connections.close_all()

        thread = threading.Thread(target=hold_an_approval)
        thread.start()
        self.assertTrue(holding.wait(15), "the holding thread never started")
        self.assertEqual(failed, [], f"the holding thread failed: {failed}")

        try:
            with connected_to(self.school):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT id FROM academics_term WHERE id = %s "
                            "FOR UPDATE NOWAIT",
                            [self.term_id],
                        )
                        cursor.fetchall()
                        cursor.execute(
                            "SELECT id FROM academics_classgroup WHERE id = %s "
                            "FOR UPDATE NOWAIT",
                            [self.group_id],
                        )
                        cursor.fetchall()
        except OperationalError as exc:
            self.fail(f"a transition locked a row it never writes: {exc}")
        finally:
            release.set()
            thread.join(15)
