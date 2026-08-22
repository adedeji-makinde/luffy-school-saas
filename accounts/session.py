"""What the API says when a session is no longer good, and why it matters.

A teacher marking a class of thirty is the case this exists for. Each cell saves
as it loses focus, so the session is not exercised once at the start of a form
and then forgotten — it is exercised thirty times, once per blur, over however
long it takes to enter a register. Two things follow, and neither is Django's
default.

**An active session must not expire.** Django's default is
`SESSION_SAVE_EVERY_REQUEST = False`, which means the clock runs from the moment
of *login* and no amount of work extends it. A teacher who signed in at the far
end of the window can be marking, saving successfully, and be logged out
mid-sheet with the cursor still in a cell. The fix is in `settings.py`, where the
session now slides forward on every request; this module is the other half.

**A refusal has to say which refusal it is.** `SessionAuth` answers a bare 401
whether the caller never signed in or signed in and has just been timed out. To
a program those are the same status code and completely different situations: the
second one means *"your work is still good — sign in again and send it"*, and a
client that cannot tell them apart has to treat every 401 as fatal and drop
whatever the teacher had typed.

Telling them apart is possible because the browser says so. A request carrying a
session cookie that no longer resolves to a signed-in user is one whose session
ended — expired, flushed, or logged out elsewhere. A request with no cookie at
all never had one.

Unlike most "which error is it" questions in this codebase, that one discloses
nothing, and it is worth being precise about why rather than assuming it. The
answer is a fact about the request the caller just sent, and there is no account,
token or row on the other side of it. In particular it is **not** a session-key
oracle: a forged or random cookie is unusable for exactly the same reason an
expired one is, so it gets the same `session_expired` answer. Nothing here says
whether the key was ever real — only that whatever arrived does not authenticate
anybody now. Compare the invitation routes, which must answer a bad token with a
flat 404 precisely because there *is* something on the other side to leak.

What this deliberately does **not** do is keep the teacher's unsaved marks
anywhere. The server has nowhere to put them — writing them would need the
authority that just lapsed — so replaying them is the client's job, and the
gradebook's part of the bargain is that replaying is *safe*: every write is
conditional on the version the teacher was shown, and a repeat of the caller's
own write is recognised and swallowed rather than counted twice. See
`gradebook.api._is_our_write_arriving_twice()`, and the test that holds this line
across a re-login.
"""

from ninja.security import SessionAuth

#: The session cookie was presented and is no longer good: expired, flushed, or
#: signed out somewhere else. Recoverable — sign in again and send it again.
SESSION_EXPIRED = "session_expired"

#: No session cookie at all. Nothing was lost, because nothing was signed in.
NOT_AUTHENTICATED = "not_authenticated"


class SchoolSessionAuth(SessionAuth):
    """`SessionAuth`, plus a record of *why* it said no.

    The reason is stamped on the request rather than raised, because ninja's
    auth protocol has exactly two answers — a user, or `None` — and the 401 is
    built later by the exception handler in `api.py`. Stamping is what carries
    the answer across that gap.
    """

    def authenticate(self, request, key):
        user = super().authenticate(request, key)
        if user is not None:
            return user

        # `key` is the session cookie as sent. Present-but-unusable is the
        # timed-out teacher; absent is a caller who never signed in.
        request.unauthenticated_because = (
            SESSION_EXPIRED if key else NOT_AUTHENTICATED
        )
        return None


#: The one instance every authenticated endpoint uses, replacing `django_auth`.
#: A single object rather than one per router so that a fourth router added later
#: inherits the behaviour instead of having to remember it — the same reasoning
#: `gradebook.api` gives for putting auth on the router rather than per-operation.
session_auth = SchoolSessionAuth()


def why_unauthenticated(request) -> str:
    """The stamp, or the safe default if nothing set one.

    Defaults to `NOT_AUTHENTICATED`, which is the answer that claims least: it
    says "you are not signed in" rather than asserting something about a session
    this code never saw.
    """
    return getattr(request, "unauthenticated_because", NOT_AUTHENTICATED)


__all__ = [
    "NOT_AUTHENTICATED",
    "SESSION_EXPIRED",
    "SchoolSessionAuth",
    "session_auth",
    "why_unauthenticated",
]
