"""Moving a result sheet along the chain. The only supported way to write one.

Five acts: submit, check, approve, release, and send back. Each is its own
function because each is a different decision by a different person, and a
single `transition(to=...)` would have made the authority table a lookup that
every caller could get wrong.

Every one of them does the same four things in the same order, and the order is
the security-relevant part:

1. **Check who is asking**, at the school on the connection — *before* anything
   is read or locked. `gradebook.api`'s `ExistenceOracleTests` settled this for
   the codebase: a caller who may not act should not be able to tell a sheet
   that exists from one that does not by which refusal comes back. It also
   keeps somebody with no authority from holding a row lock at all.
2. **Take the row lock.** `select_for_update()` on the sheet. Two people
   pressing approve at the same instant would otherwise both read `checked`,
   both find the move legal, and both write.

   What the lock buys was measured rather than assumed, and it is narrower than
   it sounds: `one_transition_to_each_state_per_cycle` already refuses the
   second row, so an unlocked race does **not** corrupt the audit. The lock is
   what makes the loser receive a `WrongState` naming where the sheet got to,
   instead of an unhandled `IntegrityError` — a 500 on a principal's screen
   saying nothing. See `tests/test_approval_concurrency.py` for the control run.
3. **Check the state under that lock**, never the state on the instance the
   caller passed in — that one was read at some earlier moment and can have
   moved since.
4. **Check they have not already signed this pass** — the same-signatory rule,
   also under the lock.

Then the row is written and the sheet is updated inside one transaction, so a
sheet whose state says `approved` always has a row saying who approved it.

The `_as()` split the other service modules use is deliberately *absent* here.
There are no primitives: every act in this module is somebody's signature, so
there is no version of it that makes sense without an actor. A data migration
that wants to move a sheet has to name the person it is moving it on behalf of,
which is the right amount of friction for rewriting an approval chain.
"""

from django.db import connection, transaction

from accounts.models import Role

from .models import (
    ADVANCING_STATES,
    SENDABLE_BACK_FROM,
    ResultSheet,
    ResultSheetTransition,
    SheetState,
)


class ResultsError(Exception):
    """A sheet could not be moved as asked.

    One base class for the module, as `fees.services.FeeLedgerError` and
    `gradebook.services.GradebookError` are for theirs.
    """


class NotAllowedToActOnResults(ResultsError):
    """The actor holds no role at this school that may take this step."""


class WrongState(ResultsError):
    """The sheet is not where it would have to be for this step.

    Carries `state` — where it actually is — so a caller can say what happened
    rather than only that something did. That matters most when the answer is
    "somebody else already approved it while you were reading".
    """

    def __init__(self, message, state=None):
        super().__init__(message)
        self.state = state


class AlreadySignedThisCycle(ResultsError):
    """This person has already taken a step on this pass through the chain.

    The separation-of-duties refusal. Carries the transition they already made,
    so the refusal can name it: "you submitted this sheet" is a far more useful
    sentence than "you may not check it".
    """

    def __init__(self, message, existing=None):
        super().__init__(message)
        self.existing = existing


class ReleaseIsFinal(ResultsError):
    """A released result cannot be moved. It can only be revised."""


# ---------------------------------------------------------------------------
# Who may take which step.
#
# Sets rather than single roles because a school is a building with people off
# sick in it. The separation that matters is not "only the principal may
# approve" — it is that no one person can walk a sheet from draft to a parent
# alone, and that is held by the same-signatory rule below rather than by making
# these sets as narrow as possible.
# ---------------------------------------------------------------------------

#: A class teacher submits. An administrator is here because entering and
#: submitting a paper sheet is office work in most schools — the same reasoning
#: `gradebook.MARK_ENTERING_ROLES` gives for admitting one.
SUBMITTING_ROLES = frozenset({Role.TEACHER.value, Role.ADMIN.value})

#: The academic check. Deliberately one role: this is the step the chain exists
#: for, and widening it to admins would let the office both submit and check.
CHECKING_ROLES = frozenset({Role.VICE_PRINCIPAL_ACADEMIC.value})

#: Approval is the principal's, and only the principal's.
APPROVING_ROLES = frozenset({Role.PRINCIPAL.value})

#: Publishing an approval already given. An administrator may do it because
#: release is commonly gated on something clerical — fees settled, cards
#: printed — and is not a second academic judgement.
RELEASING_ROLES = frozenset({Role.PRINCIPAL.value, Role.ADMIN.value})

#: Sending back is refusing at whatever stage you sit, so it is the union of the
#: people who could have said yes instead.
SENDING_BACK_ROLES = CHECKING_ROLES | APPROVING_ROLES


def _school_on_this_connection():
    """The school whose schema is being written.

    Read from the connection rather than passed in, for the reason
    `accounts.students.why_not_a_student_here()` reads it there: the sheet being
    written is already chosen by the `search_path`, so a school in an argument
    is a second opinion that can disagree with it.
    """
    from schools.models import School

    return School.objects.get(schema_name=connection.schema_name)


def _require_authority(actor, allowed, step):
    if not getattr(actor, "is_authenticated", False):
        raise NotAllowedToActOnResults(f"Signing in is required to {step} results.")

    school = _school_on_this_connection()
    if not set(actor.roles_at(school)) & allowed:
        raise NotAllowedToActOnResults(
            f"{actor} may not {step} results at {school}. That step is taken by "
            f"{', '.join(sorted(allowed))}."
        )
    return school


def _require_not_already_signed(sheet, actor, to_state):
    """The same-signatory rule, asked before the row is written.

    The unique index is what actually holds it — this is what turns the refusal
    into a sentence naming what they already did. Both are needed: the index
    catches the concurrent pair where each request reads "not signed yet", and
    this catches the ordinary case with an error somebody can act on.

    Only asked of an *advancing* step, and only counts advancing steps. A
    send-back and a release are not signatures; `models.ADVANCING_STATES` sets
    out why, and the same scoping is on the index so the two cannot drift.
    """
    if to_state not in ADVANCING_STATES:
        return

    existing = (
        ResultSheetTransition.objects.filter(
            sheet=sheet, cycle=sheet.cycle, actor_id=actor.pk
        )
        .filter(to_state__in=ADVANCING_STATES)
        .first()
    )
    if existing is None:
        return
    raise AlreadySignedThisCycle(
        f"{actor} already moved this sheet from {existing.from_state} to "
        f"{existing.to_state} on this pass. Two steps in one chain have to be "
        f"two people; ask somebody else to take this one.",
        existing=existing,
    )


def _locked(sheet):
    """Re-read the sheet under a row lock. Everything decides on this copy.

    The instance the caller is holding was read at some earlier moment and its
    `state` is a fact about that moment. Deciding on it is the stale-read bug
    this codebase has hit before — see `schools.Invitation.accept()`, which was
    validating guards against rows it had not locked.
    """
    return ResultSheet.objects.select_for_update().get(pk=sheet.pk)


def _move(sheet, actor, *, expected, to_state, reason="", roles, step):
    """One step. Locked, checked, recorded and applied in a single transaction."""
    _require_authority(actor, roles, step)

    with transaction.atomic():
        locked = _locked(sheet)

        if locked.state == SheetState.RELEASED:
            raise ReleaseIsFinal(
                f"{locked.class_group} — {locked.term} has been released to "
                f"parents. A released result is corrected by issuing a revision, "
                f"which keeps this one standing, not by moving it back."
            )
        if locked.state not in expected:
            raise WrongState(
                f"This sheet is {locked.get_state_display().lower()}, and {step} "
                f"applies to a sheet that is "
                f"{' or '.join(sorted(SheetState(s).label.lower() for s in expected))}.",
                state=locked.state,
            )

        _require_not_already_signed(locked, actor, to_state)

        recorded = ResultSheetTransition.objects.create(
            sheet=locked,
            from_state=locked.state,
            to_state=to_state,
            cycle=locked.cycle,
            actor_id=actor.pk,
            reason=reason,
        )

        locked.state = to_state
        fields = ["state", "updated_at"]
        if to_state == SheetState.DRAFT:
            # A send-back closes this pass. The row above belongs to the pass
            # that is ending — it is that pass's last act — so the bump happens
            # after it is written, not before.
            locked.cycle += 1
            fields.append("cycle")
        locked.save(update_fields=fields)

    return recorded


def open_sheet(class_group, term):
    """The sheet for this class and term, created in `draft` if it is new.

    `get_or_create` rather than a plain create: opening a class's results is
    something a screen does on being looked at, and the second person to look
    must not be an error. The unique constraint settles the race between two
    first-lookers.
    """
    sheet, _ = ResultSheet.objects.get_or_create(class_group=class_group, term=term)
    return sheet


def submit(sheet, actor):
    """Teacher: these results are ready to be checked."""
    return _move(
        sheet,
        actor,
        expected={SheetState.DRAFT},
        to_state=SheetState.SUBMITTED,
        roles=SUBMITTING_ROLES,
        step="submit",
    )


def check(sheet, actor):
    """Vice principal: I have looked at these and they are right."""
    return _move(
        sheet,
        actor,
        expected={SheetState.SUBMITTED},
        to_state=SheetState.CHECKED,
        roles=CHECKING_ROLES,
        step="check",
    )


def approve(sheet, actor):
    """Principal: these may go out."""
    return _move(
        sheet,
        actor,
        expected={SheetState.CHECKED},
        to_state=SheetState.APPROVED,
        roles=APPROVING_ROLES,
        step="approve",
    )


def release(sheet, actor):
    """Publish to parents. The last thing that happens to this version."""
    return _move(
        sheet,
        actor,
        expected={SheetState.APPROVED},
        to_state=SheetState.RELEASED,
        roles=RELEASING_ROLES,
        step="release",
    )


def send_back(sheet, actor, reason: str):
    """Refuse at whatever stage you sit, and say what is wrong.

    The transition the task list did not have. A chain that only goes forward
    does not mean mistakes are not made — it means the fix is somebody editing
    the database, which leaves no record that anything was ever wrong.

    `reason` is required and is not allowed to be blank. A refusal that does not
    say what is wrong sends a teacher back to forty-five scores with no idea
    which one to look at, and the check constraint refuses the row anyway.
    """
    if not (reason or "").strip():
        raise ResultsError(
            "A send-back has to say what is wrong. The teacher is looking at "
            "forty-five scores and needs to know which one."
        )
    return _move(
        sheet,
        actor,
        expected=SENDABLE_BACK_FROM,
        to_state=SheetState.DRAFT,
        reason=reason.strip(),
        roles=SENDING_BACK_ROLES,
        step="send back",
    )


def history(sheet):
    """Every step this sheet has taken, oldest first. The audit."""
    return ResultSheetTransition.objects.filter(sheet=sheet)


__all__ = [
    "APPROVING_ROLES",
    "CHECKING_ROLES",
    "RELEASING_ROLES",
    "SENDING_BACK_ROLES",
    "SUBMITTING_ROLES",
    "AlreadySignedThisCycle",
    "NotAllowedToActOnResults",
    "ReleaseIsFinal",
    "ResultsError",
    "WrongState",
    "approve",
    "check",
    "history",
    "open_sheet",
    "release",
    "send_back",
    "submit",
]
