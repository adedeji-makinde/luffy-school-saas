"""Entering and clearing marks. The only supported way to write a `Score`.

Two functions, because there are only two things a teacher does to a mark:
they set it, or they take it back. There is deliberately no `create_sheet()`
and no "initialise this class's scores" — a sheet is thirty students and
however many of them have been marked, and materialising rows for the rest is
the thing `models.py` exists to prevent.

Every write goes through `set_score()`, and every one of them answers the same
three questions before touching the table:

1. **Is this child ours?** `student_membership_id` is a bare id with no foreign
   key (docs/tenancy.md), so the column will take any integer, including a
   child at another school. Asked here because it cannot be asked in SQL.
2. **Can the assessment hold this mark?** A value above `max_score` is a
   percentage over 100 somewhere downstream. A cross-row rule, so no check
   constraint can express it.
3. **Is the mark still what the teacher was shown?** Two teachers with the same
   sheet open is ordinary. Every write is conditional on the `version` the
   caller was handed, and a stale one is refused rather than applied on top of
   whoever moved first.

A fourth question — **may this person mark at all?** — is asked only by the
`_as()` variants at the foot of this module, on the idiom `accounts.services`
set: the plain functions are primitives an import or a data migration can use,
and anything with a request behind it goes through `set_score_as()`. Authority
is the one rule that cannot live in the primitive, because a management command
has no actor to check.

Still no screens and no HTTP here, for the same reason `fees.services` has
none: the other three rules have to hold for an import too, and a rule that
lives in a view only holds for the view. `gradebook/api.py` is a caller of this
module, not a second place the rules are written.
"""

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import Role
from accounts.students import why_not_a_student_here

from .models import Score


class GradebookError(Exception):
    """A mark could not be written as asked.

    One base class for the whole module, as `fees.services.FeeLedgerError` and
    `schools.models.InvitationError` are for theirs: `except GradebookError`
    catches every refusal made here, not the half the caller thought to import.
    """


class NotThisSchoolsStudent(GradebookError):
    """The membership named is not a student of the school being marked.

    The check that earns the bare id, and the same one `fees.services` makes.
    Deliberately duplicated rather than shared: a gradebook that imports the
    bursar's module in order to score a child has the dependency backwards, and
    the two apps answer to different people. If a third tenant app needs it, it
    moves to `accounts` — where the `Membership` it asks about already lives —
    rather than one tenant app importing another.
    """


class InvalidScore(GradebookError):
    """Not a mark this assessment can hold.

    Three cases, one refusal from the caller's side: a fractional mark, a
    negative one, or one above what the assessment was out of. The last is the
    load-bearing one — `max_score` is the denominator of every percentage this
    data produces, so a value above it is a mark over 100% that no report card
    can explain, and it is a comparison between two rows, which is why it is
    here and in `Score.clean()` rather than in a check constraint.
    """


class ScoreChangedMeanwhile(GradebookError):
    """Somebody else wrote this mark after the caller was shown it.

    The refusal that makes a shared sheet safe. Carries `current` — the `Score`
    as it now stands, or `None` if the mark has since been cleared — so the
    caller can say *what* changed rather than only that something did.
    """

    def __init__(self, message, current=None):
        super().__init__(message)
        #: The row as it stands now, or None if there is no longer one.
        self.current = current


class _AnyVersion:
    """Sentinel type. See `ANY_VERSION`."""

    def __repr__(self):  # pragma: no cover - debugging aid
        return "ANY_VERSION"


#: Write regardless of what is already there. For callers with no screen and
#: nobody to conflict with — a bulk import, a data migration, a management
#: command — where the version check protects nothing and would only mean
#: reading every row twice.
#:
#: A sentinel rather than `expected_version=None` meaning "don't care", because
#: `None` already means something: *"I was shown no mark at all."* Overloading
#: it would make the default an unchecked overwrite, and the default has to be
#: the safe one — a caller who forgets to pass the version they were given is
#: refused, not silently allowed to clobber.
ANY_VERSION = _AnyVersion()


def _require_student_of_this_school(membership):
    """Refuse a membership that is not a student here, before anything is written.

    The rule itself now lives in `accounts.students`, which is where the
    docstring on `NotThisSchoolsStudent` above said it would go once a third
    tenant app needed it. `academics.ClassPlacement` was the third. What stays
    here is the *raising*: a caller catching `GradebookError` must still catch
    this, which it would not if the shared module raised a type of its own.
    """
    reason = why_not_a_student_here(membership, subject="a mark", holder="gradebook")
    if reason:
        raise NotThisSchoolsStudent(reason)
    return membership


def _require_a_mark_this_assessment_can_hold(assessment, value):
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidScore(
            f"Marks are whole numbers, not {type(value).__name__}. An assessment "
            f"scored in halves is one out of twice as many marks — set "
            f"`max_score` accordingly rather than storing a fraction."
        )
    if value < 0:
        raise InvalidScore(f"A mark cannot be negative. Got {value}.")
    if value > assessment.max_score:
        raise InvalidScore(
            f"{assessment.name} is out of {assessment.max_score}; {value} is more "
            f"than that."
        )
    return value


def _stamp(by):
    """A user, a pk, or None — all three spellings a caller might reach for."""
    return getattr(by, "pk", by)


def _current(assessment, membership_id):
    return Score.objects.filter(
        assessment=assessment, student_membership_id=membership_id
    ).first()


#: SQLSTATE 23505. Named rather than spelled inline at two call sites.
_UNIQUE_VIOLATION = "23505"

#: The constraint whose firing means "somebody else got there first". Matched by
#: name, because it is the only one of the eight on this table that means that.
_COLLISION = "one_score_per_student_per_assessment"


def _is_the_first_mark_colliding(exc) -> bool:
    """Did `one_score_per_student_per_assessment` fire, or something else?

    `IntegrityError` is the table refusing a row, and it says nothing about
    *which* rule refused it. Exactly one of them — the uniqueness of a mark per
    student per assessment — means a second writer got there first, which is the
    conflict `ScoreChangedMeanwhile` describes and the one a caller fixes by
    reloading. The other seven are the five `CHECK (... >= 0)` constraints, the
    primary key, and the foreign key onto `Assessment`; reporting any of those
    as "somebody changed this mark" sends the caller round a reload loop that
    cannot terminate and buries a real failure behind a routine-looking refusal.

    Asked of the constraint that actually fired, via psycopg2's `diag`, rather
    than inferred from whether a row is there now. The inference is a proxy and
    wrong in both directions: a genuine collision whose row is cleared in the
    moment between the failed insert and the check looks like a real failure,
    and a `CHECK` violation on a student who *does* already have a mark looks
    like a collision. The constraint name is the fact itself.

    Postgres-only, like the rest of this codebase — `django_tenants` puts each
    school in its own schema and no other backend has those. A cause that
    carries no `pgcode` is treated as "not a collision", so an unrecognised
    error is re-raised intact rather than relabelled as a conflict.
    """
    cause = exc.__cause__
    diagnostics = getattr(cause, "diag", None)
    return (
        getattr(cause, "pgcode", None) == _UNIQUE_VIOLATION
        and getattr(diagnostics, "constraint_name", None) == _COLLISION
    )


def set_score(assessment, membership, value, *, expected_version=None, by=None):
    """Enter or change one student's mark on one assessment. Returns the `Score`.

    `expected_version` is the contract with whoever is holding a sheet: it is
    the `version` they were shown, and `None` means they were shown no mark at
    all. The write happens only if that is still true, so the second of two
    teachers editing the same cell is refused with `ScoreChangedMeanwhile`
    rather than quietly overwriting the first. Pass `ANY_VERSION` to write
    regardless — see the sentinel's own note for when that is the honest answer.

    Not `update_or_create()`, which reads and then writes and loses the race in
    between; the version lives in the `WHERE` clause of a single statement, and
    the unique constraint catches the insert half. Both losses are reported the
    same way, because from the caller's side they are the same event.
    """
    _require_student_of_this_school(membership)
    _require_a_mark_this_assessment_can_hold(assessment, value)

    if expected_version is ANY_VERSION:
        return _write_regardless(assessment, membership.pk, value, by)
    if expected_version is None:
        return _insert_first_mark(assessment, membership.pk, value, by)
    return _update_the_mark_shown(
        assessment, membership.pk, value, expected_version, by
    )


def _insert_first_mark(assessment, membership_id, value, by):
    """The caller was shown no mark, so this must be an insert."""
    try:
        # Its own atomic block: an IntegrityError marks the *enclosing*
        # transaction unusable, so a caller who wraps a whole sheet in
        # `transaction.atomic()` and catches ScoreChangedMeanwhile could
        # otherwise not go on to write the next student.
        with transaction.atomic():
            return Score.objects.create(
                assessment=assessment,
                student_membership_id=membership_id,
                value=value,
                recorded_by_id=_stamp(by),
                updated_by_id=_stamp(by),
            )
    except IntegrityError as exc:
        # Which IntegrityError, though? Only the uniqueness collision means
        # somebody entered the first mark between the caller being shown the
        # sheet and this write. Anything else is a real failure and leaves here
        # untouched — see `_is_the_first_mark_colliding()` for why that is asked
        # of the constraint rather than of whether a row is there now.
        if not _is_the_first_mark_colliding(exc):
            raise
        # `current` can still be None: the row this collided with may have been
        # cleared in the moment since. That is a different sentence, not a
        # different outcome — the caller's write did not happen either way.
        current = _current(assessment, membership_id)
        stands_at = (
            "has been marked and cleared since"
            if current is None
            else f"now stands at {current.value}/{assessment.max_score}"
        )
        raise ScoreChangedMeanwhile(
            f"This was unmarked when you opened it and {stands_at}. Reload "
            f"before entering it again.",
            current=current,
        ) from None


def _update_the_mark_shown(assessment, membership_id, value, expected_version, by):
    """Conditional on the version the caller was handed, in one statement."""
    rows = Score.objects.filter(
        assessment=assessment,
        student_membership_id=membership_id,
        version=expected_version,
    ).update(
        value=value,
        version=F("version") + 1,
        updated_by_id=_stamp(by),
        # Set by hand: `auto_now` is applied by `Model.save()`, and a queryset
        # `update()` never calls it. Without this line the column would keep
        # the time of the last write that went through the ORM's save path,
        # which after this function exists is none of them.
        updated_at=timezone.now(),
    )
    if rows == 0:
        current = _current(assessment, membership_id)
        if current is None:
            raise ScoreChangedMeanwhile(
                "That mark has been cleared since you were shown it. Enter it "
                "afresh if it should be there.",
                current=None,
            )
        raise ScoreChangedMeanwhile(
            f"You were shown version {expected_version}; it now stands at "
            f"{current.value}/{assessment.max_score} (version {current.version}). "
            f"Reload before saving.",
            current=current,
        )
    return Score.objects.get(
        assessment=assessment, student_membership_id=membership_id
    )


def _write_regardless(assessment, membership_id, value, by):
    """`ANY_VERSION`: update if there is a row, insert if there is not.

    Two attempts, because either half can lose a race with a concurrent writer
    — the update finding nothing because the row was just cleared, or the
    insert colliding because it was just created. A second pass resolves it;
    a third would mean something other than a race, so it is reported.
    """
    for attempt in (1, 2):
        rows = Score.objects.filter(
            assessment=assessment, student_membership_id=membership_id
        ).update(
            value=value,
            version=F("version") + 1,
            updated_by_id=_stamp(by),
            updated_at=timezone.now(),
        )
        if rows:
            return Score.objects.get(
                assessment=assessment, student_membership_id=membership_id
            )
        try:
            with transaction.atomic():
                return Score.objects.create(
                    assessment=assessment,
                    student_membership_id=membership_id,
                    value=value,
                    recorded_by_id=_stamp(by),
                    updated_by_id=_stamp(by),
                )
        except IntegrityError as exc:
            # Same question as `_insert_first_mark()`, and it matters more here
            # because this path retries: a constraint that is not the collision
            # will fail again on the second pass and every pass after it, so
            # retrying it only delays the error and then mislabels it.
            if not _is_the_first_mark_colliding(exc):
                raise
            if attempt == 2:
                raise ScoreChangedMeanwhile(
                    "This mark is being written by somebody else faster than it "
                    "can be replaced. Try again.",
                    current=_current(assessment, membership_id),
                ) from None
    raise AssertionError("unreachable")  # pragma: no cover


def clear_score(assessment, membership, *, expected_version, by=None):
    """Take back a mark. Returns nothing; the row is gone.

    A delete, not a zero and not a null. That is the module's whole premise:
    "not marked yet" is the absence of a row, so un-marking is removing one. A
    teacher who clears a mark and a teacher who enters 0 have said two different
    things, and the table has to keep them apart.

    `expected_version` is required — there is no default, because "clear
    whatever is there" is exactly the destructive write the version exists to
    prevent, and it is not worth a convenient spelling. `ANY_VERSION` is
    accepted for the callers that genuinely have no sheet.

    Clearing a mark that is already gone is a no-op rather than an error: the
    end state the caller asked for is the end state that holds, and a retried
    request should not fail because it succeeded the first time.
    """
    _require_student_of_this_school(membership)

    rows = Score.objects.filter(
        assessment=assessment, student_membership_id=membership.pk
    )
    if expected_version is not ANY_VERSION:
        rows = rows.filter(version=expected_version)

    deleted, _ = rows.delete()
    if deleted:
        return

    current = _current(assessment, membership.pk)
    if current is None:
        return  # Already clear. Nothing to do and nothing to complain about.
    raise ScoreChangedMeanwhile(
        f"You were shown version {expected_version}; this mark now stands at "
        f"{current.value}/{assessment.max_score} (version {current.version}) and "
        f"has not been cleared. Reload before clearing it.",
        current=current,
    )


# ---------------------------------------------------------------------------
# Actor-checked entry points.
#
# The functions above are primitives, in the same sense `accounts.services`
# means it: they keep a mark honest but ask nothing about who is entering it,
# which is what lets an import and a management command use them. Anything with
# a request behind it comes through here instead.
# ---------------------------------------------------------------------------

#: Roles that may write a mark at their own school.
#:
#: A teacher does the marking. A principal and an administrator are here
#: because entering a term's marks from a paper sheet is office work in most
#: schools, and a system that refused it would be worked around with a borrowed
#: teacher login — which is strictly worse, because then `recorded_by_id` names
#: the wrong person on every row it touches.
#:
#: The load-bearing half is who is absent. A bursar keeps the books and does not
#: mark; a parent and a student are the *subjects* of this data, and a STUDENT
#: membership is the very thing a `Score` is keyed on. Narrow this set if a
#: school wants marking kept to teachers — nothing else reads it.
MARK_ENTERING_ROLES = frozenset(
    {Role.TEACHER.value, Role.PRINCIPAL.value, Role.ADMIN.value}
)


class NotAllowedToMark(GradebookError):
    """The actor holds no role at this school that may enter marks.

    Under `GradebookError` like every other refusal here, so `except
    GradebookError` still means "the mark was not written". Callers that need to
    tell a refusal of *authority* from a refusal of *state* — an HTTP layer
    choosing between 403 and 409 — catch this one first.
    """


def can_enter_marks(actor, school) -> bool:
    """May `actor` write marks at `school`?

    Access-scoped, like every other authority question in this codebase: an
    invited or suspended teacher has a membership and no authority, because
    `roles_at()` is scoped to ACCESS_STATUSES.

    Platform staff are **not** admitted, and that is the one place this departs
    from `accounts.services.can_grant_memberships()`. Support staff repairing a
    membership is an operational act on the platform's own plumbing. Writing a
    child's academic record is not: it is the school's own act, it is what a
    report card is built from, and `recorded_by_id` would name a platform
    operator on the row. There is no case needing the override either — a mark
    is always entered by somebody at the school that taught it.
    """
    if not getattr(actor, "is_authenticated", False):
        return False
    return bool(set(actor.roles_at(school)) & MARK_ENTERING_ROLES)


def _require_marking_authority(actor, school):
    if not can_enter_marks(actor, school):
        raise NotAllowedToMark(
            f"{actor} may not enter marks at {school}. Marking is done by a "
            f"teacher, a principal or an administrator of the school that set "
            f"the assessment."
        )


def set_score_as(
    actor, assessment, membership, value, *, expected_version=None, by=None
):
    """`set_score()` for a caller with a request behind it.

    Authority is asked at the *student's* school, which
    `_require_student_of_this_school()` then pins to the schema being written.
    Both questions have to be asked and they are not the same one: the first is
    whether this person may mark here at all, the second whether this child is
    taught here.

    `by` defaults to the actor, because on this path they are the same person
    and repeating them at every call site is how they eventually disagree.
    """
    _require_marking_authority(actor, membership.school)
    return set_score(
        assessment,
        membership,
        value,
        expected_version=expected_version,
        by=actor if by is None else by,
    )


def clear_score_as(actor, assessment, membership, *, expected_version, by=None):
    """`clear_score()` for a caller with a request behind it."""
    _require_marking_authority(actor, membership.school)
    return clear_score(
        assessment,
        membership,
        expected_version=expected_version,
        by=actor if by is None else by,
    )


__all__ = [
    "ANY_VERSION",
    "MARK_ENTERING_ROLES",
    "GradebookError",
    "InvalidScore",
    "NotAllowedToMark",
    "NotThisSchoolsStudent",
    "ScoreChangedMeanwhile",
    "can_enter_marks",
    "clear_score",
    "clear_score_as",
    "set_score",
    "set_score_as",
]
