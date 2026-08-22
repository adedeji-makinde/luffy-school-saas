"""The chain a term's results walk before a parent sees them, and its audit.

Two tables. `ResultSheet` is one class's results for one term and holds *where
they have got to*; `ResultSheetTransition` is the append-only log of how they
got there. The split is the whole design, and it is the opposite of the obvious
one.

**The obvious one is columns.** Put `submitted_by`, `checked_by` and
`approved_by` on the sheet and read them back. It survives exactly until the
first send-back: a vice principal returns the sheet, the teacher fixes a score
and resubmits, and `submitted_by` is overwritten. The sheet then says who
submitted it *this time* and has silently forgotten that it was ever refused,
who refused it, and why. A results system whose whole promise is "this is what
was released, and here is how it came to be released" cannot have a memory that
edits itself.

So every transition is a row, written once and never changed — the same rule
`fees.FeeLedgerEntry` holds, enforced the same two ways: a model that refuses,
for the developer, and a database trigger, for the import and the `psql`
session that never touch the model.

## The states

    draft ──submit──▶ submitted ──check──▶ checked ──approve──▶ approved
      ▲                   │                   │                    │
      └───────────────────┴───────────────────┴────────────────────┘
                        send back (with a reason)
                                                                   │
                                                              release
                                                                   ▼
                                                              released ✱

`released` is terminal, and that is a constraint rather than a convention —
`nothing_moves_out_of_released` refuses any row whose `from_state` is
`released`. A released result is one a parent is holding; correcting it is a
*revision*, which makes a new version and leaves this one standing, and that is
built separately. Without the constraint, "released is final" would be a
sentence in a docstring that one `.update()` disagrees with.

## Cycles, and why the log carries one

`cycle` counts how many times the sheet has been sent back. Every transition
row records the cycle it happened in, and a send-back is the last act of its
cycle — it writes its own row, then bumps the sheet.

It exists to make the same-signatory rule expressible in SQL. The rule is that
one person may not perform two different steps on one sheet: a teacher who is
also the acting vice principal must not both submit and check. As a unique
index on `(sheet, actor)` that would be wrong, because a teacher who submits,
is sent back, and resubmits appears twice quite legitimately. On
`(sheet, cycle, actor)` it is right: within one pass through the chain each
person signs at most once, and a send-back opens a fresh pass.

Only *advancing* steps count as signatures on that index — see
`ADVANCING_STATES` below, which sets out why a send-back and a release are
deliberately not signatures, and what breaks if a send-back is counted as one.
"""

from django.db import models
from django.db.models import Q


class SheetState(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted by teacher"
    CHECKED = "checked", "Checked by vice principal"
    APPROVED = "approved", "Approved by principal"
    RELEASED = "released", "Released to parents"


#: The states a sheet can be sent back *from*. Not `draft`, which is where a
#: send-back goes, and not `released` — see `nothing_moves_out_of_released`.
#:
#: Holds the same three members as `ADVANCING_STATES` below, and the two are
#: kept apart rather than aliased because they answer different questions: this
#: one is about a `from_state`, that one about a `to_state`, and they would stop
#: agreeing the moment a state is added that can be signed but not returned
#: from, or the reverse.
SENDABLE_BACK_FROM = frozenset(
    {SheetState.SUBMITTED.value, SheetState.CHECKED.value, SheetState.APPROVED.value}
)

#: Arriving at one of these is a **signature**: somebody moved the sheet closer
#: to a parent, on their own authority. These are the steps the same-signatory
#: rule counts, and the reason it counts only these is worth stating.
#:
#: Sending back is not a signature. It is a retraction, and a retraction can
#: only ever *reduce* how far a result has travelled — so letting the same
#: person do it twice, or do it after signing, risks nothing. Counting it would
#: do real harm: at `approved` in a small school the teacher, the vice principal
#: and the principal have all signed that pass, so if a send-back were a
#: signature there would be nobody left who could take one. A sheet with a known
#: wrong score would be stuck, with release as its only exit.
#:
#: Release is not a signature either — it publishes a decision already taken, so
#: the principal who approved may also release.
ADVANCING_STATES = frozenset(
    {SheetState.SUBMITTED.value, SheetState.CHECKED.value, SheetState.APPROVED.value}
)


class TransitionsAreAppendOnly(Exception):
    """Something tried to edit or delete a transition that already exists."""


class ResultSheet(models.Model):
    """One class's results for one term, and where they have got to.

    The unit of approval is `(class_group, term)` rather than a subject or a
    student. A subject-scoped chain would give a report card no single moment of
    release — it would become releasable only once every subject had passed
    independently, and there would be nothing for a snapshot to be frozen
    against. A student-scoped one would make a principal approve forty-five
    times to release a class.
    """

    # Both tenant-local, so both are real foreign keys with real integrity —
    # the note `gradebook.Assessment` and `academics.ClassPlacement` carry.
    class_group = models.ForeignKey(
        "academics.ClassGroup", related_name="result_sheets", on_delete=models.PROTECT
    )
    term = models.ForeignKey(
        "academics.Term", related_name="result_sheets", on_delete=models.PROTECT
    )

    state = models.CharField(
        max_length=16, choices=SheetState, default=SheetState.DRAFT
    )

    #: How many times this sheet has been sent back. Not history — the history
    #: is in the log — but the number each log row is stamped with, which is
    #: what makes the same-signatory index correct across a resubmission.
    cycle = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["term", "class_group"]
        constraints = [
            # One chain per class per term. Two sheets for one class would mean
            # two answers to "have this term's results been released?", and the
            # snapshot frozen at release would have two things to be frozen
            # from.
            models.UniqueConstraint(
                fields=["class_group", "term"], name="one_result_sheet_per_class_term"
            ),
        ]

    def __str__(self):
        return f"{self.class_group} — {self.term} ({self.get_state_display()})"

    @property
    def is_released(self) -> bool:
        return self.state == SheetState.RELEASED


class ResultSheetTransition(models.Model):
    """One recorded step. Written once, never changed, never deleted.

    Carries `from_state` as well as `to_state` even though the previous row's
    `to_state` implies it. That redundancy is on purpose: it is what lets
    `nothing_moves_out_of_released` be a check constraint on *this row* rather
    than a rule that has to walk the log, and a constraint that needs no context
    is one no future query can get wrong.
    """

    sheet = models.ForeignKey(
        ResultSheet,
        related_name="transitions",
        # PROTECT, not CASCADE. A sheet with an approval history is not a row
        # anybody should be able to delete out from under its own audit — and a
        # cascade would do it silently, which is the failure this table exists
        # to prevent.
        on_delete=models.PROTECT,
    )

    from_state = models.CharField(max_length=16, choices=SheetState)
    to_state = models.CharField(max_length=16, choices=SheetState)

    #: The sheet's cycle when this happened. See the module docstring.
    cycle = models.PositiveIntegerField()

    # A bare id, not a ForeignKey, pointing at the actor's `accounts.User` in
    # the public schema — the policy docs/tenancy.md settles and `Score` and
    # `ClassPlacement` already follow: `on_delete` resolves against whichever
    # schema the connection is on, so a key across the boundary neither
    # protects nor cascades correctly.
    #
    # Not nullable, unlike `Score.recorded_by_id`. A mark can arrive from an
    # import with nobody behind it; an approval cannot. The entire value of this
    # table is that every step names the person who took it.
    actor_id = models.PositiveBigIntegerField(db_index=True)

    #: Why, in the actor's own words. Required on a send-back and optional
    #: elsewhere — a refusal that does not say what is wrong sends the teacher
    #: back to a sheet of forty-five scores with no idea which one to look at.
    reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sheet", "created_at", "pk"]
        indexes = [
            models.Index(fields=["sheet", "cycle"]),
        ]
        constraints = [
            # **Release is terminal**, as a rule Postgres holds. Task 8's
            # revision does not violate this: a revision makes a new version
            # rather than moving this one out of `released`.
            models.CheckConstraint(
                condition=~Q(from_state=SheetState.RELEASED),
                name="nothing_moves_out_of_released",
            ),
            # **The same person may not sign twice in one pass through the
            # chain.** The application refuses this first, with a sentence
            # naming what they already did; this is what holds when the refusal
            # is bypassed, and what holds under concurrency where two requests
            # can both read "they have not signed yet".
            #
            # Scoped to `ADVANCING_STATES` — see that constant for why a
            # send-back and a release are deliberately not signatures.
            models.UniqueConstraint(
                fields=["sheet", "cycle", "actor_id"],
                condition=Q(to_state__in=sorted(ADVANCING_STATES)),
                name="one_signature_per_person_per_review_cycle",
            ),
            # One arrival at each state per pass. The backstop for two people
            # approving at the same instant: `select_for_update()` serialises
            # them, and this is what would refuse the second even if it did not.
            models.UniqueConstraint(
                fields=["sheet", "cycle", "to_state"],
                name="one_transition_to_each_state_per_cycle",
            ),
            # A send-back must say why. Enforced here rather than in a form,
            # because the import and the shell session that skip the service
            # are exactly the callers most likely to leave it blank.
            models.CheckConstraint(
                condition=~Q(to_state=SheetState.DRAFT) | ~Q(reason=""),
                name="a_send_back_says_why",
            ),
        ]

    def __str__(self):
        return (
            f"{self.sheet_id}: {self.from_state} -> {self.to_state} "
            f"by {self.actor_id}"
        )

    def save(self, *args, **kwargs):
        """Refuse to rewrite a row that already exists.

        The trigger installed by migration 0002 is the rule that actually
        holds; this is the one that produces a readable error before Postgres
        produces a less readable one. Both exist on purpose — the same split
        `fees.FeeLedgerEntry` makes, for the same reason.
        """
        if self.pk is not None and not self._state.adding:
            raise TransitionsAreAppendOnly(
                f"Transition {self.pk} has already been recorded and cannot be "
                f"changed. Record the next step instead — an approval history "
                f"that can be edited is not an approval history."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TransitionsAreAppendOnly(
            f"Transition {self.pk} cannot be deleted. The chain has to keep "
            f"saying what it said."
        )
