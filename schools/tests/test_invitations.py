"""The staff invitation flow, end to end.

The case worth watching throughout is the second one: a person who already has
an account being invited to another school. docs/membership.md warns that a
flow written as "invite ⇒ create account" breaks exactly the case this data
model exists to serve, so several tests below exist only to pin that an existing
`User` is reused, keeps their password, and is never asked for a new one.

These are plain `TestCase`s against the public schema. Every model in the flow —
`User`, `Membership`, `Invitation` — is shared, so no schema switching is needed
and none is done; `make_school()` here skips `CREATE SCHEMA` for the same reason
`accounts/tests/test_membership.py` does.
"""

from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Membership, MembershipStatus, Role, User
from accounts.services import NotPermitted, grant_membership
from schools import invitations as invitation_service
from schools.delivery import NoDeliveryAddress
from schools.models import (
    Invitation,
    InvitationError,
    InvitationStatus,
    PasswordRequired,
    School,
    WeakPassword,
    hash_token,
)

PASSWORD = "correct-horse-battery"


def make_school(name, slug, schema_name):
    school = School(name=name, slug=slug, schema_name=schema_name)
    # Every model touched here is public-schema, so skip CREATE SCHEMA.
    school.auto_create_schema = False
    school.save()
    return school


class RecordingChannel:
    """A delivery channel that keeps what it was handed instead of sending it.

    Deliberately not a subclass of anything: the seam in `delivery.py` is "an
    object with a `send()`", and a test double proving that is worth more than
    one that inherits its way into compliance.
    """

    sent = []

    def send(self, invitation, raw_token, *, accept_url):
        type(self).sent.append(
            {"invitation": invitation, "raw_token": raw_token, "accept_url": accept_url}
        )


def recording():
    RecordingChannel.sent = []
    return override_settings(
        INVITATION_CHANNEL="schools.tests.test_invitations.RecordingChannel"
    )


class InvitationSetUp(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.admin = User.objects.create_user(
            "admin@st-marys.school",
            PASSWORD,
            full_name="Ada Admin",
            email="admin@st-marys.school",
        )
        grant_membership(self.admin, self.stmarys, Role.ADMIN)

    def invite(self, **kwargs):
        kwargs.setdefault("email", "new.teacher@example.com")
        kwargs.setdefault("full_name", "New Teacher")
        role = kwargs.pop("role", Role.TEACHER)
        school = kwargs.pop("school", self.stmarys)
        actor = kwargs.pop("actor", self.admin)
        kwargs.setdefault("accept_url_for", lambda token: f"https://portal/i/{token}/")
        return invitation_service.invite_staff(actor, school, role, **kwargs)


class NewUserInviteTests(InvitationSetUp):
    """Somebody with no account on the platform at all."""

    def test_the_whole_flow(self):
        with recording():
            invitation, raw_token = self.invite()

        # A placeholder account exists but cannot be signed into yet.
        user = invitation.user
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.email, "new.teacher@example.com")

        # The relationship exists and grants nothing.
        self.assertEqual(invitation.membership.status, MembershipStatus.INVITED)
        self.assertFalse(user.has_access_to(self.stmarys))

        # The invitee is told a password is needed, then supplies one.
        self.assertTrue(invitation.needs_password)
        membership = invitation.accept(password="a-brand-new-password")

        self.assertEqual(membership.status, MembershipStatus.ACTIVE)
        user.refresh_from_db()
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password("a-brand-new-password"))
        self.assertTrue(user.has_access_to(self.stmarys))

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.ACCEPTED)
        self.assertIsNotNone(invitation.accepted_at)
        # The token is spent.
        self.assertIsNone(Invitation.validate_token(raw_token))

    def test_accepting_without_a_password_is_refused(self):
        with recording():
            invitation, _ = self.invite()
        with self.assertRaises(PasswordRequired):
            invitation.accept()
        # ...and nothing moved.
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.PENDING)
        self.assertEqual(invitation.membership.status, MembershipStatus.INVITED)


class ExistingUserInviteTests(InvitationSetUp):
    """The case the membership model exists for: a second school."""

    def setUp(self):
        super().setUp()
        self.teacher = User.objects.create_user(
            "kemi@example.com",
            PASSWORD,
            full_name="Kemi Bello",
            email="kemi@example.com",
        )
        grant_membership(self.teacher, self.grace, Role.TEACHER)

    def test_the_existing_account_is_reused_and_needs_no_password(self):
        before = User.objects.count()

        with recording():
            invitation, _ = self.invite(email="kemi@example.com")

        self.assertEqual(User.objects.count(), before, "must not create a second login")
        self.assertEqual(invitation.user, self.teacher)
        self.assertFalse(invitation.needs_password)

        # Accepted with no password at all.
        membership = invitation.accept()
        self.assertEqual(membership.status, MembershipStatus.ACTIVE)

        # Their original password still works, and now both schools are reachable.
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(PASSWORD))
        self.assertEqual(
            {s.name for s in self.teacher.schools()}, {"St Mary's", "Grace Academy"}
        )

    def test_a_password_sent_anyway_does_not_overwrite_the_existing_one(self):
        """An invite link must never be a password reset for a live account."""
        with recording():
            invitation, _ = self.invite(email="kemi@example.com")

        invitation.accept(password="attacker-chosen-password")

        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(PASSWORD))
        self.assertFalse(self.teacher.check_password("attacker-chosen-password"))

    def test_matching_is_case_insensitive_like_sign_in(self):
        with recording():
            invitation, _ = self.invite(email="KEMI@EXAMPLE.COM")
        self.assertEqual(invitation.user, self.teacher)


class TokenLifecycleTests(InvitationSetUp):
    def test_only_the_hash_is_stored(self):
        with recording():
            invitation, raw_token = self.invite()

        self.assertEqual(invitation.token_hash, hash_token(raw_token))
        self.assertNotIn(raw_token, invitation.token_hash)
        # Nothing anywhere in the row carries the token itself.
        stored = {
            str(value) for value in Invitation.objects.filter(pk=invitation.pk).values()[0].values()
        }
        self.assertFalse(any(raw_token in value for value in stored))

    def test_an_expired_token_is_rejected_and_marked(self):
        with recording():
            invitation, raw_token = self.invite()

        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save(update_fields=["expires_at"])

        self.assertIsNone(Invitation.validate_token(raw_token))
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.EXPIRED)

        # And accepting it directly is refused too, not just the lookup.
        with self.assertRaises(InvitationError):
            invitation.accept(password="whatever")

    def test_a_revoked_token_is_rejected(self):
        with recording():
            invitation, raw_token = self.invite()

        invitation_service.revoke_invitation(self.admin, invitation)

        self.assertIsNone(Invitation.validate_token(raw_token))
        with self.assertRaises(InvitationError):
            invitation.accept(password="whatever")
        # The membership is untouched: revoking kills the link, not the plan.
        invitation.membership.refresh_from_db()
        self.assertEqual(invitation.membership.status, MembershipStatus.INVITED)

    def test_resending_invalidates_the_previous_token(self):
        with recording():
            first, first_token = self.invite()
            second, second_token = invitation_service.resend_invitation(
                self.admin, first, accept_url_for=lambda t: f"https://portal/i/{t}/"
            )

        self.assertNotEqual(first_token, second_token)
        first.refresh_from_db()
        self.assertEqual(first.status, InvitationStatus.REVOKED)

        self.assertIsNone(Invitation.validate_token(first_token))
        self.assertEqual(Invitation.validate_token(second_token), second)

        # Same membership, second row — a resend is not an update in place.
        self.assertEqual(second.membership_id, first.membership_id)
        self.assertEqual(Invitation.objects.count(), 2)

    def test_an_accepted_invitation_cannot_be_resent(self):
        """Enforced in the service, not only at the HTTP edge.

        Minting a fresh token against a membership that is already ACTIVE would
        put a working credential for a live account into somebody's inbox.
        """
        with recording():
            invitation, _ = self.invite()
        invitation.accept(password="already-in-thanks")

        with self.assertRaises(invitation_service.AlreadyAccepted):
            invitation_service.resend_invitation(self.admin, invitation)
        self.assertEqual(Invitation.objects.count(), 1)

    def test_a_revoked_or_expired_invitation_may_be_resent(self):
        with recording():
            invitation, _ = self.invite()
        invitation_service.revoke_invitation(self.admin, invitation)

        with recording():
            fresh, token = invitation_service.resend_invitation(
                self.admin, invitation, accept_url_for=lambda t: f"https://portal/i/{t}/"
            )

        self.assertEqual(Invitation.validate_token(token), fresh)
        self.assertEqual(fresh.membership_id, invitation.membership_id)

    def test_an_unknown_token_is_simply_none(self):
        self.assertIsNone(Invitation.validate_token("not-a-real-token"))
        self.assertIsNone(Invitation.validate_token(""))
        self.assertIsNone(Invitation.validate_token(None))

    def test_a_token_cannot_be_accepted_twice(self):
        with recording():
            invitation, _ = self.invite()
        invitation.accept(password="first-time")
        with self.assertRaises(InvitationError):
            invitation.accept(password="second-time")


class MembershipStateGuardTests(InvitationSetUp):
    """The invitation is a credential for a relationship, so the relationship rules.

    Every case below is one where the *invitation row* looks perfectly fine and
    the membership behind it does not. Reading each rule off the row instead of
    off the membership is what let all of them through.
    """

    def test_a_revoked_sibling_cannot_be_resent_once_the_membership_is_active(self):
        """The resend rule belongs to the membership, not to the row handed in.

        invite → resend → accept leaves row one REVOKED and the membership
        ACTIVE. By its own status row one reads as "revoked, therefore
        resendable", and resending it minted a live token for an account that
        was already in — the exact outcome `resend_invitation` forbids.
        """
        with recording():
            first, _ = self.invite()
            _second, second_token = invitation_service.resend_invitation(
                self.admin, first, accept_url_for=lambda t: f"https://portal/i/{t}/"
            )
        Invitation.validate_token(second_token).accept(password="now-i-am-staff")

        first.refresh_from_db()
        self.assertEqual(first.status, InvitationStatus.REVOKED)

        with self.assertRaises(invitation_service.AlreadyAccepted):
            invitation_service.resend_invitation(self.admin, first)
        self.assertEqual(Invitation.objects.count(), 2, "no third row was minted")

    def test_a_suspended_membership_cannot_be_resent_against(self):
        with recording():
            invitation, _ = self.invite()
        Membership.objects.filter(pk=invitation.membership_id).update(
            status=MembershipStatus.SUSPENDED
        )
        invitation.refresh_from_db()

        with self.assertRaises(invitation_service.MembershipNotOpen):
            invitation_service.resend_invitation(self.admin, invitation)

    def test_ending_the_membership_kills_the_outstanding_token(self):
        """A withdrawn relationship must not leave a working link behind.

        `Membership.end()` does not touch the invitation, so the row stays
        PENDING. The token dies anyway, because what `validate_token()` asks is
        whether the *relationship* is still open — and it answers with the same
        flat None as any other dead token, since "your membership was ended" is
        not something a link holder should learn from a link.
        """
        with recording():
            invitation, raw_token = self.invite()
        invitation.membership.end()

        self.assertIsNone(Invitation.validate_token(raw_token))
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.PENDING)

    def test_accepting_against_an_ended_membership_is_refused_directly(self):
        """Asked again in `accept()`, because `accept()` is callable directly.

        The credential half is the part that matters: without this, redeeming
        the stale link set a *global* password on the account — a working
        platform credential handed to somebody whose relationship was withdrawn.
        """
        with recording():
            invitation, _ = self.invite()
        invitation.membership.end()

        with self.assertRaises(InvitationError):
            invitation.accept(password="a-brand-new-password")

        user = invitation.membership.user
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.PENDING)

    def test_inviting_somebody_who_already_holds_the_role_is_refused(self):
        """`grant_membership()` is idempotent, which is the trap.

        It returns a live row untouched, so `status=INVITED` was silently
        dropped: the endpoint answered 201 "pending" while the membership was
        ACTIVE, and the token minted against it was a working credential for a
        live account, emailed to somebody already on staff.
        """
        kemi = User.objects.create_user(
            "kemi@example.com",
            PASSWORD,
            full_name="Kemi Bello",
            email="kemi@example.com",
        )
        grant_membership(kemi, self.stmarys, Role.TEACHER)

        with recording():
            with self.assertRaises(invitation_service.AlreadyAMember):
                self.invite(email="kemi@example.com")

        self.assertEqual(Invitation.objects.count(), 0)
        self.assertEqual(
            Membership.objects.get(
                user=kemi, school=self.stmarys, role=Role.TEACHER
            ).status,
            MembershipStatus.ACTIVE,
        )

    def test_a_former_member_can_be_invited_back(self):
        """The guard is about *live* memberships. Re-hiring is a real thing.

        Note the consequence of tying a token's life to its membership: reviving
        the row to INVITED revives any invitation still pending against it, so
        the earlier link works again too. Both are the same person, school and
        role, and both still expire on their own.
        """
        with recording():
            invitation, _ = self.invite()
        invitation.membership.end()

        with recording():
            again, token = self.invite()

        self.assertEqual(again.membership_id, invitation.membership_id)
        self.assertEqual(again.membership.status, MembershipStatus.INVITED)
        self.assertEqual(Invitation.validate_token(token), again)


class PasswordStrengthTests(InvitationSetUp):
    """`accept()` is the only place that sets a password on somebody's behalf."""

    def test_a_trivially_weak_password_is_refused(self):
        with recording():
            invitation, _ = self.invite()

        with self.assertRaises(WeakPassword):
            invitation.accept(password="short")

        # Nothing moved: not the credential, not the invitation, not the
        # membership. The invitee can pick a better one and try the same link.
        user = invitation.membership.user
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.PENDING)
        invitation.membership.refresh_from_db()
        self.assertEqual(invitation.membership.status, MembershipStatus.INVITED)

        membership = invitation.accept(password="a-brand-new-password")
        self.assertEqual(membership.status, MembershipStatus.ACTIVE)

    def test_an_existing_password_is_never_re_validated(self):
        """Somebody joining their second school is not asked to prove anything.

        Their password was accepted under whatever policy applied when they set
        it; tightening the rule must not lock them out of an invitation.
        """
        kemi = User.objects.create_user(
            "kemi@example.com", PASSWORD, full_name="Kemi Bello", email="kemi@example.com"
        )
        with recording():
            invitation, _ = self.invite(email="kemi@example.com")

        membership = invitation.accept()
        self.assertEqual(membership.status, MembershipStatus.ACTIVE)
        kemi.refresh_from_db()
        self.assertTrue(kemi.check_password(PASSWORD))


class PendingIsNotStaffTests(InvitationSetUp):
    """An unaccepted invitation must not read as a working member of staff."""

    def test_a_pending_membership_is_absent_from_active_staff_until_accepted(self):
        with recording():
            invitation, _ = self.invite(full_name="New Teacher")

        listed = {m.user.full_name for m in invitation_service.active_staff(self.stmarys)}
        self.assertNotIn("New Teacher", listed)
        self.assertIn("Ada Admin", listed)  # the admin, who is active

        invitation.accept(password="now-i-am-staff")

        listed = {m.user.full_name for m in invitation_service.active_staff(self.stmarys)}
        self.assertIn("New Teacher", listed)

    def test_the_roster_still_shows_them_because_that_is_a_different_question(self):
        """Visibility and access are separate predicates; see services.school_directory."""
        from accounts import services

        with recording():
            self.invite(full_name="New Teacher")

        roster = {m.user.full_name for m in services.school_directory(self.stmarys)}
        self.assertIn("New Teacher", roster)

    def test_active_staff_can_be_narrowed_to_one_role(self):
        with recording():
            invitation, _ = self.invite(role=Role.BURSAR, full_name="Bursar Person")
        invitation.accept(password="x-y-z-1234")

        bursars = {
            m.user.full_name
            for m in invitation_service.active_staff(self.stmarys, role=Role.BURSAR)
        }
        self.assertEqual(bursars, {"Bursar Person"})


class AuthorityTests(InvitationSetUp):
    def test_an_admin_cannot_invite_into_another_school(self):
        with self.assertRaises(NotPermitted):
            with recording():
                self.invite(school=self.grace)
        self.assertEqual(Invitation.objects.count(), 0)

    def test_a_teacher_cannot_invite_at_all(self):
        teacher = User.objects.create_user(
            "teacher@example.com", PASSWORD, full_name="Tunde Teacher"
        )
        grant_membership(teacher, self.stmarys, Role.TEACHER)
        with self.assertRaises(NotPermitted):
            with recording():
                self.invite(actor=teacher)

    def test_platform_staff_may_invite_anywhere(self):
        operator = User.objects.create_user(
            "ops@luffy.school", PASSWORD, full_name="Ops Person", is_platform_staff=True
        )
        with recording():
            invitation, _ = self.invite(actor=operator, school=self.grace)
        self.assertEqual(invitation.school, self.grace)

    def test_only_staff_roles_can_be_invited_in_this_pass(self):
        for role in (Role.PARENT, Role.STUDENT):
            with self.subTest(role=role):
                with self.assertRaises(invitation_service.NotStaffRole):
                    with recording():
                        self.invite(role=role)


class DeliveryTests(InvitationSetUp):
    """The channel seam: the model knows nothing about how the token travels.

    Every test here wraps the invite in `captureOnCommitCallbacks(execute=True)`,
    and that is not boilerplate — it is the delivery contract showing through.
    `_deliver()` dispatches via `transaction.on_commit`, so nothing is sent for
    an invitation whose transaction then rolls back. A `TestCase` never commits,
    so without this the callbacks queue and are discarded, which is precisely
    what would happen in production if the invite failed.
    """

    def test_nothing_is_sent_until_the_invitation_actually_commits(self):
        """The reason for the wrapper, pinned rather than assumed."""
        with recording():
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                self.invite()
            self.assertEqual(RecordingChannel.sent, [], "sent before commit")
        self.assertEqual(len(callbacks), 1, "delivery should be queued for commit")

    def test_the_raw_token_is_handed_to_the_channel_and_never_persisted(self):
        with recording():
            with self.captureOnCommitCallbacks(execute=True):
                invitation, raw_token = self.invite()

        self.assertEqual(len(RecordingChannel.sent), 1)
        delivered = RecordingChannel.sent[0]
        self.assertEqual(delivered["raw_token"], raw_token)
        self.assertEqual(delivered["invitation"], invitation)
        self.assertIn(raw_token, delivered["accept_url"])

    def test_the_default_channel_sends_an_email(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
            INVITATION_CHANNEL="schools.delivery.EmailChannel",
        ):
            mail.outbox = []
            with self.captureOnCommitCallbacks(execute=True):
                _invitation, raw_token = self.invite()

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["new.teacher@example.com"])
        self.assertIn("St Mary's", message.subject)
        self.assertIn(raw_token, message.body)

    def test_the_model_does_not_import_the_delivery_module(self):
        """The seam, stated as a test rather than as a comment.

        The dependency runs one way only: `invitations.py` reaches for a channel,
        `models.py` never does. That is what makes adding WhatsApp a new class
        and a settings value rather than an edit to `Invitation`. Checked against
        the import statements themselves — the word "delivery" appears in prose
        in that module and should be allowed to.
        """
        import ast
        import inspect

        from schools import models as schools_models

        imported = set()
        for node in ast.walk(ast.parse(inspect.getsource(schools_models))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertTrue(imported, "expected to find some imports")
        self.assertFalse(
            [name for name in imported if "delivery" in name],
            f"schools.models must not import a delivery channel; got {imported}",
        )

    def test_an_invitee_with_no_email_is_refused_by_the_email_channel(self):
        before = (
            Invitation.objects.count(),
            User.objects.count(),
            Membership.objects.count(),
        )

        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
            INVITATION_CHANNEL="schools.delivery.EmailChannel",
        ):
            with self.assertRaises(NoDeliveryAddress):
                with self.captureOnCommitCallbacks(execute=True):
                    self.invite(email=None, phone="08031234567")

        # The half this test used to leave unsaid, and the reason the bug it
        # was meant to cover survived it. Refusing is only half the job: asking
        # the channel *after* commit meant the placeholder user, the membership
        # and an undeliverable invitation all outlived the error, one more
        # orphaned set per retry.
        self.assertEqual(
            (
                Invitation.objects.count(),
                User.objects.count(),
                Membership.objects.count(),
            ),
            before,
            "a refused delivery must leave nothing behind",
        )

    def test_a_channel_that_cannot_pre_check_still_works(self):
        """`check_deliverable()` is the optional half of the seam.

        `RecordingChannel` defines `send()` and nothing else, which is the point
        of it — the contract is duck-typed, and a double must not have to
        inherit or implement its way into compliance.
        """
        self.assertFalse(hasattr(RecordingChannel, "check_deliverable"))
        with recording():
            with self.captureOnCommitCallbacks(execute=True):
                invitation, raw_token = self.invite()

        self.assertEqual(RecordingChannel.sent[-1]["raw_token"], raw_token)
        self.assertEqual(Invitation.validate_token(raw_token), invitation)


class InviteeResolutionTests(InvitationSetUp):
    def test_two_people_may_be_invited_to_the_same_school(self):
        """No uniqueness is assumed per person or per contact detail."""
        with recording():
            first, _ = self.invite(email="one@example.com", full_name="One Person")
            second, _ = self.invite(email="two@example.com", full_name="Two Person")

        self.assertNotEqual(first.user, second.user)
        self.assertEqual(Invitation.objects.count(), 2)

    def test_the_same_person_may_hold_two_invitations_at_once(self):
        """A resend is a second row, and so is an invite to a second role."""
        with recording():
            self.invite(email="kemi@example.com", role=Role.TEACHER)
            self.invite(email="kemi@example.com", role=Role.BURSAR)

        user = User.objects.get(email="kemi@example.com")
        self.assertEqual(Invitation.objects.filter(membership__user=user).count(), 2)

    def test_an_invite_needs_some_way_to_reach_the_person(self):
        with self.assertRaises(invitation_service.InvitationError):
            with recording():
                self.invite(email=None, phone=None)
