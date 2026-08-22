"""Putting a child in a class, and moving them. The only supported way to write
a `ClassPlacement`.

Three things a school does to a placement: it makes one, it changes one, and it
carries a term's worth forward into the next term. Each is a separate function
because each is a different act with a different failure — and in particular
**placing and moving are not the same call**. `place_student()` refuses a child
who is already placed that term rather than quietly reassigning them: a silent
reassignment is a position in class changing under a report card that has
already been printed, and it is precisely the kind of write that should have to
be spelled.

Every write answers the same question first, on the idiom `fees.services` and
`gradebook.services` established: **is this child ours?** `student_membership_id`
is a bare id with no foreign key (docs/tenancy.md), so the column will take any
integer, including a child at another school. The rule itself now lives in
`accounts.students` — this module is the third caller, which is the trigger
`gradebook.services.NotThisSchoolsStudent` named for moving it there.

Authority is asked only by the `_as()` variants at the foot of the module, the
same split `gradebook.services` uses: the plain functions are primitives an
import or a data migration can call, and anything with a request behind it goes
through `place_student_as()`. A management command has no actor to check, so
authority cannot live in the primitive.

No screens and no HTTP here, for the reason the other two service modules have
none: the rules have to hold for an import too, and a rule that lives in a view
only holds for the view.
"""

from django.db import IntegrityError, connection, transaction

from accounts.models import LIVE_STATUSES, Membership, Role
from accounts.students import why_not_a_student_here

from .models import ClassPlacement


class AcademicsError(Exception):
    """A placement could not be written as asked.

    One base class for the module, as `fees.services.FeeLedgerError` and
    `gradebook.services.GradebookError` are for theirs: `except AcademicsError`
    catches every refusal made here, not the half the caller thought to import.
    """


class NotThisSchoolsStudent(AcademicsError):
    """The membership named is not a student of the school doing the placing.

    Raised here rather than in `accounts.students`, which returns a sentence and
    raises nothing. That split is deliberate and its reasoning is in that
    module: each app keeps its own exception hierarchy, so a caller writing
    `except AcademicsError` still means "the placement was not written".
    """


class AlreadyPlaced(AcademicsError):
    """This child already sits in a group this term.

    Carries `current` — the placement as it stands — so a caller can say *which*
    group they are in rather than only that they are in one. Refused rather than
    overwritten: see this module's docstring on why placing and moving are two
    calls.
    """

    def __init__(self, message, current=None):
        super().__init__(message)
        #: The placement that already exists, or None if it was removed in the
        #: moment between the collision and the re-read.
        self.current = current


class NotPlaced(AcademicsError):
    """There is no placement to move, so there is nothing to change."""


class NotAllowedToPlace(AcademicsError):
    """The actor holds no role at this school that may place children.

    Under `AcademicsError` like every other refusal here, so `except
    AcademicsError` still means "the placement was not written". A caller that
    needs to tell a refusal of *authority* from one of *state* — an HTTP layer
    choosing between 403 and 409 — catches this one first.
    """


#: SQLSTATE 23505, named rather than spelled inline.
_UNIQUE_VIOLATION = "23505"

#: The constraint whose firing means "somebody placed this child first".
_COLLISION = "one_class_placement_per_student_per_term"


def _is_the_placement_colliding(exc) -> bool:
    """Did `one_class_placement_per_student_per_term` fire, or something else?

    `IntegrityError` is the table refusing a row and says nothing about *which*
    rule refused it. Exactly one of them means a second writer got there first,
    which is what `AlreadyPlaced` describes and what a caller fixes by reloading.
    Reporting any other failure that way would send a caller looking for a
    placement that does not exist, and bury the real fault behind a
    routine-looking refusal.

    Which other failures can actually arrive here was measured rather than
    assumed, and the answer is narrower than it looks. Django declares
    PostgreSQL foreign keys `DEFERRABLE INITIALLY DEFERRED`, so a placement
    naming a `ClassGroup` or `Term` that does not exist **does not raise here at
    all** — the insert succeeds and the violation surfaces at commit, far from
    this `except`. `test_a_missing_class_group_is_not_reported_as_a_collision`
    pins that, because it is the sort of thing that changes under you: were a
    future migration to make those keys immediate, the guard below is what keeps
    them from being relabelled as a conflict.

    What does arrive here immediately is the unique constraint and the check
    constraints on the two positive-integer columns.

    Asked of the constraint that actually fired, via psycopg2's `diag`, rather
    than inferred from whether a row is there now — the inference is wrong in
    both directions under concurrency. Same reasoning, same shape, as
    `gradebook.services._is_the_first_mark_colliding()`.

    A cause carrying no `pgcode` is treated as "not a collision", so an
    unrecognised error is re-raised intact rather than relabelled.
    """
    cause = exc.__cause__
    diagnostics = getattr(cause, "diag", None)
    return (
        getattr(cause, "pgcode", None) == _UNIQUE_VIOLATION
        and getattr(diagnostics, "constraint_name", None) == _COLLISION
    )


def _require_student_of_this_school(membership):
    reason = why_not_a_student_here(
        membership, subject="a class placement", holder="register"
    )
    if reason:
        raise NotThisSchoolsStudent(reason)
    return membership


def _stamp(by):
    """A user, a pk, or None — all three spellings a caller might reach for."""
    return getattr(by, "pk", by)


def placement_of(membership_id, term):
    """Where this child sits this term, or None."""
    return ClassPlacement.objects.filter(
        term=term, student_membership_id=membership_id
    ).first()


def place_student(class_group, term, membership, *, by=None):
    """Put one child in one group for one term. Returns the `ClassPlacement`.

    Refuses a child who is already placed that term, **including one already in
    this very group**. That second case looks like it should be a no-op, and is
    not: two administrators both believing they made the placement is a real
    disagreement about who did what, and `placed_by_id` would name whichever of
    them lost. A caller that genuinely wants "make sure they are in JSS 1A"
    asks `placement_of()` first, which is one line and says what it means.

    Deliberately does **not** refuse a child whose membership has ended. Placing
    one is usually a mistake, but not always: entering a past term's roster after
    the fact is real work, and the children in it have often left by then.
    Refusing here would block that with no way round it, and the failure it would
    prevent is a person placing the wrong child in the current term — which no
    status check catches anyway. `carry_forward_placements()`, which is the
    automated path and the one that would repeat such a mistake silently across a
    whole school, does filter them out.
    """
    _require_student_of_this_school(membership)

    try:
        # Its own atomic block: an IntegrityError marks the *enclosing*
        # transaction unusable, so a caller placing a whole class inside one
        # `transaction.atomic()` and catching AlreadyPlaced could otherwise not
        # go on to place the next child.
        with transaction.atomic():
            return ClassPlacement.objects.create(
                class_group=class_group,
                term=term,
                student_membership_id=membership.pk,
                placed_by_id=_stamp(by),
            )
    except IntegrityError as exc:
        if not _is_the_placement_colliding(exc):
            raise
        # `current` can still be None: the row this collided with may have been
        # removed in the moment since. A different sentence, not a different
        # outcome — the caller's write did not happen either way.
        current = placement_of(membership.pk, term)
        sits_in = (
            "has since been taken out of it"
            if current is None
            else f"is in {current.class_group}"
        )
        raise AlreadyPlaced(
            f"{membership.name or membership.user} {sits_in} for {term}. Move "
            f"them rather than placing them again.",
            current=current,
        ) from None


def move_student(class_group, term, membership, *, by=None):
    """Move a child who is already placed into a different group. Returns the row.

    Separate from `place_student()` because it is a different act: this one
    changes a position in class and a class average that may already have been
    read, and a school should have had to mean it.

    Moving a child into the group they are already in is a no-op rather than an
    error — the end state the caller asked for is the end state that holds, and
    a retried request should not fail because it succeeded the first time. That
    is the opposite of `place_student()`'s answer to the same input, and
    deliberately so: there, a second placement is a claim about who placed them;
    here, it is a claim about where they sit, and they already sit there.
    """
    _require_student_of_this_school(membership)

    with transaction.atomic():
        # Locked, because this is read-modify-write on one row and two
        # administrators moving the same child at the same instant would
        # otherwise both read the old group and both write their own — losing
        # one move with nothing to show a move was lost.
        current = (
            ClassPlacement.objects.select_for_update()
            .filter(term=term, student_membership_id=membership.pk)
            .first()
        )
        if current is None:
            raise NotPlaced(
                f"{membership.name or membership.user} is not in any group for "
                f"{term}, so there is nothing to move. Place them instead."
            )
        if current.class_group_id == class_group.pk:
            return current

        current.class_group = class_group
        current.placed_by_id = _stamp(by)
        # `updated_at` is listed so that `auto_now` fires for it. Unlike a
        # queryset `update()`, `save(update_fields=...)` does run each named
        # field's `pre_save`, so it does not need setting by hand here — see
        # `gradebook.services._update_the_mark_shown()`, which does need to.
        current.save(update_fields=["class_group", "placed_by_id", "updated_at"])
        return current


def remove_placement(term, membership) -> bool:
    """Take a child out of their group for a term. True if a row went.

    Returns False rather than raising when there was nothing there, on the same
    reasoning as `gradebook.services.clear_score()`: the end state asked for is
    the end state that holds.
    """
    _require_student_of_this_school(membership)
    deleted, _ = ClassPlacement.objects.filter(
        term=term, student_membership_id=membership.pk
    ).delete()
    return bool(deleted)


def carry_forward_placements(from_term, to_term, *, by=None) -> int:
    """Copy a term's placements into the next term. Returns how many were made.

    Without this, the first day of every term is a school with no rosters: every
    position in class and every class average has no denominator until somebody
    re-enters forty-five children per group by hand. That is not a convenience
    — it is the difference between per-term placement being viable and being
    quietly abandoned.

    **Children already placed in `to_term` are left exactly as they are**, not
    overwritten. Running this twice is therefore safe, and running it after a
    few children have already been placed by hand does not undo that work. The
    count returned is placements actually made, so a second run returns 0 and
    says so honestly rather than reporting the same work twice.

    Promotion is deliberately *not* what this does. It carries each child into
    the same group they were in, because moving JSS 1A into JSS 2A is a decision
    about who passed, and that decision is not this function's to make.

    Children whose membership has ended are left behind — see the comment on
    `still_enrolled` below for why that is a correctness rule and not a tidiness
    one. Their existing placement in `from_term` is untouched: they *were* in
    that class that term, and the report card for it says so.
    """
    already = set(
        ClassPlacement.objects.filter(term=to_term).values_list(
            "student_membership_id", flat=True
        )
    )
    source = list(ClassPlacement.objects.filter(term=from_term))

    # **Children who have left are not carried forward.** A placement is not
    # evidence that somebody is still enrolled — it is a record of where they
    # sat last term, and it stays true for the report card of the term it
    # belongs to. Copying it forward is a different claim, and for a child who
    # graduated or transferred in December it is a false one: they would appear
    # on January's roster, be counted in the class size, and drag the class
    # average towards a mark they were never going to be given.
    #
    # `LIVE_STATUSES`, not `ACCESS_STATUSES` — the question is "is this child
    # still ours?", not "can they sign in?". A suspended student is still
    # enrolled and still sits in the class.
    #
    # The school is checked as well as the status, cheaply, because this is the
    # one path that writes placements *without* going through
    # `_require_student_of_this_school()` for each child: the ids come from rows
    # already in this schema, and a guard that trusts its own input is the guard
    # that stops guarding the day something else writes that input.
    still_enrolled = set(
        Membership.objects.filter(
            pk__in=[placement.student_membership_id for placement in source],
            role=Role.STUDENT,
            status__in=LIVE_STATUSES,
            school__schema_name=connection.schema_name,
        ).values_list("pk", flat=True)
    )

    carried = [
        ClassPlacement(
            class_group_id=placement.class_group_id,
            term=to_term,
            student_membership_id=placement.student_membership_id,
            placed_by_id=_stamp(by),
        )
        for placement in source
        if placement.student_membership_id not in already
        and placement.student_membership_id in still_enrolled
    ]
    if not carried:
        return 0

    with transaction.atomic():
        # `ignore_conflicts` so that a placement created by somebody else
        # between the read above and this write is left alone rather than
        # taking the whole batch down. The alternative — one insert per child in
        # its own atomic block — is forty-five round trips to avoid a race this
        # handles in one, and the outcome is the same: the existing row stands.
        #
        # Counted before and after rather than from `bulk_create`'s return
        # value, which with `ignore_conflicts` hands back the list it was given
        # and cannot say which of them landed. The count is therefore of rows
        # that appeared, which is the honest number; a concurrent writer adding
        # a placement to `to_term` inside this window would be counted here too,
        # and that is the one inaccuracy, in a number used for reporting rather
        # than for any decision.
        before = ClassPlacement.objects.filter(term=to_term).count()
        ClassPlacement.objects.bulk_create(carried, ignore_conflicts=True)
        after = ClassPlacement.objects.filter(term=to_term).count()
    return after - before


# ---------------------------------------------------------------------------
# Actor-checked entry points.
#
# The functions above are primitives in the sense `accounts.services` means it:
# they keep a placement honest but ask nothing about who is making it, which is
# what lets an import and a management command use them. Anything with a request
# behind it comes through here instead.
# ---------------------------------------------------------------------------

#: Roles that may place a child in a group at their own school.
#:
#: Narrower than `gradebook.services.MARK_ENTERING_ROLES`, and the difference is
#: the point: a teacher enters marks for the children in front of them, but
#: which children those are is not a teacher's decision. Moving a child between
#: arms changes whose class average they count towards and whose position they
#: displace, which is an office act.
#:
#: A bursar keeps the books; a parent and a student are the subjects of this
#: data. None of the three appear.
PLACEMENT_ROLES = frozenset({Role.PRINCIPAL.value, Role.ADMIN.value})


def can_place_students(actor, school) -> bool:
    """May `actor` place children into groups at `school`?

    Access-scoped like every other authority question here: an invited or
    suspended administrator has a membership and no authority, because
    `roles_at()` is scoped to ACCESS_STATUSES.

    Platform staff are **not** admitted, on the reasoning
    `gradebook.services.can_enter_marks()` set out: deciding which class a child
    sits in is the school's own act, it is what a position in class is computed
    from, and `placed_by_id` would name a platform operator on the row.
    """
    if not getattr(actor, "is_authenticated", False):
        return False
    return bool(set(actor.roles_at(school)) & PLACEMENT_ROLES)


def _require_placement_authority(actor, school):
    if not can_place_students(actor, school):
        raise NotAllowedToPlace(
            f"{actor} may not place children at {school}. Which group a child "
            f"sits in is set by a principal or an administrator of the school."
        )


def place_student_as(actor, class_group, term, membership, *, by=None):
    """`place_student()` for a caller with a request behind it.

    Authority is asked at the *student's* school, which
    `_require_student_of_this_school()` then pins to the schema being written.
    Both questions have to be asked and they are not the same one: the first is
    whether this person may place anyone here, the second whether this child is
    taught here.
    """
    _require_placement_authority(actor, membership.school)
    return place_student(
        class_group, term, membership, by=actor if by is None else by
    )


def move_student_as(actor, class_group, term, membership, *, by=None):
    """`move_student()` for a caller with a request behind it."""
    _require_placement_authority(actor, membership.school)
    return move_student(
        class_group, term, membership, by=actor if by is None else by
    )


def remove_placement_as(actor, term, membership) -> bool:
    """`remove_placement()` for a caller with a request behind it."""
    _require_placement_authority(actor, membership.school)
    return remove_placement(term, membership)


__all__ = [
    "PLACEMENT_ROLES",
    "AcademicsError",
    "AlreadyPlaced",
    "NotAllowedToPlace",
    "NotPlaced",
    "NotThisSchoolsStudent",
    "can_place_students",
    "carry_forward_placements",
    "move_student",
    "move_student_as",
    "place_student",
    "place_student_as",
    "placement_of",
    "remove_placement",
    "remove_placement_as",
]
