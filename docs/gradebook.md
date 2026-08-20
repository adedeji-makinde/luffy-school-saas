# The gradebook

Subjects, assessments and marks — and the sheet a teacher types them into.

Unlike [the fee ledger](fees.md), this pass ships an HTTP surface, because the
interaction is what decided the data model rather than the other way round. A
teacher marking a class of thirty does not fill in a form and press Save at the
bottom; they tab through thirty cells and each one saves as it loses focus. Every
choice below follows from that, and the rules still live in `gradebook/services.py`
rather than in the view, so an import and a management command get the same ones.

## Three rules, and why each is where it is

### "Not marked yet" and "scored zero" are different facts

This is the whole model. A schema that cannot tell those two apart will average
them together and print a number on a report card that nobody can defend.

So a `Score` row exists **if and only if** a teacher has entered a value:

| | |
| --- | --- |
| `value` | `NOT NULL`. There is no such thing as a blank score row. |
| opening a sheet | writes nothing. Thirty unmarked children are *absent from the table*, not present with a null. |
| clearing a mark | deletes the row, rather than writing a zero or a null. |

The naive implementation — materialise a row per student when the assessment is
created and fill them in later — is exactly what makes the two states
indistinguishable. It also makes "how many are still to be marked?" unanswerable,
which is the question a head of department actually asks.

This propagates into the totals. `total_for()` returns `scored`, `available` and
`marked`, and **`available` counts only the assessments this child has a score
for**. Summing every assessment's `max_score` would silently treat "not marked
yet" as zero — the same conflation again — and would drop a child's percentage
every time a teacher created next week's test.

### No total is ever stored

A total that lives in a column is a total that can be stale, and "refresh it
before display" is a rule that holds until the day somebody adds a second write
path. `ScoreQuerySet.total_for()` aggregates on read, exactly as
`fees.FeeLedgerQuerySet.balance()` does, so there is nothing to refresh and
nothing to forget. It returns `0` rather than `None` over no rows, because `None`
is not a total — a caller adding it to another number gets a `TypeError`, and one
rendering it prints "None".

`test_no_model_here_stores_a_total` enforces the claim rather than asserting it in
prose: it walks every field of every model in the app and fails on a name
containing *total*, *aggregate*, *average*, *percentage* or *cached*.

Three numbers reach the client, not a percentage. The percentage is a rendering
decision and `available` can be zero; a client dividing by it knows to show a
dash, whereas a server that had already divided would have had to invent
something.

### Every write is conditional on the version the writer was shown

Two teachers with the same sheet open is the ordinary case, not an exotic one,
and a last-write-wins update silently discards one of them. Every `Score` carries
a `version`, handed out with the sheet and handed back to save.

`set_score()` puts that version **in the `WHERE` clause of a single statement**
rather than reading and then writing:

- **Update half.** `UPDATE ... WHERE version = %s`. Zero rows affected means
  somebody moved first.
- **Insert half.** `expected_version=None` means "I was shown no mark", so this
  must be an insert; the `one_score_per_student_per_assessment` unique constraint
  is what stops the second of two simultaneous first marks.

Both losses are reported the same way, because from the caller's side they are
the same event: `ScoreChangedMeanwhile`, carrying `current` — the row as it now
stands, or `None` if the mark has since been cleared. That is what lets a screen
say *"Kemi entered 17 while you were typing"* instead of "somebody changed this".

Not `update_or_create()`, which reads and then writes and loses the race in
between. And the insert runs in its own `transaction.atomic()` block, because an
`IntegrityError` marks the *enclosing* transaction unusable — without it, a caller
wrapping a whole sheet in `atomic()` and catching the refusal could not go on to
write the next student.

`ANY_VERSION` is the escape hatch, for a bulk import or a data migration where
there is no screen and nobody to conflict with. It is a **sentinel, not
`expected_version=None`**, because `None` already means "I was shown no mark".
Overloading it would make the default an unchecked overwrite, and the default has
to be the safe one: a caller who forgets to pass the version they were given is
refused, not silently allowed to clobber.

`clear_score()` takes `expected_version` as a keyword argument with **no
default**, for the same reason: "clear whatever is there" is precisely the
destructive write the version exists to prevent, and it is not worth a convenient
spelling. Clearing a mark that is already gone is a no-op rather than an error —
the end state the caller asked for is the end state that holds.

## Who may mark

`MARK_ENTERING_ROLES` is teacher, principal and administrator. A principal and an
administrator are in because entering a term's marks from a paper sheet is office
work in most schools, and a system that refused it would be worked around with a
borrowed teacher login — strictly worse, because then `recorded_by_id` names the
wrong person on every row it touches.

The load-bearing half is who is absent. A bursar keeps the books and does not
mark. A parent and a student are the *subjects* of this data, and a `STUDENT`
membership is the very thing a `Score` is keyed on.

**Platform staff are not admitted**, which is the one place this departs from
`accounts.services.can_grant_memberships()`. Support staff repairing a membership
is an operational act on the platform's own plumbing; writing a child's academic
record is the school's own act, it is what a report card is built from, and
`recorded_by_id` would name a platform operator on the row.

Authority is access-scoped through `roles_at()`, so an invited or suspended
teacher has a membership and no authority.

The check lives only in the `_as()` variants — `set_score_as()`,
`clear_score_as()` — on the idiom `accounts.services` set. The plain functions are
primitives an import or a data migration can use; anything with a request behind
it goes through the actor-checked pair. Authority is the one rule that cannot live
in the primitive, because a management command has no actor to check.

## The HTTP surface

Three endpoints, mounted at `/api/gradebook/` from `gradebook/api.py`.

```
GET    /assessments/{id}/sheet/                     the roll, marked and unmarked
PUT    /assessments/{id}/scores/{membership_id}/    enter or change one mark
DELETE /assessments/{id}/scores/{membership_id}/    take one back
```

**One mark per request.** Blur fires per cell, so the unit of a write is a single
student's mark on a single assessment. There is no bulk save and a sheet is not a
transaction: thirty independent writes is what actually happened, and it is what
should be recorded.

**Every write answers with the new version and the recomputed total** — the two
things on screen that a save invalidates. Returning them in the response the
client is already waiting for makes "refresh the total before display" true by
construction, and a client that never issues a second request cannot forget to.
That is also why clearing a mark answers `200` with a body rather than `204`: a
`204` would be honest about the mark and leave the total stale.

The total on a sheet is scoped to **this subject, this term**. Not the child's
every mark ever, which spans sessions and is not a number anybody wants next to a
Mathematics First CA; and not this assessment alone, which is already the cell.

**A conflict is a body, not a bare status.** The `409` carries `current`, nullable
because "it has been cleared since you were shown it" is a real and different
outcome from "it now reads 17".

**A retried blur is not a conflict.** Blur fires more than once for one edit —
tabbing out and then submitting, a browser retrying a request whose response was
lost, a double-fired event. Each retry carries the version the teacher was
*shown*, which the first attempt has already moved past, so a plain version check
calls the retry a conflict and tells a teacher that somebody else is editing their
sheet when nobody is. Cry wolf once and the warning stops being read, which costs
exactly the protection the version exists to give. Swallowing it is only safe when
the row already says precisely what this request asked for **and** the same person
wrote it; a different value, or another person's write, is still reported.

**The sheet is gated on `can_enter_marks()`, not on membership.** `SchoolAccessMiddleware`
establishes only that the caller belongs to this school — and this school's parents
and students belong to it too. A marking sheet is the whole class's marks side by
side, which is the one thing a parent must not be handed. What a parent may see is
their own child's marks; that is a different endpoint with a different shape, and
it is not this one wearing a filter.

**Another school's child is *not found*, not refused.** `services` raises
`NotThisSchoolsStudent`, whose message names the child and the school they
actually attend — correct for a log and a test, and a cross-tenant leak if it ever
reached an HTTP caller. So the endpoint scopes the lookup to this school and to
`STUDENT` up front, and the refusal never gets that far. Same reasoning as the
flat `404` the invitation routes answer a bad token with.

**No school slug in any path**, unlike `/api/schools/{slug}/...`. Those routes
write shared tables, where the school is a row that has to be named. The gradebook
is a tenant app: `TenantMainMiddleware` has already chosen the schema from the
hostname before any code here runs. A slug would be a *second* opinion about which
school this is, free to disagree with the connection — and a disagreement means
authorising against one school and writing into another's schema. On the portal
host there is no schema, so the sheet is a `404`: not "forbidden", but "there is no
such gradebook here".

## What it points at, and what it does not

`student_membership_id` is a **bare id**, not a `ForeignKey`, following the policy
settled in [tenancy.md](tenancy.md) and first applied by `fees.FeeLedgerEntry`.
`recorded_by_id` and `updated_by_id` are the same, for `accounts.User`. The short
version of the reasoning: `on_delete` is resolved against whichever schema the
connection is on, so `PROTECT` does not protect and `CASCADE` would cascade one
school's rows only.

The id is checked, not trusted. `_require_student_of_this_school()` refuses a
membership that is not a `STUDENT` one, and one whose school is not the school
whose marks are being written — read from the connection's own schema, because
that is what already chose the table being written to. This is a check a foreign
key could not have made anyway: `Membership` is **shared**, so every school's
students are in that one table and an FK would constrain only that the row exists.

**This app deliberately does not freeze the student's name**, which is where it
parts company with `FeeLedgerEntry`. A receipt has to keep saying what it said, so
the ledger stores `student_name` as it stood at posting time. A marking sheet is
the opposite: it is a live working document, and a teacher who corrects the
spelling of a child's name wants the corrected spelling on the sheet they are
typing into now. So the roll comes from `school_directory()` at read time and only
the mark is stored here. The trade is that a `Score` on its own does not say whose
it is once the membership is gone — acceptable while a mark is only ever read
through a sheet, and the thing to revisit if marks ever have to outlive the roll.

The roll is relationship-scoped, so a suspended student still appears. A teacher
marking a register works from the roster the office keeps, and silently dropping a
child from a sheet is how a mark goes missing with nobody noticing.

Tenant → **tenant** foreign keys are unaffected and this app uses three: `Assessment.term`
onto `academics.Term`, `Assessment.subject` onto `Subject`, and `Score.assessment`.
All live in the same schema, so `PROTECT` there really does protect — a subject
with marks against it cannot be deleted, and a test pins it.

## The shape

```
Subject
    name                    unique per school
    code                    "MTH", "ENG" — the school's own, not a slug of the name
    is_active               no longer taught, kept because old scores name it

Assessment
    term                    FK  -> academics.Term   (same schema, PROTECT)
    subject                 FK  -> Subject          (same schema, PROTECT)
    name                    "First CA", "Exam"
    max_score               what a perfect score is; >= 1
    created_at
    unique (term, subject, name)

Score
    assessment              FK  -> Assessment       (same schema, PROTECT)
    student_membership_id   int -> accounts.Membership (bare id, indexed)
    value                   NOT NULL. A row exists only if somebody was marked.
    version                 bumped on every write; the optimistic-lock token
    recorded_by_id          int -> accounts.User    (bare id, nullable)
    updated_by_id           int -> accounts.User    (bare id, nullable)
    created_at / updated_at
    unique (assessment, student_membership_id)
```

`max_score` is stored per assessment rather than assumed to be 100, because a CA
is commonly out of 20 or 30 and a total that treated it as a percentage would be
wrong by a factor of five. It is guarded `>= 1` at the database, because it is the
denominator of every percentage this data produces and a zero there is a division
error somewhere far away from here.

The rule that a mark cannot exceed `max_score` **compares two rows**, so no check
constraint can express it. It lives in `Score.clean()` and in
`services.set_score()`, and its absence from `Meta.constraints` is recorded there
in a comment so it reads as a decision rather than an oversight.

`recorded_by_id` and `updated_by_id` are nullable because a score can arrive from
an import with no person behind it, and naming a fictional one is worse.

## Not built

- **No class or stream model.** An `Assessment` belongs to a (term, subject) and
  not to a class, because there is no class model in this project yet and
  inventing one here would be guessing at how a school groups its children. Who
  was scored is answered by which students have a `Score`, which is enough for a
  sheet and a total.
- **No grading scale and no report card.** Turning 17/20 into a "B", weighting CA
  against exam, and printing a term's results are each their own decision with
  their own arguments. The three numbers this app returns are what those would be
  built from.
- **No history of a mark.** `recorded_by_id`, `updated_by_id` and `version` say
  who touched a mark last and how many times it moved, not what it used to be.
  This is the deliberate opposite of the fee ledger, which is append-only: a
  correction to a receipt is a fact about money and has to stay legible, while a
  teacher fixing a typo before submission is not an event anybody needs kept. If
  changed marks ever need an audit trail — and after results are released they
  probably do — that is a separate append-only table, not a nullable column here.
- **No view of a child's own marks.** A parent or a student may see their own; the
  marking sheet is not that endpoint with a filter on it, and the shape they need
  is different.
- **No bulk import.** `set_score(..., expected_version=ANY_VERSION)` is the
  primitive one would be built on, and it exists for that reason.
