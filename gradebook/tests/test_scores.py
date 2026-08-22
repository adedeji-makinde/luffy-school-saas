"""The gradebook: what a mark means, what it refuses, and who wins a tie.

Tenant-scoped, so every test runs inside a real school schema created the
production way — `CREATE SCHEMA` followed by a real `migrate_schemas`. See
docs/tenancy.md for why a plain `TestCase` is the right harness for that.

Three properties carry the module, and each has a section:

- a row exists if and only if somebody has been marked, so "not marked yet" and
  "scored zero" are never the same fact;
- no total is stored anywhere, so no total can be stale;
- every write is conditional on the version the writer was shown, so two
  teachers with one sheet open cannot silently overwrite each other.
"""

import contextlib
from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.utils import ProgrammingError
from django.test import TestCase
from django_tenants.utils import schema_context

from academics.models import Term, TermName
from accounts.models import Role, User
from accounts.services import enroll_student, grant_membership
from gradebook import services
from gradebook.models import Assessment, Score, Subject
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


class GradebookSetUp(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")

        self.ada = enroll_student(
            User.objects.create_user("ada", PASSWORD, full_name="Ada Obi"),
            self.stmarys,
        )
        self.emeka = enroll_student(
            User.objects.create_user("emeka", PASSWORD, full_name="Emeka Nwosu"),
            self.stmarys,
        )
        self.teacher = grant_membership(
            User.objects.create_user("kemi", PASSWORD, full_name="Kemi Bello"),
            self.stmarys,
            Role.TEACHER,
        )

        with connected_to(self.stmarys):
            self.term_id = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            ).pk
            self.maths_id = Subject.objects.create(name="Mathematics", code="MTH").pk
            self.first_ca_id = Assessment.objects.create(
                term_id=self.term_id,
                subject_id=self.maths_id,
                name="First CA",
                max_score=20,
            ).pk

    # Re-read inside the schema under test rather than held across the
    # `schema_context` boundary, the way fees/tests/test_ledger.py reloads its
    # term: an instance fetched on one connection state is not a licence to
    # write on another.
    def first_ca(self):
        return Assessment.objects.get(pk=self.first_ca_id)

    def exam(self, max_score=100):
        return Assessment.objects.create(
            term_id=self.term_id,
            subject_id=self.maths_id,
            name="Exam",
            max_score=max_score,
        )


class MarkedOrNotTests(GradebookSetUp):
    """A row exists if and only if a teacher has entered a mark."""

    def test_creating_an_assessment_marks_nobody(self):
        """Opening a sheet for a class writes nothing.

        The naive implementation materialises a row per student when the
        assessment is created and fills them in later, which is precisely what
        makes "not marked yet" and "scored zero" indistinguishable — and makes
        "how many are left to mark?" unanswerable.
        """
        with connected_to(self.stmarys):
            self.exam()
            self.assertEqual(Score.objects.count(), 0)

    def test_a_mark_of_zero_is_a_mark(self):
        """The distinction the whole module exists for, stated as an assertion."""
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            services.set_score(assessment, self.ada, 0)

            marked = Score.objects.total_for(self.ada.pk)
            unmarked = Score.objects.total_for(self.emeka.pk)

            self.assertEqual(marked, {"scored": 0, "available": 20, "marked": 1})
            self.assertEqual(unmarked, {"scored": 0, "available": 0, "marked": 0})
            self.assertNotEqual(
                marked,
                unmarked,
                "a child who scored nothing and a child nobody has marked must "
                "not be the same row of a report",
            )

    def test_clearing_a_mark_deletes_the_row(self):
        """Not a zero and not a null — the absence of a row is the state."""
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            score = services.set_score(assessment, self.ada, 17)
            services.clear_score(
                assessment, self.ada, expected_version=score.version
            )

            self.assertEqual(Score.objects.count(), 0)
            self.assertEqual(
                Score.objects.total_for(self.ada.pk)["marked"],
                0,
                "a cleared mark must read as unmarked, not as zero",
            )

    def test_clearing_a_mark_that_is_already_gone_is_not_an_error(self):
        """The end state asked for is the end state that holds.

        A retried request should not fail because the first attempt worked.
        """
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            score = services.set_score(assessment, self.ada, 17)
            services.clear_score(assessment, self.ada, expected_version=score.version)
            services.clear_score(assessment, self.ada, expected_version=score.version)
            self.assertEqual(Score.objects.count(), 0)

    def test_a_value_is_never_null(self):
        """There is no such thing as a blank score row, at the database too."""
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Score.objects.create(
                    assessment=self.first_ca(),
                    student_membership_id=self.ada.pk,
                    value=None,
                )


class TotalTests(GradebookSetUp):
    """Totals are computed on read. Nothing here can go stale."""

    def test_no_model_here_stores_a_total(self):
        """The claim `models.py` makes, enforced rather than asserted in prose.

        A total in a column is a total that can be stale, and "refresh it before
        display" holds until the day somebody adds a second write path.
        """
        from django.apps import apps

        looks_like_a_total = ("total", "aggregate", "average", "percentage", "cached")
        for model in apps.get_app_config("gradebook").get_models():
            for field in model._meta.get_fields():
                with self.subTest(model=model.__name__, field=field.name):
                    self.assertFalse(
                        any(word in field.name.lower() for word in looks_like_a_total),
                        f"{model.__name__}.{field.name} looks like a stored total; "
                        f"aggregate on read instead — see ScoreQuerySet.total_for()",
                    )

    def test_a_total_is_the_marks_earned_over_the_marks_offered(self):
        with connected_to(self.stmarys):
            services.set_score(self.first_ca(), self.ada, 15)
            services.set_score(self.exam(), self.ada, 80)

            self.assertEqual(
                Score.objects.total_for(self.ada.pk),
                {"scored": 95, "available": 120, "marked": 2},
            )

    def test_an_unmarked_assessment_does_not_drag_a_total_down(self):
        """`available` counts what this child was marked on, deliberately.

        Summing every assessment's `max_score` would treat "not marked yet" as
        zero — the exact conflation this app exists to prevent — and would drop
        a child's percentage the moment a teacher created next week's test.
        """
        with connected_to(self.stmarys):
            services.set_score(self.first_ca(), self.ada, 15)
            before = Score.objects.total_for(self.ada.pk)

            self.exam()  # created, nobody marked on it yet

            self.assertEqual(Score.objects.total_for(self.ada.pk), before)

    def test_an_unmarked_student_totals_to_zero_not_none(self):
        """`Sum` over no rows is NULL, and NULL is not a total.

        A caller adding it to another number gets a TypeError, and one
        rendering it prints "None".
        """
        with connected_to(self.stmarys):
            self.assertEqual(
                Score.objects.total_for(999),
                {"scored": 0, "available": 0, "marked": 0},
            )

    def test_a_total_can_be_narrowed_before_it_is_taken(self):
        """`total_for` is a queryset method, so a term or a subject filters it."""
        with connected_to(self.stmarys):
            english = Subject.objects.create(name="English", code="ENG")
            english_ca = Assessment.objects.create(
                term_id=self.term_id, subject=english, name="First CA", max_score=20
            )
            services.set_score(self.first_ca(), self.ada, 15)
            services.set_score(english_ca, self.ada, 12)

            maths_only = Score.objects.for_subject(
                Subject.objects.get(pk=self.maths_id)
            ).total_for(self.ada.pk)
            self.assertEqual(
                maths_only, {"scored": 15, "available": 20, "marked": 1}
            )
            self.assertEqual(
                Score.objects.for_term(Term.objects.get(pk=self.term_id)).total_for(
                    self.ada.pk
                ),
                {"scored": 27, "available": 40, "marked": 2},
            )


class ConcurrentEditTests(GradebookSetUp):
    """Two teachers, one sheet. The second writer is refused, not applied."""

    def test_a_stale_version_is_refused(self):
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            shown = services.set_score(assessment, self.ada, 15)

            # Somebody else corrects it while this teacher's sheet is open.
            services.set_score(assessment, self.ada, 17, expected_version=shown.version)

            with self.assertRaises(services.ScoreChangedMeanwhile) as caught:
                services.set_score(
                    assessment, self.ada, 12, expected_version=shown.version
                )

            self.assertEqual(
                Score.objects.get(pk=shown.pk).value,
                17,
                "the first writer's mark must survive the second's refusal",
            )
            self.assertEqual(
                caught.exception.current.value,
                17,
                "the refusal must be able to say what it now stands at",
            )

    def test_the_version_moves_on_every_write(self):
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            first = services.set_score(assessment, self.ada, 15)
            self.assertEqual(first.version, 1)

            second = services.set_score(
                assessment, self.ada, 16, expected_version=first.version
            )
            self.assertEqual(second.version, 2)

    def test_an_update_stamps_updated_at(self):
        """`auto_now` is applied by `save()`, and a conditional update is not one.

        Without `set_score()` setting the column by hand, `updated_at` would
        keep the time of the last write that went through the ORM's save path —
        which, once this module exists, is only ever the first one.
        """
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            first = services.set_score(assessment, self.ada, 15)
            second = services.set_score(
                assessment, self.ada, 16, expected_version=first.version
            )
            self.assertGreater(second.updated_at, first.updated_at)

    def test_entering_a_first_mark_twice_is_refused(self):
        """Both teachers find no row and both insert; the constraint decides.

        `expected_version=None` is the claim "I was shown no mark at all", so
        the second one is a conflict for the same reason a stale version is —
        the sheet the writer was looking at is out of date.
        """
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            services.set_score(assessment, self.ada, 15)

            with self.assertRaises(services.ScoreChangedMeanwhile):
                services.set_score(assessment, self.ada, 18)

            self.assertEqual(Score.objects.get().value, 15)

    def test_an_integrity_error_that_is_not_a_collision_is_not_called_one(self):
        """Only a uniqueness collision is a conflict. The rest are real failures.

        The insert path catches `IntegrityError` and reads it as "somebody
        entered the first mark while you were typing", which is right for the
        one constraint it was written for and wrong for the other seven ways
        this table can refuse a row. Telling a caller to reload when the real
        problem is a malformed row sends them round a loop that cannot
        terminate, and buries the actual error underneath a routine-looking
        refusal.

        `recorded_by_id` is a bare id with a `CHECK (... >= 0)` behind it and no
        foreign key (docs/tenancy.md), so a negative stamp is refused by the
        table at INSERT — a genuine, synchronous, non-collision `IntegrityError`
        of exactly the kind that must not be relabelled.
        """
        with connected_to(self.stmarys):
            assessment = self.exam()

            with self.assertRaises(IntegrityError):
                services.set_score(assessment, self.ada, 15, by=-1)

            self.assertFalse(
                Score.objects.exists(), "the refused row must not have landed"
            )

    def test_the_same_holds_for_a_write_that_skips_the_version_check(self):
        """`ANY_VERSION` retries, and a retry must not paper over a real error.

        Worse here than on the insert path: a constraint that is not the
        collision fails identically on the second pass, so retrying it only
        delays the error before mislabelling it.
        """
        with connected_to(self.stmarys):
            assessment = self.exam()

            with self.assertRaises(IntegrityError):
                services.set_score(
                    assessment,
                    self.ada,
                    15,
                    expected_version=services.ANY_VERSION,
                    by=-1,
                )

    def test_a_collision_is_still_told_apart_from_the_rest(self):
        """The other half of the discrimination: the real conflict still reports.

        Narrowing `except IntegrityError` is only correct if the constraint it
        was written for still lands in it. A test that only proves things are
        re-raised would pass just as well if nothing were ever caught at all.
        """
        with connected_to(self.stmarys):
            assessment = self.exam()
            services.set_score(assessment, self.ada, 15)

            with self.assertRaises(services.ScoreChangedMeanwhile) as caught:
                services.set_score(assessment, self.ada, 18)

            self.assertEqual(caught.exception.current.value, 15)

    def test_a_refusal_leaves_the_surrounding_transaction_usable(self):
        """A whole sheet is one transaction, and one refused cell is not fatal.

        The insert path takes its own `atomic()` block for this: an
        IntegrityError marks the enclosing transaction unusable, so without it a
        caller who caught the refusal could not go on to write the next child.
        """
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            services.set_score(assessment, self.ada, 15)

            with transaction.atomic():
                with self.assertRaises(services.ScoreChangedMeanwhile):
                    services.set_score(assessment, self.ada, 18)
                services.set_score(assessment, self.emeka, 19)

            self.assertEqual(Score.objects.count(), 2)

    def test_updating_a_mark_somebody_cleared_is_refused(self):
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            shown = services.set_score(assessment, self.ada, 15)
            services.clear_score(assessment, self.ada, expected_version=shown.version)

            with self.assertRaises(services.ScoreChangedMeanwhile) as caught:
                services.set_score(
                    assessment, self.ada, 16, expected_version=shown.version
                )
            self.assertIsNone(caught.exception.current)
            self.assertEqual(Score.objects.count(), 0)

    def test_clearing_on_a_stale_version_is_refused(self):
        """The destructive write is exactly the one worth guarding."""
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            shown = services.set_score(assessment, self.ada, 15)
            services.set_score(assessment, self.ada, 17, expected_version=shown.version)

            with self.assertRaises(services.ScoreChangedMeanwhile):
                services.clear_score(
                    assessment, self.ada, expected_version=shown.version
                )
            self.assertEqual(Score.objects.get().value, 17)

    def test_any_version_writes_regardless(self):
        """The escape hatch for a caller with no sheet and nobody to race."""
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            services.set_score(assessment, self.ada, 15)

            imported = services.set_score(
                assessment, self.ada, 19, expected_version=services.ANY_VERSION
            )
            self.assertEqual(imported.value, 19)
            self.assertEqual(imported.version, 2)

    def test_any_version_inserts_when_there_is_nothing_there(self):
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            imported = services.set_score(
                assessment, self.ada, 19, expected_version=services.ANY_VERSION
            )
            self.assertEqual(imported.value, 19)
            self.assertEqual(imported.version, 1)

    def test_the_database_refuses_a_second_row_for_the_same_child(self):
        """The service checks; the constraint is what makes it true.

        Two teachers entering the first mark for one child at the same instant
        both find nothing, so the guarantee has to live below the service.
        """
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            services.set_score(assessment, self.ada, 15)
            with self.assertRaises(IntegrityError), transaction.atomic():
                Score.objects.create(
                    assessment=assessment,
                    student_membership_id=self.ada.pk,
                    value=18,
                )

    def test_who_entered_a_mark_and_who_changed_it_are_both_kept(self):
        with connected_to(self.stmarys):
            assessment = self.first_ca()
            head = User.objects.create_user("head", PASSWORD, full_name="Head Teacher")
            first = services.set_score(
                assessment, self.ada, 15, by=self.teacher.user
            )
            self.assertEqual(first.recorded_by_id, self.teacher.user.pk)

            corrected = services.set_score(
                assessment, self.ada, 17, expected_version=first.version, by=head
            )
            self.assertEqual(
                corrected.recorded_by_id,
                self.teacher.user.pk,
                "who first entered the mark is not rewritten by a correction",
            )
            self.assertEqual(corrected.updated_by_id, head.pk)


class RangeTests(GradebookSetUp):
    """A mark this assessment can hold, and nothing else."""

    def test_a_mark_above_the_maximum_is_refused(self):
        """`max_score` is the denominator of every percentage downstream."""
        with connected_to(self.stmarys):
            with self.assertRaises(services.InvalidScore):
                services.set_score(self.first_ca(), self.ada, 21)
            self.assertEqual(Score.objects.count(), 0)

    def test_full_marks_are_fine(self):
        """The guard must not be so tight that it refuses a perfect score."""
        with connected_to(self.stmarys):
            score = services.set_score(self.first_ca(), self.ada, 20)
            self.assertEqual(score.value, 20)

    def test_a_fractional_mark_is_refused(self):
        """An assessment scored in halves is one out of twice as many marks."""
        with connected_to(self.stmarys):
            with self.assertRaises(services.InvalidScore):
                services.set_score(self.first_ca(), self.ada, 17.5)

    def test_a_negative_mark_is_refused(self):
        with connected_to(self.stmarys):
            with self.assertRaises(services.InvalidScore):
                services.set_score(self.first_ca(), self.ada, -1)

    def test_the_model_refuses_it_too(self):
        """A cross-row rule, so it lives in `clean()` rather than a constraint.

        Recorded here so that the absence of a check constraint reads as a
        decision rather than as an oversight.
        """
        with connected_to(self.stmarys):
            score = Score(
                assessment=self.first_ca(),
                student_membership_id=self.ada.pk,
                value=21,
            )
            with self.assertRaises(ValidationError):
                score.full_clean(validate_unique=False, validate_constraints=False)

    def test_an_assessment_out_of_nothing_is_refused(self):
        """Zero is not a mark scheme; it is a division error somewhere else."""
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Assessment.objects.create(
                    term_id=self.term_id,
                    subject_id=self.maths_id,
                    name="Weightless",
                    max_score=0,
                )

    def test_one_assessment_of_a_given_name_per_subject_per_term(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Assessment.objects.create(
                    term_id=self.term_id,
                    subject_id=self.maths_id,
                    name="First CA",
                    max_score=20,
                )

    def test_a_subject_with_marks_against_it_cannot_be_deleted(self):
        """PROTECT, and it works — both tables are in the same schema."""
        with connected_to(self.stmarys):
            from django.db.models import ProtectedError

            services.set_score(self.first_ca(), self.ada, 15)
            with self.assertRaises(ProtectedError), transaction.atomic():
                Subject.objects.get(pk=self.maths_id).delete()


class WrongStudentTests(GradebookSetUp):
    """The check that earns the bare id.

    `student_membership_id` has no foreign key — see docs/tenancy.md — so the
    column will take any integer, including the id of a child at another school.
    Nothing about the result would look wrong: the mark sits in this school's
    gradebook, counts towards a total here, and names a student this school has
    never taught. A foreign key would not have caught it either; `Membership` is
    shared, so an FK into it constrains only that the row exists.
    """

    def setUp(self):
        super().setUp()
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.theirs = enroll_student(
            User.objects.create_user("chidi", PASSWORD, full_name="Chidi Eze"),
            self.grace,
        )

    def test_another_schools_student_cannot_be_marked_here(self):
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.set_score(self.first_ca(), self.theirs, 15)
            self.assertEqual(Score.objects.count(), 0)

    def test_a_staff_membership_cannot_be_marked(self):
        """The gradebook is keyed on the STUDENT membership, which pins the school."""
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.set_score(self.first_ca(), self.teacher, 15)

    def test_the_check_covers_clearing_too(self):
        with connected_to(self.stmarys):
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.clear_score(self.first_ca(), self.theirs, expected_version=1)
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.set_score(
                    self.first_ca(),
                    self.theirs,
                    15,
                    expected_version=services.ANY_VERSION,
                )

    def test_our_own_student_is_fine(self):
        """The guard must not be so tight that it refuses the ordinary case."""
        with connected_to(self.stmarys):
            services.set_score(self.first_ca(), self.ada, 15)
            self.assertEqual(Score.objects.count(), 1)


class GradebookIsolationTests(TestCase):
    """One school cannot see another's marks. The reason this app is tenanted."""

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.ada = enroll_student(
            User.objects.create_user("ada", PASSWORD, full_name="Ada Obi"),
            self.stmarys,
        )

    def mark_ada_at_st_marys(self):
        with connected_to(self.stmarys):
            term = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            )
            assessment = Assessment.objects.create(
                term=term,
                subject=Subject.objects.create(name="Mathematics", code="MTH"),
                name="First CA",
                max_score=20,
            )
            services.set_score(assessment, self.ada, 15)

    def test_a_mark_at_one_school_is_invisible_at_another(self):
        self.mark_ada_at_st_marys()
        with connected_to(self.grace):
            self.assertEqual(Score.objects.count(), 0)
            self.assertEqual(
                Score.objects.total_for(self.ada.pk),
                {"scored": 0, "available": 0, "marked": 0},
                "a child's marks at one school must not follow them to another",
            )

    def test_two_schools_may_each_teach_mathematics(self):
        """`uniq_subject_code` is per schema, which is the isolation."""
        for school in (self.stmarys, self.grace):
            with connected_to(school):
                Subject.objects.create(name="Mathematics", code="MTH")
                self.assertEqual(Subject.objects.count(), 1)

    def test_the_score_table_is_absent_from_public_not_merely_empty(self):
        """The same load-bearing claim `academics` and `fees` make, for marks.

        An empty result here instead of an exception would mean the gradebook
        had leaked into the shared schema and isolation had become a query
        filter that somebody can forget.
        """
        with self.assertRaises(ProgrammingError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("select 1 from public.gradebook_score")


class MigrationStabilityTests(TestCase):
    """`makemigrations` must not propose the same constraints forever.

    CI runs `makemigrations --check`, so a constraint that does not survive
    being written to a migration and read back is a build that goes red at
    random. `fees` learned this from a frozenset in a `Q(kind__in=...)`; this
    test is the same guard for the constraints declared here.
    """

    def test_every_constraint_survives_a_migration_round_trip(self):
        from django.db.migrations.loader import MigrationLoader

        state = MigrationLoader(None, ignore_no_migrations=True).project_state()
        for model in (Subject, Assessment, Score):
            migrated = {
                constraint.name: constraint
                for constraint in state.models[
                    "gradebook", model.__name__.lower()
                ].options.get("constraints", [])
            }
            declared = {c.name: c for c in model._meta.constraints}
            with self.subTest(model=model.__name__):
                self.assertEqual(set(declared), set(migrated))
                for name, constraint in declared.items():
                    self.assertEqual(
                        constraint,
                        migrated[name],
                        f"{name} does not survive being written to a migration "
                        f"and read back, so makemigrations will propose it forever",
                    )


class ConstraintTimingTests(GradebookSetUp):
    """*When* a constraint fires, which decides what `set_score()` can catch.

    `_insert_first_mark()` turns one specific `IntegrityError` into
    `ScoreChangedMeanwhile` and re-raises the rest. That is only coherent if the
    collision actually reaches the `except` block, and Postgres decides that:
    an immediate constraint fires at the statement, a deferred one fires at
    COMMIT — long after the handler has returned.

    Both facts below are load-bearing and neither is visible in `models.py`, so
    they are pinned here. If a future migration makes the unique constraint
    deferrable, the conflict path silently stops working and every 409 this
    module promises becomes a 500 at commit time; this test fails first.
    """

    def _timing_of(self, where, params):
        """(deferrable, deferred) for the one constraint matching `where`."""
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT condeferrable, condeferred
                FROM pg_constraint
                WHERE conrelid = 'gradebook_score'::regclass AND {where}
                """,
                params,
            )
            rows = cursor.fetchall()
        self.assertEqual(len(rows), 1, f"expected exactly one match for {where}")
        return rows[0]

    def test_the_collision_fires_at_the_statement_not_at_commit(self):
        with connected_to(self.stmarys):
            deferrable, deferred = self._timing_of(
                "conname = %s", ["one_score_per_student_per_assessment"]
            )

        self.assertFalse(
            deferrable or deferred,
            "the uniqueness collision must raise inside set_score(), not at "
            "commit — the whole conflict path is built on catching it there",
        )

    def test_the_assessment_foreign_key_is_deferred_and_so_cannot_be_tested_here(self):
        """Documented because it is the trap, not because anything relies on it.

        Django writes every foreign key as DEFERRABLE INITIALLY DEFERRED, so a
        `Score` pointing at an assessment deleted a moment ago does *not* fail at
        the INSERT. It fails at COMMIT — and *where* that lands depends on who
        opened the transaction:

        - In production there is no `ATOMIC_REQUESTS`, so the `transaction
          .atomic()` inside `_insert_first_mark()` is the outermost one. Exiting
          it is a real COMMIT, the violation is raised there, and it *does* reach
          the `except IntegrityError` handler. Classifying it correctly matters.
        - Under `TestCase` every test already runs in a transaction, so that same
          block is only a savepoint. Nothing commits, the violation is held until
          teardown, and the handler never sees it.

        So the case is real and the handler must get it right, but it cannot be
        provoked from here: a test that deletes the assessment and expects an
        error reports "IntegrityError not raised" and then errors in teardown,
        which reads like a bug in the code under test and is not one. Pinned as
        a schema fact instead, with the classification itself covered by the
        `CHECK` constraint tests in `ConcurrentEditTests`, which do fire at the
        statement.
        """
        with connected_to(self.stmarys):
            # By type, not by name: Django hashes the column into the name, so
            # spelling it out here would be a test that breaks on a rename
            # rather than on the behaviour it is about. `Score` has exactly one
            # foreign key — the bare ids have none, by design.
            deferrable, deferred = self._timing_of("contype = 'f'", [])

        self.assertTrue(deferrable and deferred)
