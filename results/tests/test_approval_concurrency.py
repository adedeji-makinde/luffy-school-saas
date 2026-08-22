"""Two people pressing approve at the same instant.

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

`TransactionTestCase` and real threads, on the reasoning
`accounts/tests/test_signin_concurrency.py` sets out: two connections whose
commits are visible to each other, released together by a barrier rather than
interleaved with sleeps, so both attempts are provably in flight.

Two schemas are not needed here — the race is between two connections on one
row — but the school is still created the production way, because the sheet
lives in a tenant schema and a thread that did not set its `search_path` would
find no table at all.
"""

import contextlib
import threading
from datetime import date

from django.db import connection, connections
from django.test import TransactionTestCase
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


class ApproveUnderConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.school = School(name="St Mary's", slug="st-marys", schema_name="st_marys")
        self.school.save()

        self.teacher = self._staff("kemi", "Kemi Bello", Role.TEACHER)
        self.vp = self._staff("ngozi", "Ngozi Eze", Role.VICE_PRINCIPAL_ACADEMIC)
        # Two principals, because the interesting race is two *different*
        # people approving at once. One person twice would be refused by the
        # same-signatory rule instead, which is a different test.
        self.principal = self._staff("tunde", "Tunde Alabi", Role.PRINCIPAL)
        self.other_principal = self._staff("amaka", "Amaka Obi", Role.PRINCIPAL)

        with connected_to(self.school):
            term = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            )
            group = ClassGroup.objects.create(name="JSS 1A", level=1)
            sheet = services.open_sheet(group, term)
            services.submit(sheet, self.teacher)
            services.check(sheet, self.vp)
            self.sheet_id = sheet.pk

    def _staff(self, username, full_name, role):
        user = User.objects.create_user(username, PASSWORD, full_name=full_name)
        grant_membership(user, self.school, role)
        return user

    def tearDown(self):
        connection.set_schema_to_public()
        # Drop the schema, not just the rows. `TransactionTestCase` flushes the
        # *public* tables between tests, which removes the `School` row — but a
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
        # is refused while this test's four staff memberships point at it. The
        # schema is the thing that has to go; the row is flushed for us.
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{self.school.schema_name}" CASCADE')
        super().tearDown()

    def _approve_together(self, actors):
        """Both actors call approve() on one sheet, released at once."""
        ready = threading.Barrier(len(actors), timeout=15)
        refusals = []
        unexpected = []

        def run(actor):
            try:
                with connected_to(self.school):
                    sheet = ResultSheet.objects.get(pk=self.sheet_id)
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
            self._staff(f"head-{n}", f"Head {n}", Role.PRINCIPAL) for n in range(2)
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
