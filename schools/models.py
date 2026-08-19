"""The tenant itself, and the invitations that bring people into one.

`School` and `Domain` are django_tenants plumbing. `Invitation` is here for a
less obvious reason: it lives in the **public** schema, like `Membership` and
for the same cause. An invitation is a (person, school, role) triple, and the
person half is global — a teacher at St Mary's invited to Grace Academy is one
`accounts.User` gaining a second `Membership`, not a new account. A per-school
invitations table could not express that without reaching across schemas, which
is the pattern `docs/tenancy.md` still blocks.

Being shared is also what lets the foreign keys below be real foreign keys,
with real referential integrity, exactly as `Membership.user` is.
"""

import hashlib
import secrets
from datetime import timedelta

from django.db import models, transaction
from django.utils import timezone
from django_tenants.models import DomainMixin, TenantMixin


class InvitationError(Exception):
    """An invitation could not be issued, redeemed or cancelled as asked.

    The base for every refusal in the flow, on both sides of the seam: the
    states this module enforces (spent, expired, membership no longer invited)
    and the ones `invitations.py` enforces (not a staff role, ambiguous invitee,
    already a member). It lives here rather than there because the dependency
    already runs this way — `invitations.py` imports this module, not the
    reverse — so `except InvitationError` catches the whole flow no matter which
    of the two a caller imported it from.
    """


class PasswordRequired(InvitationError):
    """Accepting needs a password because the invitee has no usable one yet.

    Its own type so a caller can tell "you must choose a password" — which the
    invitee can act on — apart from "this link is spent", which they cannot.
    """


class WeakPassword(InvitationError):
    """The password offered at acceptance failed `AUTH_PASSWORD_VALIDATORS`.

    Separate from `PasswordRequired` for the same reason that one is separate
    from the rest: "choose a better one" is something the invitee can act on,
    and it carries the validators' own messages so they can be shown what to fix.
    """


#: How long an invite link stays good for. Long enough to survive a weekend and
#: a forwarded email; short enough that a leaked link goes stale on its own.
DEFAULT_INVITATION_TTL = timedelta(days=7)

#: Bytes of entropy behind each token, before urlsafe-base64 expands it.
TOKEN_BYTES = 32


class School(TenantMixin):
    """One customer school. Lives in the public schema; owns a private schema.

    A School row is the thing every Membership points at, so it is also the
    unit of access control: being able to see a school's data means holding a
    live Membership here.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=60, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Schemas are created on save in real use; tests turn this off per instance.
    auto_create_schema = True
    auto_drop_schema = False

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Domain(DomainMixin):
    """Hostname a school is reached on, e.g. stmarys.luffy.school."""

    pass


def hash_token(raw_token: str) -> str:
    """The only representation of a token this system is allowed to keep."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class InvitationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    EXPIRED = "expired", "Expired"
    REVOKED = "revoked", "Revoked"


class InvitationQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=InvitationStatus.PENDING)

    def for_school(self, school):
        return self.filter(membership__school=school)


class Invitation(models.Model):
    """A pending offer of a role at a school, and the token that redeems it.

    Points at the `Membership` rather than repeating the person, the school and
    the role as three columns of its own. That one foreign key already pins all
    three, and it cannot drift out of step with the membership it is supposed to
    activate — which three loose columns could. `user`, `school` and
    `intended_role` below read them back off it.

    The membership exists first, at `INVITED`, and acceptance moves it to
    `ACTIVE`. So the invitation is a *credential* for a relationship that
    already exists in the data, not a promise of one to be created later.

    Nothing here is unique but `token_hash`. In particular there is deliberately
    no "one invitation per person" or per contact detail: a resend issues a
    second row, and a phone number shared between two people (a household with
    one handset — the case that arrives with parents) must not collide.
    """

    membership = models.ForeignKey(
        "accounts.Membership",
        related_name="invitations",
        on_delete=models.PROTECT,
        help_text="The INVITED membership this token activates.",
    )
    invited_by = models.ForeignKey(
        "accounts.User", related_name="invitations_sent", on_delete=models.PROTECT
    )

    # Only ever the SHA-256 of the token, never the token. Same reasoning as a
    # password digest: whoever can read this table still cannot mint a working
    # link from it. A leaked backup is not a set of live invitations.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)

    status = models.CharField(
        max_length=16, choices=InvitationStatus, default=InvitationStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    objects = InvitationQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["membership", "status"]),
        ]

    def __str__(self):
        return f"{self.get_status_display()} invitation for {self.membership}"

    # -- read the triple back off the membership -----------------------------

    @property
    def user(self):
        return self.membership.user

    @property
    def school(self):
        return self.membership.school

    @property
    def intended_role(self) -> str:
        return self.membership.role

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def needs_password(self) -> bool:
        """Whether accepting this has to set a password.

        The question is about the *person*, not this school: someone who already
        signs in somewhere else keeps the password they have. This is the
        `User`-level half of the two orthogonal states in docs/membership.md,
        and it is what the preview endpoint reports so the form knows whether to
        show a password field at all.
        """
        return not self.membership.user.has_usable_password()

    # -- minting and redeeming -----------------------------------------------

    @classmethod
    def create_with_token(cls, membership, invited_by, *, ttl=DEFAULT_INVITATION_TTL):
        """Create an invitation and return `(invitation, raw_token)`.

        The raw token is returned and never stored. It exists in memory for as
        long as it takes a delivery channel to put it in a link, and after that
        only in whatever the recipient received — so a lost token is reissued,
        never recovered.
        """
        raw_token = secrets.token_urlsafe(TOKEN_BYTES)
        invitation = cls.objects.create(
            membership=membership,
            invited_by=invited_by,
            token_hash=hash_token(raw_token),
            expires_at=timezone.now() + ttl,
        )
        return invitation, raw_token

    @classmethod
    def validate_token(cls, raw_token):
        """The pending invitation `raw_token` redeems, or None.

        Returns None for a token that is unknown, already used, revoked, past
        its expiry, or whose membership is no longer INVITED — the caller cannot
        tell which, and should not: distinguishing "no such invitation" from
        "that one is spent" tells an attacker holding a guessed token whether
        they guessed a real one.

        That last condition is why the membership's status is in the query. A
        PENDING invitation is not enough on its own: ending or suspending a
        membership leaves any outstanding invitation untouched, so without this
        the link would still open, and redeeming it would set a password on an
        account whose reason for existing has been withdrawn. Falling into the
        same flat None as every other dead token is also the right answer to the
        holder — "your membership was ended" is not their business to learn from
        a link.

        Expiry is settled here rather than by a scheduled job. An invitation is
        only ever looked at when someone presents its token, so the lazy sweep
        is enough, and it means a row cannot sit in PENDING past its date while
        a cron job is broken.
        """
        if not raw_token:
            return None

        # Local for the same app-registry reason as the import in accept().
        from accounts.models import MembershipStatus

        invitation = (
            cls.objects.select_related("membership__user", "membership__school")
            .filter(
                token_hash=hash_token(raw_token),
                status=InvitationStatus.PENDING,
                membership__status=MembershipStatus.INVITED,
            )
            .first()
        )
        if invitation is None:
            return None
        if invitation.is_expired:
            invitation.status = InvitationStatus.EXPIRED
            invitation.save(update_fields=["status"])
            return None
        return invitation

    @transaction.atomic
    def accept(self, password=None):
        """Redeem this invitation. Returns the now-active `Membership`.

        Two states move, and they are not the same state — which is the whole
        reason this method is careful. The *person* may or may not already have
        a usable credential; *this school's* relationship goes from INVITED to
        ACTIVE regardless. A teacher accepting their second school has a
        password already and must not be asked for another; a brand-new hire has
        the unusable placeholder from `create_user(username, None)` and has to
        set one now.

        `has_usable_password()` is the test rather than "is this their first
        membership", because the two can disagree: someone can hold a membership
        and still have never set a password.
        """
        if self.status != InvitationStatus.PENDING:
            raise InvitationError(f"This invitation is {self.get_status_display().lower()}.")
        if self.is_expired:
            self.status = InvitationStatus.EXPIRED
            self.save(update_fields=["status"])
            raise InvitationError("This invitation has expired.")

        # Imported here, not at module scope. `schools` is loaded before
        # `accounts` (see SHARED_APPS), so importing accounts.models while this
        # module is being read would ask for an app registry that is still
        # filling. The foreign keys above use string references for the same
        # reason and never need the class at import time.
        from accounts.models import MembershipStatus, User

        # These two are local only to sit beside that one, not for any registry
        # reason of their own.
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        # PENDING is not sufficient. The invitation is a credential for a
        # relationship, and that relationship must still be open — a membership
        # ended or suspended after the link went out must not be revivable by
        # redeeming it, and the token must not become a way to set a password on
        # an account whose reason for existing has been withdrawn. `validate_token()`
        # already filters on this; asked again here because `accept()` is callable
        # directly, and the rule is worth more than the path that reaches it.
        membership = self.membership
        if membership.status != MembershipStatus.INVITED:
            raise InvitationError(
                f"The membership this invitation activates is "
                f"{membership.get_status_display().lower()}, not invited."
            )

        # select_for_update: two clicks on the same link should not both run
        # this. The loser finds the row no longer PENDING and is refused above.
        user = User.objects.select_for_update().get(pk=self.membership.user_id)

        if not user.has_usable_password():
            if not password:
                raise PasswordRequired(
                    "This is your first sign-in, so a password is required."
                )
            # Validated here rather than at the endpoint because this is the
            # only place in the codebase that sets a password on somebody's
            # behalf, and what it writes is a *global* credential: it signs them
            # in at every school they hold a membership at, not just this one.
            # A rule worth having is not worth having only over HTTP.
            try:
                validate_password(password, user)
            except DjangoValidationError as exc:
                raise WeakPassword(" ".join(exc.messages)) from exc
            user.set_password(password)
            user.save(update_fields=["password"])
        # ...and if they already have one, `password` is ignored rather than
        # rejected: the form had no reason to send it, but a client that does
        # must not be able to silently reset an existing account's credential
        # through an invite link.

        # Unconditional: the guard above already established that this is the
        # INVITED membership acceptance exists to promote.
        membership.status = MembershipStatus.ACTIVE
        membership.save(update_fields=["status", "updated_at"])

        self.status = InvitationStatus.ACCEPTED
        self.accepted_at = timezone.now()
        self.save(update_fields=["status", "accepted_at"])
        return membership

    def revoke(self):
        """Cancel a pending invitation. Its token stops working immediately.

        Leaves the INVITED membership alone. Revoking an invite is "that link is
        dead", not "this person is not joining" — a resend issues a new token
        against the same membership, which is exactly why the two are separate.
        `end_membership` on the service side is the other decision.
        """
        if self.status != InvitationStatus.PENDING:
            raise InvitationError(
                f"Only a pending invitation can be revoked; this one is "
                f"{self.get_status_display().lower()}."
            )
        self.status = InvitationStatus.REVOKED
        self.save(update_fields=["status"])
        return self
