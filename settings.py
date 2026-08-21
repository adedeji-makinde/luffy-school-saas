import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# The three settings a deploy is most likely to forget
#
# All three used to have a *convenient* default, which meant a deployment that
# set none of them started successfully and was wrong in three ways at once.
# They now fail the way the email backend does (see DEFAULT_EMAIL_BACKEND
# below): closed, loudly, and with development opting in explicitly rather than
# production opting out by accident.
# ---------------------------------------------------------------------------

# **Off unless something says otherwise.** This used to default to on, so a
# deploy that never set it served tracebacks — settings, environment and all —
# to anybody who could provoke one. Development turns it on in
# docker-compose.yml, beside EMAIL_BACKEND, for the same reason and in the same
# place.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

# **No usable default outside development.** A `SECRET_KEY` is what signs
# session cookies and CSRF tokens, so a known one is not a weaker key — it is no
# key at all: anybody holding this repository could mint a session for any
# account on the platform. That was survivable while sessions only ever came
# from `force_login()` in tests. It stopped being survivable the moment
# `/api/login/` could mint one from a password.
_DEVELOPMENT_SECRET_KEY = "dev-only-not-for-production"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or (
    _DEVELOPMENT_SECRET_KEY if DEBUG else ""
)
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is not set. Refusing to start with a known key: it "
        "signs every session cookie and CSRF token on the platform. Set it, or "
        "set DJANGO_DEBUG=1 for local development."
    )

# **The `Domain` table is the real allowlist**, which is why this could be `*`
# for as long as it was. `TenantMainMiddleware` resolves every request's host
# against `schools.Domain` and raises `Http404` for one it does not recognise,
# before any view runs — so an invented `Host` header is already refused, by
# data rather than by a list somebody has to remember to update.
#
# Kept overridable anyway, for defence in depth and for the paths that do not go
# through that middleware. `*` remains the default because narrowing it here
# without narrowing `Domain` buys nothing and would silently break a school the
# day it is added.
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
    if host.strip()
]

# ---------------------------------------------------------------------------
# Tenancy layout
#
# SHARED_APPS live in the `public` schema, once for the whole platform.
# TENANT_APPS are created per school schema.
#
# Identity and access control are deliberately SHARED, not per-tenant:
# a parent may have children at more than one school and must reach all of
# them from a single login, so `accounts.User` and `accounts.Membership`
# cannot be duplicated per schema. Academic and financial records — the data
# a school owns — belong in TENANT_APPS.
# ---------------------------------------------------------------------------
SHARED_APPS = [
    "django_tenants",
    "schools",
    "accounts",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
]

TENANT_APPS = [
    # Each school gets its own copy of these tables, in its own schema.
    # Attendance and report cards land here alongside these three.
    "django.contrib.contenttypes",
    "academics",
    # A school's own books. Separate from `academics` rather than a module
    # inside it because the two answer to different people and change on
    # different schedules — a bursar's ledger and a calendar of terms share only
    # the fact that both belong to one school.
    "fees",
    # What a teacher enters: subjects, assessments and marks. Separate from
    # both of the above on the same reasoning — a teacher's sheet and a
    # bursar's ledger have different readers and different release schedules,
    # and neither should have to migrate because the other changed.
    "gradebook",
]

INSTALLED_APPS = SHARED_APPS + [app for app in TENANT_APPS if app not in SHARED_APPS]

TENANT_MODEL = "schools.School"
TENANT_DOMAIN_MODEL = "schools.Domain"

MIDDLEWARE = [
    # First, as Django asks: it is the one that can end a request before the
    # rest of the stack has done any work. What it buys here is the header set
    # — nosniff, referrer policy, and HSTS once a deployment turns it on.
    "django.middleware.security.SecurityMiddleware",
    "django_tenants.middleware.main.TenantMainMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Enforces that whoever is signed in actually belongs to the school whose
    # domain they are on. Must come after AuthenticationMiddleware.
    "accounts.middleware.SchoolAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Last, so the header lands on whatever the stack finally produced. There is
    # nothing on this platform that belongs in somebody else's frame, and the
    # admin — a form that performs privileged writes on submit — is the exact
    # thing clickjacking is for.
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# ---------------------------------------------------------------------------
# Transport and header hardening
#
# Set here where the answer is the same everywhere, and deliberately left unset
# where it is a fact about a deployment's topology rather than about this
# application.
#
# `SECURE_HSTS_SECONDS` and `SECURE_SSL_REDIRECT` are the two left out. Both
# depend on where TLS is terminated and whether the load balancer already
# redirects; turning on the redirect in a process that is *behind* a terminator
# that does not set `X-Forwarded-Proto` produces an infinite redirect, and HSTS
# is close to irreversible for the length of its own max-age. `manage.py check
# --deploy` reports both as warnings, which is the right loudness for a decision
# somebody has to make with the infrastructure in front of them.
# ---------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

AUTH_USER_MODEL = "accounts.User"

# Django ships no validators by default, which meant the one path in this
# codebase that sets a password on somebody's behalf — Invitation.accept() —
# would take a single character. What it writes is a *global* credential: it
# signs the person in at every school they hold a membership at, so it is worth
# a floor. Add the rest of Django's stock validators here if the policy grows.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
]

# Staff, parents and students all sign in through the same door. They just
# reach for different identifiers, so accept any of username / email / phone.
AUTHENTICATION_BACKENDS = ["accounts.backends.IdentifierBackend"]

# ---------------------------------------------------------------------------
# Sessions
#
# Both settings below are Django defaults being overridden deliberately, so both
# say why. The case that decides them is a teacher marking a class of thirty:
# each cell saves as it loses focus, so a marking session is a long stretch of
# steady small writes rather than one form and one submit.
#
# **The window is idle time, not total time.** Django's default,
# `SESSION_SAVE_EVERY_REQUEST = False`, runs the clock from the moment of login
# and never extends it however hard the person is working. A teacher who signed
# in near the end of the window gets logged out mid-sheet, cursor still in a
# cell, having saved marks successfully seconds earlier. Sliding the expiry on
# every request is what makes "expired" mean "went away", which is the only
# meaning anybody expects.
#
# The cost is a session write per request, and with one request per blur that is
# thirty writes for one register rather than none. Accepted: the row is small and
# keyed by primary key, and the alternative is losing a teacher's work. If it
# ever shows up in the database's load, the fix is a cached session backend, not
# turning this back off.
#
# **Twelve hours, down from Django's two weeks.** A school day plus room either
# side, so a normal working day never trips it and a session left open on a
# shared staff-room computer is gone by the next morning. Two weeks of *idle*
# time on a machine several teachers use is a long time to leave a signed-in
# gradebook lying around; two weeks of idle time was never the intent, it was
# simply the default nobody had chosen.
#
# Not `SESSION_EXPIRE_AT_BROWSER_CLOSE`: half of marking is done in a browser
# that is never deliberately closed, and it would put the teacher back where this
# started — logged out at a moment they did not choose.
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_AGE = 60 * 60 * 12

# **A session belongs to the person, not to one school.** That is not a new
# decision here; it is the one this project already made and has been relying
# on. `accounts.Membership` is shared rather than per-tenant precisely so a
# parent with children at three schools has one login, and
# `SchoolAccessMiddleware` re-derives what they may do from the host on every
# request rather than from anything stored in the session. Writing the school
# into the session would put the same fact in two places, and make the copy in
# the session the stale one the moment a membership is suspended.
#
# What that costs is this setting. Sign-in happens on the portal host (see
# `/api/login/`), and a cookie with no Domain attribute is returned only to the
# exact host that set it — so without a domain spanning the portal and every
# school, a teacher would sign in successfully on the portal and arrive at their
# own school's host as a stranger. Set it to the parent of every host the
# platform answers on, with a leading dot: `.luffy.school`.
#
# Left unset it is not merely unconfigured, it is wrong in a way that only shows
# up on the second host, which is why `accounts/checks.py` refuses a production
# deploy without it rather than letting it be discovered by a teacher. Unset is
# still right for local single-host development, where it means "this host".
SESSION_COOKIE_DOMAIN = os.environ.get("SESSION_COOKIE_DOMAIN") or None

# The CSRF cookie has to travel exactly as far as the session it protects.
CSRF_COOKIE_DOMAIN = SESSION_COOKIE_DOMAIN

# A session cookie is a credential from the moment `/api/login/` can mint one,
# so it should never cross a plain-HTTP hop. Tied to DEBUG rather than given its
# own switch: a deployment with DEBUG on has a larger problem than this setting.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# ---------------------------------------------------------------------------
# Sign-in throttling
#
# The reasoning — counted rather than locked, failures rather than attempts,
# Postgres rather than the cache — is in `accounts/throttling.py`. These are the
# numbers, which are the part worth arguing about separately.
#
# Ten failures per identifier per quarter-hour: generous for somebody who has
# genuinely forgotten which of their two passwords this is, and against a
# ten-character minimum it leaves an attacker roughly a thousand guesses a day
# against a space where that is nothing.
#
# Fifty per address, because a staff room is one NAT address and a limit that a
# school trips by arriving in the morning would be turned off within a week.
# Only failures count, so ordinary arrivals never approach it.
# ---------------------------------------------------------------------------
SIGN_IN_THROTTLE_WINDOW = int(os.environ.get("SIGN_IN_THROTTLE_WINDOW", 15 * 60))
SIGN_IN_MAX_FAILURES_PER_IDENTIFIER = int(
    os.environ.get("SIGN_IN_MAX_FAILURES_PER_IDENTIFIER", 10)
)
SIGN_IN_MAX_FAILURES_PER_ADDRESS = int(
    os.environ.get("SIGN_IN_MAX_FAILURES_PER_ADDRESS", 50)
)

# How many entries at the right-hand end of `X-Forwarded-For` this deployment's
# own proxies wrote. Zero — believe nothing, use REMOTE_ADDR — is the only safe
# default: every hop trusted beyond the ones we actually run is one the caller
# gets to forge, and forging it is exactly how the per-address limit is escaped.
# See `accounts.throttling.client_address()`.
TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", 0))

# Parsing default only, not a restriction: a number typed with no country
# code is read as Nigerian, but any other country's numbers are still valid.
# See accounts/identifiers.py.
PHONE_DEFAULT_REGION = os.environ.get("PHONE_DEFAULT_REGION", "NG")

# Two urlconfs, chosen by schema rather than by anything a view has to check.
# `urls` is what a school's host serves; `urls_public` replaces it on the public
# schema — the portal — and is the only one carrying the admin. See both files.
ROOT_URLCONF = "urls"
PUBLIC_SCHEMA_URLCONF = "urls_public"

# ---------------------------------------------------------------------------
# Invitations
#
# The channel is a dotted path rather than a hard-coded class so that adding
# WhatsApp for parents later is a settings change and a new class beside
# EmailChannel, not an edit to the Invitation model. See schools/delivery.py.
# ---------------------------------------------------------------------------
INVITATION_CHANNEL = os.environ.get(
    "INVITATION_CHANNEL", "schools.delivery.EmailChannel"
)

#: Not the console backend, which is what this used to default to. An invite
#: link is a live credential, and the console backend writes the whole message
#: — accept URL, token and all — to stdout, which in a container is the
#: application log, readable by anyone who can read logs. It failed open in the
#: other direction too: nothing was delivered and nothing raised, so a
#: production deploy that never set this looked exactly like a working one.
#:
#: SMTP is Django's own default and fails closed on both counts: no silent
#: non-delivery, and no credential in the logs. Local development opts into the
#: console backend explicitly — see docker-compose.yml. (Django's test runner
#: substitutes the locmem backend regardless of what is set here.)
DEFAULT_EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", DEFAULT_EMAIL_BACKEND)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@luffy.school")

DATABASES = {
    "default": {
        "ENGINE": "django_tenants.postgresql_backend",
        "NAME": os.environ.get("POSTGRES_DB", "luffy_db"),
        "USER": os.environ.get("POSTGRES_USER", "luffy_admin"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "changeme"),
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

DATABASE_ROUTERS = ["django_tenants.routers.TenantSyncRouter"]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "Africa/Lagos")
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
