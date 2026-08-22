"""The chain a term's results walk, and the five things it must not allow.

Every fixture builds **two schools**, both as real schemas. A single-tenant test
here cannot fail for the thing most likely to be wrong — a sheet, an audit row
or an authority check reaching across a schema boundary — and the actor is an
`accounts.User` in the *shared* schema, so "which school is this person a vice
principal of" is a genuinely cross-schema question.

Sections, one per property:

- the forward path records who and when, as rows;
- a sheet can be sent back, with a reason, from every stage but released;
- one person cannot take two steps on one pass, in the service *and* in the
  database;
- released is terminal, in the database and not only in a docstring;
- the log cannot be edited or deleted, by the model or by raw SQL.

Concurrency lives in `test_approval_concurrency.py`, which needs real threads.
"""

import contextlib
from datetime import date

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django_tenants.utils import schema_context

from academics.models import ClassGroup, Term, TermName
from accounts.models import MembershipStatus, Role, User
from accounts.services import grant_membership
from results import services
from results.models import (
    ResultSheet,
    ResultSheetTransition,
    SheetState,
    TransitionsAreAppendOnly,
)
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


class ChainSetUp(TestCase):
    """St Mary's and Grace Academy, each with the four signatories."""

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

        self.teacher = self._staff("kemi", "Kemi Bello", self.stmarys, Role.TEACHER)
        self.vp = self._staff(
            "ngozi", "Ngozi Eze", self.stmarys, Role.VICE_PRINCIPAL_ACADEMIC
        )
        self.principal = self._staff(
            "tunde", "Tunde Alabi", self.stmarys, Role.PRINCIPAL
        )
        self.registrar = self._staff("bola", "Bola Ade", self.stmarys, Role.ADMIN)

        # Grace Academy's own principal — authority at one school is not
        # authority at another, and this is who proves it.
        self.their_principal = self._staff(
            "chidi", "Chidi Okafor", self.grace, Role.PRINCIPAL
        )

        with connected_to(self.stmarys):
            self.term_id = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            ).pk
            self.jss1a_id = ClassGroup.objects.create(name="JSS 1A", level=1).pk

        with connected_to(self.grace):
            self.their_term_id = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            ).pk
            self.their_jss1a_id = ClassGroup.objects.create(name="JSS 1A", level=1).pk

    def _staff(self, username, full_name, school, role):
        user = User.objects.create_user(username, PASSWORD, full_name=full_name)
        grant_membership(user, school, role)
        return user

    def tearDown(self):
        connection.set_schema_to_public()
        super().tearDown()

    def sheet(self, actor=None):
        return services.open_sheet(
            ClassGroup.objects.get(pk=self.jss1a_id),
            Term.objects.get(pk=self.term_id),
            actor or self.teacher,
        )

    def walk_to(self, state):
        """A sheet standing at `state`, having got there the ordinary way."""
        sheet = self.sheet()
        if state == SheetState.DRAFT:
            return sheet
        services.submit(sheet, self.teacher)
        if state == SheetState.SUBMITTED:
            return ResultSheet.objects.get(pk=sheet.pk)
        services.check(sheet, self.vp)
        if state == SheetState.CHECKED:
            return ResultSheet.objects.get(pk=sheet.pk)
        services.approve(sheet, self.principal)
        if state == SheetState.APPROVED:
            return ResultSheet.objects.get(pk=sheet.pk)
        services.release(sheet, self.principal)
        return ResultSheet.objects.get(pk=sheet.pk)


class TheForwardPathTests(ChainSetUp):
    def test_a_new_sheet_starts_in_draft_with_no_history(self):
        with connected_to(self.stmarys):
            sheet = self.sheet()

            self.assertEqual(sheet.state, SheetState.DRAFT)
            self.assertEqual(services.history(sheet).count(), 0)

    def test_opening_the_same_sheet_twice_returns_the_same_row(self):
        """A screen opens a sheet by being looked at; the second look is not an
        error, and must not be a second chain for one class."""
        with connected_to(self.stmarys):
            first = self.sheet()
            second = self.sheet()

            self.assertEqual(first.pk, second.pk)
            self.assertEqual(ResultSheet.objects.count(), 1)

    def test_the_whole_chain_walks_and_records_four_steps(self):
        with connected_to(self.stmarys):
            released = self.walk_to(SheetState.RELEASED)

            self.assertEqual(released.state, SheetState.RELEASED)
            steps = [
                (t.from_state, t.to_state, t.actor_id)
                for t in services.history(released)
            ]

        self.assertEqual(
            steps,
            [
                (SheetState.DRAFT, SheetState.SUBMITTED, self.teacher.pk),
                (SheetState.SUBMITTED, SheetState.CHECKED, self.vp.pk),
                (SheetState.CHECKED, SheetState.APPROVED, self.principal.pk),
                (SheetState.APPROVED, SheetState.RELEASED, self.principal.pk),
            ],
        )

    def test_every_step_records_when(self):
        with connected_to(self.stmarys):
            released = self.walk_to(SheetState.RELEASED)
            stamps = [t.created_at for t in services.history(released)]

        self.assertEqual(len(stamps), 4)
        self.assertTrue(all(stamp is not None for stamp in stamps))
        self.assertEqual(stamps, sorted(stamps))

    def test_steps_cannot_be_taken_out_of_order(self):
        with connected_to(self.stmarys):
            sheet = self.sheet()

            with self.assertRaises(services.WrongState) as caught:
                services.approve(sheet, self.principal)

            self.assertEqual(caught.exception.state, SheetState.DRAFT)
            self.assertEqual(services.history(sheet).count(), 0)

    def test_the_principal_who_approved_may_also_release(self):
        """Release is not a second judgement — it publishes one already made.

        Deliberately outside the same-signatory rule; the index that holds that
        rule excludes `released` for exactly this.
        """
        with connected_to(self.stmarys):
            released = self.walk_to(SheetState.RELEASED)
            last_two = list(services.history(released))[-2:]

        self.assertEqual(
            [t.actor_id for t in last_two], [self.principal.pk, self.principal.pk]
        )

    def test_an_administrator_may_not_release(self):
        """Release is the principal's act, and task 8 rests on that.

        This asserted the opposite — an administrator releasing — on the
        argument that release is often gated on something clerical rather than
        being a second academic judgement. That is true of schools and was
        still the wrong call here: the settled rule for this phase makes
        revision principal-only *because* release is the principal's act, so
        widening release would have left the next task's authority resting on a
        premise this module no longer honoured.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.APPROVED)

            with self.assertRaises(services.NotAllowedToActOnResults):
                services.release(sheet, self.registrar)

            self.assertEqual(
                ResultSheet.objects.get(pk=sheet.pk).state, SheetState.APPROVED
            )

    def test_a_refusal_names_roles_the_way_a_person_reads_them(self):
        """"Principal", not "vp_academic".

        The stored value is an internal key — `vp_academic` was truncated to fit
        `Membership.role`'s sixteen characters precisely because nobody was
        meant to read it — and the refusal was joining the raw keys.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(services.NotAllowedToActOnResults) as caught:
                services.check(sheet, self.registrar)

            message = str(caught.exception)
            self.assertIn(Role.VICE_PRINCIPAL_ACADEMIC.label, message)
            self.assertNotIn(Role.VICE_PRINCIPAL_ACADEMIC.value, message)


class OpeningASheetTests(ChainSetUp):
    """`open_sheet()` is a writer, so it asks who is calling like the rest.

    It did not, and the module docstring is what makes that a defect rather
    than a gap: it argues the `_as()` split other service modules use is absent
    here because there are no actor-less primitives. `open_sheet()` was exactly
    that primitive — exported, writing a tenant table, never once resolving the
    school on the connection. Anything reachable from a future screen could
    mint `ResultSheet` rows for arbitrary (class, term) pairs, each of which
    `ResultSheetTransition.sheet`'s `PROTECT` then makes awkward to remove.
    """

    def _open(self, actor):
        return services.open_sheet(
            ClassGroup.objects.get(pk=self.jss1a_id),
            Term.objects.get(pk=self.term_id),
            actor,
        )

    def test_every_role_that_can_act_on_the_chain_can_open_a_sheet(self):
        """Not narrower than the narrowest step. Opening decides nothing."""
        for actor in (self.teacher, self.vp, self.principal, self.registrar):
            with self.subTest(actor=str(actor)):
                with connected_to(self.stmarys):
                    self.assertIsNotNone(self._open(actor))

    def test_a_parent_cannot_open_a_sheet(self):
        parent = User.objects.create_user("ade", PASSWORD, full_name="Ade Parent")
        grant_membership(parent, self.stmarys, Role.PARENT)

        with connected_to(self.stmarys):
            with self.assertRaises(services.NotAllowedToActOnResults):
                self._open(parent)

            self.assertEqual(ResultSheet.objects.count(), 0)

    def test_another_schools_principal_cannot_open_a_sheet_here(self):
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotAllowedToActOnResults):
                self._open(self.their_principal)

            self.assertEqual(ResultSheet.objects.count(), 0)

    def test_results_cannot_be_acted_on_from_the_portal(self):
        """The public schema is the portal, not a customer.

        `_school_on_this_connection()` had no answer here. A caller that never
        entered a tenant got `School.DoesNotExist`, which is outside
        `ResultsError`, so every `except ResultsError` handler missed it and a
        refusal arrived as a 500. Worse where a `School(schema_name="public")`
        row exists — this codebase creates one in its own tests — because then
        the lookup *succeeds* and authority is checked against the portal's
        memberships rather than any school's.
        """
        portal = School(name="Portal", slug="portal", schema_name="public")
        portal.auto_create_schema = False
        portal.save()
        grant_membership(self.principal, portal, Role.PRINCIPAL)

        connection.set_schema_to_public()
        with self.assertRaises(services.NotAllowedToActOnResults) as caught:
            services.open_sheet(None, None, self.principal)

        self.assertIn("portal", str(caught.exception))


class WhoMayActTests(ChainSetUp):
    def test_a_teacher_may_not_check_their_own_submission(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(services.NotAllowedToActOnResults):
                services.check(sheet, self.teacher)

    def test_a_vice_principal_may_not_approve(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.CHECKED)

            with self.assertRaises(services.NotAllowedToActOnResults):
                services.approve(sheet, self.vp)

    def test_an_administrator_may_not_check(self):
        """The office may submit and may release. The academic check is the one
        step it does not get, or the office could both submit and check."""
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(services.NotAllowedToActOnResults):
                services.check(sheet, self.registrar)

    def test_another_schools_principal_may_not_approve_here(self):
        """Authority is asked at the school on the connection, not at any school.

        The actor is a row in the *shared* schema, so this is the question a
        single-tenant test could never ask.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.CHECKED)

            with self.assertRaises(services.NotAllowedToActOnResults):
                services.approve(sheet, self.their_principal)

            self.assertEqual(
                ResultSheet.objects.get(pk=sheet.pk).state, SheetState.CHECKED
            )

    def test_a_suspended_principal_may_not_approve(self):
        membership = self.principal.memberships.get(school=self.stmarys)
        membership.status = MembershipStatus.SUSPENDED
        membership.save(update_fields=["status"])

        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.CHECKED)

            with self.assertRaises(services.NotAllowedToActOnResults):
                services.approve(sheet, self.principal)

    def test_platform_staff_may_not_approve(self):
        operator = User.objects.create_superuser(
            "ops@luffy.school", PASSWORD, full_name="Ope Rator"
        )

        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.CHECKED)

            with self.assertRaises(services.NotAllowedToActOnResults):
                services.approve(sheet, operator)


class SendingBackTests(ChainSetUp):
    def test_a_vice_principal_can_send_a_submission_back(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            recorded = services.send_back(sheet, self.vp, "Chemistry CA is out of 30.")

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.DRAFT)
            self.assertEqual(recorded.reason, "Chemistry CA is out of 30.")

    def test_a_principal_can_send_back_from_checked(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.CHECKED)

            services.send_back(sheet, self.principal, "Two names swapped.")

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.DRAFT)

    def test_a_principal_can_send_back_from_approved(self):
        """The late catch: approved, not yet released, and something is wrong.

        Its own test rather than a subTest of the one above, because the log is
        append-only — a loop would have to either share one sheet, which the
        same-signatory rule then refuses, or rewrite fixtures between passes,
        and a test that edits an audit log to test an audit log proves nothing.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.APPROVED)

            services.send_back(sheet, self.principal, "Two names swapped.")

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.DRAFT)

    def test_a_send_back_must_say_why(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(services.ResultsError):
                services.send_back(sheet, self.vp, "   ")

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.SUBMITTED)

    def test_the_database_refuses_a_reasonless_send_back_too(self):
        """The service is not the only writer. An import is.

        Both spellings of "no reason", because the two layers have to refuse the
        same inputs to be worth calling two layers. `send_back()` compares
        `reason.strip()`, so a reason of three spaces is refused by the service;
        the constraint's first spelling was `~Q(reason="")`, which accepted it.
        The gap was exactly the caller the constraint exists for, and what
        reached the teacher was a send-back whose reason renders blank.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            for reason in ("", "   ", "\t\n "):
                with self.subTest(reason=repr(reason)):
                    with self.assertRaises(IntegrityError):
                        with transaction.atomic():
                            ResultSheetTransition.objects.create(
                                sheet=sheet,
                                from_state=SheetState.SUBMITTED,
                                to_state=SheetState.DRAFT,
                                cycle=sheet.cycle,
                                actor_id=self.vp.pk,
                                reason=reason,
                            )

            # And the rule is not "no send-backs" — a real reason is accepted
            # by the same constraint. Without this the three above would pass
            # against a constraint that refused everything.
            ResultSheetTransition.objects.create(
                sheet=sheet,
                from_state=SheetState.SUBMITTED,
                to_state=SheetState.DRAFT,
                cycle=sheet.cycle,
                actor_id=self.vp.pk,
                reason="Chemistry CA is out of 30.",
            )

    def test_a_send_back_keeps_the_whole_history(self):
        """The reason columns exist for. After a send-back and a resubmit, a
        `submitted_by` column would say only who resubmitted."""
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)
            services.send_back(sheet, self.vp, "Chemistry CA is out of 30.")
            sheet.refresh_from_db()
            services.submit(sheet, self.teacher)

            steps = [(t.from_state, t.to_state, t.cycle) for t in services.history(sheet)]

        self.assertEqual(
            steps,
            [
                (SheetState.DRAFT, SheetState.SUBMITTED, 0),
                (SheetState.SUBMITTED, SheetState.DRAFT, 0),
                (SheetState.DRAFT, SheetState.SUBMITTED, 1),
            ],
        )

    def test_a_send_back_opens_a_new_cycle(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)
            self.assertEqual(sheet.cycle, 0)

            services.send_back(sheet, self.vp, "Wrong term.")

            sheet.refresh_from_db()
            self.assertEqual(sheet.cycle, 1)

    def test_the_same_teacher_may_resubmit_after_a_send_back(self):
        """The reason the index is keyed on the cycle and not on the sheet.

        A teacher who submits, is refused, and submits again is the ordinary
        case, not a separation-of-duties breach.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)
            services.send_back(sheet, self.vp, "Chemistry CA is out of 30.")
            sheet.refresh_from_db()

            services.submit(sheet, self.teacher)

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.SUBMITTED)

    def test_the_same_vice_principal_may_check_after_sending_back(self):
        """They refused in cycle 0 and check in cycle 1 — different passes."""
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)
            services.send_back(sheet, self.vp, "Chemistry CA is out of 30.")
            sheet.refresh_from_db()
            services.submit(sheet, self.teacher)
            sheet.refresh_from_db()

            services.check(sheet, self.vp)

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.CHECKED)

    def test_a_teacher_cannot_send_back(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(services.NotAllowedToActOnResults):
                services.send_back(sheet, self.teacher, "I changed my mind.")


class TheCycleIsNotTheCallersToSetTests(ChainSetUp):
    """`cycle` is load-bearing, so nothing outside a send-back may move it.

    Both database guards that matter are scoped *per cycle*:
    `one_signature_per_person_per_review_cycle` and
    `one_transition_to_each_state_per_cycle`. A transition written at the wrong
    cycle therefore lands in the wrong bucket for both — a second approval
    stamped `cycle=1` would not collide with the first at `cycle=0`, and the
    double-approval guard would simply not fire. The value has to come from one
    place and one place only.

    It does: `_move()` reads it off the row it locked, never off the instance
    the caller passed in, and only a send-back increments it.
    """

    def test_each_send_back_and_re_advance_increments_the_cycle_once(self):
        with connected_to(self.stmarys):
            sheet = self.sheet()

            # Pass 0: submitted, then refused.
            services.submit(sheet, self.teacher)
            sheet.refresh_from_db()
            services.send_back(sheet, self.vp, "Chemistry CA is out of 30.")
            sheet.refresh_from_db()

            # Pass 1: submitted, checked, then refused by the principal.
            services.submit(sheet, self.teacher)
            sheet.refresh_from_db()
            services.check(sheet, self.vp)
            sheet.refresh_from_db()
            services.send_back(sheet, self.principal, "Two names swapped.")
            sheet.refresh_from_db()

            # Pass 2: all the way through.
            services.submit(sheet, self.teacher)
            sheet.refresh_from_db()
            services.check(sheet, self.vp)
            sheet.refresh_from_db()
            services.approve(sheet, self.principal)
            sheet.refresh_from_db()

            steps = [(t.to_state, t.cycle) for t in services.history(sheet)]
            final_cycle = sheet.cycle

        self.assertEqual(
            steps,
            [
                (SheetState.SUBMITTED, 0),
                (SheetState.DRAFT, 0),
                (SheetState.SUBMITTED, 1),
                (SheetState.CHECKED, 1),
                (SheetState.DRAFT, 1),
                (SheetState.SUBMITTED, 2),
                (SheetState.CHECKED, 2),
                (SheetState.APPROVED, 2),
            ],
        )
        # Two send-backs, two increments. Not three — advancing never bumps it.
        self.assertEqual(final_cycle, 2)

    def test_only_a_send_back_increments_it(self):
        """Walking the whole chain forward leaves the cycle at zero."""
        with connected_to(self.stmarys):
            released = self.walk_to(SheetState.RELEASED)

            self.assertEqual(released.cycle, 0)
            self.assertEqual(
                {t.cycle for t in services.history(released)}, {0}
            )

    def test_a_stale_sheet_instance_cannot_write_at_an_old_cycle(self):
        """The one that would actually happen, and the reason `_move()` re-reads.

        A screen loads the sheet, somebody else sends it back, and the screen
        then submits using the instance it is still holding — whose `cycle` says
        0. If the transition were stamped from that instance, it would be
        written into a bucket the guards have already used, and a later genuine
        step at cycle 1 would not collide with it.

        `_move()` takes the cycle from the row it locked, so the stale instance
        contributes nothing but a primary key.
        """
        with connected_to(self.stmarys):
            sheet = self.sheet()
            services.submit(sheet, self.teacher)

            # A second reference, as a long-lived screen would hold.
            stale = ResultSheet.objects.get(pk=sheet.pk)
            self.assertEqual(stale.cycle, 0)

            fresh = ResultSheet.objects.get(pk=sheet.pk)
            services.send_back(fresh, self.vp, "Chemistry CA is out of 30.")

            # The stale instance still believes it is cycle 0.
            self.assertEqual(stale.cycle, 0)
            services.submit(stale, self.teacher)

            latest = services.history(sheet).last()
            self.assertEqual(latest.to_state, SheetState.SUBMITTED)
            self.assertEqual(latest.cycle, 1)

    def test_no_transition_function_lets_a_caller_name_the_cycle(self):
        """Structural, and cheap. The value has exactly one source.

        A `cycle=` argument added to any of these in a hurry would silently
        unscope both database guards, and nothing else in the suite would fail.
        """
        import inspect

        for function in (
            services.submit,
            services.check,
            services.approve,
            services.release,
            services.send_back,
        ):
            with self.subTest(function=function.__name__):
                self.assertNotIn(
                    "cycle", inspect.signature(function).parameters
                )

    def test_the_same_signatory_guard_still_bites_in_a_later_cycle(self):
        """Scoping per cycle must not have switched the guard off after a
        send-back — which is exactly what a mis-stamped cycle would look like.

        **This test used to be unable to fail.** It walked the sheet to
        `approved` in cycle 1 and then asserted a second `approve()` was
        refused — but a sheet at `approved` fails the *state* check first, so
        `_require_not_already_signed` was never reached. Deleting both the guard
        and its unique constraint left it green, and `OnePersonCannotTakeTwoSteps`
        covers only cycle 0, so the property the class claims had no coverage
        anywhere.

        What actually exercises the guard is one person taking **two different
        advancing steps in the same later cycle** — Kemi, class teacher and
        acting vice principal, submitting and then checking in cycle 1. The
        sheet is at `submitted` and `check()` expects `submitted`, so the state
        check passes and the signature rule is the only thing that can refuse.
        """
        # Kemi is the class teacher *and* the acting vice principal, the small
        # school case `OnePersonCannotTakeTwoStepsTests` is built on.
        grant_membership(self.teacher, self.stmarys, Role.VICE_PRINCIPAL_ACADEMIC)

        with connected_to(self.stmarys):
            sheet = self.sheet()
            services.submit(sheet, self.teacher)
            sheet.refresh_from_db()
            services.send_back(sheet, self.principal, "Chemistry CA is out of 30.")
            sheet.refresh_from_db()
            self.assertEqual(sheet.cycle, 1)

            services.submit(sheet, self.teacher)
            sheet.refresh_from_db()

            with self.assertRaises(services.AlreadySignedThisCycle) as caught:
                services.check(sheet, self.teacher)

            # It names the step they took *this* pass, not the one in cycle 0.
            self.assertEqual(caught.exception.existing.cycle, 1)
            self.assertEqual(
                caught.exception.existing.to_state, SheetState.SUBMITTED
            )

    def test_the_database_holds_the_same_signatory_rule_in_a_later_cycle(self):
        """The half the service cannot hold, asked in cycle 1 rather than 0.

        `one_signature_per_person_per_review_cycle` is what stands when two
        concurrent requests both read "they have not signed yet". Its scoping is
        `(sheet, cycle, actor)`, so a cycle stamped from the wrong place would
        leave it guarding an empty bucket — and the service-level test above
        cannot see that, because it never reaches the index.
        """
        grant_membership(self.teacher, self.stmarys, Role.VICE_PRINCIPAL_ACADEMIC)

        with connected_to(self.stmarys):
            sheet = self.sheet()
            services.submit(sheet, self.teacher)
            sheet.refresh_from_db()
            services.send_back(sheet, self.principal, "Chemistry CA is out of 30.")
            sheet.refresh_from_db()
            services.submit(sheet, self.teacher)
            sheet.refresh_from_db()

            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ResultSheetTransition.objects.create(
                        sheet=sheet,
                        from_state=SheetState.SUBMITTED,
                        to_state=SheetState.CHECKED,
                        cycle=1,
                        actor_id=self.teacher.pk,
                    )


class OnePersonCannotTakeTwoStepsTests(ChainSetUp):
    """The small-school case: one person holding two memberships.

    `grant_membership` allows it, and it is the ordinary state of a school with
    nine staff. The rule is not that the roles cannot be held together — it is
    that one person cannot sign twice on one pass.
    """

    def setUp(self):
        super().setUp()
        # Kemi is the class teacher *and* the acting vice principal.
        grant_membership(self.teacher, self.stmarys, Role.VICE_PRINCIPAL_ACADEMIC)

    def test_the_service_refuses_and_names_what_they_already_did(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(services.AlreadySignedThisCycle) as caught:
                services.check(sheet, self.teacher)

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.SUBMITTED)

        self.assertEqual(caught.exception.existing.to_state, SheetState.SUBMITTED)
        self.assertIn("submitted", str(caught.exception))

    def test_the_database_refuses_it_when_the_service_is_bypassed(self):
        """The constraint, not the convention. An import writes rows directly."""
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ResultSheetTransition.objects.create(
                        sheet=sheet,
                        from_state=SheetState.SUBMITTED,
                        to_state=SheetState.CHECKED,
                        cycle=sheet.cycle,
                        actor_id=self.teacher.pk,
                    )

    def test_holding_both_roles_is_still_allowed(self):
        """The membership is legitimate; only the double signature is not."""
        self.assertEqual(
            self.teacher.roles_at(self.stmarys),
            {Role.TEACHER.value, Role.VICE_PRINCIPAL_ACADEMIC.value},
        )


class ReleaseIsTerminalTests(ChainSetUp):
    def test_a_released_sheet_cannot_be_sent_back(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.RELEASED)

            with self.assertRaises(services.ReleaseIsFinal):
                services.send_back(sheet, self.principal, "Wrong scores went out.")

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.RELEASED)

    def test_a_released_sheet_cannot_be_released_again(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.RELEASED)

            with self.assertRaises(services.ReleaseIsFinal):
                services.release(sheet, self.principal)

    def test_the_database_refuses_any_transition_out_of_released(self):
        """`nothing_moves_out_of_released` — the guard on the *log*.

        It stops a reversal being **recorded**. On its own it does not stop one
        being **performed**; that is the next test, and the two are stated
        separately because conflating them is what left the gap.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.RELEASED)

            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ResultSheetTransition.objects.create(
                        sheet=sheet,
                        from_state=SheetState.RELEASED,
                        to_state=SheetState.DRAFT,
                        cycle=sheet.cycle,
                        actor_id=self.principal.pk,
                        reason="Undo it.",
                    )

    def test_the_database_refuses_a_bulk_update_moving_a_sheet_out_of_released(self):
        """The guard on the *sheet*, which was missing entirely.

        `nothing_moves_out_of_released` lives on the transition table, and
        migration 0002's trigger fires on the transition table. Nothing touched
        `results_resultsheet`, so this — from a psql session, an import, or a
        bulk fix — succeeded silently:

            ResultSheet.objects.filter(state="released").update(state="draft")

        No transition row is written, so no constraint is met. The sheet
        reverted and the audit showed no retraction at all, which is worse than
        an unguarded revert: the log then reads as though the result is still
        released while the sheet is a draft somebody can edit.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.RELEASED)

            for target in (SheetState.DRAFT, SheetState.APPROVED):
                with self.subTest(target=target):
                    with self.assertRaises(Exception) as caught:
                        with transaction.atomic():
                            ResultSheet.objects.filter(pk=sheet.pk).update(
                                state=target
                            )
                    self.assertIn("released to parents", str(caught.exception))

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.RELEASED)

    def test_a_released_sheet_may_still_be_written_for_anything_but_its_state(self):
        """The trigger guards the rule, not the row.

        A guard broader than the rule is one somebody eventually turns off
        wholesale, and task 8's revision has to be able to write this row.
        """
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.RELEASED)

            ResultSheet.objects.filter(pk=sheet.pk).update(
                state=SheetState.RELEASED, updated_at=sheet.updated_at
            )

            sheet.refresh_from_db()
            self.assertEqual(sheet.state, SheetState.RELEASED)


class TheLogIsAppendOnlyTests(ChainSetUp):
    def test_the_model_refuses_to_rewrite_a_transition(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)
            recorded = services.history(sheet).first()

            recorded.reason = "something else"
            with self.assertRaises(TransitionsAreAppendOnly):
                recorded.save()

    def test_the_model_refuses_to_delete_a_transition(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)
            recorded = services.history(sheet).first()

            with self.assertRaises(TransitionsAreAppendOnly):
                recorded.delete()

    def test_the_database_refuses_a_bulk_update_that_skips_the_model(self):
        """The one that matters. `.update()` never calls `save()`."""
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(Exception) as caught:
                with transaction.atomic():
                    ResultSheetTransition.objects.filter(sheet=sheet).update(
                        actor_id=self.principal.pk
                    )

            self.assertIn("append-only", str(caught.exception))

    def test_the_database_refuses_a_bulk_delete_that_skips_the_model(self):
        with connected_to(self.stmarys):
            sheet = self.walk_to(SheetState.SUBMITTED)

            with self.assertRaises(Exception) as caught:
                with transaction.atomic():
                    ResultSheetTransition.objects.filter(sheet=sheet).delete()

            self.assertIn("append-only", str(caught.exception))


class TwoSchoolsTests(ChainSetUp):
    def test_a_sheet_is_invisible_to_the_other_school(self):
        with connected_to(self.stmarys):
            self.walk_to(SheetState.RELEASED)
            self.assertEqual(ResultSheet.objects.count(), 1)

        with connected_to(self.grace):
            self.assertEqual(ResultSheet.objects.count(), 0)
            self.assertEqual(ResultSheetTransition.objects.count(), 0)

    def test_each_school_runs_its_own_chain_for_the_same_class_name(self):
        with connected_to(self.stmarys):
            self.walk_to(SheetState.RELEASED)

        with connected_to(self.grace):
            theirs = services.open_sheet(
                ClassGroup.objects.get(pk=self.their_jss1a_id),
                Term.objects.get(pk=self.their_term_id),
                self.their_principal,
            )
            self.assertEqual(theirs.state, SheetState.DRAFT)

        with connected_to(self.stmarys):
            ours = ResultSheet.objects.get()
            self.assertEqual(ours.state, SheetState.RELEASED)

    def test_one_sheet_per_class_per_term_is_a_database_rule(self):
        with connected_to(self.stmarys):
            self.sheet()

            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ResultSheet.objects.create(
                        class_group_id=self.jss1a_id, term_id=self.term_id
                    )
