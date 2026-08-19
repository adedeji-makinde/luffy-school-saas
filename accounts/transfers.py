"""The transfer handshake: two schools agreeing to move one child.

`release_student_as()` and `enroll_student_as()` already let two schools move a
child without either writing at the other's school. What they do not do is
connect: nothing ties a release to the admission it was meant for, nothing
records that anybody agreed, and between the two acts the child belongs to no
school at all. This module is what connects them.

The idea worth holding on to is in one sentence: **the handshake assembles
two-sided authority out of two one-sided acts.** `services.transfer_student()`
genuinely needs authority at both ends — it ends a membership at one school and
opens one at another — and requiring a single caller to hold both is exactly
what made ordinary transfers impossible, since a school admin holds one school.
So one school signs by requesting and the other signs by accepting, and only
once both signatures exist does the transfer run, in a single transaction.
Neither side ever acted at the other's school. The pair of consents did.

Either end may ask. "We are letting this child go to Grace Academy" and "we
would like to admit this child from St Mary's" are the same proposal seen from
opposite sides, and which came first is a fact about the family rather than
about the model — so `requested_side` records it, because "who asked" is the
first question anybody has when a transfer is disputed.

Two exception families meet here, and the split is deliberate rather than an
oversight. `TransferError` and its subclasses describe states *of the
handshake* — already answered, nothing left to hand over, wrong end of the
table. `MembershipError` and `NotPermitted`, from `services`, describe what was
already true of memberships before any of this existed, and every other
membership service raises them. A caller that wants "did the handshake refuse?"
catches `TransferError`; one that wants "was this a sensible thing to ask?"
catches the other. Collapsing them would have meant either a circular import or
a second exception wearing a name that already means something else, which is a
mistake this codebase has made once already and documented.
"""

from django.db import IntegrityError, transaction

from .models import (
    Role,
    TransferAlreadyResolved,
    TransferError,
    TransferRequest,
    TransferRequestStatus,
    TransferSide,
    EnrolmentMovedOn,
)
from .services import (
    NotAStudent,
    NotPermitted,
    _require_grant_authority,
    can_grant_memberships,
)


class TransferAlreadyPending(TransferError):
    """There is already an open request to move this child.

    Enforced in the database as a partial unique index, not merely here. Two
    open requests would let one admin agree to Grace and another agree to
    Hillside for the same child, and whichever landed second would find the
    enrolment already gone. A second destination waits for the first to be
    declined or withdrawn — a real constraint on schools, and the honest one: a
    child transfers to one place.
    """


class AlreadyAtThatSchool(TransferError):
    """The child is already enrolled at the school named as the destination."""


class SameSignatory(TransferError):
    """The person who asked cannot also answer.

    Not a permissions check — by this point the actor has been found to hold
    authority at the answering end too, which happens for platform staff and for
    the occasional admin with memberships at both schools. It is a check on what
    the record *means*. This row exists to show that two parties agreed, and one
    person signing both halves of it would make it say something untrue.

    Somebody who really does hold both ends does not need a handshake at all:
    `services.transfer_student_as()` is the one-caller path, and it says plainly
    in its signature that one person is doing the whole thing.
    """


def _side_for(actor, student, to_school):
    """Which end `actor` is entitled to sign for.

    Releasing is tried first, and that ordering only matters for somebody who
    holds both — platform staff, in practice. It costs them nothing: holding
    both ends means `transfer_student_as()` was always available, and
    `SameSignatory` will stop them answering their own request either way.
    """
    if can_grant_memberships(actor, student.school):
        return TransferSide.RELEASING
    if can_grant_memberships(actor, to_school):
        return TransferSide.RECEIVING
    raise NotPermitted(
        f"{actor} holds no authority at {student.school} or at {to_school}, so "
        f"there is no side of this transfer they can act for."
    )


def _answering_side(request):
    return TransferSide(request.requested_side).other


def _require_answering_authority(actor, request):
    """`actor` may give the second signature on `request`."""
    _require_grant_authority(actor, request.school_for(_answering_side(request)))
    if actor.pk == request.requested_by_id:
        raise SameSignatory(
            "The same person cannot both request a transfer and answer it. "
            "Somebody at the other school has to agree — or, if you genuinely "
            "hold authority at both, use transfer_student_as()."
        )


@transaction.atomic
def request_transfer_as(actor, student, to_school, *, note="", reference=""):
    """Propose moving `student` to `to_school`. Returns the pending request.

    `actor` signs for whichever end they hold authority at, and needs authority
    at that end only — which is the entire point. The other end answers with
    `accept_transfer_as()` or `decline_transfer_as()`.

    Nothing moves here. A request is a proposal about an enrolment, and the
    enrolment is untouched until somebody accepts; a request that is never
    answered leaves the child exactly where they are, which is the right
    failure mode for a handshake nobody completes.

    `reference` is the receiving school's admission number for the child. It is
    accepted here so a receiving school can offer it when it initiates, and may
    equally be supplied at acceptance — either way it is theirs to set, and a
    releasing school passing one would only be guessing at another school's
    numbering.
    """
    if student.role != Role.STUDENT:
        raise NotAStudent("Only a STUDENT membership can be transferred.")
    if not student.is_live:
        raise EnrolmentMovedOn(
            f"{student.user}'s enrolment at {student.school} is "
            f"{student.get_status_display().lower()}, so there is nothing to "
            f"transfer."
        )
    if student.school_id == to_school.pk:
        raise AlreadyAtThatSchool(
            f"{student.user} is already enrolled at {to_school}."
        )

    side = _side_for(actor, student, to_school)

    try:
        # Inside its own atomic block: a violated constraint marks the whole
        # transaction unusable, so without this the friendlier error below would
        # be raised on a connection that can no longer do anything with it.
        with transaction.atomic():
            return TransferRequest.objects.create(
                student=student,
                to_school=to_school,
                requested_by=actor,
                requested_side=side,
                reference=reference,
                note=note,
            )
    except IntegrityError as exc:
        open_request = (
            TransferRequest.objects.pending()
            .filter(student=student)
            .select_related("to_school")
            .first()
        )
        if open_request is None:
            # Some other constraint failed, and dressing it up as "already
            # pending" would send whoever reads the message looking for a
            # request that does not exist.
            raise
        raise TransferAlreadyPending(
            f"There is already an open request to transfer {student.user} to "
            f"{open_request.to_school}. Decline or withdraw it before "
            f"proposing another."
        ) from exc


@transaction.atomic
def accept_transfer_as(actor, request, *, reference=""):
    """Give the second signature and move the child. Returns the new membership.

    This is the only path in the codebase by which `transfer_student()` runs for
    a caller who does not personally hold both schools, and it is legitimate for
    exactly one reason: the row it is called from carries both consents.
    """
    _require_answering_authority(actor, request)
    return request.accept(actor, reference=reference)


@transaction.atomic
def decline_transfer_as(actor, request):
    """Refuse the proposal. The enrolment is untouched."""
    _require_answering_authority(actor, request)
    return request.decline(actor)


@transaction.atomic
def withdraw_transfer_as(actor, request):
    """Call off a proposal from the side that made it.

    Authority at the requesting school, not identity with the requester: people
    change jobs, and a school must be able to withdraw its own proposal after
    the admin who raised it has gone.
    """
    _require_grant_authority(
        actor, request.school_for(TransferSide(request.requested_side))
    )
    return request.withdraw(actor)


def transfers_awaiting(school):
    """Pending requests it is `school`'s turn to answer, newest first."""
    return (
        TransferRequest.objects.awaiting(school)
        .select_related("student__user", "student__school", "to_school", "requested_by")
    )


__all__ = [
    "AlreadyAtThatSchool",
    "EnrolmentMovedOn",
    "NotPermitted",
    "SameSignatory",
    "TransferAlreadyPending",
    "TransferAlreadyResolved",
    "TransferError",
    "TransferRequest",
    "TransferRequestStatus",
    "TransferSide",
    "accept_transfer_as",
    "decline_transfer_as",
    "request_transfer_as",
    "transfers_awaiting",
    "withdraw_transfer_as",
]
