"""How many times sign-in may be got wrong, and what happens then.

This codebase had no throttle of any kind before the sign-in endpoint existed,
and a login route is the wrong place to introduce the concept casually: it is
the one endpoint whose whole purpose is to accept guesses from people who are
not yet anybody. So the shape is stated here rather than left implicit at the
call site.

**Counted, not locked.** Reaching the limit closes the window for a few
minutes; it never disables an account. A lockout looks like the stronger
control and is in fact a weapon: the identifiers here are semi-public by
design — a school publishes staff email addresses, a parent's phone number is
on the enrolment form, a student's handle is printed on their report card — so
anyone who can read a school's website could hold a teacher out of the
gradebook for as long as they cared to keep typing. Against a ten-character
password floor (`AUTH_PASSWORD_VALIDATORS`) a few guesses per quarter-hour buys
an attacker nothing, and costs a teacher who has genuinely forgotten their
password a short wait rather than a call to an administrator who may not be in
the building.

**Failures, not attempts.** A successful sign-in is not counted at all, which
is what makes the per-address limit survive contact with a school. A staff room
reaches the internet through one NAT address, so counting every sign-in would
mean the fortieth teacher to arrive at 07:50 is refused for being the fortieth.
Only mistakes accumulate.

**Postgres, not the cache.** There is no `CACHES` entry in this project, so
Django's default is `LocMemCache` — per process. A cache-backed throttle would
therefore count separately in each worker, turning a limit of ten into ten
times however many processes are running, and nothing about it would look
wrong: the setting would read correctly, the tests would pass on a single
process, and the limit would simply not be the limit in production. The
database is the one shared thing this project already deploys.

The cost is a write per failed attempt, which is the cheapest possible traffic
to pay for, and a row per distinct key ever seen. Rows are reset in place
rather than accumulated, so the table is bounded by distinct keys rather than
by attempts; a sweep of long-idle rows is a maintenance job, not a correctness
one, and is left out deliberately rather than half-built.
"""

import hashlib
import math
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .identifiers import canonical_username
from .models import SignInAttempts, SignInScope

#: Constraint that fires when two requests create the counter for one key at the
#: same instant. Named so the collision can be told apart from any other
#: integrity failure, on the same reasoning as
#: `gradebook.services._is_the_first_mark_colliding()`.
_COUNTER_COLLISION = "one_signin_counter_per_key"


def _window() -> timedelta:
    """The counting window, read per call so `override_settings` is honoured."""
    return timedelta(seconds=settings.SIGN_IN_THROTTLE_WINDOW)


def _limit(scope) -> int:
    if scope == SignInScope.ADDRESS:
        return settings.SIGN_IN_MAX_FAILURES_PER_ADDRESS
    return settings.SIGN_IN_MAX_FAILURES_PER_IDENTIFIER


def client_address(request) -> str:
    """The address to count against, and only as much of it as we can trust.

    `X-Forwarded-For` is written by whoever sent the request until a proxy we
    control overwrites it, so believing it by default would hand every client a
    way to opt out of the per-address limit by inventing an address per
    guess. `TRUSTED_PROXY_COUNT` says how many hops at the *right-hand* end of
    that header this deployment put there; the default of zero means the header
    is ignored entirely and `REMOTE_ADDR` — the only address the socket itself
    can vouch for — is used.

    Setting it too high is the dangerous direction: each extra hop hands one
    more entry back to the caller. It is a deployment fact, so it lives in
    settings rather than being guessed from the request.

    A request with no `REMOTE_ADDR` at all — which WSGI does not produce, but
    which is the one input that would matter — falls into a single shared
    bucket rather than skipping the limit. Fifty failures would then close that
    bucket for everybody, so the failure mode is a wait for all rather than a
    free pass for one; of the two, that is the one to be caught by.
    """
    hops = settings.TRUSTED_PROXY_COUNT
    remote_addr = request.META.get("REMOTE_ADDR") or ""
    if hops < 1:
        return remote_addr

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if not forwarded:
        return remote_addr

    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not chain:
        return remote_addr
    # Count in from the right: the last entry was written by our own edge, the
    # one before it by the hop in front of that, and so on.
    return chain[-min(hops, len(chain))]


def key_for(scope, value: str) -> str:
    """The stored form of a throttle key.

    Identifiers are normalized first so that the three spellings of one phone
    number share a bucket — `matching_identifier()` resolves them to one
    account, so counting them as three would give an attacker three windows for
    one target — and then hashed, because what arrives here is not reliably an
    identifier. See `SignInAttempts`.
    """
    if scope == SignInScope.ADDRESS:
        return value
    normalized = canonical_username((value or "").strip()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def blocked_for(scope, value: str):
    """Seconds until `value` may try again, or None if it may try now.

    Read without a lock. Two failures racing can leave the count one short of
    where a serial run would have put it, which lets a single extra guess
    through a window of several minutes — the wrong thing to buy a row lock on
    the path every honest sign-in also takes. The count itself is not lost:
    `record_failure()` does take the lock.
    """
    row = (
        SignInAttempts.objects.filter(scope=scope, key=key_for(scope, value))
        .only("failures", "window_started_at")
        .first()
    )
    if row is None or row.failures < _limit(scope):
        return None

    ends_at = row.window_started_at + _window()
    remaining = (ends_at - timezone.now()).total_seconds()
    if remaining <= 0:
        # The window has run out; the count is stale and the next failure
        # resets it. Nothing to serve here.
        return None
    return max(1, math.ceil(remaining))


def _is_the_counter_colliding(exc) -> bool:
    """Did two requests create this key's row together, or did something else fail?

    Postgres names the constraint that refused the row, so this is asked of the
    failure itself rather than inferred from whether a row is there now — the
    inference is wrong in both directions under concurrency. A cause carrying no
    diagnostics is treated as "not a collision", so an unrecognised failure is
    raised rather than swallowed into a retry.
    """
    diag = getattr(getattr(exc, "__cause__", None), "diag", None)
    return getattr(diag, "constraint_name", None) == _COUNTER_COLLISION


def _locked_counter(scope, key: str) -> SignInAttempts:
    """This key's row, locked for update, created if it is not there yet."""
    row = SignInAttempts.objects.select_for_update().filter(scope=scope, key=key).first()
    if row is not None:
        return row
    try:
        # Its own atomic block: an IntegrityError leaves the enclosing
        # transaction unusable, and the enclosing transaction is the one that
        # still has to record the failure.
        with transaction.atomic():
            return SignInAttempts.objects.create(scope=scope, key=key)
    except IntegrityError as exc:
        if not _is_the_counter_colliding(exc):
            raise
        # Somebody else created it between the select and the insert. Theirs is
        # as good as ours; take the lock on it.
        return SignInAttempts.objects.select_for_update().get(scope=scope, key=key)


def record_failure(scope, value: str) -> None:
    """Count one wrong answer against `value`, starting a window if none is open.

    Locked, because two wrong answers arriving together would otherwise both
    read the same count and both write count+1 — losing one, which is the whole
    of the limit if it happens at the boundary.
    """
    with transaction.atomic():
        row = _locked_counter(scope, key_for(scope, value))
        now = timezone.now()
        if now - row.window_started_at >= _window():
            row.window_started_at = now
            row.failures = 0
        row.failures += 1
        row.save(update_fields=["failures", "window_started_at"])


def clear(scope, value: str) -> None:
    """Forget the failures counted against `value`.

    Called for the identifier on a successful sign-in, so that a teacher who
    mistypes twice in the morning is not one mistake from a wait in the
    afternoon.

    Deliberately **not** called for the address. Clearing that on success would
    hand an attacker a reset button: sign in to an account they own, and the
    count of everything they just tried against everybody else's goes to zero.
    """
    SignInAttempts.objects.filter(scope=scope, key=key_for(scope, value)).delete()
