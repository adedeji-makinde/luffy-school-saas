"""Where a child came in their class, and in each subject.

Two numbers a Nigerian report card is judged on, and neither exists without a
class to be out of — which is what `academics.ClassPlacement` is for. Position
is reckoned over the **roster of the class for that term**, not over everybody
who happens to have a mark: a child who moved from JSS 1A to JSS 1B in January
is ranked among the children they were actually taught with.

## Dense ranking

Two children tied at 3rd means the next one is **4th, not 5th**. A tie does not
consume the position below it. Standard Nigerian practice, and the reason is
practical rather than mathematical: a school with a big tie at the top would
otherwise print "12th" on a card where eleven children are ahead of nobody.

    scores   88  74  74  61
    dense     1   2   2   3        <- what this module does
    standard  1   2   2   4        <- what it deliberately does not do

Hardcoded, and it may not stay that way — a school could reasonably want
standard competition ranking. `dense_positions()` is the one place it is
decided, so becoming a per-school setting is a change to one function rather
than to every caller.

## Ties are an equality test, so the number being compared has to be exact

Positions are decided by two children having *the same* score. That makes this
one of the few places where the difference between `Decimal` and `float`
changes an answer somebody reads: `float` equality on a computed percentage is
a coin toss on the last bit, and the visible symptom is two children printed
with identical percentages and different positions — which no teacher can
explain to a parent.

So percentages are `Decimal`, and **ranking compares the value as printed**,
quantised to two places before anything is sorted. Ranking on the unrounded
value and printing the rounded one is the same failure wearing a different hat:
75.004 and 74.996 both print as 75.00 and would be given different positions.

## "Not marked" is not zero

`gradebook` keeps that distinction in the table by having no row at all, and
this module has to keep it in the arithmetic. A child with no marks in a
subject has **no position** in it — `None`, not last. A child with no marks at
all has no overall position either.

Ranking them last would be a specific lie: it says the school assessed them and
they scored nothing. A child off sick for the term, or one who joined in week
ten, would be printed bottom of the class on a card that goes home.

## The overall average is the child's own, across their subjects

Settled for this phase, and worth stating because there are two defensible
readings and they disagree:

    Maths    10/10  = 100%
    English  40/80  =  50%

    mean of the subject percentages   = 75.0%   <- what this module does
    total scored over total available = 55.6%   (50/90)

The first is what "average across their subjects" means on a Nigerian report
card, and it is the one that does not let a subject with a large `max_score`
quietly outweigh the rest. The second is a weighted average pretending not to
be one.

It is **not** a class average. That is computed on demand and is staff-only,
for the reason `class_average()` gives.

## Who may see a position

Staff, and no one else — see `results.api`. Not a rendering preference: Nigerian
secondary schools do not print position on report cards, and parents and
students see the cumulative average only. The rule is enforced at the serializer
rather than the template, because omitting a field from a card while leaving it
in the JSON is the same leak with an extra step.
"""

from decimal import Decimal
from typing import Mapping

from django.db.models import Sum

from academics.models import ClassPlacement
from gradebook.models import Score

#: Two decimal places, which is what a report card prints. Ranking compares the
#: quantised value, so what decides a tie is the number a parent can see.
PLACES = Decimal("0.01")

#: The percentage every score is expressed as before anything is compared.
FULL_MARKS = Decimal(100)


def _percentage(scored: int, available: int) -> Decimal | None:
    """A mark as a percentage, or `None` when there is nothing to divide by.

    `available` is zero exactly when the child has no marks, because it sums
    the `max_score` of the assessments they were *actually marked on* — the
    rule `ScoreQuerySet.total_for()` sets out. Returning `None` rather than
    `Decimal(0)` is what keeps "not marked" out of the ranking.
    """
    if not available:
        return None
    return (Decimal(scored) * FULL_MARKS / Decimal(available)).quantize(PLACES)


def roster_ids(class_group, term) -> list[int]:
    """Who sat in this class this term. The set a position is out of."""
    return ClassPlacement.objects.student_ids(class_group, term)


def _subject_totals(term, student_ids) -> dict[tuple[int, int], tuple[int, int]]:
    """`(student, subject) -> (scored, available)`, in one query.

    `.order_by()` is load-bearing and is not a leftover. `Score.Meta.ordering`
    is `["assessment", "student_membership_id"]`, and Django appends ordering
    columns to the GROUP BY of a `.values().annotate()` — so without clearing
    it the rows come back grouped by assessment as well, which is one row per
    mark and a "total" that is just the mark. `gradebook.api._totals_for_everyone`
    carries the same note, and it is the same bug both times.
    """
    if not student_ids:
        return {}
    grouped = (
        Score.objects.filter(
            assessment__term=term, student_membership_id__in=student_ids
        )
        .values("student_membership_id", "assessment__subject_id")
        .annotate(scored=Sum("value"), available=Sum("assessment__max_score"))
        .order_by()
    )
    return {
        (row["student_membership_id"], row["assessment__subject_id"]): (
            row["scored"] or 0,
            row["available"] or 0,
        )
        for row in grouped
    }


def subject_percentages(class_group, term, subject_id) -> dict[int, Decimal]:
    """Every rostered child's percentage in one subject.

    Children with no mark in it are **absent from the mapping** rather than
    present with a zero or a `None`. A caller ranking this gets only the
    children who can be ranked, and a caller displaying it asks with `.get()`
    and renders a blank.
    """
    students = roster_ids(class_group, term)
    totals = _subject_totals(term, students)
    percentages = {}
    for student_id in students:
        scored, available = totals.get((student_id, subject_id), (0, 0))
        percentage = _percentage(scored, available)
        if percentage is not None:
            percentages[student_id] = percentage
    return percentages


def overall_percentages(class_group, term) -> dict[int, Decimal]:
    """Every rostered child's own average across the subjects they were marked in.

    Not a class average, and not a weighted one — see the module docstring for
    the worked example that separates the two readings. Children with no marks
    at all are absent from the mapping, for the reason `subject_percentages()`
    leaves them out of a single subject.
    """
    students = roster_ids(class_group, term)
    totals = _subject_totals(term, students)

    per_student: dict[int, list[Decimal]] = {student: [] for student in students}
    for (student_id, _subject_id), (scored, available) in totals.items():
        percentage = _percentage(scored, available)
        if percentage is not None:
            per_student[student_id].append(percentage)

    averages = {}
    for student_id, subjects in per_student.items():
        if not subjects:
            continue
        averages[student_id] = (sum(subjects) / Decimal(len(subjects))).quantize(
            PLACES
        )
    return averages


def dense_positions(values: Mapping[int, Decimal]) -> dict[int, int]:
    """Dense ranking, highest first: 1, 2, 2, 3.

    The single place the tie rule is decided. A school wanting standard
    competition ranking (1, 2, 2, 4) changes this function and nothing else.

    Ties are found by equality on the `Decimal` handed in, so a caller that
    quantises differently from `_percentage()` would get a different answer —
    which is why nothing else in this module rounds.
    """
    ordered = sorted(set(values.values()), reverse=True)
    position_of = {value: index + 1 for index, value in enumerate(ordered)}
    return {student: position_of[value] for student, value in values.items()}


def subject_positions(class_group, term, subject_id) -> dict[int, int]:
    """Position in one subject, out of the class roster for that term."""
    return dense_positions(subject_percentages(class_group, term, subject_id))


def class_positions(class_group, term) -> dict[int, int]:
    """Position in class, on the child's own average across their subjects."""
    return dense_positions(overall_percentages(class_group, term))


def class_average(class_group, term) -> Decimal | None:
    """The class's average of its children's averages. **Staff only.**

    Computed here and deliberately **never stored**, on the reasoning settled
    for this phase: a stored copy is a fact about forty-five other children,
    and a later revision to any one of them would leave a released card
    carrying a number that disagrees with the rows it claims to summarise.
    Position is the opposite case and *is* frozen at release — it depends on
    everyone else's scores at that moment and cannot be recomputed later
    without changing.

    `None` when nobody in the class has a mark, which is the honest answer and
    the one a caller can render as a dash. Zero would claim the class sat
    exams and scored nothing.
    """
    averages = overall_percentages(class_group, term)
    if not averages:
        return None
    return (sum(averages.values()) / Decimal(len(averages))).quantize(PLACES)


__all__ = [
    "class_average",
    "class_positions",
    "dense_positions",
    "overall_percentages",
    "roster_ids",
    "subject_percentages",
    "subject_positions",
]
