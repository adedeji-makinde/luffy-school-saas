"""Proof that schema-per-school is real, against a real Postgres.

Nothing here is mocked and nothing skips schema creation. Every `make_school()`
below runs the production path — `CREATE SCHEMA` followed by the full
`migrate_schemas` run for TENANT_APPS — and the assertions read from
`pg_namespace`, `pg_tables` and `pg_constraint` rather than taking Django's
word for any of it.

The claim being defended is the one the whole product rests on: a school's
records are not *filtered* away from other schools, they are in a different
Postgres schema which is not on the other connection's `search_path` at all.
`test_the_tenant_table_is_absent_from_public_not_merely_empty` is the load
bearing test — if it ever goes green by returning an empty list instead of
raising, isolation has silently become a query filter and the claim is false.

Note these are plain `TestCase`s, not `TenantTestCase`. See the harness notes
in docs/tenancy.md and `TenantTestCaseHarnessTests` at the bottom of this file.
"""

import contextlib
from datetime import date

from django.db import IntegrityError, ProgrammingError, connection, transaction
from django.test import TestCase
from django_tenants.test.cases import TenantTestCase

from academics.models import Term, TermName
from accounts.models import Membership, Role, User
from schools.models import Domain, School

PASSWORD = "correct-horse-battery"


def make_school(name, slug, schema_name):
    """A real tenant, the real way.

    `auto_create_schema` is deliberately left alone (it defaults to True), so
    saving this really does issue CREATE SCHEMA and really does migrate
    TENANT_APPS into it. accounts/tests/test_membership.py switches that off
    because those tests only care about public-schema rows; these tests exist
    precisely to exercise what that flag turns off.
    """
    school = School(name=name, slug=slug, schema_name=schema_name)
    school.save()
    return school


def make_term(session="2025/2026", name=TermName.FIRST, **extra):
    extra.setdefault("starts_on", date(2025, 9, 15))
    extra.setdefault("ends_on", date(2025, 12, 12))
    return Term.objects.create(session=session, name=name, **extra)


def query(sql, *params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params or None)
        return cursor.fetchall()


def schema_names():
    """Straight from Postgres — the equivalent of \\dn."""
    return [
        row[0]
        for row in query(
            "select nspname from pg_namespace "
            "where nspname not like 'pg_%%' and nspname <> 'information_schema' "
            "order by 1"
        )
    ]


def tables_in(schema):
    return [
        row[0]
        for row in query(
            "select tablename from pg_tables where schemaname = %s order by 1", schema
        )
    ]


def search_path():
    return query("show search_path")[0][0]


@contextlib.contextmanager
def connected_to(school):
    """Scope the connection to `school`'s schema, as the middleware would.

    Always lands back on public, because leaving a test connected to a dropped
    schema makes the *next* test fail in a thoroughly confusing place.
    """
    connection.set_tenant(school)
    try:
        yield
    finally:
        connection.set_schema_to_public()


class RealSchemaCreationTests(TestCase):
    """Saving a School creates a genuine Postgres schema with genuine tables."""

    def test_saving_a_school_creates_a_real_postgres_schema(self):
        self.assertNotIn("st_marys", schema_names())
        make_school("St Mary's", "st-marys", "st_marys")
        self.assertIn("st_marys", schema_names())

    def test_the_new_schema_holds_the_tenant_apps_tables(self):
        make_school("St Mary's", "st-marys", "st_marys")
        self.assertIn("academics_term", tables_in("st_marys"))

    def test_the_tenant_table_is_never_created_in_public(self):
        """TENANT_APPS must not leak into the shared schema."""
        make_school("St Mary's", "st-marys", "st_marys")
        self.assertNotIn("academics_term", tables_in("public"))

    def test_shared_tables_are_not_created_in_the_tenant_schema(self):
        """`migrate_schemas` prints "Applying accounts.0001_initial... OK" for a
        tenant schema, which reads as though the shared tables were created
        there. They are not: TenantSyncRouter skips the operations and only the
        django_migrations bookkeeping row is written. Pin the truth.
        """
        make_school("St Mary's", "st-marys", "st_marys")
        tenant_tables = tables_in("st_marys")
        self.assertNotIn("accounts_user", tenant_tables)
        self.assertNotIn("accounts_membership", tenant_tables)
        self.assertNotIn("schools_school", tenant_tables)
        # ...even though the migration is recorded as applied in that schema.
        with connected_to(School.objects.get(schema_name="st_marys")):
            applied = {row[0] for row in query("select app from django_migrations")}
        self.assertIn("accounts", applied)

    def test_constraints_and_indexes_are_created_per_schema(self):
        """Not just the table — the constraints come with it, per schema.

        Note where each one lands. A plain UniqueConstraint and a
        CheckConstraint become entries in pg_constraint, but a *partial*
        UniqueConstraint (one with a condition) is implemented by Django as a
        unique index instead, so `one_current_term` is only ever in pg_indexes.
        It is enforced either way; it just is not a table constraint.
        """
        make_school("St Mary's", "st-marys", "st_marys")
        constraints = {
            row[0]
            for row in query(
                "select conname from pg_constraint "
                "where connamespace = 'st_marys'::regnamespace"
            )
        }
        self.assertIn("uniq_term_session_name", constraints)
        self.assertIn("term_ends_after_it_starts", constraints)

        indexes = {
            row[0]
            for row in query(
                "select indexname from pg_indexes where schemaname = 'st_marys'"
            )
        }
        self.assertIn("one_current_term", indexes)

    def test_the_partial_unique_index_is_enforced_inside_the_schema(self):
        """Enforced per schema, so each school may have its own current term."""
        stmarys = make_school("St Mary's", "st-marys", "st_marys")
        grace = make_school("Grace Academy", "grace", "grace")

        with connected_to(stmarys):
            make_term(is_current=True)
            with self.assertRaises(IntegrityError), transaction.atomic():
                make_term(name=TermName.SECOND, is_current=True)

        # ...and St Mary's having a current term does not stop Grace having one.
        with connected_to(grace):
            make_term(is_current=True)
            self.assertEqual(Term.objects.filter(is_current=True).count(), 1)

    def test_saving_a_school_leaves_the_connection_on_public(self):
        """A real surprise worth pinning: django_tenants' create_schema() ends
        with set_schema_to_public(), so you are NOT left inside the school you
        just created. Anything that assumes otherwise writes to public.
        """
        make_school("St Mary's", "st-marys", "st_marys")
        self.assertEqual(connection.schema_name, "public")
        self.assertEqual(search_path(), "public")

    def test_a_domain_routes_to_the_school(self):
        school = make_school("St Mary's", "st-marys", "st_marys")
        Domain.objects.create(tenant=school, domain="stmarys.luffy.school", is_primary=True)
        self.assertEqual(
            Domain.objects.get(domain="stmarys.luffy.school").tenant, school
        )


class SchemaIsolationTests(TestCase):
    """The claim the product rests on. If any of this fails, isolation is a lie."""

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

    def test_a_row_in_one_school_is_not_visible_from_another(self):
        with connected_to(self.stmarys):
            make_term()
            self.assertEqual(Term.objects.count(), 1)

        with connected_to(self.grace):
            self.assertEqual(Term.objects.count(), 0)

    def test_the_tenant_table_is_absent_from_public_not_merely_empty(self):
        """The load-bearing test.

        From public, `academics_term` must not exist *at all*. An empty result
        here instead of an exception would mean the table had leaked into the
        shared schema and isolation had quietly become a query filter.

        The failed statement aborts the surrounding transaction, so the probe
        has to sit in its own savepoint or every later query in this test dies
        with "current transaction is aborted".
        """
        with connected_to(self.stmarys):
            make_term()

        self.assertEqual(search_path(), "public")
        with self.assertRaises(ProgrammingError) as caught:
            with transaction.atomic():
                query("select * from academics_term")
        self.assertIn("does not exist", str(caught.exception))

        # And the connection is still usable afterwards, thanks to the savepoint.
        self.assertEqual(School.objects.count(), 2)

    def test_the_same_natural_key_can_exist_in_both_schools(self):
        """Uniqueness is per-schema, because the index is per-schema.

        Both schools own a "2025/2026 First term" and neither collides. A single
        shared table with a school_id column could not do this without putting
        the school in every unique constraint by hand.
        """
        with connected_to(self.stmarys):
            make_term(starts_on=date(2025, 9, 15), ends_on=date(2025, 12, 12))
        with connected_to(self.grace):
            make_term(starts_on=date(2025, 9, 8), ends_on=date(2025, 12, 19))

        rows = query(
            "select starts_on from st_marys.academics_term "
            "union all select starts_on from grace.academics_term order by 1"
        )
        self.assertEqual([r[0] for r in rows], [date(2025, 9, 8), date(2025, 9, 15)])

    def test_the_two_schools_rows_live_in_physically_different_tables(self):
        """Not the same table filtered two ways — two tables."""
        with connected_to(self.stmarys):
            make_term()
        with connected_to(self.grace):
            make_term()

        oids = query(
            "select (select tableoid from st_marys.academics_term limit 1), "
            "       (select tableoid from grace.academics_term limit 1)"
        )[0]
        self.assertNotEqual(oids[0], oids[1])

    def test_search_path_is_what_does_the_isolating(self):
        """The mechanism itself, stated once so it cannot be quietly changed."""
        with connected_to(self.stmarys):
            self.assertEqual(search_path(), "st_marys, public")
        with connected_to(self.grace):
            self.assertEqual(search_path(), "grace, public")
        self.assertEqual(search_path(), "public")

    def test_dropping_a_school_takes_only_its_own_data(self):
        with connected_to(self.stmarys):
            make_term()
        with connected_to(self.grace):
            make_term()

        self.grace.delete(force_drop=True)  # auto_drop_schema is False by default

        self.assertNotIn("grace", schema_names())
        self.assertIn("st_marys", schema_names())
        with connected_to(self.stmarys):
            self.assertEqual(Term.objects.count(), 1)


class SharedModelsResolveFromInsideATenantTests(TestCase):
    """SHARED_APPS keep working from inside a school's schema.

    They resolve because `public` is the second entry on every tenant
    connection's search_path — which is exactly why one login can span schools.
    """

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

    def test_a_membership_created_inside_a_tenant_reads_back_correctly(self):
        with connected_to(self.stmarys):
            teacher = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
            Membership.objects.create(user=teacher, school=self.stmarys, role=Role.TEACHER)

            self.assertEqual(teacher.roles_at(self.stmarys), {"teacher"})
            self.assertTrue(teacher.has_access_to(self.stmarys))
            self.assertEqual(
                list(teacher.schools().values_list("name", flat=True)), ["St Mary's"]
            )

    def test_the_shared_row_lands_in_public_not_in_the_tenant_schema(self):
        with connected_to(self.stmarys):
            User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
            # Written from inside st_marys, but there is no accounts_user table
            # there — the search_path resolved it to public.
            self.assertNotIn("accounts_user", tables_in("st_marys"))
            self.assertEqual(query("select count(*) from public.accounts_user")[0][0], 1)

    def test_one_parent_spans_two_schools_from_inside_either_one(self):
        """The reason accounts is shared at all, proven from within a schema."""
        parent = User.objects.create_user("bisi", PASSWORD, full_name="Bisi Ade")
        Membership.objects.create(user=parent, school=self.stmarys, role=Role.PARENT)
        Membership.objects.create(user=parent, school=self.grace, role=Role.PARENT)

        for school in (self.stmarys, self.grace):
            with connected_to(school):
                found = User.objects.get(pk=parent.pk)
                self.assertEqual(
                    sorted(found.schools().values_list("name", flat=True)),
                    ["Grace Academy", "St Mary's"],
                )
                # Reachable from inside one school's schema, including the other.
                self.assertTrue(found.has_access_to(self.grace))
                self.assertTrue(found.has_access_to(self.stmarys))

    def test_tenant_data_and_shared_data_are_visible_in_the_same_breath(self):
        """One connection, both worlds: the school's own term and the shared user."""
        with connected_to(self.stmarys):
            user = User.objects.create_user("ada", PASSWORD, full_name="Ada Obi")
            Membership.objects.create(user=user, school=self.stmarys, role=Role.TEACHER)
            make_term()

            self.assertEqual(Term.objects.count(), 1)  # st_marys.academics_term
            self.assertEqual(User.objects.count(), 1)  # public.accounts_user


class TenantTestCaseHarnessTests(TenantTestCase):
    """Pins the two traps in django_tenants' own test harness.

    These assert third-party behaviour on purpose. docs/tenancy.md tells people
    to write tenant tests a particular way *because* of what is pinned here, so
    if a future django-tenants release changes it, this file should fail and
    send someone to update those docs.
    """

    setUpTestData_ran = False

    @classmethod
    def setup_tenant(cls, tenant):
        # Trap 1: without this the harness saves School(schema_name='test') with
        # name='' and slug='', because a blank CharField is not a NULL and so
        # passes the database happily.
        tenant.name = "Harness School"
        tenant.slug = "harness"

    @classmethod
    def setup_domain(cls, domain):
        domain.is_primary = True

    @classmethod
    def setUpTestData(cls):
        cls.setUpTestData_ran = True

    def test_the_harness_creates_a_real_schema_and_sets_the_connection(self):
        self.assertIn("test", schema_names())
        self.assertIn("academics_term", tables_in("test"))
        self.assertEqual(connection.schema_name, "test")
        self.assertEqual(search_path(), "test, public")

    def test_setup_tenant_is_required_for_a_sane_tenant_row(self):
        self.assertEqual(self.tenant.name, "Harness School")
        self.assertEqual(self.tenant.slug, "harness")

    def test_setUpTestData_does_not_run_under_this_harness(self):
        """Trap 2, and the nastier one.

        TenantTestCase.setUpClass never calls super().setUpClass(), so Django's
        TestCase class setup — which is what invokes setUpTestData — is skipped
        entirely. Fixtures written there are silently absent rather than
        erroring, so tests quietly assert against nothing.
        """
        self.assertFalse(
            self.setUpTestData_ran,
            "django-tenants now calls super().setUpClass(); setUpTestData works. "
            "Update the harness section of docs/tenancy.md.",
        )

    def test_tenant_rows_are_still_rolled_back_between_tests(self):
        """Per-test transactions do work, even though class-level setup does not."""
        self.assertEqual(Term.objects.count(), 0)
        make_term()
        self.assertEqual(Term.objects.count(), 1)

    def test_the_previous_test_left_nothing_behind(self):
        self.assertEqual(Term.objects.count(), 0)
