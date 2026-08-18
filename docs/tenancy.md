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

so `confdeltype` is `a` — `NO ACTION`. Two consequences, and the second is the
dangerous one:

1. **Django's `on_delete` is not honoured across schemas.** The deletion
   collector walks Django model relations against the *currently connected*
   schema only. It cannot see, cascade into, or be protected by rows sitting in
   the other forty schools.
2. **The failure is deferred to `COMMIT`.** Because the constraint is
   `INITIALLY DEFERRED`, deleting a shared row referenced from tenant schemas
   *appears to succeed*. The `DELETE` returns cleanly, the code proceeds, and
   the transaction then blows up at commit time with an `IntegrityError` naming
   a table in a schema the connection was never pointed at. It cannot be caught
   at the point of the delete, because nothing goes wrong there.

Honest note on how strong that evidence is: the failure mode was reproduced by
hand-writing the exact DDL Django emits (verified against `sqlmigrate` output
above) rather than by building a real Django model with a real `ForeignKey`.
The mechanism is confirmed; the ergonomics of hitting it through the ORM are
inferred from the collector's design rather than observed end to end.

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
