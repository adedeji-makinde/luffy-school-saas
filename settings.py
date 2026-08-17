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
    # Nothing school-owned exists yet. Academics, attendance, fees and
    # report cards land here — each school gets its own tables.
    "django.contrib.contenttypes",
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

# Staff, parents and students all sign in through the same door. They just
# reach for different identifiers, so accept any of username / email / phone.
AUTHENTICATION_BACKENDS = ["accounts.backends.IdentifierBackend"]

ROOT_URLCONF = "urls"

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
