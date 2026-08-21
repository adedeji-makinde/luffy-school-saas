# Schema-per-school: what is proven, and what is not

Companion to [membership.md](membership.md), which explains why identity is
shared. This one covers the other half: how a school's own data is kept apart,
what was actually verified against a real Postgres, and what a future person
adding a tenant-scoped model needs to know before they start.

## The mechanism, in one line

Isolation is the Postgres `search_path`, and nothing else:

| Connection scoped to | `search_path` | Consequence |
| --- | --- | --- |
| a school | `st_marys, public` | the school's own tables, plus every shared table |
| another school | `grace, public` | a *different* `academics_term`, same name |
| public | `public` | tenant tables are not reachable at all |

Two things fall out of that table, and both matter.

**A school's rows are not filtered away from other schools — they are in a
different table.** `SELECT * FROM academics_term` run against St Mary's and
against Grace Academy hits two physically distinct relations with different
`tableoid`s. There is no `school_id` column doing the work, and therefore no
query that can forget to include it.

**Shared models keep working inside a school's schema because `public` is the
second entry.** That is precisely what lets one login span several schools:
`accounts_user` resolves from inside `st_marys` even though no such table
exists there.

## What was actually tested

`schools/tests/test_tenant_isolation.py` — 23 tests. Nothing is mocked and
nothing skips schema creation: every `make_school()` leaves `auto_create_schema`
alone, so each one really does run `CREATE SCHEMA` followed by the full
`migrate_schemas` pass for `TENANT_APPS`. The assertions read `pg_namespace`,
`pg_tables`, `pg_indexes` and `pg_constraint` rather than taking Django's word.

`academics/tests/test_term.py` — 16 tests over the `Term` record itself, each
running inside a real school schema for the same reason. They cover the four
dates and the day count, the constraints that bound them, and two Django-level
traps that only show up against Postgres — see the notes below on date
subtraction and on `Func` in migrations. Still **no foreign keys**: the blocker
at the bottom of this document stands, and `Term` genuinely does not need one.

`schools/tests/test_cross_schema_fk.py` — 6 tests, holding the evidence for the
blocker at the bottom of this document. Real Django models with real foreign
keys into `public`, real `.delete()` calls, across two real schemas.

The same thing was also run by hand against the dev database first. `\dn` after
creating two real tenants through `School`/`Domain`:

```
   Name   |       Owner
----------+-------------------
 grace    | luffy_admin
 public   | pg_database_owner
 st_marys | luffy_admin
```

```
tables in st_marys : academics_term, django_content_type, django_migrations
tables in public   : accounts_user, accounts_membership, accounts_guardianship,
                     auth_*, django_* ... and zero academics_* tables
```

Isolation, proven from psql with no Django in the loop:

```sql
set search_path = st_marys, public;  select ... from academics_term;  -- 1 row
set search_path = grace,    public;  select ... from academics_term;  -- 0 rows
set search_path = public;            select ... from academics_term;
ERROR:  relation "academics_term" does not exist
```

That error is the point, and it is the one assertion in the suite worth
guarding jealously. **If querying a tenant table from public ever returns an
empty result instead of raising, the table has leaked into the shared schema
and "your school's data is isolated" has quietly become false.**

The rest of what passed:

- Both schools own a `2025/2026 First term` with different dates and neither
  collides, because the unique index is per-schema. A single shared table would
  need the school in every unique constraint by hand to manage the same thing.
- Table, unique constraint, partial unique index and check constraint are all
  created in each new schema — not just the table.
- A `Membership` created while connected to a school's schema resolves against
  the shared `accounts.User` and reads back correctly; a parent with children at
  two schools resolves from inside either one, including `has_access_to()` for
  the *other* school.
- Dropping one school takes only its own schema and leaves the other intact.

## The `Term` record

The one tenant-scoped model there is, and the shape every later school-owned
record will hang off. Six fields worth naming:

| Field | | |
| --- | --- | --- |
| `session` | `2025/2026` | the academic year, as a formatted string |
| `name` | first / second / third | with `(session, name)` unique per schema |
| `starts_on`, `ends_on` | | `ends_on > starts_on`, enforced |
| `next_term_starts_on` | nullable | what this term *announces* about the next |
| `school_days` | nullable | days actually taught, as the school counts them |
| `is_current` | | at most one per schema |

Two of those are worth explaining, because both look like they should be derived
and neither can be.

**`next_term_starts_on` is a statement, not a pointer.** A school prints "Next
term begins: 8 January" on the report card it hands out in December — at which
point next term's `Term` row usually does not exist, so a lookup would return
nothing at precisely the moment the date is wanted. It could not be derived
reliably even later: the term after 2025/2026 Third is 2026/2027 *First*, so
"the next term" crosses sessions, and `session` is a formatted string with no
ordering of its own. A `CheckConstraint` requires it to fall strictly after
`ends_on` when it is set — a day cannot belong to two terms.

**`school_days` is the school's count, not a calculation.** Weekends come out,
but so do mid-term break, public holidays that move year to year, sports day and
any day the school closed for weather. This number is the denominator of the
attendance figure on a report card, so a computed one that disagreed with the
school's own register would make every percentage wrong in a way nobody could
explain. It is bounded by the term it describes — at least 1, and no more than
`(ends_on - starts_on) + 1` — which is the constraint that turned up both
Postgres traps documented below. `Term.calendar_days` exposes that ceiling and
is deliberately *not* a default for `school_days`: they answer different
questions.

Both are nullable, because "not announced yet" and "not counted yet" are the
honest state for most of a term, and inventing a denominator to get a row saved
is worse than leaving it absent.

## What this does **not** prove

Worth stating plainly so nobody cites this document for more than it earned:

- **Domain routing.** `TenantMainMiddleware` mapping a hostname to a schema in a
  real request cycle is untested. The `Domain` row is only checked as data.
  `SchoolAccessMiddleware`'s own tests set `connection.tenant` by hand.
- **Migrating existing tenants.** Every schema here was created fresh. Adding a
  column later and rolling it across many existing schemas is a different code
  path and has never been run.
- **Scale.** Two schemas. Not fifty. Nothing here says anything about how long
  `migrate_schemas` takes at fifty, or about connection reuse under load.
- **Connection pooling.** `search_path` is per-connection state. Nothing here
  tests what a pooler that hands out connections mid-transaction would do to it.

## Writing tests for tenant-scoped models

Use a plain `TestCase` and create real schools in it. This is not the obvious
choice, so here is the reasoning.

`CREATE SCHEMA` and `migrate_schemas` work *inside* a `TestCase`'s per-test
transaction, and roll back completely — DDL is transactional in Postgres, so
the schema list returns to `['public']` with no cleanup code and no leaked
schemas. A `TransactionTestCase` is not needed, and plain `TestCase` also lets
you create *two* schools, which is the only way to test isolation at all.

`django_tenants.test.cases.TenantTestCase` exists and works, but it creates
exactly one tenant (schema `test`), which makes it structurally unable to prove
isolation. It is used in one class at the bottom of the test file purely to pin
its own behaviour. It carries two traps:

**Trap 1 — required fields are silently blank.** The harness constructs
`School(schema_name='test')` and saves it. `School.name` and `School.slug` are
required, but a blank `CharField` is `''` rather than `NULL`, so the row saves
happily with an empty name and an empty slug. Override `setup_tenant()` (and
`setup_domain()` if the domain needs fields) or you are testing against junk.

**Trap 2 — `setUpTestData` does not run.** This is the nastier one.
`TenantTestCase.setUpClass` never calls `super().setUpClass()`, so Django's
`TestCase` class-level setup — which is what invokes `setUpTestData` — is
skipped entirely. Fixtures written there are **silently absent** rather than
raising, so tests happily assert against nothing. Per-test transactions still
work, so use `setUp`. Both traps are pinned by
`TenantTestCaseHarnessTests`; if a django-tenants upgrade fixes either, those
tests fail and send someone back to this section.

## Things that surprised me

**`migrate_schemas` reports migrations it did not apply.** Creating a tenant
prints `Applying accounts.0001_initial... OK` against the *tenant* schema. It
did not create `accounts_user` there. `TenantSyncRouter.allow_migrate` returns
`False` for shared apps outside public, so every operation is skipped while the
bookkeeping row still lands in that schema's `django_migrations`. The output
reads exactly like a shared table was created per school. Verified otherwise:
zero `accounts_*` tables in `st_marys`.

**Saving a School leaves you on public.** `create_schema()` ends with
`connection.set_schema_to_public()`, so after `school.save()` you are *not*
inside the school you just made. Code that assumes otherwise writes to public.

**A missing relation poisons the transaction.** Querying a tenant table from
public raises `ProgrammingError`, and Postgres then refuses every subsequent
statement with `current transaction is aborted`. Any test asserting that error
must wrap it in `transaction.atomic()` so it takes a savepoint, or the rest of
the test dies somewhere confusing.

**A partial `UniqueConstraint` is an index, not a constraint.** `one_current_term`
has a `condition`, so Django implements it as a unique *index*. It shows up in
`pg_indexes` and never in `pg_constraint`. It is enforced identically; it just
is not where you would first look for it.

**Subtracting two `DateField`s gives you an interval, not a number of days.**
`F("ends_on") - F("starts_on")` renders as `interval '1 day' * (...)`, and
`ExpressionWrapper(..., output_field=IntegerField())` does **not** change that —
it looks like it settles the question and does not. Postgres then refuses the
surrounding arithmetic outright:

```
psycopg2.errors.UndefinedFunction: operator does not exist: interval + integer
```

Note when that lands: at `CREATE SCHEMA` time, not at the offending insert. A
check constraint written that way takes down the creation of every new school,
so it fails loudly — but nowhere near where it was written.
`academics.models.DaysBetween` spells the subtraction out with an explicit
template so the result is the `int4` Postgres actually returns.

**A `Func` built with `template=` / `arg_joiner=` will not survive a migration
round-trip.** Both arrive as `**extra`, and that dict's *key order* is part of
the expression's identity — so the instance in `models.py` and the identical
instance reconstructed from the migration compare unequal, and `makemigrations`
proposes dropping and recreating the constraint on every run. CI runs
`makemigrations --check`, so the symptom is a permanently red build with a
migration that never settles. Carry them as **class attributes** on a `Func`
subclass instead, where they never enter `extra`. `DaysBetween` does, and
`academics/tests/test_term.py` pins the round-trip so a "simplification" back to
an inline `Func(...)` fails in a test rather than in CI.

**`django.contrib.contenttypes` is in both lists.** It is in `SHARED_APPS` and
`TENANT_APPS`, so every school schema gets its own `django_content_type` table.
That is the django-tenants convention rather than an accident, but it means
content type IDs are per-schema and are not comparable across schools. Anything
built on generic foreign keys or on `ContentType` IDs as stable identifiers
needs to know that.

## HARD BLOCKER: tenant → shared foreign keys

**`academics.Term` deliberately has no foreign keys, and the next tenant-scoped
model must not add one back to `accounts` until this is resolved.** This is a
blocker on that work, not a nice-to-have.

The next model anyone writes here — attendance, fees, report cards — will want
a `ForeignKey` to `accounts.Membership` or `accounts.User`. It appears to work,
which is the problem.

What was measured. Postgres does allow a foreign key from a tenant schema into
`public`, and it binds correctly (`confrelid` resolves to `public.accounts_user`
from every schema). But **Django emits foreign keys with no `ON DELETE` clause
and `DEFERRABLE INITIALLY DEFERRED`** — confirmed straight from `sqlmigrate`:

```sql
ALTER TABLE "accounts_membership" ADD CONSTRAINT "..."
  FOREIGN KEY ("school_id") REFERENCES "schools_school" ("id")
  DEFERRABLE INITIALLY DEFERRED;
```

so `confdeltype` is `a` — `NO ACTION`, and `condeferred` is true.

This was measured end to end through the ORM, not inferred.
`schools/tests/test_cross_schema_fk.py` defines real Django models with real
`ForeignKey`s to `accounts.User` — one `CASCADE`, one `PROTECT` — creates their
tables in two real school schemas, and calls `.delete()`. (They are registered
for the life of that test class only and never migrated, because shipping a
tenant→shared foreign key is the thing this section forbids.) Three findings,
each worse than the last:

**1. `on_delete` is not honoured across schemas.** The deletion collector does
know about the relation — it is a real Django model and `related_objects`
includes it. It simply resolves the relation against the *currently connected*
schema. Deleting a user while connected to St Mary's cascades St Mary's rows
and never looks at Grace Academy's, which are left pointing at a row that no
longer exists.

**2. The delete is silent, and the failure lands at `COMMIT`.** Observed
directly: `user.delete()` raised `None`, the user really was removed from
`public.accounts_user`, and Grace Academy was still holding a live reference to
it. Nothing at all goes wrong at the point of the mistake. Only when the
deferred constraint is finally checked does it come apart:

```
user.delete() raised: None            <-- silent
grace rows still referencing the deleted user: 1
user gone from public: True
check_constraints() -> IntegrityError:
  insert or update on table "academics_probecascade" violates foreign key
  constraint "academics_probecascade_student_id_..._fk_accounts_user_id"
  DETAIL:  Key (student_id)=(7) is not present in table "accounts_user".
```

Note the wording, because it will cost someone an afternoon: Postgres reports a
deferred violation as **"insert or update on table"** even though the statement
was a `DELETE`, and names a table in a schema the connection was never pointed
at. The traceback points at the commit, not at the delete.

**3. `PROTECT` does not protect — and that is the worst part.** `PROTECT` works
by querying the referencing table, so from St Mary's that query finds nothing
and the delete proceeds. Measured: deleting a user referenced only from Grace
Academy, while connected to St Mary's, raised **no `ProtectedError` at all**. It
fails later as an `IntegrityError`, which is not what any calling code will be
catching. The guarantee most likely to be reached for as a safety net is
precisely the one that silently does not apply.

There is a fourth point that is really a warning about testing: **a
single-school deployment cannot reveal any of this.** With one school the
connected schema is the only schema, so cascade tidies everything and `PROTECT`
protects correctly. Every failure above appears the moment a second school
exists, and not one moment before. That is why this is a blocker rather than
something to notice in review.

Why it was not decided now: `Term` genuinely does not need it, and the decision
is much better made against a concrete model where the real `on_delete`
semantics are in front of you. Deferring it cost nothing. Forgetting it would
cost a data-integrity bug that only shows up at commit.

It is also asymmetric, which is the main argument for not guessing: going from
*no FK* to *FK* later is a cheap migration. Going from *FK* to *no FK* once
tenant data exists is not.

The three options, with what each actually buys:

- **Allow them.** Real referential integrity inside each schema. Defensible
  because this codebase already forbids deleting shared identity rows —
  `Membership` and `Guardianship` are `PROTECT`, and membership.md says to close
  relationships with `end()`, never `delete()`. If nothing is ever deleted the
  trap never fires. That is a convention holding up a data-integrity guarantee,
  which is worth naming out loud before relying on it.
- **Forbid them.** Tenant tables reference shared rows by bare id with no
  constraint. Each schema stays self-contained, so it can be dumped, restored
  and moved on its own — which matters if per-school backup or export is ever a
  requirement. Costs database-enforced integrity, `select_related`, and reverse
  accessors on every future tenant model.
- **Allow them, plus tooling.** Permit the FK and write a deletion path that
  iterates every schema. Keeps the integrity, pays for it in machinery that has
  to stay correct as schemas are added.

Whoever picks up the next tenant-scoped model decides this first, and records
the decision here.

### Decided: option 2, forbid them — proposed with `fees.FeeLedgerEntry`

**Status: proposed, not ratified.** This is a platform-wide policy being settled
by the first model that needed an answer, so it wants a second reader. What
follows is the reasoning; disagree with it in the PR rather than in a year.

`fees.FeeLedgerEntry` is the next tenant-scoped model, and it needs to say which
student an amount is against. It carries `student_membership_id` as a bare
`PositiveBigIntegerField` with **no foreign key**, and `recorded_by_id` the same
way. The three arguments that decided it, in order of weight:

**1. A foreign key would not deliver the guarantee this table most needs.** The
one thing a financial record must promise is that it cannot be destroyed as a
side effect of somebody being removed. `PROTECT` is the mechanism you would
reach for, and the measurements above show `PROTECT` silently does not protect
across schemas — the query it runs to find referencing rows is resolved against
whichever schema the connection happens to be on. `CASCADE` is worse: it would
delete one school's books and leave every other school's pointing at nothing,
with the breakage surfacing at `COMMIT`. So on this table the FK buys a
guarantee that is false exactly when it matters.

**2. Money has to be exportable per school.** A school's books being dumpable,
restorable and handed over on their own is a requirement for auditors, for a
school leaving the platform, and for anybody who has ever had to answer a
question about last year. Option 2 is what keeps a schema self-contained.

**3. It is the reversible direction.** This document already makes the
asymmetry argument: *no FK → FK* is a cheap migration, *FK → no FK* once tenant
data exists is not. Choosing option 2 first therefore keeps option 1 and option
3 open; choosing either of those first closes option 2.

What it costs, stated plainly rather than glossed: no database-enforced
integrity between an entry and the membership it names, no `select_related` onto
the student, and no reverse accessor. `FeeLedgerEntry` pays part of that back by
**freezing the identity it needs** — `student_name` and `student_reference` are
stored as they stood when the entry was posted, which a financial record wants
anyway. A receipt that rewrites itself when a school corrects a spelling is not
a receipt, so the join it is giving up is a join it should not have used.

Note what this does *not* forbid: **tenant → tenant** foreign keys are fine and
`FeeLedgerEntry` uses two of them (`term`, and `reverses` onto itself). Both
tables live in the same schema, so none of the above applies — and `PROTECT`
there really does protect, which a test pins.

### Half of option 3 now exists

`accounts/deletion.py` is the deletion path that iterates every schema. **This
does not decide the policy** — the blocker above still stands and no tenant model
may add a `ForeignKey` to `accounts` until someone chooses. It removes the
machinery from the cost of choosing, and it earns its place today regardless:
`Membership.user` is already `CASCADE`, so before this a plain `user.delete()`
silently took a person's memberships with it.

`hard_delete_user()` scans for references and refuses if it finds any, listing
the schemas. Two things learned while writing it, both easy to get wrong:

**Shared models must be counted once, in `public` — not once per schema.**
`schema_context` sets the `search_path` to `<tenant>, public`, so an unqualified
query for a shared table from inside a tenant reads the one public table again:

```
schema='grace'    search_path='grace, public' membership_count=1
schema='st_marys' search_path='st_marys, public' membership_count=1
public count: 1
```

That is one membership, at St Mary's, reported by both schools. A scan that
looped over schemas looking for `Membership` would name every school on the
platform as an offender for a user who belongs to one of them. So the scan
splits relations by where the table actually lives: `SHARED_APPS` once in
`public`, `TENANT_APPS` once per school.

**The delete itself cannot run from `public`** once any tenant model references
`User`. The collector walks every relation before deleting anything, so each
referencing table has to resolve on the `search_path` at that moment — and a
tenant-local table does not exist in `public`. It fails with `relation "..." does
not exist`: guard passed, delete still broken. Only a tenant's `search_path`
covers tenant-local and shared tables at once, which is why `_deletion_schema()`
picks one. Which school is irrelevant — the scan has already proven there is
nothing to collect in any of them.
