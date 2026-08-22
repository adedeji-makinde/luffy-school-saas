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

## Release is terminal — and it takes two guards, not one

`nothing_moves_out_of_released` is a `CheckConstraint` refusing any transition
row whose `from_state` is `released`. That was the whole of it, and the whole of
it was not enough. **It guards the log, not the fact the log is about.**

The gap, found in review:

```python
ResultSheet.objects.filter(state="released").update(state="draft")
```

writes no transition row, so it meets no constraint on the transition table —
and migration 0002's append-only trigger also fires only on the transition
table. Nothing in the schema touched `results_resultsheet` at all. From a psql
session, an import or a bulk fix, a released sheet reverted silently. That is
worse than an unguarded revert: the audit then reads as though the result is
still released, while the sheet is a draft somebody can edit.

So there is a second guard, in migration 0003 — a trigger on
`results_resultsheet` refusing any UPDATE that moves `state` off `released`. A
check constraint cannot express it, because the rule is about the move from one
row-version to the next and a CHECK sees only the row in front of it.

Deliberately narrow: **BEFORE UPDATE, and only on a change of `state`.** Not
DELETE — a released sheet always has transitions and `ResultSheetTransition.sheet`
is `PROTECT`, so the row cannot be deleted while its own audit points at it. Not
on every UPDATE either, because `updated_at` and the fields task 8 will add have
to stay writable, and a guard broader than its rule is one somebody eventually
turns off wholesale.

A released result is one a parent is holding. Correcting it is a **revision**,
which makes a new version and leaves this one standing; that is built separately
and neither guard is in its way, because a revision never moves this version out
of `released`.

This is why `ResultSheetTransition` stores `from_state` even though the previous
row's `to_state` implies it. The redundancy is what lets the log's half of the
rule be a check constraint on a single row rather than something that has to
walk the log — and a constraint needing no context is one no future query can
get wrong.

## Sending back

The task list described a forward path only. A chain that only goes forward does
not mean mistakes are not made; it means the fix is somebody editing the
database, which leaves no record that anything was ever wrong.

So `send_back()` is a real transition from `submitted`, `checked` or `approved`
to `draft`, taken by whoever could have said yes instead. It **requires a
reason** — refused by the service *and* by `a_send_back_says_why`, because a
refusal that does not say what is wrong sends a teacher back to forty-five
scores with no idea which one to look at.

The two layers have to refuse the *same inputs* to be worth calling two layers,
and at first they did not. `send_back()` compares `reason.strip()`; the
constraint was `~Q(reason="")`, which accepts three spaces. The gap was exactly
the caller the constraint exists for — the import, the psql session — and what
came out the other side was a send-back whose reason renders blank on a
teacher's screen, which is the failure the rule was written to prevent. It now
tests for a non-whitespace character, and the test tries `""`, `"   "` and
`"\t\n "` rather than only the first.

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

### A test that could not fail

`test_the_guard_still_bites_in_a_later_cycle` was written to prove the
same-signatory rule survives a send-back — the thing `cycle` exists for. It
walked the sheet to `approved` in cycle 1 and asserted a second `approve()` was
refused.

It was refused, but not by the guard. A sheet at `approved` fails the **state**
check first, several lines before `_require_not_already_signed()` is reached. So
the test passed with the guard deleted *and* with the unique constraint dropped,
and `OnePersonCannotTakeTwoSteps` only ever covers cycle 0 — meaning the
property the test was named for had no coverage anywhere in the suite.

What actually exercises it is one person taking **two different advancing steps
in the same later cycle**: Kemi, class teacher and acting vice principal,
submitting and then checking after a send-back. The sheet is at `submitted` and
`check()` expects `submitted`, so the state check passes and the signature rule
is the only thing that can refuse. There is a second test for the database half,
inserting the row directly, because the service-level one never reaches the
index.

The general shape is worth naming: **a test whose assertion is satisfied by an
earlier guard than the one it is named for.** Nothing about it looks wrong — it
is green, it is specific, and it mentions the right rule in its docstring.

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

### What the lock does *not* reach, and a claim that did not survive checking

A joined `SELECT ... FOR UPDATE` locks a row in **every** joined table, so an
ordering that walks a foreign key silently locks rows the statement never
writes. `ResultSheet.Meta.ordering` is `["term", "class_group"]` — two
relations — so this looked like a live bug: every transition holding the term
row, two principals approving two different classes in one term serialised on
something neither writes.

It is not, and the reason is worth recording because it is a fact about Django
that the obvious mental model gets wrong:

> **`QuerySet.get()` clears ordering itself.** Django 5.2 runs
> `clone = clone.order_by()` inside `get()` before compiling. `QuerySet.filter()`
> does not.

The SQL the tests actually capture is `FROM results_resultsheet WHERE id = %s
LIMIT 21 FOR UPDATE` — no join — with or without the `.order_by()` in
`_locked()`. Compiling `.filter(pk=...)` by hand shows the join and is what
makes the bug look real; it is a different code path.

The `.order_by()` stays anyway, and `LockScopeTests` stays with it, because the
property is one line from being lost. Rewriting `_locked()` as
`.filter(pk=...).first()` — a change nobody would describe as touching locking —
brings the join straight back, and both tests fail: one on the SQL, one on real
contention (`could not obtain lock on row in relation "academics_term"`). The
tests pin the property; the call is belt to Django's braces.

## Who may take which step

| Step | Roles |
| --- | --- |
| open a sheet | anyone who can take a step below |
| submit | teacher, admin |
| check | vice principal (academic) |
| approve | principal |
| release | principal |
| send back | vice principal (academic), principal |

An **administrator may submit** because entering and submitting a paper sheet is
office work in most schools — the reasoning `gradebook.MARK_ENTERING_ROLES`
gives for admitting one. An administrator **may not check**: that is the step the
chain exists for, and widening it would let the office both submit and check.

**Release was `{principal, admin}` and is now the principal's alone.** The
argument for the admin was a real one — release is commonly gated on something
clerical, fees settled and cards printed, rather than being a second academic
judgement. It was still wrong here, because it contradicted a decision already
taken for this phase, and the contradiction was load-bearing: task 8 makes
revision principal-only *on the stated grounds that release is the principal's
act*. Widening release would have left the next task's authority rule resting on
a premise this module had quietly stopped honouring. Narrowed rather than left
because the two resolutions are not symmetrical — narrowing costs a school an
inconvenience a later PR can undo, widening publishes forty-five children's
results on an authority nobody granted.

**Opening a sheet** is a write and therefore asks who is calling, which it did
not at first. `open_sheet()` was an actor-less primitive in a module whose
docstring argues there are none — exported, writing a tenant table, never
resolving the school on the connection. Its roles are the union of everyone who
can take a step: opening decides nothing, so it should not be narrower than the
narrowest step, and it should not admit anybody who cannot act on the row they
have just created.

Authority is asked at the school on the connection, and **the portal is refused
outright**. `_school_on_this_connection()` had no answer for the public schema:
a caller that never entered a tenant got `School.DoesNotExist`, which is outside
`ResultsError`, so a refusal arrived as a 500 — and where a
`School(schema_name="public")` row exists, as this codebase's own tests create,
the lookup *succeeded* and authority was checked against the portal's
memberships instead. `schools.logging.current_school()` had the rule already:
the public schema is the portal, not a customer.

Refusals name roles by **label**, not by stored value. `vp_academic` was
truncated to fit `Membership.role`'s sixteen characters precisely because nobody
was meant to read it, and the refusal was joining the raw keys.

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
| `.order_by()` removed from `_locked()` | **Nothing. Both lock-scope tests still pass.** | The joined-lock bug was never present: `QuerySet.get()` clears ordering itself. A finding that reads as obviously true can still be false, and the way to tell is to break it and look. |
| `_locked()` → `.filter(pk=...).first()` | `JOIN` in the locking SQL; `could not obtain lock on row in relation "academics_term"` | The same tests *do* bite on a real regression. They pin the property rather than the spelling, which is why they are kept even though the bug they were written for did not exist. |
| trigger moved from `BEFORE UPDATE` to `BEFORE DELETE` | `.update(state="draft")` on a released sheet succeeds; the sheet ends up `draft` with an audit that still says released | The sheet-level guard is doing real work, and the log constraint alone never held the rule. |
| `a_send_back_says_why` reverted to `~Q(reason="")` | a reason of `"   "` is accepted by the database and refused by the service | Two layers that refuse different inputs are one layer and a gap. |
| `_require_not_already_signed` neutered | the cycle-1 same-signatory test errors | The rewritten test reaches the guard. Its predecessor stayed green through the same break. |

The first two are the ones worth remembering for what they say about the code.
The third is worth remembering for what it says about **review**: it would have
been entirely natural to accept a well-argued finding, "fix" it, write the fix
up as a bug caught, and ship a docstring describing a bug the code never had.
The habit that caught it is the same one the first two come from — break the
thing you are about to take credit for, re-run, and read what happens.

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

**The other four `TransactionTestCase` classes do not have this problem**, and
the reason is worth writing down because it looks like an omission. Review
flagged them as leaking the same schemas; they do not. Every one of them builds
its schools through a helper that sets `auto_create_schema = False` —
`accounts/tests/test_transfer_concurrency.py`, `schools/tests/test_invitations.py`'s
`make_school()` used by both invitation-concurrency classes, and the
`School(schema_name="public")` portal rows in `test_invitation_api.py`, which
need no schema of their own. `test_signin_concurrency.py` creates no school at
all. No schema is created, so there is nothing to leak.

What *is* real is that the teardown is copied by hand into the two files that
do create schemas, and nothing structural stops the next one being written
without it. That is [issue #20](https://github.com/adedejimakinde/luffy-school-saas/issues/20),
not something this branch fixes.

## Not built here

- **The snapshot.** Release is the moment it is frozen; what gets frozen is
  task 3.
- **Revisions.** Task 8. The constraint above is written so that a revision
  makes a new version rather than moving this one.
- **Screens.** No HTTP surface, for the reason `fees.services` has none: the
  rules have to hold for an import too.
