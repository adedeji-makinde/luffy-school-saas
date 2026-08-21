import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-for-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

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
    "django_tenants.middleware.main.TenantMainMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Enforces that whoever is signed in actually belongs to the school whose
    # domain they are on. Must come after AuthenticationMiddleware.
    "accounts.middleware.SchoolAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

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

ROOT_URLCONF = "urls"

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
