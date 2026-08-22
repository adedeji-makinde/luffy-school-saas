"""Exchanging credentials for a session.

The missing half of "handle session expiry mid-form-entry". The API can already
tell a client that its session lapsed and that the refused request is safe to
send again; until this existed there was nowhere to send the credentials that
would make that true, and the only real sign-in form on the platform was Django
admin's.

`IdentifierBackend` already does the resolving, and does the part that matters
for disclosure: a missing account is charged the same password hash as a real
one, so "no such person" and "wrong password" do not differ in how long they
take. This module is what sits around that — the throttle, and the single
refusal that every failure shares.

**One refusal, whatever went wrong.** No account, wrong password, deactivated
account, two accounts matching one identifier: all four answer with the same
status, the same code and the same sentence. Splitting them is the standing
temptation on a sign-in route and it is how a login endpoint becomes an
account-existence oracle — every email and phone number on the platform, one
guess at a time, with no credential needed. The invitation routes answer a bad
token with a flat 404 for the same reason, and `accounts/session.py` explains
why the 401 on *expiry* is a different case that discloses nothing.

The throttle is deliberately outside that rule and does not break it: it is
counted against the identifier as typed, whether or not it resolves to
anybody, so being throttled says nothing about whether the account is real.
Were it counted only for accounts that exist, the 429 would be the oracle the
401 refuses to be.
"""

from django.contrib.auth import authenticate
from django.contrib.auth import login as start_session

from . import throttling
from .models import SignInScope

#: What every failed sign-in says, regardless of which failure it was.
REFUSED = "That identifier and password do not match an account."

#: What a throttled sign-in says. Names no account and no identifier: it is a
#: statement about this caller's recent traffic, not about who exists.
THROTTLED = (
    "Too many failed sign-in attempts. Wait a moment and try again — "
    "nothing has been locked."
)

BAD_CREDENTIALS = "bad_credentials"
TOO_MANY_ATTEMPTS = "too_many_attempts"


class SignInError(Exception):
    """Sign-in was refused.

    One base class for the module, as `GradebookError` and `InvitationError`
    are for theirs, so a caller catches every refusal rather than the half it
    remembered to import.
    """


class BadCredentials(SignInError):
    """The identifier and password did not authenticate anybody.

    Carries no detail about which part was wrong, because there is none to
    carry: the caller is told `REFUSED` and nothing else.
    """


class TooManyAttempts(SignInError):
    """The throttle is closed for this identifier or this address."""

    def __init__(self, retry_after: int):
        super().__init__(THROTTLED)
        #: Whole seconds until another attempt is worth making.
        self.retry_after = retry_after


def wait_before_retrying(identifier: str, address: str):
    """Seconds this attempt must wait, or None if it may go ahead.

    The longer of the two windows, so a client that waits exactly as long as it
    was told is not refused a second time by the other limit.
    """
    open_windows = [
        wait
        for wait in (
            throttling.blocked_for(SignInScope.IDENTIFIER, identifier),
            throttling.blocked_for(SignInScope.ADDRESS, address),
        )
        if wait is not None
    ]
    return max(open_windows) if open_windows else None


def note_failure(identifier: str, address: str) -> None:
    """Count one wrong answer against both keys."""
    throttling.record_failure(SignInScope.IDENTIFIER, identifier)
    throttling.record_failure(SignInScope.ADDRESS, address)


def note_success(identifier: str) -> None:
    """Forgive the identifier's failures, and deliberately not the address's."""
    throttling.clear(SignInScope.IDENTIFIER, identifier)


def sign_in(request, identifier: str, password: str):
    """Authenticate and open a session, or raise a `SignInError`.

    Order matters and is the security-relevant part of this function:

    1. **The throttle is asked first**, before any lookup or password hash.
       A closed window has to be closed to everybody, including a caller whose
       next guess happens to be right — checking afterwards would let an
       attacker keep guessing as long as they were willing to read the 429.
    2. **A failure is counted against both keys.** The identifier bounds the
       guesses one account absorbs; the address bounds the guesses one machine
       makes across every account.
    3. **Success clears the identifier and leaves the address alone.** See
       `throttling.clear()` for why the asymmetry is not an oversight.

    `start_session()` is Django's `login()`, which cycles the session key —
    so a session key fixed by an attacker before sign-in is not the key the
    person ends up holding — and rotates the CSRF token with it.
    """
    address = throttling.client_address(request)
    wait = wait_before_retrying(identifier, address)
    if wait is not None:
        raise TooManyAttempts(wait)

    user = authenticate(request, username=identifier, password=password)
    if user is None:
        note_failure(identifier, address)
        raise BadCredentials(REFUSED)

    note_success(identifier)
    start_session(request, user)
    return user
