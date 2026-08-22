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

`open_sheet()` is the sixth function and the only one that is not a signature.
It still takes an actor and still checks authority first, and the reason is the
paragraph below rather than anything about opening a sheet: a claim that a
module has no actor-less primitives is falsified by one, and it was exported.

The `_as()` split the other service modules use is deliberately *absent* here.
There are no primitives: every act in this module is somebody's signature, so
there is no version of it that makes sense without an actor. A data migration
that wants to move a sheet has to name the person it is moving it on behalf of,
which is the right amount of friction for rewriting an approval chain.

Authority is always asked at the school **on the connection**, and the portal is
refused rather than treated as a school — see `_school_on_this_connection()`,
which is where that used to raise `School.DoesNotExist` out of the module's own
exception hierarchy.
"""

from django.db import connection, transaction
from django_tenants.utils import get_public_schema_name

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

#: Publishing to parents. The principal's, and only the principal's.
#:
#: This was `{principal, admin}`, on the argument that release is commonly gated
#: on something clerical — fees settled, cards printed — rather than being a
#: second academic judgement. That argument is not wrong about schools, but it
#: contradicted a decision already taken for this phase, and the contradiction
#: was load-bearing rather than cosmetic: **task 8 makes revision principal-only
#: on the stated grounds that "release is the principal's act, so revision is
#: too."** Widening release here would leave task 8's authority rule resting on
#: a premise this module had quietly stopped honouring.
#:
#: Narrowed rather than left, because the two ways of resolving it are not
#: symmetrical. Narrowing costs a school an inconvenience a later PR can undo;
#: widening publishes forty-five children's results on an authority the person
#: who owns the decision did not grant. If an administrator should be able to
#: release, that is a change to make deliberately, in a PR that says so.
RELEASING_ROLES = frozenset({Role.PRINCIPAL.value})

#: Sending back is refusing at whatever stage you sit, so it is the union of the
#: people who could have said yes instead.
SENDING_BACK_ROLES = CHECKING_ROLES | APPROVING_ROLES

#: Opening a class's sheet, which decides nothing — everyone who can take any
#: step on the chain, and nobody else. Derived from the sets above rather than
#: written out, so a step whose roles change cannot leave this one behind.
OPENING_ROLES = (
    SUBMITTING_ROLES | CHECKING_ROLES | APPROVING_ROLES | RELEASING_ROLES
)


def _school_on_this_connection():
    """The school whose schema is being written.

    Read from the connection rather than passed in, for the reason
    `accounts.students.why_not_a_student_here()` reads it there: the sheet being
    written is already chosen by the `search_path`, so a school in an argument
    is a second opinion that can disagree with it.

    The public schema is refused explicitly, on the reasoning
    `schools.logging.current_school()` gives: **the public schema is the portal,
    not a customer.** Two things go wrong without this. A caller that never
    entered a tenant — a management command, a data migration, an `on_commit`
    callback — gets `School.DoesNotExist`, which is outside `ResultsError`, so
    every `except ResultsError` handler misses it and a refusal arrives as a
    500. And where a `School(name="Portal", schema_name="public")` row exists,
    which this codebase creates in its own tests, the lookup *succeeds* and
    authority is then checked against the portal's memberships rather than any
    school's — a silent wrong answer, which is the worse of the two.
    """
    from schools.models import School

    if connection.schema_name == get_public_schema_name():
        raise NotAllowedToActOnResults(
            "Results are a school's own records and this connection is on the "
            "portal, which is not a school. Enter the school's schema first."
        )
    return School.objects.get(schema_name=connection.schema_name)


def _named(roles) -> str:
    """Role labels, for a sentence a person reads.

    Not `', '.join(sorted(roles))`, which is what this did and which renders the
    refusal as "that step is taken by vp_academic". The stored value is an
    internal key — `accounts.Role` says so, and `vp_academic` was truncated to
    fit `Membership.role`'s sixteen characters precisely because nobody was
    meant to read it. Sorted on the label, since that is the order somebody
    scanning the sentence sees.
    """
    return ", ".join(sorted(Role(role).label for role in roles))


def _require_authority(actor, allowed, step):
    if not getattr(actor, "is_authenticated", False):
        raise NotAllowedToActOnResults(f"Signing in is required to {step} results.")

    school = _school_on_this_connection()
    if not set(actor.roles_at(school)) & allowed:
        raise NotAllowedToActOnResults(
            f"{actor} may not {step} results at {school}. That step is taken by "
            f"{_named(allowed)}."
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

    `.order_by()` is **belt and braces, and the braces are Django's.** The
    concern is real and this codebase has been bitten by it three times — in
    `accounts.services`, `accounts.models` and `schools.invitations` — because
    `ResultSheet` sorts by `term` then `class_group`, both relations, and a
    joined `SELECT ... FOR UPDATE` locks a row in *every* joined table. A
    transition would then hold an exclusive lock on the term and the class,
    serialising two principals approving two different classes in one term on a
    row neither of them writes.

    It is not what happens here, and the reason is worth writing down rather
    than rediscovering: **`QuerySet.get()` already clears ordering itself.**
    Django 5.2's `get()` runs `clone = clone.order_by()` before compiling, so
    the lock this function takes is

        SELECT ... FROM results_resultsheet WHERE id = %s LIMIT 21 FOR UPDATE

    with no join, with or without the call below. That was checked by reading
    the SQL the tests actually captured, not by reasoning from `.filter()` —
    which does *not* clear ordering and which is where the intuition comes from.

    The call stays because it costs nothing and the property is one line away
    from being lost: `.filter(pk=...).first()` keeps the ordering, and so does
    anything that stops going through `get()`. `tests/LockScopeTests` pins the
    property rather than the spelling — both of its tests fail on that rewrite,
    one on the SQL and one on real contention for `academics_term`.
    """
    return ResultSheet.objects.select_for_update().order_by().get(pk=sheet.pk)


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


def open_sheet(class_group, term, actor):
    """The sheet for this class and term, created in `draft` if it is new.

    `get_or_create` rather than a plain create: opening a class's results is
    something a screen does on being looked at, and the second person to look
    must not be an error. The unique constraint settles the race between two
    first-lookers.

    `actor` is required, and was missing. This module's docstring says every
    function checks who is asking before anything is read or written, and argues
    that the `_as()` split the other service modules use is absent here because
    there are no actor-less primitives. `open_sheet()` was exactly the primitive
    that argument said did not exist: exported, writing a tenant table, and
    never once asking `_school_on_this_connection()`. Anything reachable from a
    future screen — a parent, a student, a suspended teacher — could mint
    `ResultSheet` rows for arbitrary (class, term) pairs, each of which
    `ResultSheetTransition.sheet`'s `PROTECT` then makes awkward to remove.

    Admitted roles are the union of everyone who can take a step on the chain.
    Opening a sheet decides nothing and signs nothing — it is the act of
    *looking at* a class's results — so it would be wrong to make it narrower
    than the narrowest step, and wrong to admit anybody who cannot act on the
    thing they have just created.
    """
    _require_authority(actor, OPENING_ROLES, "open a sheet for")
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
    "OPENING_ROLES",
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
