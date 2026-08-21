# The fee ledger

What a family owes a school and what they have paid, as an append-only book.

Data structure only — there are no screens, no HTTP surface and no reporting.
`fees/services.py` is the layer a bursar's screen will eventually call, and the
rules live there rather than in a view so that an import and a management
command get the same ones.

## Three rules, and why each is where it is

### Money is whole kobo, stored as a signed integer

`amount_kobo` is a `BigIntegerField`. Never a float, never a `DecimalField`
column. Naira is a presentation concern that belongs at the very edge, and the
only representation safe in between is a count of the smallest unit — `1500.10`
does not exist in binary floating point, and half the classic accounting bugs
are that sentence.

The **sign carries the meaning**:

| | |
| --- | --- |
| positive | increases what the family owes — a charge |
| negative | reduces it — a payment, a discount, a reversal of a charge |

So a balance is `SUM(amount_kobo)` with no case analysis and no "except the
cancelled ones" clause to forget. `FeeLedgerQuerySet.balance()` is exactly that
sum, and it returns `0` rather than `None` for an empty ledger, because `None`
is not a balance and would print as "None owing".

A negative balance is a real state, not an error: fees paid ahead of the charge
being posted, or an overpayment carried forward.

Because the sign is grammar rather than data, **the caller never chooses it**.
`charge()`, `record_payment()` and `discount()` each take a positive magnitude
and apply the sign themselves; a negative or zero amount is refused, and so is a
`float`. Check constraints hold the same line at the database: a charge must be
positive, a payment or discount must be negative, and nothing may be zero.

### Corrections are new entries. Nothing is ever edited or deleted

A wrong entry is undone by a `REVERSAL` that names it, and the right entry is
then posted fresh. There is deliberately **no free-form adjustment**: an entry
reading "−₦30,000, first number was wrong" tells a reader the difference and
never what actually happened, whereas a reversal plus a re-post leaves both the
mistake and the fix legible a year later.

This is enforced twice, on purpose:

- `FeeLedgerEntry.save()` refuses to write over an existing row and `.delete()`
  refuses outright, raising `LedgerIsAppendOnly`. That is the error a developer
  sees, in the caller's own language.
- A **Postgres trigger**, installed per schema by `fees/migrations/0002`, refuses
  `UPDATE` and `DELETE` on the table. That is the one a `psql` session, a data
  import or a `QuerySet.update()` runs into — none of which go anywhere near the
  model's methods. It arrives as an `IntegrityError`.

The trigger is row-level and deliberately does **not** cover `TRUNCATE`: a
statement-level guard would also block Django's test teardown, and a schema being
emptied wholesale is a different act from a row being quietly rewritten.

Two further rules about reversals, because the interesting ones span rows:

- An entry may be reversed **at most once**, as a partial unique index. Two
  reversals of one charge would take the balance below where it started and read
  as a refund that never happened. `reverse_entry()` also checks under
  `select_for_update()`, because two bursars clicking undo at the same instant
  both pass an unlocked check before either commits — the service call is what
  gives a good error, the index is what makes it true.
- A reversal cannot itself be reversed. Undoing an undo is how a book stops
  being readable; reverse the original, or post a fresh entry.
- A reversal must be exactly equal and opposite to its target. No check
  constraint can express that (it compares two rows), so it lives in
  `Model.clean()` and every service path calls `full_clean()`.

### An entry freezes who it was for

`student_name` and `student_reference` are stored as they stood when the entry
was posted. This is not denormalisation for speed. A financial record has to keep
saying what it said: if a school corrects a child's name or reissues admission
numbers, last term's receipt must still read the way it was issued, and a join to
the live membership would silently rewrite every historical entry.

It is also what keeps the books legible after a student leaves, when the
membership the entry names has ended.

## What it points at, and what it does not

`student_membership_id` is a **bare id**, not a `ForeignKey` — it names the
student's `STUDENT` membership, which pins both the child and their school in one
value, exactly as `Guardianship.student` does. `recorded_by_id` is the same, for
`accounts.User`.

That is the [tenancy.md](tenancy.md) blocker being answered rather than dodged,
and the full reasoning is recorded there. The short version: `PROTECT` does not
protect across schemas, so on the one table where "this history cannot be
destroyed by deleting somebody" is the whole point, a foreign key buys a
guarantee that is false exactly when it matters. **That decision is proposed, not
ratified** — it is platform-wide policy settled by the first model that needed an
answer.

**The bare id is checked, not trusted.** `fees/services.py` refuses a membership that is
not a `STUDENT` membership, and one whose school is not the school whose books are being
written — read from the connection's own schema, since that is what already chose the
table being written to. Without it, one school's ledger could name a child at another
school and nothing would look wrong: the entry would sit in St Mary's books, count towards
a St Mary's balance, and name a student St Mary's has never taught.

Worth being precise about what the missing foreign key actually costs here, because the
obvious reading is wrong. `Membership` is **shared**, so a foreign key into it would
constrain only that the row *exists* — every school's students live in that one table, so
the school half of the question needs asking in code either way. What is genuinely given up
is the existence half.

Tenant → **tenant** foreign keys are unaffected and this model uses two:
`term` onto `academics.Term`, and `reverses` onto itself. Both live in the same
schema, so `PROTECT` there really does protect — a term with money against it
cannot be deleted, and a test pins it.

## The shape

```
FeeLedgerEntry
    term                    FK  -> academics.Term      (same schema, PROTECT)
    student_membership_id   int -> accounts.Membership (bare id, indexed)
    student_name            frozen at posting time
    student_reference       frozen at posting time
    kind                    charge | payment | discount | reversal
    amount_kobo             signed whole kobo
    narration               what this line is for
    reference               teller / receipt number, free text
    reverses                FK  -> self                (reversals only)
    effective_on            the date it counts for, not the date it was typed
    recorded_at             when it was typed
    recorded_by_id          int -> accounts.User       (bare id, nullable)
```

`effective_on` and `recorded_at` are separate because they disagree: a payment
made on Friday and entered on Monday belongs to Friday.

`reference` is free text because every bank and every school reconciles
differently, and a format guessed now is a format wrong later.

Ordering is `-effective_on, -id`. The tiebreak is not decorative — two entries
posted in the same millisecond need a stable order, and a ledger that reorders
itself between two reads is one nobody can reconcile.

## Not built

- **No screens, no API.** Deliberate; this pass is the data structure.
- **No fee schedule.** Charges are posted one at a time. "Every JSS1 student owes
  ₦150,000 this term" is a template that generates charges, and it is a separate
  model with its own questions (does editing a schedule change past charges? no —
  but then what does it change?).
- **No allocation.** A payment reduces the balance; it is not matched against
  particular charges. Schools that need "which term is this ₦50,000 against?"
  need payment allocation, which is a real feature and a bigger one.
- **No double entry.** There is one ledger per student, not a chart of accounts.
  Correct for a school's fee book, and it does not generalise to the school's own
  accounting.
