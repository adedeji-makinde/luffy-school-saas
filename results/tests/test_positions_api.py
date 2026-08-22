"""Who can see a position, asked of the HTTP surface rather than the function.

The rule is not a rendering preference and cannot be enforced in a template:

> Position is **staff-only**. It must not reach a parent or a student, in a card
> or in a payload. Enforced at the serializer, because omitting a field from a
> card while leaving it in the JSON is the same leak with an extra step.

So these tests ask the endpoint, through the real middleware stack, as each kind
of caller — and the parent's and student's assertions are about the *bytes that
came back*, not about a flag. `assertNotIn("position", response.content)` is
deliberately cruder than parsing: a leak that renamed the field, nested it, or
put it in an error body would still be a leak, and a structural assertion on
parsed JSON would walk straight past it.

Not `RequestFactory`, for the reason `gradebook/tests/test_api.py` gives: these
are tenant tables and `TenantMainMiddleware` chooses the schema from the
hostname. A test that skipped it would be querying whichever schema the
connection was left on.

Two schools, both with hosts, because "can St Mary's principal read Grace
Academy's broadsheet" is a question about the middleware and the authority check
together, and neither one alone answers it.
"""

from django.db import connection

from academics.models import ClassGroup, Term
from accounts.models import Role, User
from accounts.services import grant_membership, link_guardian
from results.tests.test_positions import PASSWORD, PositionSetUp
from schools.models import Domain, School

HOST = "st-marys.testserver"
THEIR_HOST = "grace.testserver"


class BroadsheetApiSetUp(PositionSetUp):
    def setUp(self):
        super().setUp()

        Domain.objects.create(tenant=self.stmarys, domain=HOST, is_primary=True)
        Domain.objects.create(tenant=self.grace, domain=THEIR_HOST, is_primary=True)

        # The portal. No schema of its own; it is the public one.
        portal = School(name="Portal", slug="portal", schema_name="public")
        portal.auto_create_schema = False
        portal.save()
        Domain.objects.create(tenant=portal, domain="testserver", is_primary=True)

        self.teacher = self._staff("kemi", "Kemi Bello", Role.TEACHER)
        self.vp = self._staff("ngozi", "Ngozi Eze", Role.VICE_PRINCIPAL_ACADEMIC)
        self.registrar = self._staff("bola", "Bola Ade", Role.ADMIN)
        self.bursar = self._staff("femi", "Femi Cash", Role.BURSAR)

        # A child with marks, and their parent.
        self.child = self.enrol(
            self.stmarys, "ada", "Ada A", self.group_id, self.term_id
        )
        self.classmate = self.enrol(
            self.stmarys, "bisi", "Bisi B", self.group_id, self.term_id
        )
        self.mark(self.stmarys, self.term_id, self.maths_id, self.child, 88)
        self.mark(self.stmarys, self.term_id, self.maths_id, self.classmate, 61)

        self.parent_user = User.objects.create_user(
            "mama", PASSWORD, full_name="Mama Ada"
        )
        link_guardian(self.parent_user, self.child)

        self.student_user = self.child.user

    def _staff(self, username, full_name, role):
        user = User.objects.create_user(username, PASSWORD, full_name=full_name)
        grant_membership(user, self.stmarys, role)
        return user

    def tearDown(self):
        # `TenantMainMiddleware` leaves the connection on the school's schema,
        # and `School.save()` refuses to create a tenant from anywhere but
        # public — so without this the failure lands in an unrelated suite.
        connection.set_schema_to_public()
        super().tearDown()

    def signed_in(self, user, host=HOST):
        self.client.force_login(user)
        return self.get(host=host)

    def get(self, host=HOST, group_id=None, term_id=None):
        return self.client.get(
            f"/api/results/classes/{group_id or self.group_id}/broadsheet/"
            f"?term_id={term_id or self.term_id}",
            HTTP_HOST=host,
        )


class StaffCanSeeThePositionTests(BroadsheetApiSetUp):
    def test_each_admitted_role_gets_the_broadsheet(self):
        for actor in (self.teacher, self.vp, self.principal, self.registrar):
            with self.subTest(actor=str(actor)):
                self.client.logout()
                response = self.signed_in(actor)
                self.assertEqual(response.status_code, 200, response.content)
                body = response.json()
                rows = {r["student_membership_id"]: r for r in body["rows"]}
                self.assertEqual(rows[self.child.pk]["position"], 1)
                self.assertEqual(rows[self.classmate.pk]["position"], 2)

    def test_the_class_average_is_staff_visible_and_computed_not_stored(self):
        response = self.signed_in(self.principal)
        self.assertEqual(response.json()["class_average"], "74.50")

    def test_a_subject_position_is_on_every_row(self):
        response = self.signed_in(self.principal)
        rows = {r["student_membership_id"]: r for r in response.json()["rows"]}
        maths = [
            s for s in rows[self.child.pk]["subjects"] if s["subject_id"] == self.maths_id
        ][0]
        self.assertEqual(maths["position"], 1)
        self.assertEqual(maths["percentage"], "88.00")

    def test_an_unmarked_child_comes_back_blank_rather_than_zero(self):
        absent = self.enrol(
            self.stmarys, "chika", "Chika C", self.group_id, self.term_id
        )
        response = self.signed_in(self.principal)
        rows = {r["student_membership_id"]: r for r in response.json()["rows"]}

        self.assertIsNone(rows[absent.pk]["average"])
        self.assertIsNone(rows[absent.pk]["position"])


class PositionNeverReachesAFamilyTests(BroadsheetApiSetUp):
    """The half of the rule that matters, asserted on the raw bytes."""

    def test_a_parent_gets_nothing_and_the_response_carries_no_position(self):
        response = self.signed_in(self.parent_user)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"position", response.content.lower())

    def test_a_student_gets_nothing_and_the_response_carries_no_position(self):
        response = self.signed_in(self.student_user)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"position", response.content.lower())

    def test_an_unauthenticated_caller_gets_nothing(self):
        """**401, not the flat 404 the authenticated refusals give.**

        The router's `session_auth` answers before the view runs, so
        `_require_position_authority()` is never reached. That is not a hole in
        the oracle argument: a 401 is returned whether or not the class exists,
        so it discloses nothing either. The distinction only has to hold among
        callers who got *past* authentication.

        This is the shape the unauthenticated result checker will be. It does
        not exist yet, and when it does it must be built from its own schema
        rather than by filtering this one — issue #21.
        """
        response = self.get()

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(b"position", response.content.lower())

    def test_a_bursar_is_not_admitted_either(self):
        """Staff, but not this staff. A bursar keeps the books."""
        response = self.signed_in(self.bursar)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"position", response.content.lower())

    def test_no_class_average_leaks_to_a_parent_either(self):
        """Settled with position: the class average is a fact about forty-five
        other children, so it is staff-only for the same reason."""
        response = self.signed_in(self.parent_user)

        self.assertNotIn(b"class_average", response.content.lower())
        self.assertNotIn(b"74.50", response.content)


class TheRefusalIsNotAnExistenceOracleTests(BroadsheetApiSetUp):
    """A 404, not a 403, on the reasoning `gradebook.api` settled.

    A parent who could tell "you may not read this class" from "no such class"
    could map the school's entire class list by walking ids. Both answers have
    to be the same answer.
    """

    def test_a_real_class_and_a_missing_one_refuse_identically(self):
        self.client.force_login(self.parent_user)
        real = self.get()
        missing = self.get(group_id=99999)

        self.assertEqual(real.status_code, missing.status_code)
        self.assertEqual(real.content, missing.content)

    def test_the_refusal_does_not_depend_on_the_term_existing(self):
        self.client.force_login(self.parent_user)
        real = self.get()
        missing_term = self.get(term_id=99999)

        self.assertEqual(real.status_code, missing_term.status_code)
        self.assertEqual(real.content, missing_term.content)


class OneSchoolsStaffCannotReadAnothersTests(BroadsheetApiSetUp):
    def test_our_principal_is_refused_at_the_other_schools_host(self):
        """**403 from the door, not this view's 404.**

        `SchoolAccessMiddleware` refuses anybody with no active membership at
        the school whose host they asked for, before routing. So the refusal
        never reaches `_require_position_authority()` at all — worth pinning as
        the layer that actually answers, because a change to this view's
        refusals would not change this case and somebody would assume it had.
        """
        response = self.signed_in(self.principal, host=THEIR_HOST)

        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"position", response.content.lower())

    def test_their_principal_reads_their_own_class_and_sees_only_their_children(self):
        theirs = self.enrol(
            self.grace, "uche", "Uche U", self.their_group_id, self.their_term_id
        )
        self.mark(self.grace, self.their_term_id, self.their_maths_id, theirs, 95)

        self.client.force_login(self.their_principal)
        response = self.get(
            host=THEIR_HOST,
            group_id=self.their_group_id,
            term_id=self.their_term_id,
        )

        self.assertEqual(response.status_code, 200, response.content)
        ids = {r["student_membership_id"] for r in response.json()["rows"]}
        self.assertEqual(ids, {theirs.pk})
        self.assertNotIn(self.child.pk, ids)

    def test_there_is_no_broadsheet_on_the_portal(self):
        """The tables are not there at all, so a 404 rather than a 403 — the
        answer `gradebook.api._school_of()` gives on the same host."""
        response = self.signed_in(self.principal, host="testserver")

        self.assertEqual(response.status_code, 404)


class TheEndpointReadsLiveMarksForNowTests(BroadsheetApiSetUp):
    def test_a_new_mark_changes_the_position_immediately(self):
        """Stated so the *absence* of freezing is a recorded fact rather than an
        assumption. Once task 3 lands, a released term must be served from the
        snapshot instead — a position recomputed after release can silently
        disagree with the card a parent is holding.
        """
        before = self.signed_in(self.principal).json()
        rows = {r["student_membership_id"]: r for r in before["rows"]}
        self.assertEqual(rows[self.child.pk]["position"], 1)
        self.assertEqual(rows[self.classmate.pk]["position"], 2)

        # Maths stands at 88 and 61. English flips it: the child averages
        # (88 + 30) / 2 = 59, the classmate (61 + 100) / 2 = 80.5.
        self.mark(self.stmarys, self.term_id, self.english_id, self.child, 30)
        self.mark(self.stmarys, self.term_id, self.english_id, self.classmate, 100)

        after = self.signed_in(self.principal).json()
        rows = {r["student_membership_id"]: r for r in after["rows"]}
        self.assertEqual(rows[self.classmate.pk]["position"], 1)
        self.assertEqual(rows[self.child.pk]["position"], 2)


class TheSchemaComesFromTheHostNotThePathTests(BroadsheetApiSetUp):
    def test_the_same_class_id_means_a_different_class_on_each_host(self):
        """Per-schema sequences make the ids collide, so this is not academic.

        Both schools' first `ClassGroup` is `pk=1` and both terms are `pk=1`.
        The only thing separating them is the hostname the request arrived on,
        which is exactly why these paths carry no `{slug}` — a slug would be a
        second opinion free to disagree with the connection.
        """
        theirs = self.enrol(
            self.grace, "uche", "Uche U", self.their_group_id, self.their_term_id
        )
        self.mark(self.grace, self.their_term_id, self.their_maths_id, theirs, 95)

        self.assertEqual(self.group_id, self.their_group_id)
        self.assertEqual(self.term_id, self.their_term_id)

        self.client.force_login(self.principal)
        ours = self.get(host=HOST)
        self.client.logout()
        self.client.force_login(self.their_principal)
        theirs_response = self.get(
            host=THEIR_HOST, group_id=self.group_id, term_id=self.term_id
        )

        self.assertEqual(
            {r["student_membership_id"] for r in ours.json()["rows"]},
            {self.child.pk, self.classmate.pk},
        )
        self.assertEqual(
            {r["student_membership_id"] for r in theirs_response.json()["rows"]},
            {theirs.pk},
        )
