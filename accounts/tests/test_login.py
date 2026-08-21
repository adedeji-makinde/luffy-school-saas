"""Signing in: where it is served, what it refuses, and what it will not say.

Three things are being pinned here, and they are worth separating because they
fail differently.

**Where.** Sign-in is on the portal host only. That is the decision that makes
one login reach every school a person belongs to, and it is testable directly:
the same request is a 200 on the portal and a 404 on a school's host.

**What it will not say.** A sign-in route is the easiest place in a platform to
build an account-existence oracle, because every reason to refuse is a fact
somebody would like to know. The tests below compare whole responses rather than
status codes — no account, wrong password and deactivated account must be
indistinguishable, and so must a throttled attempt against a real identifier and
one against an invented one.

**The throttle.** Counted, not locked. The tests hold both halves of that: the
window closes, and it opens again on its own without anybody being unlocked.

`SESSION_COOKIE_DOMAIN` is the one part of this that a test client cannot
honestly exercise — Django's client sends every cookie it holds to every host it
is pointed at, so a session arriving at a school host proves the *session* is
good there, not that a browser would have sent it. What the cookie is stamped
with is asserted directly instead, and the deployment rule that it must be
stamped at all is `accounts.checks.session_cookie_spans_every_host()`.
"""

from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts import throttling
from accounts.models import Role, SignInAttempts, SignInScope, User
from accounts.services import grant_membership
from accounts.session import NOT_AUTHENTICATED
from accounts.signin import BAD_CREDENTIALS, TOO_MANY_ATTEMPTS
from schools.models import Domain, School
from schools.tests.test_invitations import make_school

PASSWORD = "correct-horse-battery"
PORTAL = "testserver"
SCHOOL_HOST = "st-marys.testserver"


class SignInSetUp(TestCase):
    def setUp(self):
        portal = School(name="Portal", slug="portal", schema_name="public")
        portal.auto_create_schema = False
        portal.save()
        Domain.objects.create(tenant=portal, domain=PORTAL, is_primary=True)

        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        Domain.objects.create(tenant=self.stmarys, domain=SCHOOL_HOST, is_primary=True)

        self.teacher = User.objects.create_user(
            "tayo",
            PASSWORD,
            email="tayo@st-marys.school",
            phone="08031234567",
            full_name="Tayo Teacher",
        )
        grant_membership(self.teacher, self.stmarys, Role.TEACHER)

    def tearDown(self):
        """Back to `public`, for the reason `gradebook/tests/test_api.py` gives.

        `TenantMainMiddleware` leaves the connection on whichever schema the
        host resolved to, and a test process hands that connection to the next
        test — which then cannot create a tenant. Some of the requests below go
        to a school's host, so this suite springs the same trap.
        """
        connection.set_schema_to_public()
        super().tearDown()

    def sign_in(self, identifier="tayo", password=PASSWORD, host=PORTAL, **extra):
        return self.client.post(
            "/api/login/",
            data={"identifier": identifier, "password": password},
            content_type="application/json",
            HTTP_HOST=host,
            **extra,
        )

    def signed_in(self) -> bool:
        return "_auth_user_id" in self.client.session


class WhereSignInLivesTests(SignInSetUp):
    """The portal, and nowhere else."""

    def test_the_portal_serves_it(self):
        self.assertEqual(self.sign_in().status_code, 200)

    def test_a_schools_own_host_does_not(self):
        """A 404: on this host there is no such route.

        Not a redirect and not a 403. A school's host refuses anybody without an
        active membership *there*, so a sign-in served from it would quietly
        mean something narrower than sign-in — the parent with children at two
        schools would need one session per school.
        """
        response = self.sign_in(host=SCHOOL_HOST)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.signed_in())


class SigningInTests(SignInSetUp):
    def test_any_of_the_three_identifiers_works(self):
        for identifier in ("tayo", "tayo@st-marys.school", "08031234567"):
            with self.subTest(identifier=identifier):
                self.client.logout()
                self.assertEqual(self.sign_in(identifier=identifier).status_code, 200)
                self.assertTrue(self.signed_in())

    def test_an_identifier_is_matched_however_it_is_spelled(self):
        """The stored phone is E.164; a parent types what is on the form."""
        self.assertEqual(self.sign_in(identifier="+234 803 123 4567").status_code, 200)

    def test_the_response_says_where_the_work_is(self):
        """Name and slug are not enough — the client has to send them to a host."""
        body = self.sign_in().json()

        self.assertEqual(body["full_name"], "Tayo Teacher")
        self.assertEqual(
            body["schools"],
            [{"slug": "st-marys", "name": "St Mary's", "host": SCHOOL_HOST}],
        )
        self.assertTrue(body["csrf_token"])

    def test_a_school_with_no_primary_domain_is_still_listed(self):
        """Reported by omission. A setup fault must not cost somebody their login."""
        grace = make_school("Grace Academy", "grace", "grace")
        grant_membership(self.teacher, grace, Role.PARENT)

        hosts = {s["slug"]: s["host"] for s in self.sign_in().json()["schools"]}

        self.assertEqual(hosts, {"st-marys": SCHOOL_HOST, "grace": None})

    def test_signing_in_replaces_the_session_key(self):
        """Django's `login()` cycles the key, so a key fixed beforehand is not kept."""
        self.client.get("/api/invitations/nothing/", HTTP_HOST=PORTAL)
        before = self.client.session.session_key

        self.sign_in()

        self.assertNotEqual(self.client.session.session_key, before)

    @override_settings(SESSION_COOKIE_DOMAIN=".testserver")
    def test_the_session_cookie_is_stamped_for_every_host(self):
        """The mechanism that makes one login reach every school.

        A cookie set with no Domain goes back only to the host that set it, so
        without this the teacher signs in on the portal and arrives at their own
        school as a stranger. Asserted on the cookie itself because a test
        client sends what it holds regardless of domain and so could never
        notice the difference.
        """
        response = self.sign_in()

        self.assertEqual(response.cookies["sessionid"]["domain"], ".testserver")

    def test_the_session_is_good_on_the_schools_host(self):
        """Signed in on the portal, authenticated at the school.

        403 rather than 201 because a teacher may not issue invitations — which
        is the point: authorisation is being applied, so authentication already
        happened. A session that did not travel would be a 401 here.
        """
        self.sign_in()

        response = self.client.post(
            "/api/schools/st-marys/invitations/",
            data={"role": Role.TEACHER.value, "email": "new@st-marys.school"},
            content_type="application/json",
            HTTP_HOST=SCHOOL_HOST,
        )

        self.assertEqual(response.status_code, 403)


class RefusalSaysNothingTests(SignInSetUp):
    """Every way of failing looks the same from outside.

    Compared as whole responses, not as status codes. A body that differed by a
    word would be as good an oracle as a body that differed by a status.
    """

    def _refusal(self, **kwargs):
        response = self.sign_in(**kwargs)
        return response.status_code, response.json()

    def test_wrong_password(self):
        status, body = self._refusal(password="not-the-password")

        self.assertEqual(status, 401)
        self.assertEqual(body["code"], BAD_CREDENTIALS)
        self.assertFalse(body["retryable"])
        self.assertFalse(self.signed_in())

    def test_an_account_that_does_not_exist_is_told_the_same_thing(self):
        self.assertEqual(
            self._refusal(identifier="nobody@nowhere.test"),
            self._refusal(password="not-the-password"),
        )

    def test_a_deactivated_account_is_told_the_same_thing(self):
        """Right password, disabled account — and it must not read as different.

        `IdentifierBackend` already refuses an inactive user; what is pinned
        here is that the refusal is not distinguishable from any other, because
        "this account exists but is disabled" is a fact about somebody else.
        """
        self.teacher.is_active = False
        self.teacher.save(update_fields=["is_active"])

        self.assertEqual(
            self._refusal(),
            self._refusal(identifier="nobody@nowhere.test"),
        )

    def test_nothing_echoes_the_password(self):
        response = self.sign_in(password="not-the-password")

        self.assertNotIn("not-the-password", response.content.decode())


@override_settings(
    SIGN_IN_MAX_FAILURES_PER_IDENTIFIER=3, SIGN_IN_MAX_FAILURES_PER_ADDRESS=100
)
class ThrottleTests(SignInSetUp):
    def fail_times(self, count, **kwargs):
        for _ in range(count):
            self.sign_in(password="not-the-password", **kwargs)

    def test_the_window_closes_after_the_limit(self):
        self.fail_times(3)

        response = self.sign_in(password="not-the-password")

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["code"], TOO_MANY_ATTEMPTS)
        self.assertTrue(body["retryable"])
        self.assertGreater(body["retry_after"], 0)
        self.assertEqual(response["Retry-After"], str(body["retry_after"]))

    def test_the_right_password_is_refused_while_the_window_is_closed(self):
        """The throttle is asked before the credentials, not after.

        Checking afterwards would leave the endpoint fully guessable by anyone
        willing to read past the 429 — and would make the 429 itself report
        whether the guess had been right.
        """
        self.fail_times(3)

        self.assertEqual(self.sign_in().status_code, 429)
        self.assertFalse(self.signed_in())

    def test_being_throttled_says_nothing_about_who_exists(self):
        """A real identifier and an invented one are throttled identically.

        This is the half of the no-oracle rule that a throttle is most likely to
        break: counting only against accounts that turned out to exist would make
        the 429 mean "this one is real".
        """
        self.fail_times(3)
        real = self.sign_in(password="not-the-password")

        self.fail_times(3, identifier="nobody@nowhere.test")
        invented = self.sign_in(
            identifier="nobody@nowhere.test", password="not-the-password"
        )

        self.assertEqual(real.status_code, 429)
        self.assertEqual(invented.status_code, 429)
        self.assertEqual(real.json()["code"], invented.json()["code"])
        self.assertEqual(real.json()["detail"], invented.json()["detail"])

    def test_the_window_reopens_on_its_own(self):
        """Counted, not locked: waiting is the whole of the remedy.

        Nobody is unlocked and no administrator is involved, which is the
        property that makes a semi-public identifier safe to publish.
        """
        self.fail_times(3)
        self.assertEqual(self.sign_in().status_code, 429)

        SignInAttempts.objects.filter(scope=SignInScope.IDENTIFIER).update(
            window_started_at=timezone.now() - timedelta(seconds=901)
        )

        self.assertEqual(self.sign_in().status_code, 200)

    def test_signing_in_forgives_the_earlier_mistakes(self):
        """Two mistypes in the morning must not cost a wait in the afternoon."""
        self.fail_times(2)
        self.assertEqual(self.sign_in().status_code, 200)

        self.client.logout()
        self.fail_times(2)

        self.assertEqual(self.sign_in().status_code, 200)

    def test_signing_in_does_not_forgive_the_address(self):
        """Otherwise an attacker resets the address count with their own account.

        The identifier counter is cleared by a success and the address counter
        is not, and the difference is exactly this: one is the account being
        guessed at, the other is the machine doing the guessing.
        """
        self.fail_times(2, identifier="nobody@nowhere.test")
        before = SignInAttempts.objects.get(scope=SignInScope.ADDRESS).failures

        self.sign_in()

        self.assertEqual(
            SignInAttempts.objects.get(scope=SignInScope.ADDRESS).failures, before
        )

    @override_settings(SIGN_IN_MAX_FAILURES_PER_ADDRESS=4)
    def test_one_machine_is_bounded_across_every_account(self):
        """The credential-stuffing shape the per-identifier limit cannot see.

        Four different identifiers, one failure each: no identifier is near its
        own limit and the address is at the end of its own.
        """
        for n in range(4):
            self.sign_in(password="wrong", identifier=f"person{n}@nowhere.test")

        self.assertEqual(self.sign_in().status_code, 429)

    @override_settings(SIGN_IN_MAX_FAILURES_PER_ADDRESS=2)
    def test_another_machine_is_unaffected(self):
        """A school behind one NAT address must not lock out the school next door."""
        self.fail_times(2, identifier="nobody@nowhere.test", REMOTE_ADDR="10.0.0.9")

        self.assertEqual(self.sign_in(REMOTE_ADDR="10.0.0.10").status_code, 200)

    def test_the_identifier_is_not_stored_in_the_clear(self):
        """Whatever was typed may not be an identifier at all.

        People put their password in the identifier box. A throttle table that
        kept those verbatim would be a credential store that nobody decided to
        build.
        """
        self.fail_times(1, identifier="hunter2-is-my-password")

        keys = SignInAttempts.objects.filter(
            scope=SignInScope.IDENTIFIER
        ).values_list("key", flat=True)

        self.assertNotIn("hunter2-is-my-password", keys)
        self.assertEqual({len(key) for key in keys}, {64})


class CountersCollideOnlyOnTheirOwnConstraintTests(TestCase):
    """`_locked_counter()` retries a collision and re-raises everything else.

    The retry is reached only when two requests create one key's row at the same
    instant, which a threaded test can arrange but not guarantee. What the
    recognition rests on — the name of the constraint Postgres refused the row
    with — is deterministic, so it is asked directly. Same reasoning as
    `gradebook.services._is_the_first_mark_colliding()`: an `IntegrityError`
    says a rule fired, not which, and treating them alike sends a caller round a
    retry loop that cannot terminate.
    """

    def _integrity_error_from(self, build):
        try:
            with transaction.atomic():
                build()
        except IntegrityError as exc:
            return exc
        self.fail("the constraint did not fire")

    def test_two_rows_for_one_key_is_the_collision(self):
        SignInAttempts.objects.create(scope=SignInScope.IDENTIFIER, key="k")

        exc = self._integrity_error_from(
            lambda: SignInAttempts.objects.create(scope=SignInScope.IDENTIFIER, key="k")
        )

        self.assertTrue(throttling._is_the_counter_colliding(exc))

    def test_any_other_refusal_is_not(self):
        """A negative count is a different rule, and must be raised, not retried."""
        exc = self._integrity_error_from(
            lambda: SignInAttempts.objects.create(
                scope=SignInScope.IDENTIFIER, key="other", failures=-1
            )
        )

        self.assertFalse(throttling._is_the_counter_colliding(exc))


class SigningOutTests(SignInSetUp):
    def test_signing_out_ends_the_session(self):
        self.sign_in()

        response = self.client.post("/api/logout/", HTTP_HOST=PORTAL)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.signed_in())

    def test_a_session_that_already_went_gets_the_ordinary_answer(self):
        """"Sign me out" when there is nothing to end is not a special case."""
        response = self.client.post("/api/logout/", HTTP_HOST=PORTAL)

        self.assertEqual(response.status_code, 401)

    def test_signing_out_on_a_schools_host_works_too(self):
        """Sign-in is portal-only; sign-out is wherever the person happens to be."""
        self.sign_in()

        response = self.client.post("/api/logout/", HTTP_HOST=SCHOOL_HOST)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.signed_in())

    def test_the_next_request_is_told_it_is_not_signed_in(self):
        """`not_authenticated`, and deliberately not `session_expired`.

        The two 401s are told apart by whether a session cookie was presented,
        and `logout()` deletes the cookie as well as the session — so after a
        sign-out there is nothing to present, which is the honest answer. A
        *lapse* leaves the cookie in the browser, and that is the case
        `session_expired` exists for: work in the form that is still good.
        Telling somebody who chose to sign out that their session "has ended
        and can be resent" would invite a client to replay what they had just
        deliberately abandoned.
        """
        self.sign_in()
        self.client.post("/api/logout/", HTTP_HOST=PORTAL)

        response = self.client.post(
            "/api/schools/st-marys/invitations/",
            data={"role": Role.TEACHER.value, "email": "new@st-marys.school"},
            content_type="application/json",
            HTTP_HOST=SCHOOL_HOST,
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], NOT_AUTHENTICATED)
        self.assertFalse(response.json()["retryable"])
