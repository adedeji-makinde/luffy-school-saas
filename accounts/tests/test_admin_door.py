"""The admin is a sign-in door too, and it used to be the unguarded one.

`/api/login/` counts failures, is served on the portal only, and answers every
failure identically. `/admin/login/` did none of that: Django's admin
authenticates through `AuthenticationForm`, which never passes near
`accounts/signin.py`. Forty wrong passwords at that URL recorded nothing at all,
and it was served from **every school's host** as well as the portal.

That combination was the wrong way round twice. The door with no counter on it
was the one guarding `is_platform_staff` accounts — the operator's own logins,
the only ones that can reach every school's data at once. And the admin edits
*shared* tables, so serving it from a tenant host meant privileged writes to
platform-wide rows issued on a connection whose `search_path` had been set to
one school's schema.

Two fixes, tested here: the route moved to the portal's urlconf, and the form
now goes through the same three throttle calls the API endpoint uses.
"""

import sys
from pathlib import Path
from subprocess import run

from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.test import TestCase

from accounts.forms import ThrottledAdminAuthenticationForm
from accounts.models import Role, SignInAttempts, SignInScope, User
from accounts.services import grant_membership
from accounts.throttling import key_for
from schools.models import Domain, School
from schools.tests.test_invitations import make_school

PASSWORD = "correct-horse-battery"
PORTAL = "testserver"
SCHOOL_HOST = "st-marys.testserver"


class AdminSetUp(TestCase):
    def setUp(self):
        portal = School(name="Portal", slug="portal", schema_name="public")
        portal.auto_create_schema = False
        portal.save()
        Domain.objects.create(tenant=portal, domain=PORTAL, is_primary=True)

        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        Domain.objects.create(tenant=self.stmarys, domain=SCHOOL_HOST, is_primary=True)

        self.operator = User.objects.create_superuser(
            "ops@luffy.school", PASSWORD, full_name="Ope Rator"
        )
        # A perfectly good account that is not platform staff. The admin must
        # refuse it, and must count the refusal.
        self.teacher = User.objects.create_user(
            "tayo", PASSWORD, full_name="Tayo Teacher"
        )
        grant_membership(self.teacher, self.stmarys, Role.TEACHER)

    def tearDown(self):
        connection.set_schema_to_public()
        super().tearDown()

    def attempt(self, username="ops@luffy.school", password=PASSWORD, host=PORTAL):
        return self.client.post(
            "/admin/login/",
            {"username": username, "password": password},
            HTTP_HOST=host,
        )

    def failures_for(self, identifier):
        row = SignInAttempts.objects.filter(
            scope=SignInScope.IDENTIFIER, key=key_for(SignInScope.IDENTIFIER, identifier)
        ).first()
        return row.failures if row else 0


class WhereTheAdminIsServedTests(AdminSetUp):
    def test_the_portal_serves_it(self):
        self.assertEqual(self.client.get("/admin/login/", HTTP_HOST=PORTAL).status_code, 200)

    def test_a_schools_own_host_does_not(self):
        """Routing, not a guard inside a view somebody could forget to add.

        `PUBLIC_SCHEMA_URLCONF` carries the admin and `ROOT_URLCONF` does not,
        so a school's host has no such route to reach.
        """
        response = self.client.get("/admin/login/", HTTP_HOST=SCHOOL_HOST)

        self.assertEqual(response.status_code, 404)

    def test_the_api_is_still_served_on_both(self):
        """The split must not have cost a school its own API."""
        for host in (PORTAL, SCHOOL_HOST):
            with self.subTest(host=host):
                response = self.client.get("/api/csrf/", HTTP_HOST=host)
                self.assertEqual(response.status_code, 200)


class TheAdminFormIsThrottledTests(AdminSetUp):
    def test_the_form_is_wired_up(self):
        """Pins the wiring itself.

        Everything else in this file would keep passing if the form were
        swapped back to Django's, right up until the moment it mattered.
        """
        self.assertIs(admin.site.login_form, ThrottledAdminAuthenticationForm)

    def test_a_wrong_password_is_counted(self):
        self.attempt(password="not-the-password")

        self.assertEqual(self.failures_for("ops@luffy.school"), 1)

    def test_a_right_password_for_a_non_staff_account_is_counted_too(self):
        """`confirm_login_allowed()` refuses it, and a refusal is a failure.

        Otherwise the admin would hand out unlimited guesses to anybody holding
        one ordinary teacher's password.
        """
        self.attempt(username="tayo")

        self.assertEqual(self.failures_for("tayo"), 1)

    def test_an_empty_form_forgives_nothing(self):
        """A submission that never reaches authentication must not clear a count.

        The reset happens on `get_user()`, not on "no exception was raised" —
        an empty form raises none and authenticates nobody.
        """
        self.attempt(password="not-the-password")
        self.attempt(username="", password="")

        self.assertEqual(self.failures_for("ops@luffy.school"), 1)

    def test_the_window_closes_on_the_admin_too(self):
        with self.settings(SIGN_IN_MAX_FAILURES_PER_IDENTIFIER=3):
            for _ in range(3):
                self.attempt(password="not-the-password")

            # The right password now, and it must still be refused: the
            # throttle is asked before the credentials, here as in the API.
            response = self.attempt()

        self.assertEqual(response.status_code, 200)  # the form, re-rendered
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signing_in_clears_the_identifier(self):
        self.attempt(password="not-the-password")
        self.assertEqual(self.failures_for("ops@luffy.school"), 1)

        response = self.attempt()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.failures_for("ops@luffy.school"), 0)

    def test_the_api_and_the_admin_share_one_count(self):
        """One person, one identifier, one window — whichever door they use.

        Two independent counters would mean the limit was twice what it says,
        reachable by alternating between them.
        """
        self.attempt(password="not-the-password")

        self.client.post(
            "/api/login/",
            data={"identifier": "ops@luffy.school", "password": "not-the-password"},
            content_type="application/json",
            HTTP_HOST=PORTAL,
        )

        self.assertEqual(self.failures_for("ops@luffy.school"), 2)


class SettingsFailClosedTests(TestCase):
    """The three settings a deploy is most likely to forget.

    Asserted by starting a second process with the environment stripped,
    because these are import-time decisions: by the time a test is running,
    `settings` has already been imported with whatever the environment said.
    `override_settings` cannot reach them, and a test that pretended to would be
    pinning nothing.
    """

    def _check_with(self, **env):
        stripped = {
            key: value
            for key, value in __import__("os").environ.items()
            if key not in {"DJANGO_DEBUG", "DJANGO_SECRET_KEY"}
        }
        stripped.update(env)
        return run(
            [sys.executable, "manage.py", "check"],
            cwd=Path(settings.BASE_DIR),
            env=stripped,
            capture_output=True,
            text=True,
        )

    def test_it_refuses_to_start_with_no_secret_key(self):
        result = self._check_with()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_a_real_key_is_enough_to_start(self):
        result = self._check_with(DJANGO_SECRET_KEY="x" * 60)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_may_still_opt_in(self):
        """Local development is not made painful by this — it opts in, loudly."""
        result = self._check_with(DJANGO_DEBUG="1")

        self.assertEqual(result.returncode, 0, result.stderr)
