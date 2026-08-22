"""Posting to the fee ledger. The only supported way to write one.

Four functions, one per thing that can happen to a family's account: they are
charged, they pay, they are given a discount, or somebody made a mistake and it
is undone. There is deliberately no general "adjust" — see `reverse_entry()`.

Everything here takes the caller's *student membership id* rather than a
`Membership` object, because the ledger stores a bare id and the two must not
drift apart. `snapshot_student()` is the one place that turns a live membership
into the frozen identity an entry carries.

No screens, no HTTP. This is the data layer the eventual bursar's screen will
call, and keeping the rules here rather than in a view is what makes them true
for an import and a management command too.
"""

from django.db import transaction
from django.utils import timezone

from accounts.students import why_not_a_student_here

from .models import FeeEntryKind, FeeLedgerEntry, LedgerIsAppendOnly


class FeeLedgerError(Exception):
    """A ledger entry could not be posted as asked.

    One base class for the whole module, for the same reason
    `schools.models.InvitationError` is one for the invitation flow: a caller
    that writes `except FeeLedgerError` should catch every refusal this module
    makes, not the half it happened to import.
    """


class NotPositive(FeeLedgerError):
    """An amount was zero or negative where a magnitude was wanted.

    Every function here takes the amount as a plain positive number of kobo and
    applies the sign itself. Letting a caller pass a negative to `charge()`
    would make the sign a caller's choice, and the sign is the ledger's whole
    grammar.
    """


class AlreadyReversed(FeeLedgerError):
    """That entry has been undone once already."""


class CannotReverse(FeeLedgerError):
    """That entry is not one that can be undone."""


class NotThisSchoolsStudent(FeeLedgerError):
    """The membership named is not a student of the school whose books these are.

    This is the check that earns the bare id. `student_membership_id` has no
    foreign key — see docs/tenancy.md for why — which means the database will
    happily store *any* integer there, including the id of a child at another
    school entirely. Nothing about that would be visible: the entry would sit in
    St Mary's ledger, count towards a St Mary's balance, and name a student St
    Mary's has never taught.

    A foreign key would not have caught it either, note. `Membership` is shared,
    so a foreign key into it constrains only that the row *exists* — every
    school's students are in the same table. The school half of the question has
    to be asked in code whichever way the column is declared.
    """


def _require_student_of_this_school(membership):
    """Refuse a membership that is not a student here, before anything is written.

    The rule itself now lives in `accounts.students` — see the note on
    `NotThisSchoolsStudent` above, which said it would move there once a third
    tenant app asked the same question. What stays here is the *raising*, so
    that `except FeeLedgerError` still means "the entry was not posted".
    """
    reason = why_not_a_student_here(
        membership, subject="a fee entry", holder="books"
    )
    if reason:
        raise NotThisSchoolsStudent(reason)
    return membership


def snapshot_student(membership):
    """The identity fields an entry freezes, taken from a live membership.

    Kept in one place because getting it wrong is silent: an entry posted with
    a blank name is unreadable a year later, and one posted by joining to the
    live row rewrites itself when a school corrects a spelling.
    """
    return {
        "student_membership_id": membership.pk,
        "student_name": membership.name,
        "student_reference": membership.reference,
    }


def _post(*, membership, term, kind, amount_kobo, narration, effective_on,
          reference="", recorded_by=None, reverses=None):
    """Create one entry. Every public function below funnels through here."""
    _require_student_of_this_school(membership)
    entry = FeeLedgerEntry(
        term=term,
        kind=kind,
        amount_kobo=amount_kobo,
        narration=narration,
        reference=reference,
        effective_on=effective_on or timezone.localdate(),
        recorded_by_id=getattr(recorded_by, "pk", recorded_by),
        reverses=reverses,
        **snapshot_student(membership),
    )
    # `full_clean()` rather than a bare save: the cross-row rules for a reversal
    # live in `Model.clean()` because no check constraint can express "equal and
    # opposite to another row". Excluding nothing, so the field-level rules are
    # asked here too rather than only at the database.
    entry.full_clean(exclude=None, validate_unique=False, validate_constraints=False)
    entry.save()
    return entry


def _magnitude(amount_kobo):
    if not isinstance(amount_kobo, int) or isinstance(amount_kobo, bool):
        raise NotPositive(
            f"Amounts are whole kobo as an int, not {type(amount_kobo).__name__}. "
            f"A float cannot hold a naira amount exactly, which is the reason "
            f"this column is kobo in the first place."
        )
    if amount_kobo <= 0:
        raise NotPositive(
            f"Pass the amount as a positive number of kobo; the ledger applies "
            f"the sign. Got {amount_kobo}."
        )
    return amount_kobo


@transaction.atomic
def charge(membership, term, amount_kobo, *, narration, effective_on=None,
           reference="", recorded_by=None):
    """Bill a student. Increases what the family owes."""
    return _post(
        membership=membership,
        term=term,
        kind=FeeEntryKind.CHARGE,
        amount_kobo=_magnitude(amount_kobo),
        narration=narration,
        effective_on=effective_on,
        reference=reference,
        recorded_by=recorded_by,
    )


@transaction.atomic
def record_payment(membership, term, amount_kobo, *, narration="Payment received",
                   effective_on=None, reference="", recorded_by=None):
    """Record money received. Reduces what the family owes."""
    return _post(
        membership=membership,
        term=term,
        kind=FeeEntryKind.PAYMENT,
        amount_kobo=-_magnitude(amount_kobo),
        narration=narration,
        effective_on=effective_on,
        reference=reference,
        recorded_by=recorded_by,
    )


@transaction.atomic
def discount(membership, term, amount_kobo, *, narration, effective_on=None,
             recorded_by=None):
    """Reduce what is owed without money changing hands.

    A bursary, a staff child's concession, a sibling discount. Its own kind
    rather than a negative charge, because "we waived it" and "they paid it" are
    different facts and a school's books have to be able to tell them apart.
    """
    return _post(
        membership=membership,
        term=term,
        kind=FeeEntryKind.DISCOUNT,
        amount_kobo=-_magnitude(amount_kobo),
        narration=narration,
        effective_on=effective_on,
        recorded_by=recorded_by,
    )


@transaction.atomic
def reverse_entry(entry, *, narration=None, effective_on=None, recorded_by=None,
                  membership=None):
    """Undo `entry` by posting its exact opposite. Returns the new entry.

    The only correction there is, and deliberately the only one. There is no
    "edit the amount" and no free-form adjustment: a charge raised for the wrong
    amount is reversed in full and the right one posted fresh, which leaves both
    the mistake and the fix legible a year later. An adjustment of "-30,000
    because the first number was wrong" tells a reader the difference and never
    what actually happened.

    The identity snapshot is taken from `entry` rather than from a live lookup,
    unless a `membership` is passed. A reversal is part of the original story and
    should read the way the original read — including when the student has since
    left and their membership has ended.
    """
    # Locked and re-read before deciding, because "has this been reversed
    # already?" is a question about the present, and two bursars clicking undo
    # on the same charge is exactly the race this guards. `select_for_update()`
    # on the entry itself; `.order_by()` is not needed here because
    # `FeeLedgerEntry.Meta.ordering` sorts by its own columns and joins nothing —
    # the trap docs/membership.md records for `Membership` does not apply.
    locked = (
        FeeLedgerEntry.objects.select_for_update()
        .select_related("term")
        .get(pk=entry.pk)
    )

    if locked.kind == FeeEntryKind.REVERSAL:
        raise CannotReverse(
            "That entry is itself a reversal. Reverse the original entry, or "
            "post a fresh one — undoing an undo is a way to lose track of what "
            "actually happened."
        )
    if FeeLedgerEntry.objects.filter(reverses=locked).exists():
        raise AlreadyReversed(
            f"Ledger entry {locked.pk} has already been reversed. Post a new "
            f"entry if something further needs correcting."
        )

    if membership is not None:
        identity = {}
        snapshot = membership
    else:
        identity = {
            "student_membership_id": locked.student_membership_id,
            "student_name": locked.student_name,
            "student_reference": locked.student_reference,
        }
        snapshot = None

    reversal = FeeLedgerEntry(
        term=locked.term,
        kind=FeeEntryKind.REVERSAL,
        amount_kobo=-locked.amount_kobo,
        narration=narration or f"Reversal of: {locked.narration}",
        reference=locked.reference,
        effective_on=effective_on or timezone.localdate(),
        recorded_by_id=getattr(recorded_by, "pk", recorded_by),
        reverses=locked,
        **(identity if snapshot is None else snapshot_student(snapshot)),
    )
    reversal.full_clean(exclude=None, validate_unique=False, validate_constraints=False)
    reversal.save()
    return reversal


__all__ = [
    "AlreadyReversed",
    "CannotReverse",
    "FeeLedgerError",
    "LedgerIsAppendOnly",
    "NotPositive",
    "NotThisSchoolsStudent",
    "charge",
    "discount",
    "record_payment",
    "reverse_entry",
    "snapshot_student",
]
