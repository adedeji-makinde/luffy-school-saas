"""The `Term` record, and the four things Postgres refuses to store.

`Term` is tenant-scoped, so these run inside a real school schema rather than
against `public` — `academics_term` does not exist there at all, which is the
whole point of `docs/tenancy.md` and is asserted directly in
`schools/tests/test_tenant_isolation.py`. `make_school()` here therefore leaves
`auto_create_schema` alone, the way that module does: saving really does issue
`CREATE SCHEMA` and really does migrate TENANT_APPS into it.

Every rule below is a database constraint rather than a `clean()` check, for the
reason `docs/tenancy.md` already gives about the one-current-term index: a rule
that lives only in Python is a rule a data import, a shell session or a future
service function walks straight around.
"""

import contextlib
from datetime import date

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django_tenants.utils import schema_context

from academics.models import Term, TermName
from schools.models import School

SESSION = "2025/2026"


def make_school(name, slug, schema_name):
    """A real tenant, schema and all — `academics_term` lives nowhere else."""
    school = School(name=name, slug=slug, schema_name=schema_name)
    school.save()
    return school


@contextlib.contextmanager
def connected_to(school):
    with schema_context(school.schema_name):
        yield


def make_term(**extra):
    extra.setdefault("session", SESSION)
    extra.setdefault("name", TermName.FIRST)
    extra.setdefault("starts_on", date(2025, 9, 15))
    extra.setdefault("ends_on", date(2025, 12, 12))
    return Term.objects.create(**extra)


class TermSetUp(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")


class TermRecordTests(TermSetUp):
    def test_a_term_records_all_four_dates_and_the_day_count(self):
        with connected_to(self.stmarys):
            term = make_term(
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
                next_term_starts_on=date(2026, 1, 8),
                school_days=61,
            )
            term.refresh_from_db()

            self.assertEqual(term.starts_on, date(2025, 9, 15))
            self.assertEqual(term.ends_on, date(2025, 12, 12))
            self.assertEqual(term.next_term_starts_on, date(2026, 1, 8))
            self.assertEqual(term.school_days, 61)

    def test_the_announcement_and_the_count_are_both_optional(self):
        """"Not announced yet" is the honest state for most of a term.

        A term record is created with the calendar, often before the school has
        settled either — the next term's start is a decision the proprietor
        makes later, and the day count moves when a public holiday is declared.
        Requiring them would mean inventing a number to get the row saved, and
        an invented denominator is worse than an absent one.
        """
        with connected_to(self.stmarys):
            term = make_term()
            term.refresh_from_db()
            self.assertIsNone(term.next_term_starts_on)
            self.assertIsNone(term.school_days)

    def test_calendar_days_counts_both_endpoints(self):
        with connected_to(self.stmarys):
            term = make_term(starts_on=date(2025, 9, 15), ends_on=date(2025, 9, 19))
            # Monday to Friday is five days, not four.
            self.assertEqual(term.calendar_days, 5)

    def test_calendar_days_is_not_a_default_for_school_days(self):
        """They answer different questions and must not be confused.

        `calendar_days` is the ceiling; `school_days` is what the school taught.
        A term that ran twelve weeks and lost eight days to holidays has both,
        and they differ — which is the entire reason the second is stored.
        """
        with connected_to(self.stmarys):
            term = make_term(school_days=61)
            self.assertEqual(term.calendar_days, 89)
            self.assertNotEqual(term.school_days, term.calendar_days)


class TermConstraintTests(TermSetUp):
    """Each rule, refused by Postgres rather than by Python."""

    def test_a_next_term_cannot_begin_before_this_one_ends(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                make_term(
                    ends_on=date(2025, 12, 12),
                    next_term_starts_on=date(2025, 11, 1),
                )

    def test_a_next_term_cannot_begin_on_the_day_this_one_ends(self):
        """A day cannot belong to two terms, so the bound is strict."""
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                make_term(
                    ends_on=date(2025, 12, 12),
                    next_term_starts_on=date(2025, 12, 12),
                )

    def test_a_next_term_the_day_after_is_allowed(self):
        """Tight, but not a mistake — the constraint must not overreach."""
        with connected_to(self.stmarys):
            term = make_term(
                ends_on=date(2025, 12, 12),
                next_term_starts_on=date(2025, 12, 13),
            )
            self.assertEqual(term.next_term_starts_on, date(2025, 12, 13))

    def test_a_term_cannot_hold_more_school_days_than_it_has_days(self):
        """The count is a claim about this term, so the term bounds it."""
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                make_term(
                    starts_on=date(2025, 9, 15),
                    ends_on=date(2025, 12, 12),  # 89 calendar days
                    school_days=90,
                )

    def test_school_days_may_equal_the_whole_span(self):
        """A school that taught every single day is unusual, not impossible."""
        with connected_to(self.stmarys):
            term = make_term(
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 9, 19),
                school_days=5,
            )
            self.assertEqual(term.school_days, term.calendar_days)

    def test_a_term_cannot_have_zero_school_days(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                make_term(school_days=0)

    def test_the_bounds_are_checked_on_update_too(self):
        """A constraint, not a validation on the create path.

        Worth its own test because the two are easy to confuse: a rule enforced
        only where rows are created is one an `UPDATE` from a shell or a data
        fix walks straight past.
        """
        with connected_to(self.stmarys):
            term = make_term(school_days=61)
            term.school_days = 500
            with self.assertRaises(IntegrityError), transaction.atomic():
                term.save(update_fields=["school_days"])


class TermConstraintSqlTests(TermSetUp):
    """What Postgres was actually given, read back out of Postgres.

    The day-count bound is the reason this exists. Written the obvious way —
    `F("ends_on") - F("starts_on")` — Django renders a subtraction of two
    DateFields as `interval '1 day' * (...)`, even inside an
    `ExpressionWrapper(output_field=IntegerField())`. Postgres will not compare a
    smallint to an interval, so the constraint is created happily and then
    errors at the first row that tests it. `models._TERM_SPAN` spells the
    subtraction out to avoid that, and this test pins the generated SQL so a
    later "simplification" back to `F() - F()` fails here rather than in
    production.
    """

    def constraint_defs(self, schema):
        with connection.cursor() as cursor:
            cursor.execute(
                "select conname, pg_get_constraintdef(oid) from pg_constraint "
                "where connamespace = %s::regnamespace and contype = 'c'",
                [schema],
            )
            return dict(cursor.fetchall())

    def test_the_day_count_bound_is_integer_arithmetic_not_an_interval(self):
        defs = self.constraint_defs("st_marys")
        sql = defs["school_days_fit_inside_the_term"]

        self.assertNotIn("interval", sql.lower())
        self.assertIn("ends_on - starts_on", sql.replace('"', ""))

    def test_the_constraint_survives_a_migration_round_trip(self):
        """`makemigrations` must not propose the same constraint forever.

        `Func`'s `template` and `arg_joiner` arrive as `**extra` when passed to
        the constructor, and that dict's *key order* lands in the expression's
        identity. An instance built in `models.py` and the identical instance
        reconstructed from the migration therefore compared unequal, and the
        autodetector proposed dropping and recreating this constraint on every
        run. `DaysBetween` carries them as class attributes so `extra` stays
        empty. CI runs `makemigrations --check`, so getting this wrong is a
        permanently red build — which is exactly why it is worth a test that
        says so rather than a puzzled bug report later.
        """
        # Through the loader rather than by importing a migration by filename:
        # this is the state `makemigrations` itself compares against, and it
        # survives the migrations being renamed or squashed later.
        from django.db.migrations.loader import MigrationLoader

        state = MigrationLoader(None, ignore_no_migrations=True).project_state()
        migrated = {
            constraint.name: constraint
            for constraint in state.models["academics", "term"]
            .options.get("constraints", [])
        }
        declared = {c.name: c for c in Term._meta.constraints}

        self.assertEqual(
            set(declared), set(migrated), "a constraint is missing from the migrations"
        )
        for name, constraint in declared.items():
            with self.subTest(constraint=name):
                self.assertEqual(
                    constraint,
                    migrated[name],
                    f"{name} does not survive being written to a migration and "
                    f"read back, so makemigrations will propose it forever",
                )

    def test_every_term_rule_reached_the_database(self):
        defs = self.constraint_defs("st_marys")
        for name in (
            "term_ends_after_it_starts",
            "next_term_starts_after_this_one_ends",
            "school_days_fit_inside_the_term",
            "a_term_has_at_least_one_school_day",
        ):
            with self.subTest(constraint=name):
                self.assertIn(name, defs)


class TermIsolationTests(TestCase):
    """The new columns are per-school, like everything else in this app.

    Not a formality: the whole reason `Term` is tenant-scoped is that two
    schools run the same session on different dates. A shared "next term begins"
    date would be wrong for one of them by construction.
    """

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

    def test_two_schools_hold_different_dates_for_the_same_session(self):
        with connected_to(self.stmarys):
            make_term(
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
                next_term_starts_on=date(2026, 1, 8),
                school_days=61,
            )
        with connected_to(self.grace):
            make_term(
                starts_on=date(2025, 9, 8),
                ends_on=date(2025, 12, 19),
                next_term_starts_on=date(2026, 1, 12),
                school_days=68,
            )

        with connected_to(self.stmarys):
            mine = Term.objects.get(session=SESSION, name=TermName.FIRST)
            self.assertEqual(mine.next_term_starts_on, date(2026, 1, 8))
            self.assertEqual(mine.school_days, 61)
            self.assertEqual(Term.objects.count(), 1)

        with connected_to(self.grace):
            theirs = Term.objects.get(session=SESSION, name=TermName.FIRST)
            self.assertEqual(theirs.next_term_starts_on, date(2026, 1, 12))
            self.assertEqual(theirs.school_days, 68)
            self.assertEqual(Term.objects.count(), 1)

    def test_the_day_count_bound_is_enforced_in_each_schema(self):
        """The constraint is created per schema, so prove it exists in both."""
        for school in (self.stmarys, self.grace):
            with self.subTest(school=school.slug):
                with connected_to(school):
                    with self.assertRaises(IntegrityError), transaction.atomic():
                        make_term(
                            starts_on=date(2025, 9, 15),
                            ends_on=date(2025, 9, 19),
                            school_days=6,
                        )
