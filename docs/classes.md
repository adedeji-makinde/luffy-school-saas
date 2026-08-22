# Classes, and who sits in them

Two tables in `academics`: `ClassGroup` — "JSS 1A", the group a school teaches —
and `ClassPlacement` — which group one child sat in for one term.

They exist for a narrow reason. **Position in class and any class-wide average
are computed against a group of children, and neither has a denominator without
a roster.**

Both of those are **staff-only** numbers. Nigerian secondary schools do not
print position on a report card — a parent and a student see the child's own
cumulative average across their subjects, and nothing that ranks them against
anybody else. The school uses position internally, so it is computed and frozen;
it simply never reaches a parent-facing page or payload. What a roster buys the
report card itself is therefore indirect: it is what makes a class a thing that
can be marked, ranked and reported on at all.

`gradebook.Assessment` had already noticed the gap and refused to fill it:

> Belongs to a (term, subject). Deliberately *not* to a class or a stream —
> there is no class model in this project yet, and inventing one here would be
> guessing at how a school groups its children.

It is no longer a guess, because the results phase cannot be built without one.

## The shape

`ClassGroup` is **not** tied to a session or a term. "JSS 1A" is the same group
year after year; who sits in it is what changes. Putting a session on the group
instead would mean a fresh row every year and every historical placement
pointing at a different one — so "how did JSS 1A do last year?" would have to be
asked of a different class, which is not what anybody means by the question.

`name` is stored rather than composed from a level and an arm, on the same
reasoning `Subject.code` is stored rather than slugged from `Subject.name`:
schools spell it their own way — "JSS 1A", "Primary 4 Gold", "Year 7 Blue" — and
a generated name would fight them. `level` carries the school's own ordering
because no string rule survives a school that runs Nursery, Primary and Senior
in one place: "JSS 1A" sorts before "JSS 10A" as text. It is not unique — a year
with three arms has three groups at one level.

`ClassPlacement` is **per term, not per session**, and that is the decision the
table turns on. Everything a report card is reckoned from is already per term,
and a child who moves from JSS 1A to JSS 1B in January must be ranked in the
group they were actually taught in. A session-scoped placement would have to be
*edited* to describe that move — and that edit would reach backwards into a term
that had already been released, changing which roster a frozen result was
reckoned against. In a phase whose whole premise is that a released result looks
the same in three years, a table that can be edited into disagreeing with what
was released is the wrong table.

## One group per child per term

`one_class_placement_per_student_per_term` is the load-bearing constraint. A
child in two groups at once does not read as corrupt anywhere — it reads as two
perfectly ordinary rows — and produces two positions in class and two class
averages for one term, with nothing to say which is the one to print.

It is enforced in the database, not only in `services.place_student()`, because
an import, a data migration and a shell session all write rows directly. A test
bypasses the service and asserts the table refuses it.

It is also the backstop for the race. Two administrators placing the same child
into different arms at the same instant both find no row and both insert;
`place_student()` catches the resulting `IntegrityError` and reports it as
`AlreadyPlaced`, naming the group that won. **Only that one constraint**, matched
by name through psycopg2's `diag` — the reasoning is
`gradebook.services._is_the_first_mark_colliding()`'s, and it matters for the
same reason: labelling any integrity failure a conflict sends the caller round a
reload loop that cannot terminate, and buries a real fault behind a
routine-looking refusal.

One thing there was measured rather than assumed, and did not behave the way it
reads: Django declares PostgreSQL foreign keys `DEFERRABLE INITIALLY DEFERRED`,
so placing into a `ClassGroup` that does not exist **does not raise inside
`place_student()` at all**. The insert succeeds and the violation surfaces at
commit. The guard still earns its place — it is what keeps that from being
relabelled if a future migration makes those keys immediate — and the test says
so out loud rather than implying a refusal that never happens.

## Placing and moving are two calls

`place_student()` refuses a child who is already placed that term, **including
one already in the group being asked for**. That last case looks like it should
be a no-op and is not: two administrators each believing they made the placement
is a real disagreement about who did what, and `placed_by_id` would name
whichever of them lost.

`move_student()` is the one that changes an existing placement, and it takes the
row lock before reading it — read-modify-write on one row is the shape that
quietly loses one of two simultaneous moves. Moving a child into the group they
are already in *is* a no-op here, because the end state asked for is the end
state that holds and a retried request should not fail for having succeeded.
The two functions give opposite answers to the same input on purpose: placing
twice is a claim about who placed them, moving twice is a claim about where they
sit, and they already sit there.

## Carrying a term forward

Without `carry_forward_placements()`, the first day of every term is a school
with no rosters, and every position and average has no denominator until
somebody re-types forty-five children per group. That is not a convenience; it
is the difference between per-term placement being viable and being quietly
abandoned.

It **does not promote**. Each child is carried into the same group they were in,
because moving JSS 1A into JSS 2A is a decision about who passed and this
function does not get to make it.

It **does not overwrite** a placement already made by hand in the target term,
which is the ordinary order these two things happen in: a school moves one child
into next term's correct group, and somebody runs the carry-forward afterwards.
Running it twice makes nothing and returns 0.

It **leaves behind children whose membership has ended**. A placement is not
evidence that somebody is still enrolled — it is a record of where they sat last
term, and it stays true for the report card of the term it belongs to. Copying
it forward is a different claim, and for a child who graduated or transferred in
December it is a false one: they would appear on January's roster, be counted in
the class size, and drag the class average towards a mark nobody was ever going
to give them. Their *existing* placement is untouched — leaving is a fact about
the future, not a reason to rewrite the past.

The predicate is `LIVE_STATUSES`, not `ACCESS_STATUSES`: the question is whether
the child is still ours, not whether they can sign in. A suspended student is
still enrolled and still has a report card coming.

## Who may place

`PLACEMENT_ROLES` is `{principal, admin}` — narrower than
`gradebook.MARK_ENTERING_ROLES`, which includes teachers, and the difference is
the point. A teacher enters marks for the children in front of them; *which*
children those are is not a teacher's decision. Moving a child between arms
changes whose class average they count towards and whose position they displace.

Platform staff are not admitted, on the reasoning
`gradebook.services.can_enter_marks()` set out: deciding which class a child sits
in is the school's own act, and `placed_by_id` would name a platform operator on
the row.

Authority is access-scoped, so a suspended administrator has a membership and no
authority — and it is asked at the *child's* school, which
`_require_student_of_this_school()` then pins to the schema being written. Both
questions have to be asked and they are not the same one.

## The bare id, and where the check now lives

`student_membership_id` carries no foreign key, on the policy settled in
[tenancy.md](tenancy.md) and already applied by `fees.FeeLedgerEntry` and
`gradebook.Score`: `on_delete` is resolved against whichever schema the
connection is on, so `PROTECT` does not protect and `CASCADE` cascades one
school's rows only.

A foreign key would not have helped anyway. `Membership` is shared, so a key into
it constrains only that the row *exists* — every school's children are in that
one table. The school half has to be asked in code however the column is
declared, and `academics.services` asks it before anything is written.

That check used to be copied into `fees` and `gradebook`, each copy's docstring
saying the same thing:

> If a third tenant app needs it, it moves to `accounts` — where the
> `Membership` it asks about already lives — rather than one tenant app
> importing another.

`ClassPlacement` is the third, so it moved. `accounts.students` now holds the one
definition, and **raises nothing** — it returns a sentence or `None`. That split
is what lets each app keep its own hierarchy: `fees` still raises
`NotThisSchoolsStudent(FeeLedgerError)` and `gradebook` still raises
`NotThisSchoolsStudent(GradebookError)`, so `except FeeLedgerError` still means
"the entry was not posted". The rule is shared; the refusing is not.

## Tested with two schools, never one

Every fixture in `academics/tests/test_classes.py` builds St Mary's *and* Grace
Academy, both as real schemas. A single-tenant test cannot fail for any of the
reasons this table is most likely to be got wrong: a uniqueness constraint that
should be per-schema, a roster query missing its `term` filter, a placement
written on the wrong connection.

One test asserts something that looks like a bug and is not: St Mary's first
class group and Grace Academy's first class group are **both id 1**. Each schema
has its own sequence. That is the trap under every bare id in this codebase — an
id carried across the boundary does not fail to resolve, it resolves to somebody
else's row — and it is worth a test that states it rather than a comment
somewhere hoping to be read.

## Not built here

- **Promotion.** `level` exists to make it expressible, and nothing computes it
  yet. It is a decision about who passed, and it belongs with the combined
  three-term view.
- **A subject-to-group link.** `Assessment` still belongs to a (term, subject)
  and not to a group. Nothing in the results phase needs it yet, and adding it
  now would be the same guess this table was built to stop making.
- **Screens.** No HTTP surface, for the reason `fees.services` has none: the
  rules have to hold for an import too, and a rule that lives in a view only
  holds for the view.
