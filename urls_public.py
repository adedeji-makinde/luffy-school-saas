"""What the **portal host** serves: everything a school's host does, plus the admin.

The admin is here and only here, for the same reason `/api/login/` is: it is a
door, and a door on thirty hostnames is thirty doors to watch. It was previously
served from every school's host as well, which was worse than untidy — the admin
edits *shared* tables (users, memberships, schools, invitations), so serving it
from a tenant host meant privileged writes to platform-wide data issued from a
connection whose `search_path` had been set to one school's schema.

The API is reused from `urls.py` rather than repeated, so a route added there
cannot go missing here.
"""

from django.contrib import admin
from django.urls import path

from urls import urlpatterns as tenant_urlpatterns

urlpatterns = [
    path("admin/", admin.site.urls),
    *tenant_urlpatterns,
]
