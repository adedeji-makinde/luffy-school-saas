"""Position in class and in subject, and the three ways ranking goes wrong.

Two schools throughout, as everywhere in this project: a position is *out of a
class*, and the class roster comes from `academics.ClassPlacement`, so "did this
ranking reach into the other school's children" is a question only a second
tenant can ask.

The properties, one section each:

- dense ranking, where a tie does not consume the position below it;
- ties decided on the number as printed, not on an unrounded one;
- an unmarked child has no position rather than the last one;
- the overall average is the child's own across their subjects, not a weighted
  total and not the class's.
"""

import contextlib
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django_tenants.utils import schema_context

from academics.models import ClassGroup, Term, TermName
from academics.services import place_student
from accounts.models import Membership, Role, User
from accounts.services import enroll_student, grant_membership
from gradebook.models import Assessment, Score, Subject
from results import positions
from schools.models import School

PASSWORD = "correct-horse-battery"


def make_school(name, slug, schema_name):
    school = School(name=name, slug=slug, schema_name=schema_name)
    school.save()
    return school


@contextlib.contextmanager
def connected_to(school):
    with schema_context(school.schema_name):
        yield


class PositionSetUp(TestCase):
    """St Mary's and Grace Academy, each with a JSS 1A and a term."""

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

        self.principal = User.objects.create_user(
            "tunde", PASSWORD, full_name="Tunde Alabi"
        )
        grant_membership(self.principal, self.stmarys, Role.PRINCIPAL)
        self.their_principal = User.objects.create_user(
            "chidi", PASSWORD, full_name="Chidi Okafor"
        )
        grant_membership(self.their_principal, self.grace, Role.PRINCIPAL)

        self.term_id, self.group_id, self.maths_id, self.english_id = self._academics(
            self.stmarys
        )
        (
            self.their_term_id,
            self.their_group_id,
            self.their_maths_id,
            self.their_english_id,
        ) = self._academics(self.grace)

    def _academics(self, school):
        with connected_to(school):
            term = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            )
            group = ClassGroup.objects.create(name="JSS 1A", level=1)
            maths = Subject.objects.create(name="Mathematics", code="MTH")
            english = Subject.objects.create(name="English", code="ENG")
            return term.pk, group.pk, maths.pk, english.pk

    def enrol(self, school, username, full_name, group_id, term_id):
        """A child of this school, placed in this class for this term."""
        user = User.objects.create_user(username, PASSWORD, full_name=full_name)
        membership = enroll_student(user, school)
        with connected_to(school):
            place_student(
                ClassGroup.objects.get(pk=group_id),
                Term.objects.get(pk=term_id),
                membership,
            )
        return membership

    def mark(self, school, term_id, subject_id, membership, value, out_of=100):
        """One mark, in its own assessment so `out_of` is per call."""
        with connected_to(school):
            term = Term.objects.get(pk=term_id)
            assessment, _ = Assessment.objects.get_or_create(
                term=term,
                subject_id=subject_id,
                name=f"Exam out of {out_of}",
                defaults={"max_score": out_of},
            )
            Score.objects.create(
                assessment=assessment,
                student_membership_id=membership.pk,
                value=value,
            )

    def group_and_term(self, group_id, term_id):
        return ClassGroup.objects.get(pk=group_id), Term.objects.get(pk=term_id)


class DenseRankingTests(PositionSetUp):
    def test_a_tie_does_not_consume_the_position_below_it(self):
        """88, 74, 74, 61 places 1, 2, 2, 3 — not 1, 2, 2, 4."""
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        bola = self.enrol(self.stmarys, "bola", "Bola B", self.group_id, self.term_id)
        chika = self.enrol(self.stmarys, "chika", "Chika C", self.group_id, self.term_id)
        dele = self.enrol(self.stmarys, "dele", "Dele D", self.group_id, self.term_id)

        for student, value in ((ada, 88), (bola, 74), (chika, 74), (dele, 61)):
            self.mark(self.stmarys, self.term_id, self.maths_id, student, value)

        with connected_to(self.stmarys):
            placed = positions.subject_positions(
                *self.group_and_term(self.group_id, self.term_id), self.maths_id
            )

        self.assertEqual(
            {placed[ada.pk], placed[bola.pk], placed[chika.pk], placed[dele.pk]},
            {1, 2, 3},
        )
        self.assertEqual(placed[ada.pk], 1)
        self.assertEqual(placed[bola.pk], 2)
        self.assertEqual(placed[chika.pk], 2)
        self.assertEqual(
            placed[dele.pk], 3, "a tie consumed the position below it"
        )

    def test_everyone_tied_is_all_first(self):
        """The degenerate case, which standard ranking also gets right and which
        is worth pinning because an off-by-one in the tie branch shows up here."""
        students = [
            self.enrol(self.stmarys, f"s{n}", f"S {n}", self.group_id, self.term_id)
            for n in range(4)
        ]
        for student in students:
            self.mark(self.stmarys, self.term_id, self.maths_id, student, 70)

        with connected_to(self.stmarys):
            placed = positions.subject_positions(
                *self.group_and_term(self.group_id, self.term_id), self.maths_id
            )

        self.assertEqual(set(placed.values()), {1})

    def test_the_rule_lives_in_one_function(self):
        """`dense_positions` is where a school switching to standard ranking
        would change one thing, so it is asserted directly as well as through
        the query path."""
        self.assertEqual(
            positions.dense_positions(
                {1: Decimal("88"), 2: Decimal("74"), 3: Decimal("74"), 4: Decimal("61")}
            ),
            {1: 1, 2: 2, 3: 2, 4: 3},
        )


class TiesAreDecidedOnThePrintedNumberTests(PositionSetUp):
    def test_two_children_printing_the_same_percentage_share_a_position(self):
        """45/60 and 15/20 are both 75.00, by different arithmetic.

        The failure this guards is specific: rank on an unrounded value, print a
        rounded one, and two children show identical percentages with different
        positions — which no teacher can explain to a parent.
        """
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        bola = self.enrol(self.stmarys, "bola", "Bola B", self.group_id, self.term_id)

        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 45, out_of=60)
        self.mark(self.stmarys, self.term_id, self.maths_id, bola, 15, out_of=20)

        with connected_to(self.stmarys):
            group, term = self.group_and_term(self.group_id, self.term_id)
            percentages = positions.subject_percentages(group, term, self.maths_id)
            placed = positions.subject_positions(group, term, self.maths_id)

        self.assertEqual(percentages[ada.pk], percentages[bola.pk])
        self.assertEqual(placed[ada.pk], placed[bola.pk])

    def test_percentages_are_decimals_not_floats(self):
        """Ties are an equality test, so the type is part of the rule.

        A float percentage makes equality a coin toss on the last bit, and the
        symptom is a tie that is a tie on one school's data and not on another's.
        """
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 1, out_of=3)

        with connected_to(self.stmarys):
            percentages = positions.subject_percentages(
                *self.group_and_term(self.group_id, self.term_id), self.maths_id
            )

        self.assertIsInstance(percentages[ada.pk], Decimal)
        self.assertEqual(percentages[ada.pk], Decimal("33.33"))


class NotMarkedIsNotZeroTests(PositionSetUp):
    def test_a_child_with_no_marks_has_no_position_rather_than_the_last_one(self):
        """Ranking them last says the school assessed them and they scored
        nothing. A child off sick for the term would be printed bottom of a card
        that goes home."""
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        absent = self.enrol(
            self.stmarys, "ngozi", "Ngozi N", self.group_id, self.term_id
        )
        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 80)

        with connected_to(self.stmarys):
            group, term = self.group_and_term(self.group_id, self.term_id)
            placed = positions.class_positions(group, term)
            averages = positions.overall_percentages(group, term)

        self.assertEqual(placed[ada.pk], 1)
        self.assertNotIn(absent.pk, placed)
        self.assertNotIn(absent.pk, averages)

    def test_an_unmarked_child_does_not_drag_the_class_average_down(self):
        """The same rule, seen from the number staff read off the broadsheet."""
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        self.enrol(self.stmarys, "ngozi", "Ngozi N", self.group_id, self.term_id)
        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 80)

        with connected_to(self.stmarys):
            average = positions.class_average(
                *self.group_and_term(self.group_id, self.term_id)
            )

        self.assertEqual(average, Decimal("80.00"))

    def test_a_class_where_nobody_is_marked_has_no_average_at_all(self):
        self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)

        with connected_to(self.stmarys):
            group, term = self.group_and_term(self.group_id, self.term_id)
            self.assertIsNone(positions.class_average(group, term))
            self.assertEqual(positions.class_positions(group, term), {})

    def test_a_child_marked_in_one_subject_is_ranked_in_that_one_only(self):
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 80)

        with connected_to(self.stmarys):
            group, term = self.group_and_term(self.group_id, self.term_id)
            self.assertIn(ada.pk, positions.subject_positions(group, term, self.maths_id))
            self.assertNotIn(
                ada.pk, positions.subject_positions(group, term, self.english_id)
            )


class TheAverageIsTheChildsOwnTests(PositionSetUp):
    def test_it_is_the_mean_of_subject_percentages_not_a_weighted_total(self):
        """Maths 10/10 and English 40/80 is **75%**, not 55.56%.

        The two readings of "average across their subjects" disagree whenever
        subjects have different `max_score`, and the weighted one lets a long
        paper quietly outweigh the rest of the term.
        """
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 10, out_of=10)
        self.mark(self.stmarys, self.term_id, self.english_id, ada, 40, out_of=80)

        with connected_to(self.stmarys):
            averages = positions.overall_percentages(
                *self.group_and_term(self.group_id, self.term_id)
            )

        self.assertEqual(averages[ada.pk], Decimal("75.00"))
        self.assertNotEqual(averages[ada.pk], Decimal("55.56"))

    def test_the_class_average_is_not_the_childs_average(self):
        """Both numbers exist and they are different questions. A card shows the
        first; only staff see the second."""
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        bola = self.enrol(self.stmarys, "bola", "Bola B", self.group_id, self.term_id)
        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 90)
        self.mark(self.stmarys, self.term_id, self.maths_id, bola, 50)

        with connected_to(self.stmarys):
            group, term = self.group_and_term(self.group_id, self.term_id)
            averages = positions.overall_percentages(group, term)
            self.assertEqual(averages[ada.pk], Decimal("90.00"))
            self.assertEqual(positions.class_average(group, term), Decimal("70.00"))


class PositionIsOutOfThisClassOnlyTests(PositionSetUp):
    def test_a_child_in_another_class_does_not_affect_the_ranking(self):
        """The roster is the denominator, which is what `ClassPlacement` is for."""
        with connected_to(self.stmarys):
            jss1b = ClassGroup.objects.create(name="JSS 1B", level=1)

        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        top_of_1b = self.enrol(
            self.stmarys, "emeka", "Emeka E", jss1b.pk, self.term_id
        )
        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 70)
        self.mark(self.stmarys, self.term_id, self.maths_id, top_of_1b, 99)

        with connected_to(self.stmarys):
            placed = positions.subject_positions(
                *self.group_and_term(self.group_id, self.term_id), self.maths_id
            )

        self.assertEqual(placed, {ada.pk: 1})

    def test_the_other_schools_children_are_not_in_this_ranking(self):
        """Two schemas, two rosters. The check a single-tenant test cannot make."""
        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 55)

        theirs = self.enrol(
            self.grace, "uche", "Uche U", self.their_group_id, self.their_term_id
        )
        self.mark(self.grace, self.their_term_id, self.their_maths_id, theirs, 95)

        with connected_to(self.stmarys):
            ours = positions.subject_positions(
                *self.group_and_term(self.group_id, self.term_id), self.maths_id
            )
        with connected_to(self.grace):
            theirs_placed = positions.subject_positions(
                *self.group_and_term(self.their_group_id, self.their_term_id),
                self.their_maths_id,
            )

        self.assertEqual(ours, {ada.pk: 1})
        self.assertEqual(theirs_placed, {theirs.pk: 1})
        self.assertNotIn(theirs.pk, ours)
        self.assertNotIn(ada.pk, theirs_placed)

    def test_both_schools_first_placements_can_share_a_primary_key(self):
        """Per-schema sequences: each school's first row is `pk=1`.

        Recorded as a deliberate assertion because it is the trap — a test that
        asserted these were *different* would fail for a reason that has nothing
        to do with what it is testing.
        """
        with connected_to(self.stmarys):
            ours = ClassGroup.objects.get(pk=self.group_id).pk
        with connected_to(self.grace):
            theirs = ClassGroup.objects.get(pk=self.their_group_id).pk
        self.assertEqual(ours, theirs)


class RankingIsScopedToTheTermTests(PositionSetUp):
    def test_last_terms_marks_do_not_enter_this_terms_position(self):
        with connected_to(self.stmarys):
            second = Term.objects.create(
                session="2025/2026",
                name=TermName.SECOND,
                starts_on=date(2026, 1, 8),
                ends_on=date(2026, 4, 3),
            )
            second_id = second.pk

        ada = self.enrol(self.stmarys, "ada", "Ada A", self.group_id, self.term_id)
        with connected_to(self.stmarys):
            place_student(
                ClassGroup.objects.get(pk=self.group_id),
                Term.objects.get(pk=second_id),
                Membership.objects.get(pk=ada.pk),
            )

        self.mark(self.stmarys, self.term_id, self.maths_id, ada, 40)
        self.mark(self.stmarys, second_id, self.maths_id, ada, 90)

        with connected_to(self.stmarys):
            first_term = positions.subject_percentages(
                *self.group_and_term(self.group_id, self.term_id), self.maths_id
            )
            second_term = positions.subject_percentages(
                *self.group_and_term(self.group_id, second_id), self.maths_id
            )

        self.assertEqual(first_term[ada.pk], Decimal("40.00"))
        self.assertEqual(second_term[ada.pk], Decimal("90.00"))
