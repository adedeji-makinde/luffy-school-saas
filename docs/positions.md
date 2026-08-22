# Position in class, and who is allowed to see it

Two numbers a Nigerian report card is judged on — a child's position in the
class and their position in each subject — plus the two averages that are easy
to confuse with each other. `results/positions.py` computes them;
`results/api.py` decides who may look.

## The unit is the class roster for the term

A position is *out of something*, and the something is
`academics.ClassPlacement` for that `(class_group, term)`. Not "everybody with a
mark in this subject", which would rank a JSS 1A child against JSS 1B; not the
session, because a child who moves from 1A to 1B in January has to be ranked
among the children they were actually taught with.

This is why `ClassGroup` and `ClassPlacement` were built first. Before them
there was no denominator.

## Dense ranking

    scores   88  74  74  61
    dense     1   2   2   3      <- what this does
    standard  1   2   2   4      <- what it deliberately does not do

A tie does not consume the position below it. Standard Nigerian practice, and
the practical argument is the tie at the top: a class where eleven children draw
first would otherwise print "12th" on a card where nobody is ahead of the child
holding it.

Hardcoded for now. It may reasonably become a per-school setting, and
`dense_positions()` is the single place it is decided — a school switching to
standard competition ranking changes one function and no caller.

## Ties are an equality test, so the type matters

Positions are decided by two children having **the same** score, and that makes
this one of the few places where `Decimal` versus `float` changes a number
somebody reads. Float equality on a computed percentage turns on the last bit;
the visible symptom is two children printed with identical percentages and
different positions, which no teacher can explain to a parent.

So percentages are `Decimal`, and **ranking compares the value as printed** —
quantised to two places *before* anything is sorted. Ranking on the unrounded
value and printing the rounded one is the same bug wearing a different hat:
75.004 and 74.996 both print as 75.00 and would be given different positions.

`45/60` and `15/20` are both `75.00` by different arithmetic, and there is a
test that they share a position.

## "Not marked" is not zero, in the arithmetic as well as the table

`gradebook` keeps the distinction by having no `Score` row at all. This module
has to keep it in the ranking:

- a child with no marks in a subject has **no position** in it, not the last one;
- a child with no marks at all has no overall position and no average;
- an unmarked child does **not** drag the class average down.

Ranking them last is a specific lie — it says the school assessed them and they
scored nothing. A child off sick for the term, or one who joined in week ten,
would be printed bottom of the class on a card that goes home.

The control run for this is in the table below and is worth reading: the naive
"treat missing as zero" fix moves a class average from 80.00 to 40.00.

## Two averages, and they are different questions

**The child's own average** is the mean of their subject percentages. It is what
a report card shows and what class position is ranked on.

There are two defensible readings of "average across their subjects" and they
disagree:

|  | Maths 10/10 | English 40/80 | result |
| --- | --- | --- | --- |
| mean of subject percentages | 100% | 50% | **75.0%** ← this |
| total scored ÷ total available | | | 55.6% (50/90) |

The first is what the phrase means on a Nigerian card, and it is the one that
does not let a subject with a large `max_score` quietly outweigh the rest of the
term. The second is a weighted average pretending not to be one.

**The class average** is the mean of those, across the class. It is
**computed on demand and never stored**, and the asymmetry with position is the
point:

| | frozen at release? | why |
| --- | --- | --- |
| position | **yes** (task 3) | it depends on everyone else's scores *at that moment* and cannot be recomputed later without changing |
| class average | **no**, recomputed | a stored copy is a fact about forty-five other children, and a later revision to any one of them leaves a released card carrying a number that disagrees with the rows it claims to summarise |

## Position is staff-only, and that is a rule about payloads

> Nigerian secondary schools do not print position on report cards. Parents and
> students see the cumulative average only. Schools use it internally.

Visible to **teachers, the academic vice principal, principals and school
administrators**. Not to parents, not to students — and not to a bursar, who
keeps the books. The class average is staff-only on the same reasoning.

Enforced at the **router**, which is the earliest point available, because the
failure mode is not a template. Omitting a field from a card while the JSON
still carries it is the same leak with an extra step, and the way that happens
is a family-facing view reusing a schema written for staff. There is no schema
in `results/api.py` a family-facing route could reuse.

Refusals are a **flat 404**, not a 403, on the reasoning `gradebook.api`'s
`ExistenceOracleTests` settled: a parent who could tell "you may not read this"
from "no such class" could map the school's whole class list by walking ids.
Both answers are the same bytes, and the authority check runs before the class
or term is looked up so the refusal cannot depend on whether they exist.

Two refusals come from **other layers**, and the tests say so rather than
implying this view is the only gate:

- an **unauthenticated** caller gets 401 from the router's `session_auth`, which
  answers before the view. No oracle either — it comes back whether or not the
  class exists.
- **another school's principal** gets 403 from `SchoolAccessMiddleware`, at the
  door, before routing.

### What is not yet proven

The decision says position must be absent from the parent card and the
unauthenticated result-checker payload. **Neither surface exists**, so today the
rule holds by there being no family-facing payload at all — a weaker guarantee
than it sounds, and one that becomes false the moment either lands. Tracked as
[issue #21](https://github.com/adedejimakinde/luffy-school-saas/issues/21),
which carries what has to be true when they do. Building a placeholder now would
mean building it against live tables, which task 6 is explicitly forbidden from
doing.

## Read from live marks, for now

The broadsheet reads live `Score` rows, and there is a test asserting a new mark
changes a position immediately — stated so the *absence* of freezing is a
recorded fact rather than an assumption.

Once task 3 lands, **a released term must be served from the snapshot instead**.
A position recomputed after release can silently disagree with the card a parent
is holding, which is the whole reason position is the number that gets frozen.

## Control experiments

The method as ever: break one thing deliberately, re-run, read the failure.

| Broken | Result | What it proved |
| --- | --- | --- |
| dense ranking → standard competition ranking | `{1: 1, 2: 2, 3: 2, 4: 4} != {1: 1, 2: 2, 3: 2, 4: 3}` | the tie rule is actually asserted, not assumed |
| `_percentage(0, 0)` returns `Decimal(0)` instead of `None` | only **one** of four "not marked" tests failed | the first control was in the wrong place — `overall_percentages()` never calls it for a child with no rows at all. A passing control is information too. |
| unmarked children given an average of `0` in `overall_percentages()` | three tests fail; the class average moves **80.00 → 40.00** | the plausible wrong version *is* caught, and the cost of getting it wrong is a number on every card in the class |
| `Role.PARENT` added to `POSITION_VIEWING_ROLES` | parent gets `200` with `"position": 1` and `"class_average": "74.50"` in the body | the visibility rule is enforced where the tests say it is |

The second row is the one worth keeping. The first attempt at that control
passed three tests it should have broken, which said nothing about the code and
everything about where the control was aimed — the naive-zero bug lives in the
aggregation, not in the percentage helper. A control that fails to break
anything is a result, not a formality.

## `.order_by()` on the aggregate, again

`_subject_totals()` clears ordering before `.values().annotate()`, and it is
load-bearing. `Score.Meta.ordering` is `["assessment", "student_membership_id"]`,
and Django appends ordering columns to the `GROUP BY` — so without it the rows
come back grouped by assessment as well, which is one row per mark and a "total"
that is just the mark. `gradebook.api._totals_for_everyone()` carries the same
note. Same bug, third app.
