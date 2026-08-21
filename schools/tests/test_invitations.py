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

import os
from datetime import datetime, timedelta, timezone as std_timezone
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.deletion import deactivate_user, reactivate_user
from accounts.models import Membership, MembershipStatus, Role, User
from accounts.services import NotPermitted, grant_membership
from schools import invitations as invitation_service
from schools.delivery import (
    DeliveryFailed,
    DeliveryNotConfigured,
    EmailChannel,
    NoDeliveryAddress,
)
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


#: What a deploy that has been set up looks like. `settings.INVITATION_ACCEPT_URL`
#: has no default — see settings.py — so without this every test below would be
#: exercising an unconfigured platform, which is one specific case rather than
#: the ordinary one. `AcceptUrlTests` overrides it back off to pin that case.
#:
#: On the class rather than in each test: Django applies a subclass's inherited
#: `_overridden_settings`, so every `InvitationSetUp` subclass gets it.
ACCEPT_URL = "https://portal.example.school/invitations/{token}/"


@override_settings(INVITATION_ACCEPT_URL=ACCEPT_URL)
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

    def test_inviting_the_same_person_twice_leaves_one_live_token(self):
        """The hole that only `resend_invitation()` used to close.

        Revoking the previous row was done at the resend call site, on the row
        it was handed, so a second *invite* against the same membership left
        both links working — two live credentials for one relationship, and no
        way to tell from either which one the admin meant.
        """
        with recording():
            first, first_token = self.invite()
            second, second_token = self.invite()

        first.refresh_from_db()
        self.assertEqual(first.status, InvitationStatus.REVOKED)
        self.assertIsNone(Invitation.validate_token(first_token))
        self.assertEqual(Invitation.validate_token(second_token), second)

        # Both rows are kept: the audit trail is the reason a resend is a second
        # row rather than an update, and that reasoning does not change here.
        self.assertEqual(Invitation.objects.count(), 2)

    def test_at_most_one_invitation_is_pending_per_membership(self):
        """Stated as the invariant rather than as one path's behaviour."""
        with recording():
            invitation, _ = self.invite()
            for _ in range(3):
                invitation, _ = invitation_service.resend_invitation(
                    self.admin, invitation
                )
            self.invite()

        membership_id = invitation.membership_id
        self.assertEqual(
            Invitation.objects.filter(membership_id=membership_id).pending().count(), 1
        )
        self.assertEqual(Invitation.objects.count(), 5, "every row is kept")

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
        """The guard is about *live* memberships. Re-hiring is a real thing."""
        with recording():
            invitation, _ = self.invite()
        invitation.membership.end()

        with recording():
            again, token = self.invite()

        self.assertEqual(again.membership_id, invitation.membership_id)
        self.assertEqual(again.membership.status, MembershipStatus.INVITED)
        self.assertEqual(Invitation.validate_token(token), again)

    def test_re_hiring_does_not_resurrect_the_old_link(self):
        """The counterpart to tying a token's life to its membership.

        Reviving an ended membership back to INVITED would otherwise revive
        every invitation still pending against it, so a link minted for a
        relationship that somebody deliberately ended would start working again
        with nobody re-authorising it. `_issue()` revokes them on the way past.
        """
        with recording():
            old, old_token = self.invite()
        old.membership.end()

        with recording():
            _again, new_token = self.invite()

        old.refresh_from_db()
        self.assertEqual(old.status, InvitationStatus.REVOKED)
        self.assertIsNone(Invitation.validate_token(old_token))
        self.assertIsNotNone(Invitation.validate_token(new_token))


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


class ExceptionHierarchyTests(TestCase):
    """One `InvitationError`, so `except InvitationError` means what it says.

    There were two, one in each module, unrelated by inheritance and identical
    in name. Catching the flow's refusals then depended on which module you had
    imported from: `from schools.invitations import InvitationError` caught
    nothing `accept()` or `revoke()` raised, and `api.py` had to qualify one of
    the two at every call site to keep them apart. Nothing about that was
    visible at the point of use, which is what made it worth a test rather than
    a comment.
    """

    def test_the_two_modules_export_the_same_class(self):
        from schools import invitations, models

        self.assertIs(invitations.InvitationError, models.InvitationError)

    def test_every_refusal_in_the_flow_shares_that_base(self):
        from schools import invitations, models

        for exc in (
            invitations.NotStaffRole,
            invitations.AmbiguousInvitee,
            invitations.AlreadyAccepted,
            invitations.AlreadyAMember,
            invitations.MembershipNotOpen,
            models.PasswordRequired,
            models.WeakPassword,
        ):
            with self.subTest(exception=exc.__name__):
                self.assertTrue(issubclass(exc, models.InvitationError))


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

    def test_the_expiry_date_in_the_email_is_local_not_utc(self):
        """The deadline the reader is given has to be the deadline they keep.

        `expires_at` is stored in UTC and the recipient reads it in TIME_ZONE.
        23:30 UTC is already the next day in Lagos, so formatting the raw stored
        value understated the deadline by a day for every invitation whose link
        died in the last hour of the UTC day.
        """
        with recording():
            invitation, _ = self.invite()
        invitation.expires_at = datetime(
            2026, 8, 26, 23, 30, tzinfo=std_timezone.utc
        )
        invitation.save(update_fields=["expires_at"])

        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
            TIME_ZONE="Africa/Lagos",
        ):
            mail.outbox = []
            EmailChannel().send(
                invitation, "a-token", accept_url="https://portal/i/a-token/"
            )

        body = mail.outbox[0].body
        self.assertIn("27 August 2026", body)
        self.assertNotIn("26 August 2026", body)

    def test_the_default_email_backend_does_not_write_tokens_to_stdout(self):
        """A live credential must not be the log's business.

        The console backend prints the whole message — accept URL and token —
        to stdout, which in a container is the application log. As a *default*
        it also failed open the other way: nothing delivered, nothing raised, so
        a production deploy that never set EMAIL_BACKEND was indistinguishable
        from a working one. Local development opts in explicitly instead, in
        docker-compose.yml.
        """
        self.assertNotIn("console", settings.DEFAULT_EMAIL_BACKEND)
        self.assertNotIn("dummy", settings.DEFAULT_EMAIL_BACKEND)

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

    def test_a_channel_that_cannot_report_its_configuration_still_works(self):
        """`check_configured()` is optional for the same reason.

        The seam gained a second optional hook, and a double that predates it
        must keep working — otherwise "an object with a `send()`" quietly became
        "an object with a `send()` and two checks".
        """
        self.assertFalse(hasattr(RecordingChannel, "check_configured"))
        with recording():
            with self.captureOnCommitCallbacks(execute=True):
                _invitation, raw_token = self.invite()

        self.assertEqual(RecordingChannel.sent[-1]["raw_token"], raw_token)


class AcceptUrlTests(InvitationSetUp):
    """Where the link in the mail points, and who decides.

    It used to be `request.build_absolute_uri()` at two API call sites, which
    made the origin of a live credential a property of whichever host the
    issuing admin was signed in on, for a page that lives on a frontend which
    may be on neither host. `settings.INVITATION_ACCEPT_URL` is now the single
    place that decides, and the host-independence half of that is pinned over in
    `test_invitation_api.py` where there is a request to have a host at all.
    """

    def counts(self):
        return (
            Invitation.objects.count(),
            User.objects.count(),
            Membership.objects.count(),
        )

    def test_the_link_is_built_from_settings_when_the_caller_passes_none(self):
        with recording():
            with self.captureOnCommitCallbacks(execute=True):
                _invitation, raw_token = invitation_service.invite_staff(
                    self.admin,
                    self.stmarys,
                    Role.TEACHER,
                    email="new.teacher@example.com",
                    full_name="New Teacher",
                )

        self.assertEqual(
            RecordingChannel.sent[-1]["accept_url"],
            f"https://portal.example.school/invitations/{raw_token}/",
        )

    def test_an_explicit_accept_url_still_overrides_the_setting(self):
        """The parameter survives as an override, which is what tests use."""
        with recording():
            with self.captureOnCommitCallbacks(execute=True):
                _invitation, raw_token = self.invite()

        self.assertEqual(
            RecordingChannel.sent[-1]["accept_url"], f"https://portal/i/{raw_token}/"
        )

    def test_an_unconfigured_accept_url_refuses_and_leaves_nothing_behind(self):
        """The reason the check runs before the transaction commits.

        A deploy that never sets the URL is a misconfiguration, and refusing is
        the right answer — but refusing *after* the placeholder account, the
        INVITED membership and the invitation had committed would have meant one
        more orphaned set per attempt, which is exactly what `_deliver()`'s
        pre-commit checks exist to prevent.
        """
        before = self.counts()
        with recording():
            with override_settings(INVITATION_ACCEPT_URL=None):
                with self.assertRaises(DeliveryNotConfigured):
                    with self.captureOnCommitCallbacks(execute=True):
                        invitation_service.invite_staff(
                            self.admin,
                            self.stmarys,
                            Role.TEACHER,
                            email="new.teacher@example.com",
                        )

        self.assertEqual(self.counts(), before)
        self.assertEqual(RecordingChannel.sent, [])

    def test_a_template_with_no_token_placeholder_is_refused(self):
        """Otherwise every invitation ever sent carries the same link."""
        before = self.counts()
        with recording():
            with override_settings(
                INVITATION_ACCEPT_URL="https://portal.example.school/invitations/"
            ):
                with self.assertRaises(DeliveryNotConfigured):
                    with self.captureOnCommitCallbacks(execute=True):
                        invitation_service.invite_staff(
                            self.admin,
                            self.stmarys,
                            Role.TEACHER,
                            email="new.teacher@example.com",
                        )

        self.assertEqual(self.counts(), before)
        self.assertEqual(RecordingChannel.sent, [])

    def test_omitting_the_url_no_longer_mints_a_token_and_sends_nothing(self):
        """The silent no-op, pinned as refused.

        `_deliver()` used to return early when `accept_url_for` was None —
        minting a live token, skipping `check_deliverable()` entirely and
        delivering nothing, with a successful return value. Any caller that
        forgot the keyword produced a placeholder account and a dead token in
        silence. There is now no way to reach that: with the setting configured
        the link is built from it, and with the setting missing the invite is
        refused above.
        """
        before = self.counts()
        with recording():
            with override_settings(INVITATION_ACCEPT_URL=None):
                with self.assertRaises(DeliveryNotConfigured):
                    invitation_service.invite_staff(
                        self.admin,
                        self.stmarys,
                        Role.TEACHER,
                        email="silent@example.com",
                        accept_url_for=None,
                    )

        self.assertEqual(self.counts(), before)
        self.assertFalse(
            User.objects.filter(email="silent@example.com").exists(),
            "a refused invite must not leave a placeholder account behind",
        )

    def test_a_resend_takes_its_link_from_the_same_setting(self):
        with recording():
            with self.captureOnCommitCallbacks(execute=True):
                invitation, _raw = self.invite()
            with self.captureOnCommitCallbacks(execute=True):
                _fresh, resent_token = invitation_service.resend_invitation(
                    self.admin, invitation
                )

        self.assertEqual(
            RecordingChannel.sent[-1]["accept_url"],
            f"https://portal.example.school/invitations/{resent_token}/",
        )


class MailConfigurationTests(InvitationSetUp):
    """Finding #7: an SMTP default with nowhere to connect.

    `settings.EMAIL_BACKEND` defaults to SMTP on purpose, so that a deploy which
    configures nothing fails closed rather than printing live tokens to the
    application log. But Django's own SMTP defaults are `localhost:25`, which is
    not a mail server on any host this runs on — so "fails closed" arrived as a
    `ConnectionRefusedError` raised from inside an `on_commit` callback, after
    everything had committed, and reached the admin as an unexplained 500.
    """

    def counts(self):
        return (
            Invitation.objects.count(),
            User.objects.count(),
            Membership.objects.count(),
        )

    def test_smtp_with_no_host_is_refused_before_anything_commits(self):
        before = self.counts()
        with override_settings(
            INVITATION_CHANNEL="schools.delivery.EmailChannel",
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="",
        ):
            with self.assertRaises(DeliveryNotConfigured):
                with self.captureOnCommitCallbacks(execute=True):
                    self.invite()

        self.assertEqual(
            self.counts(), before, "a misconfigured deploy must leave nothing behind"
        )

    def test_the_email_host_default_is_empty_not_localhost(self):
        """What `check_configured()` quietly depends on, stated out loud.

        Django's own `EMAIL_HOST` default is `"localhost"` — truthy, and
        therefore invisible to a "is this configured?" check. The guard works
        only because `settings.py` defaults it to `""` instead. Delete that one
        line and the guard stops guarding while every other test still passes,
        so the coupling is asserted here rather than left to be rediscovered.
        """
        self.assertEqual(
            settings.EMAIL_HOST,
            os.environ.get("EMAIL_HOST", ""),
            "EMAIL_HOST must come from the environment, defaulting to empty",
        )
        # And the guard has to be able to see "unset". Read the module's source
        # default rather than the live value, which an environment that *does*
        # set EMAIL_HOST would otherwise mask.
        source = (Path(settings.BASE_DIR) / "settings.py").read_text()
        self.assertIn('EMAIL_HOST = os.environ.get("EMAIL_HOST", "")', source)

    def test_a_configured_smtp_host_passes_the_check(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.example.school",
        ):
            self.assertIsNone(EmailChannel().check_configured())

    def test_backends_that_need_no_host_are_not_asked_for_one(self):
        """The check is scoped to backends that actually dial out.

        Applying it to "anything that is not locmem" would refuse every
        development deploy on the console backend, and the test runner's own
        locmem substitution besides.
        """
        for backend in (
            "django.core.mail.backends.console.EmailBackend",
            "django.core.mail.backends.locmem.EmailBackend",
            "django.core.mail.backends.filebased.EmailBackend",
        ):
            with self.subTest(backend=backend):
                with override_settings(EMAIL_BACKEND=backend, EMAIL_HOST=""):
                    self.assertIsNone(EmailChannel().check_configured())

    def test_a_mail_outage_becomes_DeliveryFailed_and_the_invitation_survives(self):
        """The post-commit half, which is genuinely too late to undo.

        A refused connection after commit is not a reason to lose the
        invitation: the row is resendable once mail is healthy. It *is* a reason
        to tell the admin something other than an unexplained 500, which is what
        the type is for.
        """
        with recording():
            invitation, _raw = self.invite()

        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
        ):
            with mock.patch(
                "schools.delivery.send_mail",
                side_effect=ConnectionRefusedError("[Errno 111] Connection refused"),
            ):
                with self.assertRaises(DeliveryFailed):
                    EmailChannel().send(
                        invitation, "a-token", accept_url="https://portal/i/a-token/"
                    )

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.PENDING)

    def test_a_bug_in_the_channel_is_not_reported_as_a_mail_outage(self):
        """The `except` is narrow so that it cannot swallow unrelated failures.

        `except Exception` around the send would fold a `TypeError` in the body
        template into "the mail server is down" — a bug report nobody would then
        ever receive, and an admin retrying an outage that does not exist.
        """
        with recording():
            invitation, _raw = self.invite()

        with mock.patch(
            "schools.delivery.send_mail",
            side_effect=TypeError("body template took the wrong argument"),
        ):
            with self.assertRaises(TypeError):
                EmailChannel().send(
                    invitation, "a-token", accept_url="https://portal/i/a-token/"
                )

    def test_send_re_checks_its_configuration_rather_than_trusting_the_caller(self):
        """`send()` is reachable on its own, exactly as `check_deliverable()` is."""
        with recording():
            invitation, _raw = self.invite()

        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST=""
        ):
            with self.assertRaises(DeliveryNotConfigured):
                EmailChannel().send(
                    invitation, "a-token", accept_url="https://portal/i/a-token/"
                )


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


class DeactivatedInviteeTests(InvitationSetUp):
    """`deactivate_user()` is how access is taken away. Inviting must respect it.

    Deactivation is deliberately reversible and erases nothing, so the row is
    still there for `matching_identifier()` to find — which is exactly why the
    flow has to ask. Nothing did, and the result was that a school could invite
    somebody the platform had disabled, watch the membership go ACTIVE on
    acceptance, and end up with a teacher on the roster and in `active_staff()`
    who could never sign in. Acceptance would also write a fresh *global*
    password onto the disabled account.

    The rule is asked at four points because each is reachable on its own:
    resolving the invitee, minting a token, validating one, and redeeming one.
    """

    def setUp(self):
        super().setUp()
        self.kemi = User.objects.create_user(
            "kemi@example.com",
            PASSWORD,
            full_name="Kemi Bello",
            email="kemi@example.com",
        )

    def test_a_deactivated_person_cannot_be_invited(self):
        deactivate_user(self.kemi)

        with self.assertRaises(invitation_service.InviteeDeactivated):
            with recording():
                self.invite(email="kemi@example.com")

        self.assertFalse(
            Membership.objects.filter(user=self.kemi, school=self.stmarys).exists(),
            "a membership was created for a disabled account",
        )
        self.assertEqual(Invitation.objects.count(), 0)
        self.assertEqual(RecordingChannel.sent, [])

    def test_reactivating_them_makes_the_invite_work_again(self):
        """The refusal is about current state, not a permanent mark."""
        deactivate_user(self.kemi)
        reactivate_user(self.kemi)

        with recording():
            invitation, _raw = self.invite(email="kemi@example.com")

        self.assertEqual(invitation.membership.status, MembershipStatus.INVITED)

    def test_a_pending_token_dies_when_the_invitee_is_deactivated(self):
        """Minted before, deactivated after: the link stops working."""
        with recording():
            _invitation, raw_token = self.invite(email="kemi@example.com")

        self.assertIsNotNone(Invitation.validate_token(raw_token))
        deactivate_user(self.kemi)
        self.assertIsNone(
            Invitation.validate_token(raw_token),
            "a disabled account's link still resolved",
        )

    def test_accept_refuses_directly_for_a_deactivated_invitee(self):
        """`validate_token()` already filters this; `accept()` is callable alone."""
        with recording():
            invitation, _raw = self.invite(email="kemi@example.com")
        deactivate_user(self.kemi)

        with self.assertRaises(invitation_service.InviteeDeactivated):
            invitation.accept()

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.PENDING)
        self.assertEqual(
            Membership.objects.get(user=self.kemi, school=self.stmarys).status,
            MembershipStatus.INVITED,
        )

    def test_a_resend_will_not_mint_a_fresh_token_for_a_disabled_account(self):
        """The mint enforces it, so the rule holds however the row was reached."""
        with recording():
            invitation, _raw = self.invite(email="kemi@example.com")
        deactivate_user(self.kemi)

        with recording():
            with self.assertRaises(invitation_service.InviteeDeactivated):
                invitation_service.resend_invitation(
                    self.admin,
                    invitation,
                    accept_url_for=lambda token: f"https://portal/i/{token}/",
                )

        self.assertEqual(Invitation.objects.count(), 1)
        self.assertEqual(RecordingChannel.sent, [])

    def test_the_placeholder_a_new_invite_creates_is_active(self):
        """A fresh invitee has no usable password, which is not the same thing."""
        with recording():
            invitation, _raw = self.invite(email="brand.new@example.com")

        user = invitation.membership.user
        self.assertTrue(user.is_active)
        self.assertFalse(user.has_usable_password())
