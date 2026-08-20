"""The fee ledger: what it stores, what it refuses, and what it cannot forget.

Tenant-scoped, so every test runs inside a real school schema created the
production way — `CREATE SCHEMA` followed by a real `migrate_schemas`, which is
also what installs the append-only trigger. See docs/tenancy.md for why a plain
`TestCase` is the right harness for that.

Three properties are worth more than the rest, and each has a section:

- money is whole kobo and the sign carries the meaning, so a balance is a sum;
- an entry is never edited and never deleted, enforced in Postgres and not only
  in Python;
- a correction is a new row that names what it undoes.
"""

import contextlib
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.utils import ProgrammingError
from django.test import TestCase
from django_tenants.utils import schema_context

from academics.models import Term, TermName
from accounts.models import Role, User
from accounts.services import enroll_student, grant_membership
from fees import services
from fees.models import (
    FeeEntryKind,
    FeeLedgerEntry,
    KOBO_PER_NAIRA,
    LedgerIsAppendOnly,
)
from schools.models import School

PASSWORD = "correct-horse-battery"

#: ₦150,000 as the column stores it. Spelled out once so the tests below read as
#: money rather than as seven-digit integers.
TUITION = 150_000 * KOBO_PER_NAIRA


def make_school(name, slug, schema_name):
    school = School(name=name, slug=slug, schema_name=schema_name)
    school.save()
    return school


@contextlib.contextmanager
def connected_to(school):
    with schema_context(school.schema_name):
        yield


class LedgerSetUp(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.student = User.objects.create_user(
            "ada", PASSWORD, full_name="Ada Obi"
        )
        self.membership = enroll_student(self.student, self.stmarys)
        self.membership.reference = "STM/2025/113"
        self.membership.save(update_fields=["reference"])

        self.bursar = User.objects.create_user(
            "bursar", PASSWORD, full_name="Bola Bursar"
        )

        with connected_to(self.stmarys):
            self.term = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            )
            self.term_id = self.term.pk

    def reload_term(self):
        return Term.objects.get(pk=self.term_id)


class MoneyTests(LedgerSetUp):
    """Whole kobo, a signed column, and a balance that is a plain sum."""

    def test_a_charge_and_a_payment_net_out(self):
        with connected_to(self.stmarys):
            term = self.reload_term()
            services.charge(
                self.membership, term, TUITION, narration="First term tuition"
            )
            services.record_payment(
                self.membership, term, 100_000 * KOBO_PER_NAIRA, reference="TLR-4471"
            )

            outstanding = FeeLedgerEntry.objects.for_student(self.membership.pk).balance()
            self.assertEqual(outstanding, 50_000 * KOBO_PER_NAIRA)

    def test_an_empty_ledger_balances_to_zero_not_none(self):
        """`Sum` over no rows is NULL, and NULL is not a balance.

        A caller adding this to another figure would get a TypeError, and one
        rendering it would print "None owing".
        """
        with connected_to(self.stmarys):
            self.assertEqual(FeeLedgerEntry.objects.for_student(999).balance(), 0)

    def test_overpayment_is_a_credit_not_an_error(self):
        """A real state: fees paid ahead, or a term's charge not yet posted."""
        with connected_to(self.stmarys):
            term = self.reload_term()
            services.charge(self.membership, term, TUITION, narration="Tuition")
            services.record_payment(
                self.membership, term, 200_000 * KOBO_PER_NAIRA
            )
            self.assertEqual(
                FeeLedgerEntry.objects.for_student(self.membership.pk).balance(),
                -50_000 * KOBO_PER_NAIRA,
            )

    def test_the_sign_is_the_ledgers_choice_not_the_callers(self):
        """Every function takes a positive magnitude and applies the sign.

        Letting a caller pass a negative to `charge()` would make the grammar of
        the column a per-call decision, and one wrong sign is a balance nobody
        can explain.
        """
        with connected_to(self.stmarys):
            term = self.reload_term()
            for function in (services.charge, services.record_payment, services.discount):
                with self.subTest(function=function.__name__):
                    with self.assertRaises(services.NotPositive):
                        function(self.membership, term, -5000, narration="wrong way")
                    with self.assertRaises(services.NotPositive):
                        function(self.membership, term, 0, narration="nothing")

    def test_a_float_amount_is_refused(self):
        """The reason the column is kobo, restated where it can be enforced.

        `1500.10` is not representable in binary floating point, so accepting a
        float here would reintroduce at the door the error the integer column
        exists to keep out.
        """
        with connected_to(self.stmarys):
            term = self.reload_term()
            with self.assertRaises(services.NotPositive):
                services.charge(self.membership, term, 1500.10, narration="Tuition")

    def test_naira_is_a_decimal_for_display_only(self):
        with connected_to(self.stmarys):
            term = self.reload_term()
            entry = services.charge(
                self.membership, term, 150_000_50, narration="Tuition"
            )
            self.assertEqual(entry.naira, Decimal("150000.50"))
            self.assertIsInstance(entry.naira, Decimal)

    def test_a_discount_is_not_a_payment(self):
        """Both reduce the balance and they are different facts.

        "We waived it" and "they paid it" have to be tellable apart, or a
        school cannot say how much money it actually received.
        """
        with connected_to(self.stmarys):
            term = self.reload_term()
            services.charge(self.membership, term, TUITION, narration="Tuition")
            services.discount(
                self.membership, term, 30_000 * KOBO_PER_NAIRA,
                narration="Staff child concession",
            )
            services.record_payment(
                self.membership, term, 120_000 * KOBO_PER_NAIRA
            )

            received = FeeLedgerEntry.objects.filter(kind=FeeEntryKind.PAYMENT).balance()
            waived = FeeLedgerEntry.objects.filter(kind=FeeEntryKind.DISCOUNT).balance()
            self.assertEqual(received, -120_000 * KOBO_PER_NAIRA)
            self.assertEqual(waived, -30_000 * KOBO_PER_NAIRA)
            self.assertEqual(
                FeeLedgerEntry.objects.for_student(self.membership.pk).balance(), 0
            )


class AppendOnlyTests(LedgerSetUp):
    """Nothing is edited and nothing is deleted. Twice over."""

    def post_a_charge(self):
        term = self.reload_term()
        return services.charge(self.membership, term, TUITION, narration="Tuition")

    def test_saving_over_an_existing_entry_is_refused(self):
        with connected_to(self.stmarys):
            entry = self.post_a_charge()
            entry.amount_kobo = 1
            with self.assertRaises(LedgerIsAppendOnly):
                entry.save()

    def test_deleting_an_entry_is_refused(self):
        with connected_to(self.stmarys):
            entry = self.post_a_charge()
            with self.assertRaises(LedgerIsAppendOnly):
                entry.delete()

    def test_the_database_refuses_an_update_that_bypasses_the_model(self):
        """The rule that actually holds.

        `QuerySet.update()` never calls `save()`, so the Python guard above is
        no guard at all against it — nor against a `psql` session, a data
        import, or a service function written in a hurry. This is the one that
        stops those.
        """
        with connected_to(self.stmarys):
            self.post_a_charge()
            with self.assertRaises(IntegrityError), transaction.atomic():
                FeeLedgerEntry.objects.update(amount_kobo=1)

    def test_the_database_refuses_a_delete_that_bypasses_the_model(self):
        with connected_to(self.stmarys):
            self.post_a_charge()
            with self.assertRaises(IntegrityError), transaction.atomic():
                FeeLedgerEntry.objects.all().delete()

    def test_raw_sql_cannot_rewrite_the_books_either(self):
        """No ORM in the loop at all — the trigger is the whole defence."""
        with connected_to(self.stmarys):
            self.post_a_charge()
            with self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("update fees_feeledgerentry set amount_kobo = 1")

    def test_the_trigger_exists_in_every_school_schema(self):
        """Created per schema, like the table it guards."""
        grace = make_school("Grace Academy", "grace", "grace")
        for schema in ("st_marys", grace.schema_name):
            with self.subTest(schema=schema):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select tgname from pg_trigger t "
                        "join pg_class c on c.oid = t.tgrelid "
                        "where c.relnamespace = %s::regnamespace "
                        "and not t.tgisinternal",
                        [schema],
                    )
                    triggers = {row[0] for row in cursor.fetchall()}
                self.assertIn("fees_ledger_append_only", triggers)

    def test_posting_a_new_entry_still_works_with_the_trigger_installed(self):
        """The guard must not be so broad that it blocks the ledger's one job."""
        with connected_to(self.stmarys):
            self.post_a_charge()
            self.post_a_charge()
            self.assertEqual(FeeLedgerEntry.objects.count(), 2)


class CorrectionTests(LedgerSetUp):
    """A correction is a new row that names what it undoes."""

    def post_a_charge(self, amount=TUITION):
        term = self.reload_term()
        return services.charge(
            self.membership, term, amount, narration="First term tuition"
        )

    def test_a_reversal_undoes_the_entry_exactly(self):
        with connected_to(self.stmarys):
            wrong = self.post_a_charge()
            reversal = services.reverse_entry(wrong, recorded_by=self.bursar)

            self.assertEqual(reversal.kind, FeeEntryKind.REVERSAL)
            self.assertEqual(reversal.amount_kobo, -wrong.amount_kobo)
            self.assertEqual(reversal.reverses_id, wrong.pk)
            self.assertEqual(reversal.recorded_by_id, self.bursar.pk)
            self.assertEqual(
                FeeLedgerEntry.objects.for_student(self.membership.pk).balance(), 0
            )

    def test_the_mistake_is_still_in_the_book(self):
        """The point of correcting by addition rather than by edit.

        Both rows survive, so a year later somebody can see that ₦150,000 was
        charged, that it was withdrawn, and that ₦120,000 was charged instead —
        rather than a single row that has always said ₦120,000.
        """
        with connected_to(self.stmarys):
            wrong = self.post_a_charge()
            services.reverse_entry(wrong)
            term = self.reload_term()
            services.charge(
                self.membership, term, 120_000 * KOBO_PER_NAIRA,
                narration="First term tuition (corrected)",
            )

            self.assertEqual(FeeLedgerEntry.objects.count(), 3)
            wrong.refresh_from_db()
            self.assertEqual(wrong.amount_kobo, TUITION, "the original must be untouched")
            self.assertEqual(
                FeeLedgerEntry.objects.for_student(self.membership.pk).balance(),
                120_000 * KOBO_PER_NAIRA,
            )

    def test_an_entry_cannot_be_reversed_twice(self):
        with connected_to(self.stmarys):
            wrong = self.post_a_charge()
            services.reverse_entry(wrong)
            with self.assertRaises(services.AlreadyReversed):
                services.reverse_entry(wrong)

    def test_the_database_refuses_a_second_reversal_too(self):
        """The service checks under a lock; the index is what makes it true.

        Two bursars clicking undo on one charge at the same instant both pass
        the service's existence check before either commits, so the guarantee
        has to live in the database as well.
        """
        with connected_to(self.stmarys):
            wrong = self.post_a_charge()
            first = services.reverse_entry(wrong)
            with self.assertRaises(IntegrityError), transaction.atomic():
                FeeLedgerEntry.objects.create(
                    term=first.term,
                    kind=FeeEntryKind.REVERSAL,
                    amount_kobo=-wrong.amount_kobo,
                    narration="second undo",
                    effective_on=date(2025, 10, 1),
                    reverses=wrong,
                    student_membership_id=self.membership.pk,
                    student_name="Ada Obi",
                )

    def test_a_reversal_cannot_itself_be_reversed(self):
        with connected_to(self.stmarys):
            wrong = self.post_a_charge()
            reversal = services.reverse_entry(wrong)
            with self.assertRaises(services.CannotReverse):
                services.reverse_entry(reversal)

    def test_a_reversal_must_name_what_it_undoes(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                FeeLedgerEntry.objects.create(
                    term=self.reload_term(),
                    kind=FeeEntryKind.REVERSAL,
                    amount_kobo=-TUITION,
                    narration="undoing... something",
                    effective_on=date(2025, 10, 1),
                    student_membership_id=self.membership.pk,
                    student_name="Ada Obi",
                )

    def test_an_ordinary_entry_may_not_name_another(self):
        with connected_to(self.stmarys):
            existing = self.post_a_charge()
            with self.assertRaises(IntegrityError), transaction.atomic():
                FeeLedgerEntry.objects.create(
                    term=existing.term,
                    kind=FeeEntryKind.CHARGE,
                    amount_kobo=TUITION,
                    narration="a charge pointing at a charge",
                    effective_on=date(2025, 10, 1),
                    reverses=existing,
                    student_membership_id=self.membership.pk,
                    student_name="Ada Obi",
                )

    def test_a_reversal_of_the_wrong_amount_is_refused(self):
        with connected_to(self.stmarys):
            existing = self.post_a_charge()
            partial = FeeLedgerEntry(
                term=existing.term,
                kind=FeeEntryKind.REVERSAL,
                amount_kobo=-1000,
                narration="partial undo",
                effective_on=date(2025, 10, 1),
                reverses=existing,
                student_membership_id=self.membership.pk,
                student_name="Ada Obi",
            )
            with self.assertRaises(ValidationError):
                partial.full_clean(validate_unique=False, validate_constraints=False)


class ConstraintTests(LedgerSetUp):
    """The sign, the kind, and the zero — refused by Postgres."""

    def entry(self, **overrides):
        fields = {
            "term": self.reload_term(),
            "kind": FeeEntryKind.CHARGE,
            "amount_kobo": TUITION,
            "narration": "Tuition",
            "effective_on": date(2025, 10, 1),
            "student_membership_id": self.membership.pk,
            "student_name": "Ada Obi",
        }
        fields.update(overrides)
        return FeeLedgerEntry.objects.create(**fields)

    def test_a_zero_entry_is_refused(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.entry(amount_kobo=0)

    def test_a_negative_charge_is_refused(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.entry(kind=FeeEntryKind.CHARGE, amount_kobo=-TUITION)

    def test_a_positive_payment_is_refused(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.entry(kind=FeeEntryKind.PAYMENT, amount_kobo=TUITION)

    def test_a_positive_discount_is_refused(self):
        with connected_to(self.stmarys):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.entry(kind=FeeEntryKind.DISCOUNT, amount_kobo=TUITION)

    def test_a_term_with_money_against_it_cannot_be_deleted(self):
        """PROTECT, and it works — `Term` is in the same schema as the ledger."""
        with connected_to(self.stmarys):
            self.entry()
            from django.db.models import ProtectedError

            with self.assertRaises(ProtectedError), transaction.atomic():
                self.reload_term().delete()


class IdentitySnapshotTests(LedgerSetUp):
    """What an entry freezes, and why it is not a join."""

    def test_an_entry_records_the_student_as_they_were(self):
        with connected_to(self.stmarys):
            entry = services.charge(
                self.membership, self.reload_term(), TUITION, narration="Tuition"
            )
            self.assertEqual(entry.student_membership_id, self.membership.pk)
            self.assertEqual(entry.student_name, "Ada Obi")
            self.assertEqual(entry.student_reference, "STM/2025/113")

    def test_renaming_the_student_does_not_rewrite_last_terms_receipt(self):
        """A financial record has to keep saying what it said.

        A join to the live membership would silently restate every historical
        entry the moment a school corrected a spelling or reissued admission
        numbers — and a receipt that changes after it was issued is not a
        receipt.
        """
        with connected_to(self.stmarys):
            entry = services.charge(
                self.membership, self.reload_term(), TUITION, narration="Tuition"
            )

        self.membership.display_name = "Adaeze Obi-Nwosu"
        self.membership.reference = "STM/2026/007"
        self.membership.save(update_fields=["display_name", "reference"])

        with connected_to(self.stmarys):
            entry.refresh_from_db()
            self.assertEqual(entry.student_name, "Ada Obi")
            self.assertEqual(entry.student_reference, "STM/2025/113")

    def test_the_books_survive_the_student_leaving(self):
        """The bare id is why this works, rather than in spite of it.

        A foreign key with `CASCADE` would have taken the school's financial
        history with the membership; one with `PROTECT` would have blocked an
        ordinary end-of-year departure — and docs/tenancy.md measured that
        neither actually behaves that way across schemas anyway.
        """
        with connected_to(self.stmarys):
            services.charge(
                self.membership, self.reload_term(), TUITION, narration="Tuition"
            )

        self.membership.end()

        with connected_to(self.stmarys):
            self.assertEqual(
                FeeLedgerEntry.objects.for_student(self.membership.pk).balance(),
                TUITION,
            )
            self.assertEqual(
                FeeLedgerEntry.objects.get().student_name, "Ada Obi"
            )


class WrongStudentTests(TestCase):
    """The check that earns the bare id.

    `student_membership_id` has no foreign key, so the column will accept any
    integer — including the id of a child at another school. Nothing about the
    result would look wrong: the entry sits in this school's ledger, counts
    towards a balance here, and names a student this school has never taught.

    Worth being precise about what a foreign key would have bought, since the
    obvious reading is that this is the cost of not having one. `Membership` is
    shared, so an FK into it constrains only that the row *exists* — every
    school's students are in that one table. The school half of the question has
    to be asked in code either way. What is genuinely given up is the *existence*
    half, and that is the trade docs/tenancy.md records.
    """

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

        self.ours = enroll_student(
            User.objects.create_user("ada", PASSWORD, full_name="Ada Obi"),
            self.stmarys,
        )
        self.theirs = enroll_student(
            User.objects.create_user("emeka", PASSWORD, full_name="Emeka Nwosu"),
            self.grace,
        )
        self.teacher = grant_membership(
            User.objects.create_user("kemi", PASSWORD, full_name="Kemi Bello"),
            self.stmarys,
            Role.TEACHER,
        )

        with connected_to(self.stmarys):
            Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            )

    def test_another_schools_student_cannot_be_charged_here(self):
        with connected_to(self.stmarys):
            term = Term.objects.get()
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.charge(self.theirs, term, TUITION, narration="Tuition")
            self.assertEqual(FeeLedgerEntry.objects.count(), 0)

    def test_a_staff_membership_cannot_be_charged(self):
        """The ledger is keyed on the STUDENT membership, which pins the school."""
        with connected_to(self.stmarys):
            term = Term.objects.get()
            with self.assertRaises(services.NotThisSchoolsStudent):
                services.charge(self.teacher, term, TUITION, narration="Tuition")

    def test_the_check_covers_every_way_in(self):
        with connected_to(self.stmarys):
            term = Term.objects.get()
            for function in (services.charge, services.record_payment, services.discount):
                with self.subTest(function=function.__name__):
                    with self.assertRaises(services.NotThisSchoolsStudent):
                        function(self.theirs, term, TUITION, narration="nope")
            self.assertEqual(FeeLedgerEntry.objects.count(), 0)

    def test_our_own_student_is_fine(self):
        """The guard must not be so tight that it refuses the ordinary case."""
        with connected_to(self.stmarys):
            term = Term.objects.get()
            services.charge(self.ours, term, TUITION, narration="Tuition")
            self.assertEqual(FeeLedgerEntry.objects.count(), 1)


class MigrationStabilityTests(TestCase):
    """`makemigrations` must not propose the same constraints forever.

    The sign rules go into a check constraint as `Q(kind__in=...)`, and the
    obvious spelling for "the kinds that reduce a debt" is a frozenset. A set
    has no order, `tuple(frozenset)` depends on string hashes, and Python
    randomises those per process — so the constraint baked into the migration
    and the one the model computes on the next run compare unequal, and the
    autodetector proposes dropping and recreating it. CI runs
    `makemigrations --check`, so the symptom is a build that goes red at random
    and passes when you rerun it, which is a bad afternoon.

    `INCREASES_DEBT` and `REDUCES_DEBT` are tuples for exactly this reason, and
    this test is what says so out loud.
    """

    def test_every_constraint_survives_a_migration_round_trip(self):
        from django.db.migrations.loader import MigrationLoader

        state = MigrationLoader(None, ignore_no_migrations=True).project_state()
        migrated = {
            constraint.name: constraint
            for constraint in state.models["fees", "feeledgerentry"]
            .options.get("constraints", [])
        }
        declared = {c.name: c for c in FeeLedgerEntry._meta.constraints}

        self.assertEqual(set(declared), set(migrated))
        for name, constraint in declared.items():
            with self.subTest(constraint=name):
                self.assertEqual(
                    constraint,
                    migrated[name],
                    f"{name} does not survive being written to a migration and "
                    f"read back, so makemigrations will propose it forever",
                )

    def test_the_sign_groupings_have_a_stable_order(self):
        """A frozenset here is the bug above waiting to happen again."""
        from fees.models import INCREASES_DEBT, REDUCES_DEBT

        for name, grouping in (
            ("INCREASES_DEBT", INCREASES_DEBT),
            ("REDUCES_DEBT", REDUCES_DEBT),
        ):
            with self.subTest(grouping=name):
                self.assertIsInstance(
                    grouping,
                    tuple,
                    f"{name} goes into a check constraint, so it needs an order",
                )


class LedgerIsolationTests(TestCase):
    """One school cannot see another's books. The reason this app is tenanted."""

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.student = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
        self.membership = enroll_student(self.student, self.stmarys)

    def test_an_entry_at_one_school_is_invisible_at_another(self):
        with connected_to(self.stmarys):
            term = Term.objects.create(
                session="2025/2026",
                name=TermName.FIRST,
                starts_on=date(2025, 9, 15),
                ends_on=date(2025, 12, 12),
            )
            services.charge(self.membership, term, TUITION, narration="Tuition")
            self.assertEqual(FeeLedgerEntry.objects.count(), 1)

        with connected_to(self.grace):
            self.assertEqual(FeeLedgerEntry.objects.count(), 0)
            self.assertEqual(
                FeeLedgerEntry.objects.for_student(self.membership.pk).balance(),
                0,
                "a student's balance at one school must not follow them to another",
            )

    def test_the_ledger_table_is_absent_from_public_not_merely_empty(self):
        """The same load-bearing claim `academics` makes, for money.

        An empty result here instead of an exception would mean the books had
        leaked into the shared schema and isolation had become a query filter.
        """
        with self.assertRaises(ProgrammingError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("select 1 from public.fees_feeledgerentry")
