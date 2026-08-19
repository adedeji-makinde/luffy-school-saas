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

from accounts.models import MembershipStatus, Role, User
from accounts.services import grant_membership
from schools import invitations as invitation_service
from schools.models import Domain, Invitation, InvitationStatus, School
from schools.tests.test_invitations import RecordingChannel, make_school

PASSWORD = "correct-horse-battery"


@override_settings(INVITATION_CHANNEL="schools.tests.test_invitations.RecordingChannel")
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
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/schools/{slug}/invitations/",
                data=payload,
                content_type="application/json",
            )
        return response

    def raw_token_of_last_invite(self):
        return RecordingChannel.sent[-1]["raw_token"]

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
