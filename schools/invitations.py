"""Inviting staff into a school: the rules that span identity and membership.

The model knows whether a token is good. The delivery module knows how to reach
a person. This is where the flow lives — resolving who is being invited, giving
them an INVITED membership, minting the token and handing it to a channel.

The thing worth reading carefully is `resolve_invitee()`. Identity is global
here: one `User` per person for the life of their relationship with the
platform, no matter how many schools they end up at. So inviting somebody is
mostly a question of *whether they already exist*, and getting that wrong in the
direction of "create a new account" is how a teacher ends up with two logins and
a parent loses sight of one of their children.
"""

from django.db import transaction

from accounts.identifiers import normalize_email, try_normalize_phone
from accounts.models import Membership, MembershipStatus, Role, STAFF_ROLES, User
from accounts.services import NotPermitted, _require_grant_authority, grant_membership

from .delivery import get_channel
from .models import Invitation, InvitationError

# `InvitationError` is deliberately imported rather than defined here. This
# module used to declare its own class of that name, which left two unrelated
# exceptions sharing one name in one package: `except InvitationError` caught
# the half you happened to have imported and let the other half through, and
# `api.py` had to keep the two apart by qualifying one of them. It lives in
# `models.py` because the dependency already runs that way — everything below
# subclasses it, so one `except InvitationError` now means what it looks like.


class NotStaffRole(InvitationError):
    """This pass invites staff only."""


class AmbiguousInvitee(InvitationError):
    """The contact details given point at more than one existing person."""


class AlreadyAccepted(InvitationError):
    """Nothing left to resend — the invitee is already in."""


class AlreadyAMember(InvitationError):
    """There is nothing to invite them to: they already hold this role here."""


class MembershipNotOpen(InvitationError):
    """The relationship an invitation would activate is no longer open.

    Suspended or ended, rather than accepted. Its own type because the answer
    differs: an accepted invitee needs nothing, while a suspended or ended one
    needs the membership reinstated before any link is worth sending.
    """


def resolve_invitee(*, email=None, phone=None, full_name="", username=None):
    """Find the `User` this invite is for, creating one only if there is none.

    Returns `(user, created)`.

    An existing account is reused rather than duplicated — that is the case the
    whole membership model exists to serve, and it is why acceptance may need no
    password at all. `User.objects.matching_identifier()` does the lookup, which
    is the same method sign-in resolves through, so an invite can never find a
    person that a later login would not.

    A new account gets `create_user(username, None)`: an unusable password that
    cannot authenticate, which is the placeholder docs/membership.md names.
    Acceptance is what turns it into a credential.

    No uniqueness is assumed beyond what `User` already enforces. In particular
    two people may be invited to the same school with no contact detail in
    common and nothing here couples them; and where a contact detail *is* shared
    — a household with one phone, which is a parent case rather than a staff one
    — this refuses rather than guessing, because picking one of two matches is
    how somebody is invited into a stranger's account.
    """
    email = normalize_email(email)
    phone = try_normalize_phone(phone) if phone else None
    if not email and not phone:
        raise InvitationError("An email address or a phone number is required.")

    matches = User.objects.none()
    for identifier in filter(None, (email, phone)):
        matches = matches | User.objects.matching_identifier(identifier)
    matches = list(matches.distinct())

    if len(matches) > 1:
        raise AmbiguousInvitee(
            "Those contact details match more than one existing account; "
            "invite by a single unambiguous identifier instead."
        )
    if matches:
        return matches[0], False

    # Staff are invited by email in this pass, so the email is the natural
    # handle; a phone-only invite falls back to the number, which
    # canonical_username() stores in E.164 to match the phone column.
    user = User.objects.create_user(
        username or email or phone,
        None,  # unusable password until acceptance sets one
        email=email,
        phone=phone,
        full_name=full_name,
    )
    return user, True


@transaction.atomic
def invite_staff(
    actor,
    school,
    role,
    *,
    email=None,
    phone=None,
    full_name="",
    ttl=None,
    accept_url_for=None,
):
    """Invite somebody to hold `role` at `school`. Returns `(invitation, raw_token)`.

    Authority is checked with the same `_require_grant_authority()` that guards
    every other membership write, so an admin's reach stops at their own school
    here exactly as it does in `services.grant_membership_as()`.

    The membership is created at INVITED, which is a real relationship that
    grants no access: it appears on the school's roster and not in any
    active-staff query. Acceptance is what promotes it.

    Somebody who already holds this role here is refused rather than re-invited.
    `grant_membership()` is idempotent and returns a live row untouched, so the
    requested `status=INVITED` would be silently dropped and the token would be
    minted against an ACTIVE membership — a working credential for a live
    account, sent to somebody who never asked for one. An *ended* membership is
    not in the way: re-hiring is a real thing, and reviving the row to INVITED
    is what should happen. What does not come back with it is the old link —
    `_issue()` revokes any invitation still pending against the membership
    before minting, so re-hiring issues a new credential rather than
    resurrecting one that was minted for a relationship since ended.
    """
    if role not in STAFF_ROLES:
        raise NotStaffRole(
            f"{role!r} is not a staff role. This pass invites staff only "
            f"({', '.join(sorted(STAFF_ROLES))}); parents and students come later."
        )
    _require_grant_authority(actor, school)

    user, _created = resolve_invitee(email=email, phone=phone, full_name=full_name)

    # Locked, not merely read: `grant_membership()` takes the same row under
    # `select_for_update` a line later, and checking without the lock would
    # leave a window for a concurrent invite to slip past this guard.
    existing = (
        Membership.objects.select_for_update()
        .filter(user=user, school=school, role=role)
        .first()
    )
    if existing is not None and existing.is_live:
        if existing.status == MembershipStatus.ACTIVE:
            raise AlreadyAMember(
                f"{user} is already {existing.get_role_display().lower()} at "
                f"{school}. Nothing to accept."
            )
        if existing.status != MembershipStatus.INVITED:
            raise MembershipNotOpen(
                f"{user}'s membership at {school} is "
                f"{existing.get_status_display().lower()}. Reinstate it rather "
                f"than inviting them again."
            )

    membership = grant_membership(
        user, school, role, status=MembershipStatus.INVITED
    )
    invitation, raw_token = _issue(membership, actor, ttl=ttl)
    _deliver(invitation, raw_token, accept_url_for)
    return invitation, raw_token


@transaction.atomic
def resend_invitation(actor, invitation, *, ttl=None, accept_url_for=None):
    """Issue a fresh token and kill the old one. Returns `(invitation, raw_token)`.

    The previous invitation is revoked rather than updated in place, so the old
    link stops working the moment the new one is minted and the audit trail
    keeps both. That is also why nothing here is unique per person: a resend is
    a second row by design.

    Revoked and expired invitations may be resent — a link going stale is the
    ordinary reason somebody asks for another. An accepted one may not: the
    person is already in, and minting a fresh token against a live membership
    would put a working credential for an active account in an inbox.

    That last rule is asked of the **membership**, not of the row passed in, and
    the distinction is the whole point. A resend mints a second row and revokes
    the first, so after invite → resend → accept the membership is ACTIVE while
    row one sits at REVOKED. Reading the rule off row one would see "revoked,
    therefore resendable" and mint a live token for an account that is already
    in — exactly the outcome the paragraph above forbids. The membership is the
    thing acceptance moves, so it is the thing to ask.

    Killing the old token is `_issue()`'s job, not this function's. It used to
    be done here, on the row passed in, which meant the guarantee held only for
    resends and only for that row.
    """
    _require_grant_authority(actor, invitation.school)

    status = invitation.membership.status
    if status == MembershipStatus.ACTIVE:
        raise AlreadyAccepted("That invitation has already been accepted.")
    if status != MembershipStatus.INVITED:
        raise MembershipNotOpen(
            f"The membership this invitation activates is "
            f"{invitation.membership.get_status_display().lower()}, not invited."
        )
    fresh, raw_token = _issue(invitation.membership, actor, ttl=ttl)
    _deliver(fresh, raw_token, accept_url_for)
    return fresh, raw_token


@transaction.atomic
def revoke_invitation(actor, invitation):
    """Cancel a pending invite. The INVITED membership is left alone."""
    _require_grant_authority(actor, invitation.school)
    return invitation.revoke()


def _issue(membership, actor, *, ttl=None):
    """Mint a token for `membership`, killing every other live one it has.

    At most one invitation per membership is live at a time. That was already
    the intent — `resend_invitation()` revoked the row it was handed before
    minting the replacement — but only along that one path, and only for that
    one row. Inviting the same person twice left both tokens working, and
    reviving an ended membership brought its old pending invitation back with
    it, because the rule lived at the call site rather than at the mint.

    Doing it here makes it a property of issuing rather than of resending, so
    the invariant holds however the row was reached. It is also the honest
    counterpart to tying a token's life to its membership: a link is dead once
    the relationship it activates has moved on, and a link is dead once a newer
    one exists for the same relationship.

    Locked, because the whole point is that two of these must not overlap. The
    lock serialises concurrent issues against the *existing* rows; it cannot
    stop a concurrent INSERT, so two callers minting for one membership at the
    same instant can still leave two live tokens. Both are for the same person,
    school and role, and both still expire, so the failure is benign. Making it
    impossible rather than unlikely wants a partial unique index on
    (membership) where status = 'pending' — a real constraint, in the same
    spirit as the one-school-per-student index, and a migration this does not
    need in order to hold in practice.
    """
    stale = (
        Invitation.objects.select_for_update().filter(membership=membership).pending()
    )
    for invitation in stale:
        invitation.revoke()

    kwargs = {"ttl": ttl} if ttl is not None else {}
    return Invitation.create_with_token(membership, actor, **kwargs)


def _deliver(invitation, raw_token, accept_url_for):
    """Hand the raw token to the configured channel.

    Sending happens through `transaction.on_commit`, so a delivery is never
    dispatched for an invitation that then rolls back — the alternative is a
    live link in somebody's inbox pointing at a row that does not exist.

    But `on_commit` also means a failure inside `send()` arrives *after* the
    commit, where it can no longer undo anything: the caller saw an error and
    the placeholder user, the membership and an undeliverable invitation are all
    still there, one more orphan per retry. So the deterministic half of that
    failure — "there is no address to send to" — is asked first, here, while the
    transaction is still open and raising still rolls everything back.

    What remains post-commit is genuine infrastructure failure (an SMTP outage),
    where the row surviving is the right outcome: the invitation exists and can
    be resent once the channel is healthy.
    """
    if accept_url_for is None:
        return
    channel = get_channel()

    # Optional half of the seam — a test double, or any channel that cannot
    # answer without sending, simply does not define it. See delivery.Channel.
    check_deliverable = getattr(channel, "check_deliverable", None)
    if check_deliverable is not None:
        check_deliverable(invitation)

    accept_url = accept_url_for(raw_token)
    transaction.on_commit(
        lambda: channel.send(invitation, raw_token, accept_url=accept_url)
    )


def pending_invitations(school):
    """Every live invite at one school, newest first."""
    return (
        Invitation.objects.for_school(school)
        .pending()
        .select_related("membership__user", "membership__school", "invited_by")
    )


def active_staff(school, *, role=None):
    """Staff who may actually act at `school` right now.

    Access-scoped, not roster-scoped: an invited person is on the roster (see
    `services.school_directory()`, which is deliberately wider) but is not staff
    until they accept. Keeping these two queries separate is what stops an
    unaccepted invitation from reading as a working teacher.
    """
    qs = (
        Membership.objects.for_school(school)
        .with_access()
        .filter(role__in=STAFF_ROLES)
        .select_related("user")
    )
    return qs.filter(role=role) if role else qs


__all__ = [
    "AlreadyAMember",
    "AlreadyAccepted",
    "AmbiguousInvitee",
    "InvitationError",
    "MembershipNotOpen",
    "NotPermitted",
    "NotStaffRole",
    "Role",
    "active_staff",
    "invite_staff",
    "pending_invitations",
    "resend_invitation",
    "resolve_invitee",
    "revoke_invitation",
]
