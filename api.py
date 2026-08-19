"""HTTP surface for the staff invitation flow.

Five endpoints, in two halves that differ in who is on the other end.

The three `/api/schools/{slug}/...` routes are administrative and authenticated:
a signed-in person acting at a school they hold authority at. Authority is not
re-implemented here — `invitations.py` calls the same
`_require_grant_authority()` every other membership write goes through, so an
admin's reach stops at their own school in exactly the same way.

The two `/api/invitations/{token}/` routes are the opposite: the caller is not
signed in and by definition cannot be, because the whole point of the flow is
that they may not have a usable password yet. The token *is* the credential.
Both are therefore unauthenticated, both look their invitation up through
`Invitation.validate_token()`, and both answer a bad token with a flat 404 —
never "expired" versus "revoked" versus "no such thing", which would tell
somebody testing guessed tokens which of them were real.
"""

from typing import Optional

from django.http import Http404
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts.services import NotPermitted
from schools import invitations as invitation_service
from schools.delivery import NoDeliveryAddress
from schools.models import (
    Invitation,
    InvitationError,
    InviteeDeactivated,
    PasswordRequired,
    School,
    WeakPassword,
)

api = NinjaAPI(title="Luffy School API", version="1.0.0")


# -- request and response shapes ---------------------------------------------


class InviteIn(Schema):
    role: str
    email: Optional[str] = None
    phone: Optional[str] = None
    full_name: str = ""


class InvitationOut(Schema):
    """What the issuing admin is told back.

    Deliberately says nothing about *who* the invitation resolved to. Identity
    is global here, so a matching account may belong to a person this school has
    no relationship with — echoing their stored name would hand a school admin a
    stranger's real name from another school. Worse, the echo differed between a
    reused account and a fresh placeholder, which made the endpoint an
    exists/does-not-exist oracle for any email or phone on the platform, one
    unsolicited invitation email per probe.

    The admin already knows who they invited: they typed the identifier. The
    invitee's own name is shown to the invitee, on `PreviewOut`, where the token
    is the proof that they are the person being named.
    """

    id: int
    status: str
    role: str
    school: str
    expires_at: str

    @staticmethod
    def of(invitation) -> "InvitationOut":
        return InvitationOut(
            id=invitation.pk,
            status=invitation.status,
            role=invitation.intended_role,
            school=invitation.school.name,
            expires_at=invitation.expires_at.isoformat(),
        )


class PreviewOut(Schema):
    """What the invitee is shown before they commit to anything.

    `needs_password` is the whole reason this endpoint exists. It is a question
    about the *person*, not this school: somebody who already signs in elsewhere
    keeps their password, and asking them to choose a second one would be both
    confusing and wrong. The form renders a password field if and only if this
    is true, and `/accept/` enforces the same rule server-side.

    `role` is the stored value here as it is on every other response, and
    `role_display` carries the label. This endpoint used to put the label in
    `role` itself, which made that field mean the database value on three
    endpoints and the human label on this one — so a client keying off it broke
    on whichever it had not been written against. Two fields say both things
    without either being a guess.
    """

    school: str
    role: str
    role_display: str
    invitee: str
    needs_password: bool
    expires_at: str


class AcceptIn(Schema):
    password: Optional[str] = None


class AcceptedOut(Schema):
    school: str
    role: str
    status: str


# -- administrative: issuing and cancelling ----------------------------------


@api.post(
    "/schools/{slug}/invitations/",
    response={201: InvitationOut},
    auth=django_auth,
    tags=["invitations"],
)
def create_invitation(request, slug: str, payload: InviteIn):
    school = get_object_or_404(School, slug=slug)
    try:
        invitation, _raw_token = invitation_service.invite_staff(
            request.user,
            school,
            payload.role,
            email=payload.email,
            phone=payload.phone,
            full_name=payload.full_name,
            # The link the invitee clicks. A frontend route, not this API —
            # the token is handed to the channel and never returned in the
            # response, so an admin cannot read it back out of the API either.
            accept_url_for=lambda token: request.build_absolute_uri(
                f"/invitations/{token}/"
            ),
        )
    except NotPermitted as exc:
        raise HttpError(403, str(exc))
    except (
        invitation_service.AlreadyAMember,
        invitation_service.MembershipNotOpen,
        InviteeDeactivated,
    ) as exc:
        # 409, not 400: the request is well formed and the caller has the
        # authority. It is the state — at this school, or of the account being
        # invited — that leaves nothing to do. Ahead of the InvitationError
        # handler below, which is their base class.
        raise HttpError(409, str(exc))
    except (InvitationError, NoDeliveryAddress) as exc:
        raise HttpError(400, str(exc))
    return 201, InvitationOut.of(invitation)


@api.post(
    "/schools/{slug}/invitations/{invitation_id}/resend/",
    response={201: InvitationOut},
    auth=django_auth,
    tags=["invitations"],
)
def resend_invitation(request, slug: str, invitation_id: int):
    """Issue a fresh token and kill the old one.

    201 rather than 200, and the response carries a **new** id: a resend is a
    second row, not an update in place. That is what makes the previous link
    die the instant this one is minted, and it keeps both in the audit trail.
    Revoked and expired invitations may be resent — a link going stale is the
    ordinary reason somebody asks for another; an accepted one may not, which
    `resend_invitation()` enforces so a non-HTTP caller cannot get past it.
    """
    school = get_object_or_404(School, slug=slug)
    invitation = get_object_or_404(
        Invitation.objects.select_related("membership__school", "membership__user"),
        pk=invitation_id,
        membership__school=school,
    )
    try:
        fresh, _raw_token = invitation_service.resend_invitation(
            request.user,
            invitation,
            accept_url_for=lambda token: request.build_absolute_uri(
                f"/invitations/{token}/"
            ),
        )
    except NotPermitted as exc:
        raise HttpError(403, str(exc))
    except NoDeliveryAddress as exc:
        raise HttpError(400, str(exc))
    except InvitationError as exc:
        # AlreadyAccepted and MembershipNotOpen both land here, and so does
        # anything else the flow refuses on state grounds — which is what this
        # endpoint's refusals are. One handler is safe now that there is one
        # hierarchy; while there were two, a models-side refusal escaping here
        # was a 500.
        raise HttpError(409, str(exc))
    return 201, InvitationOut.of(fresh)


@api.post(
    "/schools/{slug}/invitations/{invitation_id}/revoke/",
    response=InvitationOut,
    auth=django_auth,
    tags=["invitations"],
)
def revoke_invitation(request, slug: str, invitation_id: int):
    school = get_object_or_404(School, slug=slug)
    invitation = get_object_or_404(
        Invitation.objects.select_related("membership__school", "membership__user"),
        pk=invitation_id,
        membership__school=school,
    )
    try:
        invitation_service.revoke_invitation(request.user, invitation)
    except NotPermitted as exc:
        raise HttpError(403, str(exc))
    except InvitationError as exc:
        raise HttpError(409, str(exc))
    return InvitationOut.of(invitation)


# -- the invitee's half, unauthenticated -------------------------------------


def _validated(token: str):
    invitation = Invitation.validate_token(token)
    if invitation is None:
        # One answer for unknown, spent, revoked and expired alike.
        raise Http404("No such invitation.")
    return invitation


@api.get("/invitations/{token}/", response=PreviewOut, auth=None, tags=["invitations"])
def preview_invitation(request, token: str):
    invitation = _validated(token)
    return PreviewOut(
        school=invitation.school.name,
        role=invitation.intended_role,
        role_display=invitation.membership.get_role_display(),
        invitee=invitation.user.full_name or invitation.user.username,
        needs_password=invitation.needs_password,
        expires_at=invitation.expires_at.isoformat(),
    )


@api.post(
    "/invitations/{token}/accept/", response=AcceptedOut, auth=None, tags=["invitations"]
)
def accept_invitation(request, token: str, payload: AcceptIn):
    invitation = _validated(token)
    try:
        membership = invitation.accept(password=payload.password)
    except (PasswordRequired, WeakPassword) as exc:
        # 422 rather than 400: the request was well formed and the token is
        # good, but the password field is missing or not good enough. Both are
        # things the invitee can fix and resubmit, unlike a spent link.
        raise HttpError(422, str(exc))
    except InvitationError as exc:
        raise HttpError(409, str(exc))
    return AcceptedOut(
        school=membership.school.name,
        role=membership.role,
        status=membership.status,
    )
