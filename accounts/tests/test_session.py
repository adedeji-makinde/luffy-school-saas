"""How long a session lasts, and what the API says once it has not.

Two questions, and the second only matters because of the first. Django's
defaults answer both badly for this product: the window runs from login rather
than from last activity, and every failure to authenticate comes back as the same
bare 401. Together those mean a teacher can be timed out in the middle of work
they are actively doing, and the client that finds out cannot tell that from
"you were never signed in" — so it has nothing safe to do but discard whatever
was in the form.

The endpoint used below is an invitation route rather than a marking sheet:
these are properties of the session layer, which sits under every endpoint, and
proving them on a route with no tenant schema keeps them from looking like
gradebook behaviour. `gradebook/tests/test_session_expiry.py` tells the same
story from the teacher's end, where the stakes are.
"""

from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import TestCase, override_settings

from accounts.models import Role, User
from accounts.services import grant_membership
from accounts.session import NOT_AUTHENTICATED, SESSION_EXPIRED
from schools.models import Domain, School
from schools.tests.test_invitations import make_school

PASSWORD = "correct-horse-battery"


class SessionSetUp(TestCase):
    def setUp(self):
        portal = School(name="Portal", slug="portal", schema_name="public")
        portal.auto_create_schema = False
        portal.save()
        Domain.objects.create(tenant=portal, domain="testserver", is_primary=True)

        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.admin = User.objects.create_user(
            "admin@st-marys.school", PASSWORD, full_name="Ada Admin"
        )
        grant_membership(self.admin, self.stmarys, Role.ADMIN)

    def a_request(self):
        """Any authenticated route, chosen for having no side effect.

        The slug names no school, so an authenticated caller gets a 404 from the
        view and nothing is written. That is deliberate: these tests are about
        the session layer, which runs either way, and an invitation actually
        being minted on every one of them would be noise that could start
        failing for reasons that have nothing to do with sessions.

        An unauthenticated caller never reaches the view at all — ninja
        authenticates first — so the 401 cases are unaffected by the slug.
        """
        return self.client.post(
            "/api/schools/no-such-school/invitations/",
            data={"role": Role.TEACHER.value, "email": "t@example.com"},
            content_type="application/json",
        )

    def stored_session(self):
        return Session.objects.get(session_key=self.client.session.session_key)


class SlidingExpiryTests(SessionSetUp):
    """The window is idle time, not total time.

    This is the actual fix for "logged out mid-form-entry". Everything else in
    this branch is about handling the expiry gracefully; this is about it not
    happening to somebody who is sitting there working.
    """

    def test_activity_pushes_the_expiry_back(self):
        self.client.force_login(self.admin)
        before = self.stored_session().expire_date

        self.a_request()

        self.assertGreater(
            self.stored_session().expire_date,
            before,
            "a request must extend the session, or the clock runs from login "
            "and a teacher can be timed out while actively marking",
        )

    @override_settings(SESSION_SAVE_EVERY_REQUEST=False)
    def test_and_the_test_above_is_measuring_the_setting(self):
        """A control, because the assertion above would pass for a bad reason.

        `expire_date` moving could just as easily mean the session row is
        rewritten by something else on the way through. Turn the setting off and
        it must stop moving — otherwise the test proves nothing about why.
        """
        self.client.force_login(self.admin)
        before = self.stored_session().expire_date

        self.a_request()

        self.assertEqual(self.stored_session().expire_date, before)


class ExpiredSessionTests(SessionSetUp):
    """The 401 has to say which 401 it is.

    Deleting the session row while the client keeps its cookie is exactly the
    state a timed-out browser is in: it still presents a credential, and the
    credential no longer resolves to anybody.
    """

    def test_a_lapsed_session_is_told_apart_from_never_having_signed_in(self):
        self.client.force_login(self.admin)
        Session.objects.all().delete()

        body = self.a_request().json()

        self.assertEqual(body["code"], SESSION_EXPIRED)
        self.assertTrue(
            body["retryable"],
            "a lapsed session is the recoverable case: the work in the browser "
            "is still good once the person signs in again",
        )

    def test_a_caller_who_never_signed_in_is_told_that_instead(self):
        body = self.a_request().json()

        self.assertEqual(body["code"], NOT_AUTHENTICATED)
        self.assertFalse(
            body["retryable"],
            "nothing was lost, because nothing was signed in — telling this "
            "caller to retry would send them round a loop",
        )

    def test_a_forged_cookie_is_answered_exactly_as_an_expired_one(self):
        """`code` is not a session-key oracle, and this is what holds that.

        Splitting one 401 into two is the kind of change that quietly becomes a
        disclosure, so the claim is worth a test rather than an argument. A
        random cookie is unusable for the same reason an expired one is, and gets
        the same answer — nothing here reports whether the key was ever real.
        """
        self.client.cookies[settings.SESSION_COOKIE_NAME] = "not-a-real-session-key"

        self.assertEqual(self.a_request().json()["code"], SESSION_EXPIRED)

    def test_both_are_still_a_401(self):
        """The status code does not change; only what the body can tell you.

        A client that knows nothing of `code` must keep working exactly as it
        did, which is what makes this safe to add to a live API.
        """
        self.assertEqual(self.a_request().status_code, 401)

        self.client.force_login(self.admin)
        Session.objects.all().delete()
        self.assertEqual(self.a_request().status_code, 401)
