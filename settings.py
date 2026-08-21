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
    # Attendance and report cards land here alongside these two.
    "django.contrib.contenttypes",
    "academics",
    # A school's own books. Separate from `academics` rather than a module
    # inside it because the two answer to different people and change on
    # different schedules — a bursar's ledger and a calendar of terms share only
    # the fact that both belong to one school.
    "fees",
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

#: Where the accept page lives, as a template containing `{token}`.
#:
#: This used to be built with `request.build_absolute_uri()` at the two API call
#: sites, which made the origin of a live credential a property of *whichever
#: host the issuing admin happened to be signed in on*. `TenantMainMiddleware`
#: resolves the portal host and a school's own host differently, so the same
#: flow emitted `http://testserver/invitations/...` or
#: `http://stmarys.luffy.school/invitations/...` depending on where the admin was
#: standing — for a page that is meant to live on a frontend which may be on
#: neither of them, and which no urlconf in this project serves.
#:
#: There is deliberately **no default**. Every candidate default is wrong
#: somewhere: a hard-coded origin is wrong for every deploy that is not ours, and
#: falling back to the request host is the bug this setting exists to remove. So
#: an unset value is a misconfiguration and is refused — see
#: `invitations.configured_accept_url()`, which raises *before* the transaction
#: commits, so a deploy that never sets this creates no orphaned placeholder
#: accounts while failing.
INVITATION_ACCEPT_URL = os.environ.get("INVITATION_ACCEPT_URL")

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

#: Where that SMTP backend connects. Django's own defaults are `localhost:25`
#: with no credentials, which is not a mail server anywhere this runs — so the
#: deploy that sets `EMAIL_BACKEND` nowhere (the intended path, since SMTP is the
#: default above) got `ConnectionRefusedError` on every single invitation.
#:
#: Which host and which credentials is a deployment decision and stays one:
#: these are read from the environment and have no in-repo values. What is *not*
#: left to the deploy is what happens when they are missing — `EmailChannel`
#: refuses to accept an invitation it has nowhere to send, before the
#: transaction commits, rather than raising from inside an `on_commit` callback
#: where nothing can be undone. See `delivery.EmailChannel.check_configured()`.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "0") == "1"
#: Bounded on purpose. `send()` runs in the request/response cycle via
#: `on_commit`, so an unreachable mail host with no timeout holds the worker for
#: as long as the OS lets the connection hang.
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "10"))

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
