"""Which school a log line or an error report is about.

Every request on this platform is already scoped to one school — that is what
the `search_path` is — but nothing carried that scope into the log. A line
reading `IntegrityError on membership save` is the same line whether it came
from St Mary's or Grace Academy, and on a platform where the schools are the
whole point, "which one?" is the first question anybody asks and the one the log
could not answer.

Two pieces:

- `SchoolContextFilter` puts the school on every `LogRecord`, so a formatter can
  print it. Attached to every handler in `settings.LOGGING`.
- `SchoolAdminEmailHandler` puts it in the *subject* of the error mail, where it
  is visible in a list of forty of them without opening any.

Both work outside a request too. `connection.tenant` is set by
`TenantMainMiddleware` in a request and by `schema_context()` everywhere else —
a management command, a migration, an `on_commit` callback — so the answer is
correct in a Celery-less background path as well, and honest ("—") where there
genuinely is no school rather than misleadingly blank.
"""

import logging
import threading

from django.db import connection
from django.utils.log import AdminEmailHandler

#: What to print when there is no school. Not an empty string: a blank column
#: reads as "the name is missing" rather than "this did not happen at a school",
#: and platform-wide work — a migration, a startup message, a cron — is the
#: second thing and should look like it.
NO_SCHOOL = "—"

#: The public schema is the portal, not a customer. Naming it as though it were
#: a school would be worse than saying nothing.
PORTAL = "portal"


def current_school():
    """The school this thread is currently connected to, or None.

    Reads the connection rather than the request, and that is deliberate: the
    connection is what actually decides which school's tables are being touched,
    so it is right in the places a request object is not — `on_commit`
    callbacks, management commands, `schema_context()` blocks.
    """
    tenant = getattr(connection, "tenant", None)
    if tenant is None:
        return None
    # `schema_context()` sets a `FakeTenant`, which carries a schema name and no
    # display name. The real `School` has both. Ask for what is there.
    name = getattr(tenant, "name", None)
    schema = getattr(tenant, "schema_name", None)
    if schema in (None, "", "public"):
        return PORTAL if schema == "public" else None
    return name or schema


class SchoolContextFilter(logging.Filter):
    """Adds `school` to every record, so `%(school)s` is always safe.

    A `Filter` rather than an adapter or a `LoggerAdapter` at each call site,
    because the point is that it applies to log lines nobody wrote for this —
    Django's own `django.request` and `django.db.backends`, a third-party
    library's, an exception handler's. Those are exactly the lines where "which
    school?" is worth most and the ones no call site can be edited to add.

    Every record gets the attribute, including when there is no school, because
    a formatter referencing `%(school)s` raises on the record that lacks it —
    and a logging failure in the middle of reporting a real failure is how the
    real one gets lost.
    """

    def filter(self, record):
        try:
            record.school = current_school() or NO_SCHOOL
        except Exception:  # pragma: no cover - defensive
            # Deliberately broad, and the one place in this codebase where that
            # is right. This runs *while something is being logged*, often while
            # an exception is being reported; letting anything escape here would
            # replace a real error with a logging error and lose the first one.
            # There is no useful narrower set — `connection` is a thread-local
            # proxy and anything it touches could be mid-teardown.
            record.school = NO_SCHOOL
        return True


class SchoolAdminEmailHandler(AdminEmailHandler):
    """`AdminEmailHandler`, with the school in the subject line.

    The body already carries everything, but nobody reads forty bodies. The
    subject is what is visible in a mailbox list, and "Internal Server Error:
    /api/schools/st-marys/invitations/" does not say whether the problem is one
    school's data or the platform's code — while forty of them all naming the
    same school says exactly that, at a glance.

    The awkward part is that `format_subject()` is handed a finished string and
    never the record, so the school has to travel between the two methods. It
    travels in a **thread-local**, not on `self`: one handler instance is shared
    by every thread in the process, so an attribute would let two simultaneous
    errors at two different schools swap subjects — each correctly reported and
    each labelled with the other's name, which is worse than no label at all.
    """

    _local = threading.local()

    def format_subject(self, subject):
        school = getattr(self._local, "school", None) or NO_SCHOOL
        return super().format_subject(f"[{school}] {subject}")

    def emit(self, record):
        # Read from the record where the filter put it, falling back to asking
        # directly: this handler is reachable with no filter attached, and a
        # subject that silently lost the school is the failure it exists to
        # prevent.
        self._local.school = getattr(record, "school", None) or current_school()
        try:
            super().emit(record)
        finally:
            self._local.school = None
