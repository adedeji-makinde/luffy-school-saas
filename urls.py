"""What a **school's own host** serves.

The API, and nothing else. In particular not the admin: see `urls_public.py`,
which is what the portal serves and where the admin now lives.

`django_tenants` picks between the two by schema. `ROOT_URLCONF` (this file) is
what a tenant host gets; `PUBLIC_SCHEMA_URLCONF` replaces it on the public
schema, which is the portal. So "the admin is on the portal only" is enforced by
routing rather than by a check inside a view somebody could forget to add.
"""

from django.urls import path

from api import api

urlpatterns = [
    path("api/", api.urls),
]
