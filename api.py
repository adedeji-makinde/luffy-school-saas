"""The HTTP surface: sign-in and staff invitations here, the gradebook in its own module.

`/api/login/` and `/api/logout/` are the door. Sign-in is unauthenticated by
definition and is served on the **portal host only** — see `_portal_only()` for
why that is a rule about which schools a login reaches rather than a routing
preference. Sign-out is authenticated, which is what puts it behind a CSRF
check.

The invitation endpoints are five, in two halves that differ in who is on the
other end.

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

`/api/gradebook/...` is mounted from `gradebook/api.py` rather than written out
below, and the split is the tenancy line rather than file length. Everything in
this file writes **shared** tables, which is why those paths name their school
in a `{slug}`: the school is a row and has to be identified. The gradebook is a
tenant app whose tables live in the school's own schema, already chosen from the
hostname by `TenantMainMiddleware` — so its paths carry no slug, and keeping the
two conventions in one file would invite a route that mixes them. That module's
docstring has the argument in full.
"""

from typing import Optional

from django.contrib.auth import logout as end_session
from django.http import Http404
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema
from ninja.errors import AuthenticationError, HttpError

from accounts import signin
from accounts.services import NotPermitted
from accounts.session import SESSION_EXPIRED, session_auth, why_unauthenticated
from gradebook.api import MessageOut, router as gradebook_router
from schools import invitations as invitation_service
from schools.delivery import NoDeliveryAddress
from schools.models import (
    Domain,
    Invitation,
    InvitationError,
    InviteeDeactivated,
    PasswordRequired,
    School,
    WeakPassword,
)

api = NinjaAPI(title="Luffy School API", version="1.0.0")

api.add_router("/gradebook/", gradebook_router, tags=["gradebook"])


@api.exception_handler(AuthenticationError)
def unauthenticated(request, exc):
    """A 401 that says which 401 it is.

    ninja's stock answer is `{"detail": "Unauthorized"}` whether the caller never
    signed in or signed in and has since been timed out. Those are the same
    status code and completely different situations, and the difference is the
    whole of this item: a teacher part-way through a marking sheet whose session
    lapses has work in the browser that is still perfectly good, and a client
    that cannot tell a lapsed session from a missing one has to treat every 401
    as fatal and drop it.

    `code` is what a client branches on; `detail` is for a person. Both are the
    caller's own situation — whether the browser sent a session cookie — so this
    discloses nothing about anybody else, unlike the deliberately flat 404s the
    invitation routes answer a bad token with.

    `retryable` is stated rather than left to be inferred from `code`, because
    it is the thing a client most needs to know and it rests on a premise worth
    saying out loud: authentication runs *before* the view, so a request that
    came back 401 did not happen. Not "probably did not" — the operation was
    never entered. Sending it again after signing in therefore applies it once,
    not twice, and that holds for every endpoint here rather than only the
    idempotent ones.

    It is a narrower claim than "this request is safe to repeat in general". A
    write whose *response* was lost is a different situation, indistinguishable
    from this one at the browser and genuinely at risk of being applied twice.
    What covers that is the gradebook's own version check, not this flag — see
    `gradebook.api._is_our_write_arriving_twice()`.
    """
    expired = why_unauthenticated(request) == SESSION_EXPIRED
    return api.create_response(
        request,
        {
            "detail": (
                "Your session has ended. Sign in again — anything you were "
                "part-way through can be sent again once you have."
                if expired
                else "Sign in to use this endpoint."
            ),
            "code": why_unauthenticated(request),
            "retryable": expired,
        },
        status=401,
    )


# -- signing in and out ------------------------------------------------------


class SignInIn(Schema):
    """One field for the identifier, because there is one sign-in door.

    Not `email` / `phone` / `username` as three fields: staff reach for their
    email, parents for the phone number the school has on file, and a student
    for a school-issued handle that is neither. `User.matching_identifier()`
    resolves all three, and asking the person to first classify what they are
    about to type would be asking them to know something about our schema.
    """

    identifier: str
    password: str


class SchoolOut(Schema):
    """A school this login may act at, and where to go to do it.

    `host` is the reason this is not just a list of names. Sign-in happens on
    the portal and the work happens on a school's own host, so a client that is
    told only "St Mary's" still cannot get the teacher back to their marking
    sheet. It is `None` for a school with no primary domain registered — a
    setup fault rather than an ordinary state, and one this endpoint reports by
    omission rather than by refusing to sign somebody in.
    """

    slug: str
    name: str
    host: Optional[str] = None


class SignedInOut(Schema):
    """What a client gets back, which is what it needs to do anything next.

    `csrf_token` is here because every authenticated route in this API is
    cookie-authenticated and therefore CSRF-checked (ninja's `SessionAuth`
    does the check itself, since ninja exempts its own views from Django's
    middleware). `login()` rotates the token and the response carries the
    cookie, but a client that cannot read that cookie — `CSRF_COOKIE_HTTPONLY`
    is one setting away — would have a session it could not use for anything
    but GETs. Returning the token makes the endpoint sufficient on its own.
    It is not a secret: it defends against another origin making the request,
    not against the person holding it reading it.
    """

    full_name: str
    schools: list[SchoolOut]
    csrf_token: str


class RefusedOut(Schema):
    """A sign-in that did not happen, in the shape the 401 handler already uses.

    Same four keys as every other refusal from this API — `detail` for a
    person, `code` to branch on, `retryable` for whether sending it again could
    ever help — so a client has one refusal shape to handle rather than two.
    `retry_after` is whole seconds, and only present when waiting is the thing
    that fixes it.
    """

    detail: str
    code: str
    retryable: bool
    retry_after: Optional[int] = None


def _portal_only(request):
    """Sign-in is served on the portal host and nowhere else.

    The portal is where a parent with children at three schools signs in once
    and reaches all of them, and — per `SchoolAccessMiddleware` — the one host
    where somebody with no membership anywhere is still allowed through the
    door. A school's host is the opposite by design: it refuses anyone without
    an active membership *there*, so serving sign-in from it would mean the
    same credentials worked on one hostname and not another, and a teacher who
    is also a parent elsewhere would need two sessions to see their own child.

    A 404 rather than a 403, matching the gradebook's answer on the portal:
    the route does not exist on this host. Where it does exist is the client's
    own deployment configuration — it knows its portal host, and having the
    server name it would put the same fact in two places.
    """
    if getattr(request, "school", None) is not None:
        raise Http404("Sign in on the portal host.")


def _schools_of(user):
    """Every school this login may act at, with the host each one lives on.

    Two queries regardless of how many schools, which matters for the parent
    this shape exists for. `is_primary` because a school may answer on several
    hostnames and only one of them is the one to send somebody to.
    """
    schools = list(user.schools())
    hosts = dict(
        Domain.objects.filter(tenant__in=schools, is_primary=True).values_list(
            "tenant_id", "domain"
        )
    )
    return [
        SchoolOut(slug=school.slug, name=school.name, host=hosts.get(school.pk))
        for school in schools
    ]


@api.post(
    "/login/",
    response={200: SignedInOut, 401: RefusedOut, 429: RefusedOut},
    auth=None,
    tags=["session"],
)
def sign_in(request, payload: SignInIn):
    """Exchange an identifier and password for a session.

    The rules — one refusal for every kind of failure, a throttle that counts
    rather than locks — are in `accounts/signin.py`, where they hold for any
    caller rather than only for this view.
    """
    _portal_only(request)
    try:
        user = signin.sign_in(request, payload.identifier, payload.password)
    except signin.TooManyAttempts as exc:
        response = api.create_response(
            request,
            {
                "detail": str(exc),
                "code": signin.TOO_MANY_ATTEMPTS,
                "retryable": True,
                "retry_after": exc.retry_after,
            },
            status=429,
        )
        # The standard header as well as the field, for the clients and proxies
        # that honour it without reading the body.
        response["Retry-After"] = str(exc.retry_after)
        return response
    except signin.BadCredentials as exc:
        return 401, RefusedOut(
            detail=str(exc), code=signin.BAD_CREDENTIALS, retryable=False
        )

    return 200, SignedInOut(
        full_name=user.full_name,
        schools=_schools_of(user),
        csrf_token=get_token(request),
    )


@api.post("/logout/", response=MessageOut, auth=session_auth, tags=["session"])
def sign_out(request):
    """End the session now rather than waiting for it to lapse.

    Authenticated on purpose, which is what puts it behind ninja's CSRF check:
    an unauthenticated logout is a route any other origin can aim at a signed-in
    teacher's browser to throw away the session they are marking with. A caller
    whose session has *already* gone gets the ordinary `session_expired` 401,
    which is a true answer to "sign me out" — there is nothing left to end.

    `logout()` flushes the session rather than only forgetting the user, so the
    key that comes back is not one that was ever signed in.
    """
    end_session(request)
    return MessageOut(detail="Signed out.")


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
    auth=session_auth,
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
    auth=session_auth,
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
    auth=session_auth,
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
