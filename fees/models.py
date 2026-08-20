"""What a family owes a school, and what they have paid, as a ledger.

Tenant-scoped, like `academics`: fees are the school's own books, and one copy
per schema is the whole point. See docs/tenancy.md.

Three decisions shape everything below, and they are worth reading before the
fields.

**Money is whole kobo, stored as integers.** Never a float, never a Decimal
column. A naira amount is a presentation concern that belongs at the edge, and
the only safe representation in between is a count of the smallest unit. The
column is a *signed* `BigIntegerField`, and the sign carries meaning:

    positive  ->  increases what the family owes   (a charge)
    negative  ->  reduces it                       (a payment, a discount)

so a balance is `SUM(amount_kobo)` and there is no case analysis to get wrong.
`FeeLedgerQuerySet.balance()` is exactly that sum.

**The ledger is append-only. A correction is a new row.** Nothing here is ever
edited and nothing is ever deleted — a wrong entry is undone by a `REVERSAL`
that names it, and the right entry is then posted fresh. That is not a
convention: `save()` refuses to update an existing row, and a database trigger
refuses `UPDATE` and `DELETE` outright, so a data import, a shell session or a
future service function cannot walk around it either. What the books said last
week is still what they said last week.

**It points at a student by bare id, with no foreign key.** That is the
`docs/tenancy.md` blocker being answered rather than dodged, and the reasoning
is in `student_membership_id` below.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Sum

#: Kobo in a naira. Here so that no call site has to remember it, and so that
#: the one place it appears is next to the column it describes.
KOBO_PER_NAIRA = 100


class FeeEntryKind(models.TextChoices):
    CHARGE = "charge", "Charge"
    PAYMENT = "payment", "Payment"
    DISCOUNT = "discount", "Discount"
    REVERSAL = "reversal", "Reversal"


#: Kinds that increase what a family owes, and kinds that reduce it. Stated once
#: and enforced by check constraints below, so a negative charge or a positive
#: payment is a database error rather than a number nobody notices.
#:
#: **Tuples, not frozensets**, and that is load-bearing rather than a style
#: choice. These go into a `Q(kind__in=...)` inside a check constraint, and a set
#: has no order: `tuple(frozenset)` depends on string hashes, which Python
#: randomises per process. The constraint written into the migration and the one
#: the model computes on the next run therefore compare unequal, and
#: `makemigrations` proposes dropping and recreating it every time — on a
#: different run in a different order, forever. CI runs `makemigrations --check`,
#: so the symptom is a build that is red at random. Membership tests read the
#: same either way.
INCREASES_DEBT = (FeeEntryKind.CHARGE,)
REDUCES_DEBT = (FeeEntryKind.PAYMENT, FeeEntryKind.DISCOUNT)


class LedgerIsAppendOnly(Exception):
    """Something tried to edit or delete a ledger row that already exists."""


class FeeLedgerQuerySet(models.QuerySet):
    def for_student(self, membership_id):
        return self.filter(student_membership_id=membership_id)

    def for_term(self, term):
        return self.filter(term=term)

    def balance(self) -> int:
        """What is outstanding across these entries, in kobo.

        Positive means the family owes; negative means they are in credit, which
        is a real state — a term's fees paid before the charge is posted, or an
        overpayment carried forward.

        A plain sum, and that is the point of the signed column: reversals are
        included with no special handling, because a reversal *is* an amount of
        the opposite sign. There is no "except the cancelled ones" clause to
        forget.
        """
        return self.aggregate(total=Sum("amount_kobo"))["total"] or 0


class FeeLedgerEntry(models.Model):
    """One immutable line in one school's fee book.

    Never updated and never deleted — see the module docstring and `save()`.
    """

    # Tenant-local, so a real foreign key with real integrity: `academics_term`
    # and this table live in the same schema, and the cross-schema problem
    # docs/tenancy.md describes simply does not arise. PROTECT because a term
    # with money against it is not a row anybody should be able to delete.
    term = models.ForeignKey(
        "academics.Term",
        related_name="fee_entries",
        on_delete=models.PROTECT,
        help_text="The term this entry is reckoned against.",
    )

    # A bare id, deliberately, pointing at `accounts.Membership` — the student's
    # STUDENT membership, which pins both the child and their school in one
    # value, exactly as `Guardianship.student` does.
    #
    # No `ForeignKey`, because docs/tenancy.md measured what one does from a
    # tenant schema into `public` and the answer was: `on_delete` is resolved
    # against whichever schema the connection is on, so `PROTECT` does not
    # protect and `CASCADE` cascades only one school's rows, with the breakage
    # surfacing at COMMIT rather than at the delete. For a *financial* record
    # that is the worst of both worlds: the guarantee most worth having here is
    # "this history cannot be destroyed by deleting somebody", and a foreign key
    # is precisely the mechanism that would not deliver it.
    #
    # Keeping the schema self-contained also means a school's books can be
    # dumped, restored and handed over on their own, which for money is a
    # requirement rather than a nicety. And the choice stays cheap to revisit:
    # docs/tenancy.md notes the asymmetry — adding a foreign key later is a
    # migration, removing one once tenant data exists is not.
    student_membership_id = models.PositiveBigIntegerField(
        db_index=True,
        help_text=(
            "accounts.Membership id of the student's STUDENT membership. A bare "
            "id and not a ForeignKey — see the comment above and docs/tenancy.md."
        ),
    )

    # Identity as it stood when the entry was posted, not as it stands now.
    #
    # Not denormalisation for speed — a financial record has to keep saying what
    # it said. If a school corrects a child's name or reissues admission numbers,
    # last term's receipt must still read the way it was issued, and a join to a
    # live row would silently rewrite it. This is also what keeps the books
    # legible when the bare id above points at a membership that has since ended.
    student_name = models.CharField(
        max_length=255,
        help_text="The student's name as it stood when this entry was posted.",
    )
    student_reference = models.CharField(
        max_length=64,
        blank=True,
        help_text="Admission number as it stood when this entry was posted.",
    )

    kind = models.CharField(max_length=16, choices=FeeEntryKind)

    # Signed, in kobo. See the module docstring: positive increases what is
    # owed, negative reduces it, and a balance is the plain sum.
    amount_kobo = models.BigIntegerField(
        help_text=(
            "Whole kobo. Positive increases what the family owes; negative "
            "reduces it. Never a float, never naira."
        )
    )

    narration = models.CharField(
        max_length=255, help_text="What this line is for, in the school's words."
    )
    #: Teller number, receipt number, transfer reference — whatever the school
    #: reconciles against. Free text because every bank and every school does
    #: this differently, and a format guessed now is a format wrong later.
    reference = models.CharField(max_length=64, blank=True)

    # The entry this one undoes. Same table, same schema, so a real foreign key
    # again. PROTECT: an entry that has been reversed is part of the story and
    # cannot be removed — not that anything can remove rows here anyway.
    reverses = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="reversed_by",
        on_delete=models.PROTECT,
        help_text="For a REVERSAL, the entry being undone. Null otherwise.",
    )

    #: The date the entry counts for, which is not always the date it was typed
    #: in — a payment made on Friday and recorded on Monday belongs to Friday.
    effective_on = models.DateField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    # Bare id again, and for the same reason as the student. Nullable because an
    # entry can come from an import or a scheduled charge with no person behind
    # it, and naming a fictional one would be worse.
    recorded_by_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text="accounts.User id of whoever posted this, where there was one.",
    )

    objects = FeeLedgerQuerySet.as_manager()

    class Meta:
        # Newest first, then by id so that two entries posted in the same
        # millisecond still have a stable order — a ledger that reorders itself
        # between two reads is a ledger nobody can reconcile.
        ordering = ["-effective_on", "-id"]
        verbose_name_plural = "fee ledger entries"
        indexes = [
            models.Index(fields=["student_membership_id", "term"]),
            models.Index(fields=["term", "kind"]),
        ]
        constraints = [
            # Zero moves no money and states nothing. It is always a mistake,
            # and usually a placeholder somebody meant to fill in.
            models.CheckConstraint(
                condition=~Q(amount_kobo=0),
                name="a_ledger_entry_moves_money",
            ),
            # The sign has meaning, so the kind and the sign must agree. Without
            # this a negative charge and a positive payment both post happily
            # and the balance is quietly wrong in a way no screen would show.
            models.CheckConstraint(
                condition=~Q(kind=FeeEntryKind.CHARGE) | Q(amount_kobo__gt=0),
                name="a_charge_increases_what_is_owed",
            ),
            models.CheckConstraint(
                condition=~Q(kind__in=REDUCES_DEBT) | Q(amount_kobo__lt=0),
                name="a_payment_or_discount_reduces_what_is_owed",
            ),
            # A reversal names what it undoes, and nothing else does. Both
            # halves matter: a reversal pointing at nothing is unauditable, and
            # a charge pointing at another entry is a relationship the ledger
            # has no meaning for.
            models.CheckConstraint(
                condition=(
                    Q(kind=FeeEntryKind.REVERSAL, reverses__isnull=False)
                    | (~Q(kind=FeeEntryKind.REVERSAL) & Q(reverses__isnull=True))
                ),
                name="only_a_reversal_names_what_it_undoes",
            ),
            # An entry is undone once or not at all. Two reversals of one charge
            # would take the balance below where it started and read, to anyone
            # totalling the column, as a refund that never happened.
            models.UniqueConstraint(
                fields=["reverses"],
                condition=Q(reverses__isnull=False),
                name="an_entry_is_reversed_at_most_once",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.naira} — {self.student_name}"

    # -- money ---------------------------------------------------------------

    @property
    def naira(self) -> Decimal:
        """The amount in naira, for display only.

        `Decimal`, never `float`: the whole reason the column is kobo is that
        binary floating point cannot hold 0.1, and rendering through a float
        would reintroduce at the last step the error the column exists to avoid.
        Nothing should ever store or compare this — it is for showing a human.
        """
        return Decimal(self.amount_kobo) / KOBO_PER_NAIRA

    # -- append-only ---------------------------------------------------------

    def save(self, *args, **kwargs):
        """Refuse to rewrite a row that already exists.

        The database trigger installed by the initial migration is the rule that
        actually holds — this is the one that produces a readable error, in the
        caller's own language, before Postgres produces a less readable one. Both
        exist on purpose: the trigger is what a data import or a shell session
        runs into, and this is what a developer runs into.
        """
        if self.pk is not None and not self._state.adding:
            raise LedgerIsAppendOnly(
                f"Ledger entry {self.pk} has already been posted and cannot be "
                f"changed. Post a reversal naming it, then post the correct "
                f"entry."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerIsAppendOnly(
            f"Ledger entry {self.pk} cannot be deleted. Post a reversal naming "
            f"it instead — the books have to keep saying what they said."
        )

    def clean(self):
        """The rules a check constraint cannot express, because they span rows."""
        if self.kind != FeeEntryKind.REVERSAL:
            return
        if self.reverses is None:
            raise ValidationError({"reverses": "A reversal must name the entry it undoes."})
        if self.reverses.kind == FeeEntryKind.REVERSAL:
            raise ValidationError(
                {"reverses": "A reversal cannot itself be reversed; reverse the original."}
            )
        if self.amount_kobo != -self.reverses.amount_kobo:
            raise ValidationError(
                {
                    "amount_kobo": (
                        f"A reversal must undo its entry exactly: expected "
                        f"{-self.reverses.amount_kobo} kobo, got {self.amount_kobo}."
                    )
                }
            )
