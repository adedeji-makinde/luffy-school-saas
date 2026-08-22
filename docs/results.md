# The approval chain

What a term's results walk through before a parent sees them, and the audit that
records it. Two tables in `results`: `ResultSheet` holds *where a class's results
have got to*, and `ResultSheetTransition` is the append-only log of how they got
there.

```
draft ──submit──▶ submitted ──check──▶ checked ──approve──▶ approved ──release──▶ released ✱
  ▲                   │                   │                    │
  └───────────────────┴───────────────────┴────────────────────┘
                    send back (with a reason)
```

## The unit of approval is (class, term)

Not a subject, and not a student.

A **subject-scoped** chain gives a report card no single moment of release: it
becomes releasable only once every subject has passed independently, and the
snapshot frozen at release has nothing to be frozen *against*. A
**student-scoped** one makes a principal approve forty-five times to release one
class.

So one sheet per class per term, enforced by `one_result_sheet_per_class_term`.
Two sheets for one class would mean two answers to "have this term's results
been released?".

## The audit is rows, not columns

The obvious design is columns: `submitted_by`, `checked_by`, `approved_by` on
the sheet. It survives exactly until the first send-back. A vice principal
returns the sheet, the teacher fixes a score and resubmits, and `submitted_by`
is overwritten — the sheet now says who submitted it *this time*, and has
silently forgotten that it was ever refused, who refused it, and why.

A results system whose whole promise is "this is what was released, and here is
how it came to be released" cannot have a memory that edits itself. So every
transition is a row, written once and never changed.

**Append-only is enforced twice**, the same way `fees.FeeLedgerEntry` does it:
`save()` and `delete()` refuse, which is the error a developer sees; and a
Postgres trigger refuses, which is the error a `psql` session, a data import or
a bulk `.update()` runs into — none of which go near the model's methods. Tests
cover both paths, including `.update()`, which never calls `save()`.

## Release is terminal, as a constraint

`nothing_moves_out_of_released` is a `CheckConstraint` refusing any row whose
`from_state` is `released`. Not a docstring, not a service-layer `if` — one
`.update()` would disagree with either.

A released result is one a parent is holding. Correcting it is a **revision**,
which makes a new version and leaves this one standing; that is built separately
and does not violate this constraint, because a revision never moves this
version out of `released`.

This is why `ResultSheetTransition` stores `from_state` even though the previous
row's `to_state` implies it. The redundancy is what lets the rule be a check
constraint on a single row rather than something that has to walk the log — and
a constraint needing no context is one no future query can get wrong.

## Sending back

The task list described a forward path only. A chain that only goes forward does
not mean mistakes are not made; it means the fix is somebody editing the
database, which leaves no record that anything was ever wrong.

So `send_back()` is a real transition from `submitted`, `checked` or `approved`
to `draft`, taken by whoever could have said yes instead. It **requires a
reason** — refused by the service *and* by `a_send_back_says_why`, because a
refusal that does not say what is wrong sends a teacher back to forty-five
scores with no idea which one to look at.

## Cycles and the same-signatory rule

One person may not perform two different steps on one sheet. In a school with
nine staff, the class teacher may well also be the acting vice principal, and
`grant_membership` allows both memberships — the rule is not that the roles
cannot be held together, but that one person cannot sign twice on one pass.

Enforced in the application, which produces a sentence naming what they already
did, **and** in the database, which is what holds when the service is bypassed
and when two concurrent requests both read "they have not signed yet".

Getting that into SQL is what `cycle` is for. As a unique index on
`(sheet, actor)` the rule would be wrong: a teacher who submits, is sent back
and resubmits appears twice, quite legitimately. On `(sheet, cycle, actor)` it
is right — within one pass each person signs at most once, and a send-back opens
a fresh pass.

**Only advancing steps count as signatures** (`submitted`, `checked`,
`approved`). Two exclusions, both deliberate:

- **A send-back is a retraction**, and a retraction can only ever reduce how far
  a result has travelled, so letting the same person do it costs nothing.
  Counting it would do real harm: at `approved`, the teacher, the vice principal
  and the principal have all signed that pass, so if a send-back were a
  signature there would be nobody left who could take one. A sheet with a known
  wrong score would be **stuck**, with release as its only exit. This was found
  by writing the test for it, not by reasoning about it.
- **A release publishes a decision already taken**, so the principal who
  approved may also release. Approving and checking are the two that must be
  different people.

### `cycle` has exactly one source

The invariant, stated plainly because both database guards depend on it:

> **`cycle` is read off the row `_move()` locked, never off the instance the
> caller passed in, and only `send_back()` increments it.**

No transition function accepts a `cycle=` argument, and a test asserts that
structurally — one added in a hurry would silently unscope both guards, and
nothing else in the suite would notice.

The failure it prevents is ordinary, not exotic. A screen loads a sheet,
somebody else sends it back, and the screen then submits using the instance it
is still holding — whose `cycle` says 0. Stamped from that instance, the
resubmission is written into a bucket the guards have already used. Changing one
line to `cycle=sheet.cycle` and re-running gives:

    IntegrityError: duplicate key value violates unique constraint
    "one_signature_per_person_per_review_cycle"
    DETAIL: Key (sheet_id, cycle, actor_id)=(1, 0, 1) already exists.

— the teacher colliding with their own earlier submission at cycle 0. The
visible symptom is a crash; the invisible one, had the collision not existed,
would have been a second approval at the wrong cycle that no guard fires on.

## What the lock actually buys

`approve()` is read-modify-write on one row, and every transition takes
`select_for_update()` on the sheet before reading its state.

What that buys was measured by removing it and re-running the concurrency tests:

    IntegrityError: duplicate key value violates unique constraint
    "one_transition_to_each_state_per_cycle"

So the **constraint** is what prevents the double approval — even unlocked, the
audit never gains a second approver for one decision. The **lock** is what turns
the loser's outcome from an unhandled `IntegrityError` — a 500 on a principal's
screen, saying nothing — into a `WrongState` naming the state the sheet reached.

Two layers doing two different jobs, and it would have been easy to write the
lock's docstring claiming the constraint's job.

## Who may take which step

| Step | Roles |
| --- | --- |
| submit | teacher, admin |
| check | vice principal (academic) |
| approve | principal |
| release | principal, admin |
| send back | vice principal (academic), principal |

An **administrator may submit** because entering and submitting a paper sheet is
office work in most schools — the reasoning `gradebook.MARK_ENTERING_ROLES`
gives for admitting one. An administrator **may not check**: that is the step the
chain exists for, and widening it would let the office both submit and check.
An administrator **may release** because release is commonly gated on something
clerical — fees settled, cards printed — and is not a second academic judgement.

Authority is asked at the school on the connection, never at a school passed in
as an argument, for the reason `accounts.students.why_not_a_student_here()` reads
it there. It is access-scoped, so a suspended principal has a membership and no
authority. Platform staff are not admitted, on the reasoning
`gradebook.services.can_enter_marks()` set out: approving a child's results is
the school's own act.

The `_as()` split the other service modules use is deliberately **absent**.
There are no primitives here: every act is somebody's signature, so there is no
version that makes sense without an actor. A data migration wanting to move a
sheet must name the person it is moving it on behalf of, which is the right
amount of friction for rewriting an approval chain.

## The vice principal

`Role.VICE_PRINCIPAL_ACADEMIC`, added for this chain. Named for the scope that
exists: the chain is per (class, term) and carries no subject, so a head of
department — head of a *subject area* — would have nothing here to be head of.

Its stored value is `"vp_academic"`, not `"vice_principal_academic"`, because
`Membership.role` is `max_length=16` and the full string is 23 characters. The
value is an internal key like `"admin"` and `"bursar"`; the label is what anybody
reads. A test asserts the value fits, against the field's declared `max_length`
rather than a literal.

It slots into `STAFF_ROLES` with no special-casing: `invite_staff()`,
`active_staff()`, `Membership.staff()`, the API's `role: str` fields and
`get_role_display()` all pick it up from there. `sqlmigrate` confirms the
choices migration is a no-op — no rewrite of the shared `accounts_membership`
table.

## Two control experiments, and what each proved

Both are recorded here rather than only in a commit message, because the thing
they establish is *which layer does which job* — and that is the sort of claim a
docstring drifts away from silently.

The method both times: break one thing deliberately, re-run, read the failure.

| Broken | Result | What it proved |
| --- | --- | --- |
| `select_for_update()` removed | `one_transition_to_each_state_per_cycle` fires; the audit still holds one approval | The **constraint** prevents the double approval. The **lock** only converts the loser's 500 into a `WrongState`. The lock's docstring had been claiming the constraint's work. |
| `cycle=locked.cycle` → `cycle=sheet.cycle` | `one_signature_per_person_per_review_cycle` fires on a resubmission | `cycle` really is load-bearing, and taking it from the locked row is what keeps it correct. |

The first is the one worth remembering. It would have been entirely natural to
write "the lock prevents two approvals", ship it, and have a true-sounding
sentence in the codebase that no longer described the code the day somebody
removed the constraint as redundant.

## A test-isolation trap this app walked into

`TransactionTestCase` flushes the *public* tables between tests. A tenant schema
is not a table and survives — so the next `School.save()` finds the schema
already there, skips `CREATE SCHEMA`, and inherits the previous test's rows.

`academics.tests.test_classes.PlacementUnderConcurrencyTests` did that, passing
alone and within its own app, and left an `st_marys` holding a `Term`.
`results.tests.test_approval_concurrency` then failed three tests in its own
`setUp` with `uniq_term_session_name` — a failure that reads like a bug in the
victim and belongs entirely to the leaker.

Both now drop the schema in `tearDown`, with SQL rather than
`School.delete(force_drop=True)`: `Membership.school` is `PROTECT`, so deleting
the row is refused while the test's memberships point at it. The row is flushed
for us; the schema is the part that has to be told to go.

Plain `TestCase` needs none of this — its rollback covers tenant tables too,
because they are in the same transaction.

## Not built here

- **The snapshot.** Release is the moment it is frozen; what gets frozen is
  task 3.
- **Revisions.** Task 8. The constraint above is written so that a revision
  makes a new version rather than moving this one.
- **Screens.** No HTTP surface, for the reason `fees.services` has none: the
  rules have to hold for an import too.
