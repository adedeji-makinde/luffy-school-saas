"""The three marking-sheet endpoints, exercised the way a blur uses them.

Not `RequestFactory`: these go through the Django test client so that
`TenantMainMiddleware` and `SchoolAccessMiddleware` actually run. For the
gradebook that is not incidental — the tenant middleware is what chooses the
*schema* these tables live in, and it chooses it from the hostname. So every
request here carries a school's own host, and `setUp` registers one. A test that
skipped the middleware would be querying whichever schema the connection
happened to be left on, which is the one thing the routing is built to prevent.

The portal host is registered too, because "there is no gradebook here" is a
behaviour worth pinning rather than a state left untested.

The sections follow the interaction: read a sheet, save one cell, take one back.
The retry section is the one carrying the weight — blur fires more than once for
a single edit, and a version check that calls the second firing a conflict makes
the conflict warning worthless by crying wolf at a teacher working alone.
"""

from django.db import connection

from accounts.models import Role, User
from accounts.services import enroll_student, grant_membership
from gradebook.models import Score
from gradebook.tests.test_scores import (
    PASSWORD,
    GradebookSetUp,
    connected_to,
    make_school,
)
from schools.models import Domain, School

HOST = "st-marys.testserver"


class GradebookApiSetUp(GradebookSetUp):
    def setUp(self):
        super().setUp()

        # The school's own host. Everything below is a request to it.
        Domain.objects.create(tenant=self.stmarys, domain=HOST, is_primary=True)

        # The portal, where a parent with children at several schools signs in.
        # No schema of its own; it is the public one.
        portal = School(name="Portal", slug="portal", schema_name="public")
        portal.auto_create_schema = False
        portal.save()
        Domain.objects.create(tenant=portal, domain="testserver", is_primary=True)

        self.other_teacher = grant_membership(
            User.objects.create_user("tunde", PASSWORD, full_name="Tunde Ade"),
            self.stmarys,
            Role.TEACHER,
        )
        self.parent = grant_membership(
            User.objects.create_user("ngozi", PASSWORD, full_name="Ngozi Obi"),
            self.stmarys,
            Role.PARENT,
        )

    def tearDown(self):
        """Put the connection back on `public`, or poison every test after this one.

        `TenantMainMiddleware` calls `set_tenant()` on the way in and has no
        reason to undo it: in production the connection ends with the response.
        In a test process the same connection is handed to the next test, which
        then starts life on `st_marys` — and `School.save()` refuses to create a
        tenant from anywhere but `public`, so the failure lands in unrelated
        suites with a message about a schema they never mentioned.

        `schools/tests/test_invitation_api.py` never needed this: its host is the
        portal, which resolves to `public`, so its requests leave the connection
        where they found it. This is the first suite to make a request to a
        school's own host, which is why the trap is only being sprung now.
        """
        connection.set_schema_to_public()
        super().tearDown()

    # -- request helpers, all of them on the school's host -------------------

    def sheet(self, assessment_id=None):
        return self.client.get(
            f"/api/gradebook/assessments/{assessment_id or self.first_ca_id}/sheet/",
            HTTP_HOST=HOST,
        )

    def save(self, student, value, expected_version=None, assessment_id=None):
        body = {"value": value, "expected_version": expected_version}
        return self.client.put(
            f"/api/gradebook/assessments/{assessment_id or self.first_ca_id}"
            f"/scores/{student}/",
            data=body,
            content_type="application/json",
            HTTP_HOST=HOST,
        )

    def clear(self, student, expected_version, assessment_id=None):
        return self.client.delete(
            f"/api/gradebook/assessments/{assessment_id or self.first_ca_id}"
            f"/scores/{student}/?expected_version={expected_version}",
            HTTP_HOST=HOST,
        )

    def row_for(self, body, student):
        (row,) = [r for r in body["rows"] if r["student_membership_id"] == student.pk]
        return row


class MarkingSheetTests(GradebookApiSetUp):
    """What a teacher is handed before they type anything."""

    def test_the_sheet_lists_students_who_have_no_mark(self):
        """The unmarked children are the reason this comes from the roll.

        They have no `Score` row at all, so a sheet built from the score table
        would be a sheet of the children already marked — useless for marking.
        """
        self.client.force_login(self.teacher.user)
        body = self.sheet().json()

        self.assertEqual(len(body["rows"]), 2)
        ada = self.row_for(body, self.ada)
        self.assertIsNone(ada["value"])
        self.assertIsNone(ada["version"])
        self.assertEqual(ada["student"], "Ada Obi")

    def test_value_and_version_arrive_together_for_a_marked_student(self):
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        ada = self.row_for(self.sheet().json(), self.ada)
        self.assertEqual(ada["value"], 15)
        self.assertEqual(ada["version"], 1)
        self.assertEqual(ada["max_score"], 20)

    def test_each_student_gets_their_own_total_not_one_row_per_mark(self):
        """Regression: `values().annotate()` inherits `Meta.ordering` into GROUP BY.

        `Score.Meta.ordering` names `assessment`, so without clearing the order
        the grouping is by student *and assessment* — one row per mark, and a
        "total" that is only ever the single mark it came from. It shows up the
        moment a student has two marks in one subject, and not before.
        """
        self.client.force_login(self.teacher.user)
        with connected_to(self.stmarys):
            exam_id = self.exam(max_score=100).pk

        self.save(self.ada.pk, 15)
        self.save(self.ada.pk, 70, assessment_id=exam_id)

        ada = self.row_for(self.sheet().json(), self.ada)
        self.assertEqual(ada["total"], {"scored": 85, "available": 120, "marked": 2})

    def test_an_unmarked_student_totals_to_zero_rather_than_null(self):
        self.client.force_login(self.teacher.user)
        emeka = self.row_for(self.sheet().json(), self.emeka)
        self.assertEqual(emeka["total"], {"scored": 0, "available": 0, "marked": 0})

    def test_a_parent_at_this_school_cannot_read_the_class_sheet(self):
        """Belonging to the school is what the middleware checked. Not enough.

        A sheet is every child's marks side by side, which is precisely what a
        parent must not be handed for a class their own child sits in.
        """
        self.client.force_login(self.parent.user)
        self.assertEqual(self.sheet().status_code, 403)

    def test_a_signed_out_caller_gets_nothing(self):
        self.assertEqual(self.sheet().status_code, 401)

    def test_there_is_no_gradebook_on_the_portal_host(self):
        """404, not 403: on the portal these tables do not exist to be refused."""
        self.client.force_login(self.teacher.user)
        response = self.client.get(
            f"/api/gradebook/assessments/{self.first_ca_id}/sheet/",
            HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, 404)


class SavingOneMarkTests(GradebookApiSetUp):
    """What a blur does, and what it is told back."""

    def test_a_first_mark_is_entered_and_the_total_comes_back_with_it(self):
        """The response carries what the save invalidated: version and total.

        Both are on screen, and both are wrong the instant the write lands. A
        client that has to ask again for them is a client that can forget to.
        """
        self.client.force_login(self.teacher.user)
        response = self.save(self.ada.pk, 15)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["value"], 15)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["total"], {"scored": 15, "available": 20, "marked": 1})

    def test_no_row_is_written_until_a_mark_is_entered(self):
        self.client.force_login(self.teacher.user)
        self.sheet()

        with connected_to(self.stmarys):
            self.assertEqual(Score.objects.count(), 0)

    def test_saving_on_the_version_shown_moves_it_on(self):
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        response = self.save(self.ada.pk, 18, expected_version=1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], 2)
        self.assertEqual(response.json()["value"], 18)

    def test_a_stale_version_is_refused_and_says_what_the_mark_now_is(self):
        """The 409 has a body because "somebody changed this" is not actionable."""
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)
        self.save(self.ada.pk, 18, expected_version=1)

        response = self.save(self.ada.pk, 19, expected_version=1)
        self.assertEqual(response.status_code, 409)
        current = response.json()["current"]
        self.assertEqual(current["value"], 18)
        self.assertEqual(current["version"], 2)

    def test_a_mark_cleared_underneath_the_teacher_reports_no_current_mark(self):
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)
        self.clear(self.ada.pk, expected_version=1)

        response = self.save(self.ada.pk, 19, expected_version=1)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(response.json()["current"])

    def test_a_mark_above_the_maximum_is_the_caller_s_to_fix(self):
        self.client.force_login(self.teacher.user)
        response = self.save(self.ada.pk, 21)
        self.assertEqual(response.status_code, 422)

    def test_a_parent_cannot_enter_a_mark(self):
        self.client.force_login(self.parent.user)
        self.assertEqual(self.save(self.ada.pk, 15).status_code, 403)

    def test_a_bursar_cannot_enter_a_mark(self):
        bursar = grant_membership(
            User.objects.create_user("bola", PASSWORD, full_name="Bola Eze"),
            self.stmarys,
            Role.BURSAR,
        )
        self.client.force_login(bursar.user)
        self.assertEqual(self.save(self.ada.pk, 15).status_code, 403)

    def test_a_suspended_teacher_cannot_enter_a_mark(self):
        """The membership exists; the access does not. `roles_at()` is the line."""
        from accounts.models import MembershipStatus

        self.teacher.status = MembershipStatus.SUSPENDED
        self.teacher.save(update_fields=["status"])

        self.client.force_login(self.teacher.user)
        # The middleware refuses first: no live role here at all.
        self.assertEqual(self.save(self.ada.pk, 15).status_code, 403)

    def test_another_school_s_child_is_not_found_and_is_not_named(self):
        """A flat 404, and nothing of the other tenant's data in the body.

        `services.NotThisSchoolsStudent` names the child and the school they
        actually attend — right for a log, a cross-tenant leak in a response.
        The narrow lookup means that refusal is never reached over HTTP.
        """
        grace = make_school("Grace Academy", "grace", "grace")
        theirs = enroll_student(
            User.objects.create_user("chidi", PASSWORD, full_name="Chidi Okafor"),
            grace,
        )

        self.client.force_login(self.teacher.user)
        response = self.save(theirs.pk, 15)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Chidi", response.content.decode())
        self.assertNotIn("Grace", response.content.decode())


class ExistenceOracleTests(GradebookApiSetUp):
    """A refused caller learns nothing about what exists.

    Both write routes look an assessment and a student up before anything asks
    whether the caller may mark at all. Left that way, the status code answers a
    question the caller was never allowed to ask: 404 for a row that is not
    there, 403 for one that is. Anybody signed in at the school can read that
    difference — a parent, a student, a bursar — and walk the id space of their
    own school's assessments and student memberships without ever being able to
    write a mark.

    `marking_sheet()` has always gated first. These two now match it, so every
    refusal a non-marker can provoke is the same 403 whatever is behind it.
    """

    ABSENT = 10**9

    def setUp(self):
        super().setUp()
        self.client.force_login(self.parent.user)

    def test_a_parent_cannot_tell_a_real_assessment_from_an_invented_one(self):
        real = self.save(self.ada.pk, 15)
        invented = self.save(self.ada.pk, 15, assessment_id=self.ABSENT)

        self.assertEqual(real.status_code, 403)
        self.assertEqual(invented.status_code, 403)

    def test_a_parent_cannot_tell_a_student_from_a_stranger(self):
        student = self.save(self.ada.pk, 15)
        stranger = self.save(self.ABSENT, 15)
        # A membership at this school that is not a student's: found by the
        # id lookup, refused by the role filter. Also a 403 now.
        not_a_student = self.save(self.teacher.pk, 15)

        self.assertEqual(student.status_code, 403)
        self.assertEqual(stranger.status_code, 403)
        self.assertEqual(not_a_student.status_code, 403)

    def test_clearing_gives_nothing_away_either(self):
        real = self.clear(self.ada.pk, expected_version=1)
        invented = self.clear(
            self.ada.pk, expected_version=1, assessment_id=self.ABSENT
        )
        stranger = self.clear(self.ABSENT, expected_version=1)

        self.assertEqual(real.status_code, 403)
        self.assertEqual(invented.status_code, 403)
        self.assertEqual(stranger.status_code, 403)

    def test_a_teacher_still_gets_a_404_for_something_that_is_not_there(self):
        """The gate closes on non-markers, not on everyone.

        A teacher may mark, so for them "no such assessment" is a fact they are
        entitled to and 404 is the honest answer. Without this, hoisting the
        authority check could have been "return 403 to everybody", which hides
        the caller's own typo from the one person who can act on it.
        """
        self.client.force_login(self.teacher.user)

        self.assertEqual(
            self.save(self.ada.pk, 15, assessment_id=self.ABSENT).status_code, 404
        )
        self.assertEqual(self.save(self.ABSENT, 15).status_code, 404)


class RetriedBlurTests(GradebookApiSetUp):
    """Blur fires twice for one edit. That is not two teachers.

    The version check exists to warn about a second person in the sheet. If it
    also fires when one person's own request arrives twice — a tab-out followed
    by a submit, a retry of a request whose response was lost — then teachers
    working alone see the warning constantly and stop reading it, which costs
    exactly the protection it was added for.
    """

    def test_a_repeated_first_mark_is_not_a_conflict(self):
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        again = self.save(self.ada.pk, 15)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["version"], 1)

    def test_a_repeated_update_is_not_a_conflict(self):
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)
        self.save(self.ada.pk, 18, expected_version=1)

        again = self.save(self.ada.pk, 18, expected_version=1)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["value"], 18)
        self.assertEqual(again.json()["version"], 2)

    def test_the_repeat_does_not_write_again(self):
        """Swallowed, not re-applied: the version must not creep on a retry."""
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)
        self.save(self.ada.pk, 15)
        self.save(self.ada.pk, 15)

        with connected_to(self.stmarys):
            self.assertEqual(Score.objects.get(assessment_id=self.first_ca_id).version, 1)

    def test_a_retry_carrying_a_different_value_is_still_a_conflict(self):
        """Same author, different number — the mark genuinely moved."""
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        response = self.save(self.ada.pk, 16)
        self.assertEqual(response.status_code, 409)

    def test_somebody_else_writing_the_same_number_is_still_a_conflict(self):
        """Agreement is not the test. Another person in the sheet is the event."""
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        self.client.force_login(self.other_teacher.user)
        response = self.save(self.ada.pk, 15)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["current"]["value"], 15)


class ClearingAMarkTests(GradebookApiSetUp):
    def test_clearing_removes_the_row_rather_than_zeroing_it(self):
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        response = self.clear(self.ada.pk, expected_version=1)
        self.assertEqual(response.status_code, 200)
        with connected_to(self.stmarys):
            self.assertFalse(
                Score.objects.filter(student_membership_id=self.ada.pk).exists()
            )

    def test_the_refreshed_total_comes_back_with_the_cleared_cell(self):
        """200 with a body, not 204: the total on screen has just changed too."""
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        body = self.clear(self.ada.pk, expected_version=1).json()
        self.assertIsNone(body["value"])
        self.assertIsNone(body["version"])
        self.assertEqual(body["total"], {"scored": 0, "available": 0, "marked": 0})

    def test_clearing_on_a_stale_version_is_refused(self):
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)
        self.save(self.ada.pk, 18, expected_version=1)

        response = self.clear(self.ada.pk, expected_version=1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["current"]["value"], 18)

    def test_the_version_is_not_optional(self):
        """No default, mirroring `services.clear_score()`, and for its reason:
        "clear whatever is there" is the destructive write the version prevents.
        """
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        response = self.client.delete(
            f"/api/gradebook/assessments/{self.first_ca_id}/scores/{self.ada.pk}/",
            HTTP_HOST=HOST,
        )
        self.assertEqual(response.status_code, 422)

    def test_a_parent_cannot_clear_a_mark(self):
        self.client.force_login(self.teacher.user)
        self.save(self.ada.pk, 15)

        self.client.force_login(self.parent.user)
        self.assertEqual(self.clear(self.ada.pk, expected_version=1).status_code, 403)
