"""The five HTTP endpoints, exercised through the real middleware stack.

Not `RequestFactory`: these go through the Django test client so that
`TenantMainMiddleware` and `SchoolAccessMiddleware` actually run, which is the
part most likely to be wrong. That means the request host has to resolve to a
tenant, so `setUp` registers `testserver` as a domain of the **public** tenant —
the portal host, where a person with memberships at several schools signs in and
where an invitee with no membership anywhere can still reach an accept link.

The split that matters here is who is on each end. The three `/api/schools/...`
routes are an authenticated admin acting at their own school. The two
`/api/invitations/{token}/` routes are somebody who is not signed in and may not
even have a password yet — for them the token is the credential.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.deletion import deactivate_user
from accounts.models import Membership, MembershipStatus, Role, User
from accounts.services import grant_membership
from schools import invitations as invitation_service
from schools.models import Domain, Invitation, InvitationStatus, School
from schools.tests.test_invitations import RecordingChannel, make_school

PASSWORD = "correct-horse-battery"


#: The accept page's origin, which `settings.INVITATION_ACCEPT_URL` now pins and
#: `api.py` no longer derives from the request. Set here rather than in the
#: environment so the suite states its own expectation: these tests assert the
#: delivered link is *this*, whatever host the admin posted from.
ACCEPT_URL = "https://portal.example.school/invitations/{token}/"


@override_settings(
    INVITATION_CHANNEL="schools.tests.test_invitations.RecordingChannel",
    INVITATION_ACCEPT_URL=ACCEPT_URL,
)
class InvitationApiTests(TestCase):
    def setUp(self):
        RecordingChannel.sent = []

        # The portal host. django_tenants resolves every request by hostname, so
        # without this the test client gets a 404 from the middleware before any
        # view is reached.
        portal = School(name="Portal", slug="portal", schema_name="public")
        portal.auto_create_schema = False
        portal.save()
        Domain.objects.create(tenant=portal, domain="testserver", is_primary=True)

        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")

        self.admin = User.objects.create_user(
            "admin@st-marys.school",
            PASSWORD,
            full_name="Ada Admin",
            email="admin@st-marys.school",
        )
        grant_membership(self.admin, self.stmarys, Role.ADMIN)

    def create_invite(self, **overrides):
        payload = {
            "role": Role.TEACHER.value,
            "email": "new.teacher@example.com",
            "full_name": "New Teacher",
        }
        payload.update(overrides)
        slug = payload.pop("slug", "st-marys")
        # The host the admin happens to be standing on. It used to decide the
        # origin of the link in the mail; `AcceptLinkOriginTests` pins that it
        # no longer does, which is why this is reachable at all.
        host = payload.pop("host", None)
        extra = {"HTTP_HOST": host} if host else {}
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/schools/{slug}/invitations/",
                data=payload,
                content_type="application/json",
                **extra,
            )
        return response

    def raw_token_of_last_invite(self):
        return RecordingChannel.sent[-1]["raw_token"]

    def accept_url_of_last_invite(self):
        return RecordingChannel.sent[-1]["accept_url"]

    # -- POST /api/schools/{slug}/invitations/ --------------------------------

    def test_an_admin_can_issue_an_invitation(self):
        self.client.force_login(self.admin)
        response = self.create_invite()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], InvitationStatus.PENDING)
        self.assertEqual(body["role"], Role.TEACHER)
        self.assertEqual(body["school"], "St Mary's")

        invitation = Invitation.objects.get(pk=body["id"])
        self.assertEqual(invitation.membership.status, MembershipStatus.INVITED)

    def test_the_response_never_carries_the_token(self):
        """An admin must not be able to read a live invite link back out."""
        self.client.force_login(self.admin)
        response = self.create_invite()

        raw_token = self.raw_token_of_last_invite()
        self.assertNotIn(raw_token, response.content.decode())

    def test_the_response_does_not_reveal_whether_the_account_existed(self):
        """The 201 must not answer "is this person already on the platform?".

        It used to, twice over. It echoed the *resolved* account's stored name,
        so a St Mary's admin naming a Grace Academy teacher's email read back
        that teacher's real name; and because a brand-new invitee got the
        submitted name echoed instead, the difference between the two answers
        was a reliable existence oracle for any email or phone — with an
        unsolicited invitation email sent on every probe.
        """
        self.client.force_login(self.admin)
        kemi = User.objects.create_user(
            "kemi@example.com",
            PASSWORD,
            full_name="Kemi Bello",
            email="kemi@example.com",
        )
        grant_membership(kemi, self.grace, Role.TEACHER)  # another school entirely

        existing = self.create_invite(email="kemi@example.com", full_name="Guess Who")
        fresh = self.create_invite(email="nobody@example.com", full_name="Guess Who")

        self.assertEqual(existing.status_code, 201)
        self.assertEqual(fresh.status_code, 201)
        self.assertNotIn("Kemi Bello", existing.content.decode())

        def shape(response):
            return {
                key: value
                for key, value in response.json().items()
                if key not in {"id", "expires_at"}
            }

        self.assertEqual(shape(existing), shape(fresh), "the two answers must not differ")

    def test_inviting_somebody_already_on_staff_is_a_conflict(self):
        self.client.force_login(self.admin)
        kemi = User.objects.create_user(
            "kemi@example.com",
            PASSWORD,
            full_name="Kemi Bello",
            email="kemi@example.com",
        )
        grant_membership(kemi, self.stmarys, Role.TEACHER)

        response = self.create_invite(email="kemi@example.com")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Invitation.objects.count(), 0)
        self.assertEqual(RecordingChannel.sent, [], "nobody was emailed")

    def test_role_means_the_same_thing_on_every_endpoint(self):
        """One field, one vocabulary — the stored value, everywhere.

        `role` used to be the database value on create and accept and the human
        label on preview, so a client keying off it — to render an icon, to
        prefill a resend form — broke on whichever of the two it had not been
        written against. The label did not go away; it moved to `role_display`,
        which only the invitee-facing preview needs.
        """
        self.client.force_login(self.admin)
        created = self.create_invite()
        token = self.raw_token_of_last_invite()
        self.client.logout()

        preview = self.client.get(f"/api/invitations/{token}/")
        accepted = self.client.post(
            f"/api/invitations/{token}/accept/",
            data={"password": "a-brand-new-password"},
            content_type="application/json",
        )

        for name, body in (
            ("create", created.json()),
            ("preview", preview.json()),
            ("accept", accepted.json()),
        ):
            with self.subTest(endpoint=name):
                self.assertEqual(body["role"], Role.TEACHER)

        self.assertEqual(preview.json()["role_display"], "Teacher")

    def test_issuing_requires_a_signed_in_caller(self):
        response = self.create_invite()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Invitation.objects.count(), 0)

    def test_an_admin_cannot_invite_into_another_school(self):
        self.client.force_login(self.admin)
        response = self.create_invite(slug="grace")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Invitation.objects.count(), 0)

    def test_a_non_staff_role_is_refused(self):
        self.client.force_login(self.admin)
        response = self.create_invite(role=Role.PARENT.value)
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_school_is_a_404(self):
        self.client.force_login(self.admin)
        response = self.create_invite(slug="no-such-school")
        self.assertEqual(response.status_code, 404)

    # -- GET /api/invitations/{token}/ ----------------------------------------

    def test_the_preview_asks_a_new_person_for_a_password(self):
        self.client.force_login(self.admin)
        self.create_invite()
        self.client.logout()  # the invitee is not signed in

        response = self.client.get(f"/api/invitations/{self.raw_token_of_last_invite()}/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["needs_password"])
        self.assertEqual(body["school"], "St Mary's")
        self.assertEqual(body["role"], Role.TEACHER)
        self.assertEqual(body["role_display"], "Teacher")

    def test_the_preview_does_not_ask_an_existing_person_for_one(self):
        """The second-school case: they already have a password and keep it."""
        User.objects.create_user(
            "kemi@example.com", PASSWORD, full_name="Kemi Bello", email="kemi@example.com"
        )
        self.client.force_login(self.admin)
        self.create_invite(email="kemi@example.com")
        self.client.logout()

        response = self.client.get(f"/api/invitations/{self.raw_token_of_last_invite()}/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["needs_password"])

    def test_a_bad_token_is_a_flat_404(self):
        response = self.client.get("/api/invitations/not-a-real-token/")
        self.assertEqual(response.status_code, 404)

    def test_expired_and_revoked_are_indistinguishable_from_unknown(self):
        """Never confirm to a guesser that a token was once real."""
        self.client.force_login(self.admin)
        self.create_invite()
        token = self.raw_token_of_last_invite()
        invitation = Invitation.objects.get()
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save(update_fields=["expires_at"])
        self.client.logout()

        expired = self.client.get(f"/api/invitations/{token}/")
        unknown = self.client.get("/api/invitations/not-a-real-token/")

        self.assertEqual(expired.status_code, 404)
        self.assertEqual(expired.json(), unknown.json())

    # -- POST /api/invitations/{token}/accept/ --------------------------------

    def test_a_new_person_accepts_with_a_password(self):
        self.client.force_login(self.admin)
        self.create_invite()
        token = self.raw_token_of_last_invite()
        self.client.logout()

        response = self.client.post(
            f"/api/invitations/{token}/accept/",
            data={"password": "a-brand-new-password"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], MembershipStatus.ACTIVE)

        user = User.objects.get(email="new.teacher@example.com")
        self.assertTrue(user.check_password("a-brand-new-password"))
        self.assertTrue(user.has_access_to(self.stmarys))

    def test_accepting_without_a_required_password_is_422(self):
        self.client.force_login(self.admin)
        self.create_invite()
        token = self.raw_token_of_last_invite()
        self.client.logout()

        response = self.client.post(
            f"/api/invitations/{token}/accept/", data={}, content_type="application/json"
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            Invitation.objects.get().status, InvitationStatus.PENDING, "nothing moved"
        )

    def test_a_weak_password_is_refused_with_422(self):
        """Same 422 as a missing one: the invitee can fix it and resubmit."""
        self.client.force_login(self.admin)
        self.create_invite()
        token = self.raw_token_of_last_invite()
        self.client.logout()

        response = self.client.post(
            f"/api/invitations/{token}/accept/",
            data={"password": "short"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(Invitation.objects.get().status, InvitationStatus.PENDING)

        # The same link still works with a better one.
        retry = self.client.post(
            f"/api/invitations/{token}/accept/",
            data={"password": "a-brand-new-password"},
            content_type="application/json",
        )
        self.assertEqual(retry.status_code, 200)

    def test_an_ended_membership_makes_the_link_a_flat_404(self):
        """Not a 409 explaining why — the same answer any dead token gets."""
        self.client.force_login(self.admin)
        self.create_invite()
        token = self.raw_token_of_last_invite()
        Invitation.objects.get().membership.end()
        self.client.logout()

        ended = self.client.get(f"/api/invitations/{token}/")
        unknown = self.client.get("/api/invitations/not-a-real-token/")

        self.assertEqual(ended.status_code, 404)
        self.assertEqual(ended.json(), unknown.json())

        accepted = self.client.post(
            f"/api/invitations/{token}/accept/",
            data={"password": "a-brand-new-password"},
            content_type="application/json",
        )
        self.assertEqual(accepted.status_code, 404)
        invitee = User.objects.get(email="new.teacher@example.com")
        self.assertFalse(invitee.has_usable_password(), "no credential was handed out")

    def test_an_existing_person_accepts_with_no_password_at_all(self):
        kemi = User.objects.create_user(
            "kemi@example.com", PASSWORD, full_name="Kemi Bello", email="kemi@example.com"
        )
        grant_membership(kemi, self.grace, Role.TEACHER)
        self.client.force_login(self.admin)
        self.create_invite(email="kemi@example.com")
        token = self.raw_token_of_last_invite()
        self.client.logout()

        response = self.client.post(
            f"/api/invitations/{token}/accept/", data={}, content_type="application/json"
        )

        self.assertEqual(response.status_code, 200)
        kemi.refresh_from_db()
        self.assertTrue(kemi.check_password(PASSWORD), "their password is untouched")
        self.assertEqual({s.slug for s in kemi.schools()}, {"st-marys", "grace"})

    def test_a_token_cannot_be_spent_twice(self):
        self.client.force_login(self.admin)
        self.create_invite()
        token = self.raw_token_of_last_invite()
        self.client.logout()

        first = self.client.post(
            f"/api/invitations/{token}/accept/",
            data={"password": "a-brand-new-password"},
            content_type="application/json",
        )
        second = self.client.post(
            f"/api/invitations/{token}/accept/",
            data={"password": "a-brand-new-password"},
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 404)  # spent reads as unknown

    # -- POST /api/schools/{slug}/invitations/{id}/resend/ --------------------

    def test_resending_issues_a_new_token_and_kills_the_old_one(self):
        self.client.force_login(self.admin)
        created = self.create_invite()
        first_token = self.raw_token_of_last_invite()
        first_id = created.json()["id"]

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/schools/st-marys/invitations/{first_id}/resend/"
            )

        self.assertEqual(response.status_code, 201)
        second_token = self.raw_token_of_last_invite()
        self.assertNotEqual(first_token, second_token)

        # A second row, not an update in place.
        self.assertNotEqual(response.json()["id"], first_id)
        self.assertEqual(Invitation.objects.count(), 2)
        self.assertEqual(
            Invitation.objects.get(pk=first_id).status, InvitationStatus.REVOKED
        )
        # Same membership throughout — the person was always joining.
        self.assertEqual(
            Invitation.objects.values_list("membership_id", flat=True).distinct().count(), 1
        )

        self.client.logout()
        self.assertEqual(self.client.get(f"/api/invitations/{first_token}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/invitations/{second_token}/").status_code, 200)

    def test_an_expired_invitation_can_be_resent(self):
        """The ordinary reason somebody asks for a new link."""
        self.client.force_login(self.admin)
        created = self.create_invite()
        invitation = Invitation.objects.get()
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save(update_fields=["expires_at"])

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/schools/st-marys/invitations/{created.json()['id']}/resend/"
            )

        self.assertEqual(response.status_code, 201)
        self.client.logout()
        self.assertEqual(
            self.client.get(f"/api/invitations/{self.raw_token_of_last_invite()}/").status_code,
            200,
        )

    def test_an_accepted_invitation_cannot_be_resent(self):
        self.client.force_login(self.admin)
        created = self.create_invite()
        token = self.raw_token_of_last_invite()
        self.client.post(
            f"/api/invitations/{token}/accept/",
            data={"password": "a-brand-new-password"},
            content_type="application/json",
        )

        response = self.client.post(
            f"/api/schools/st-marys/invitations/{created.json()['id']}/resend/"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Invitation.objects.count(), 1, "no new row was minted")

    def test_a_revoked_sibling_of_an_accepted_invitation_cannot_be_resent(self):
        """The 409 has to survive the resend flow's own bookkeeping.

        A resend revokes row one and mints row two. Once row two is accepted the
        membership is ACTIVE, but row one still reads REVOKED — and "revoked" is
        a resendable state. Asking the row rather than the membership let this
        mint a working credential for an account that was already in.
        """
        self.client.force_login(self.admin)
        created = self.create_invite()
        first_id = created.json()["id"]

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"/api/schools/st-marys/invitations/{first_id}/resend/")
        second_token = self.raw_token_of_last_invite()

        self.client.logout()
        self.client.post(
            f"/api/invitations/{second_token}/accept/",
            data={"password": "a-brand-new-password"},
            content_type="application/json",
        )

        self.client.force_login(self.admin)
        response = self.client.post(
            f"/api/schools/st-marys/invitations/{first_id}/resend/"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            Invitation.objects.get(pk=first_id).status, InvitationStatus.REVOKED
        )
        self.assertEqual(Invitation.objects.count(), 2, "no third row was minted")

    def test_resending_against_an_ended_membership_is_a_conflict(self):
        self.client.force_login(self.admin)
        created = self.create_invite()
        Invitation.objects.get().membership.end()

        response = self.client.post(
            f"/api/schools/st-marys/invitations/{created.json()['id']}/resend/"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Invitation.objects.count(), 1)

    def test_resending_requires_authority_at_that_school(self):
        operator = User.objects.create_user(
            "ops@luffy.school", PASSWORD, full_name="Ops", is_platform_staff=True
        )
        with self.captureOnCommitCallbacks(execute=True):
            elsewhere, _ = invitation_service.invite_staff(
                operator,
                self.grace,
                Role.TEACHER,
                email="grace.teacher@example.com",
                full_name="Grace Teacher",
                accept_url_for=lambda token: f"https://portal/i/{token}/",
            )

        self.client.force_login(self.admin)
        # Scoped by the path, so St Mary's admin cannot reach it by id at all.
        self.assertEqual(
            self.client.post(
                f"/api/schools/st-marys/invitations/{elsewhere.pk}/resend/"
            ).status_code,
            404,
        )
        # ...and naming the right school does not help either.
        self.assertEqual(
            self.client.post(
                f"/api/schools/grace/invitations/{elsewhere.pk}/resend/"
            ).status_code,
            403,
        )
        self.assertEqual(Invitation.objects.count(), 1)

    def test_resending_requires_a_signed_in_caller(self):
        self.client.force_login(self.admin)
        created = self.create_invite()
        self.client.logout()

        response = self.client.post(
            f"/api/schools/st-marys/invitations/{created.json()['id']}/resend/"
        )
        self.assertEqual(response.status_code, 401)

    def test_the_resend_response_never_carries_the_token(self):
        self.client.force_login(self.admin)
        created = self.create_invite()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/schools/st-marys/invitations/{created.json()['id']}/resend/"
            )
        self.assertNotIn(self.raw_token_of_last_invite(), response.content.decode())

    # -- POST /api/schools/{slug}/invitations/{id}/revoke/ --------------------

    def test_an_admin_can_revoke_a_pending_invitation(self):
        self.client.force_login(self.admin)
        created = self.create_invite()
        token = self.raw_token_of_last_invite()
        invitation_id = created.json()["id"]

        response = self.client.post(
            f"/api/schools/st-marys/invitations/{invitation_id}/revoke/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], InvitationStatus.REVOKED)

        # The link is dead immediately.
        self.client.logout()
        self.assertEqual(
            self.client.get(f"/api/invitations/{token}/").status_code, 404
        )

    def test_revoking_twice_is_a_conflict_not_a_silent_success(self):
        self.client.force_login(self.admin)
        created = self.create_invite()
        invitation_id = created.json()["id"]
        path = f"/api/schools/st-marys/invitations/{invitation_id}/revoke/"

        self.assertEqual(self.client.post(path).status_code, 200)
        self.assertEqual(self.client.post(path).status_code, 409)

    def test_an_admin_cannot_revoke_another_schools_invitation(self):
        """Scoped by the path, so the id alone is not enough."""
        operator = User.objects.create_user(
            "ops@luffy.school", PASSWORD, full_name="Ops", is_platform_staff=True
        )
        with self.captureOnCommitCallbacks(execute=True):
            elsewhere, _ = invitation_service.invite_staff(
                operator,
                self.grace,
                Role.TEACHER,
                email="grace.teacher@example.com",
                full_name="Grace Teacher",
                accept_url_for=lambda token: f"https://portal/i/{token}/",
            )

        self.client.force_login(self.admin)
        response = self.client.post(
            f"/api/schools/st-marys/invitations/{elsewhere.pk}/revoke/"
        )

        self.assertEqual(response.status_code, 404)
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.status, InvitationStatus.PENDING)

    # -- a deactivated invitee ------------------------------------------------

    def test_inviting_a_deactivated_person_is_a_conflict(self):
        """409, not 400: the request is fine, the account's state is not."""
        kemi = User.objects.create_user(
            "kemi@example.com", PASSWORD, full_name="Kemi Bello",
            email="kemi@example.com",
        )
        deactivate_user(kemi)

        self.client.force_login(self.admin)
        response = self.create_invite(email="kemi@example.com")

        self.assertEqual(response.status_code, 409)
        self.assertIn("deactivated", response.json()["detail"])
        self.assertEqual(Invitation.objects.count(), 0)

    def test_a_link_stops_resolving_once_its_invitee_is_deactivated(self):
        """404 and nothing else — the same answer every dead token gets."""
        self.client.force_login(self.admin)
        self.create_invite()
        raw_token = self.raw_token_of_last_invite()
        self.client.logout()

        deactivate_user(User.objects.get(email="new.teacher@example.com"))

        preview = self.client.get(f"/api/invitations/{raw_token}/")
        self.assertEqual(preview.status_code, 404)

        accept = self.client.post(
            f"/api/invitations/{raw_token}/accept/",
            data={"password": PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(accept.status_code, 404)
        self.assertEqual(
            Membership.objects.get(user__email="new.teacher@example.com").status,
            MembershipStatus.INVITED,
        )


@override_settings(
    INVITATION_CHANNEL="schools.tests.test_invitations.RecordingChannel",
    INVITATION_ACCEPT_URL=ACCEPT_URL,
)
class AcceptLinkOriginTests(TestCase):
    """Finding #6: the link in the mail followed whichever host the admin used.

    `api.py` built it with `request.build_absolute_uri()`, so an admin on the
    portal host and the same admin on their school's own host issued links on
    two different origins — for an accept page that lives on a frontend which
    may be on neither, and which no urlconf in this project serves. Nothing
    pinned the path either: the service tests used `https://portal/i/{token}/`
    while `api.py` emitted `/invitations/{token}/`, and no test asserted either.

    Two hosts are registered below, both resolving to the public portal tenant.
    That is enough to prove the property — `build_absolute_uri()` answers with
    whatever `Host` it was given, so these two requests produced two different
    links before the fix — and it avoids needing a real `CREATE SCHEMA` for a
    question that has nothing to do with schemas.
    """

    def setUp(self):
        RecordingChannel.sent = []

        portal = School(name="Portal", slug="portal", schema_name="public")
        portal.auto_create_schema = False
        portal.save()
        Domain.objects.create(tenant=portal, domain="testserver", is_primary=True)
        # A second way in to the same tenant. An admin who follows a bookmark to
        # one of these rather than the other must not thereby change what every
        # invitee receives.
        Domain.objects.create(tenant=portal, domain="admin.testserver")

        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.admin = User.objects.create_user(
            "admin@st-marys.school",
            PASSWORD,
            full_name="Ada Admin",
            email="admin@st-marys.school",
        )
        grant_membership(self.admin, self.stmarys, Role.ADMIN)
        self.client.force_login(self.admin)

    def invite_from(self, host, email):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/schools/st-marys/invitations/",
                data={"role": Role.TEACHER.value, "email": email},
                content_type="application/json",
                HTTP_HOST=host,
            )
        self.assertEqual(response.status_code, 201, response.content)
        return RecordingChannel.sent[-1]

    def test_two_hosts_produce_the_same_origin(self):
        first = self.invite_from("testserver", "one@example.com")
        second = self.invite_from("admin.testserver", "two@example.com")

        self.assertEqual(
            first["accept_url"],
            f"https://portal.example.school/invitations/{first['raw_token']}/",
        )
        self.assertEqual(
            second["accept_url"],
            f"https://portal.example.school/invitations/{second['raw_token']}/",
        )
        # The tokens differ; everything around them does not.
        self.assertNotEqual(first["raw_token"], second["raw_token"])
        self.assertEqual(
            first["accept_url"].replace(first["raw_token"], "T"),
            second["accept_url"].replace(second["raw_token"], "T"),
        )

    def test_the_link_carries_no_trace_of_the_request_host(self):
        delivered = self.invite_from("admin.testserver", "three@example.com")
        self.assertNotIn("testserver", delivered["accept_url"])

    def test_the_token_in_the_delivered_link_is_one_the_api_accepts(self):
        """The half no test covered: that the link carries a *working* token.

        The accept page itself is a frontend route and cannot be asserted to
        resolve from here. What can be asserted is the thing that page will do
        with what it is given — read the token out of the URL and present it to
        this API — so that a change to the template that mangled the token would
        fail here rather than in somebody's inbox.
        """
        delivered = self.invite_from("testserver", "four@example.com")
        prefix = "https://portal.example.school/invitations/"
        self.assertTrue(delivered["accept_url"].startswith(prefix))
        token_from_link = delivered["accept_url"][len(prefix):].rstrip("/")

        self.client.logout()
        preview = self.client.get(f"/api/invitations/{token_from_link}/")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["school"], "St Mary's")

    @override_settings(INVITATION_ACCEPT_URL=None)
    def test_an_unconfigured_accept_url_is_a_503_that_creates_nothing(self):
        """Not a 400: the request was fine and the platform is not set up.

        And not a 500 either, which is what an uncaught misconfiguration would
        have been. The important half is the second assertion — the refusal
        happens before the commit, so a deploy that never sets the URL does not
        accumulate a placeholder account per attempt.
        """
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/schools/st-marys/invitations/",
                data={"role": Role.TEACHER.value, "email": "nobody@example.com"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(Invitation.objects.count(), 0)
        self.assertFalse(User.objects.filter(email="nobody@example.com").exists())
        self.assertEqual(RecordingChannel.sent, [])
