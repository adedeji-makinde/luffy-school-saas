from django.core.exceptions import PermissionDenied
from django.db import connection
from django_tenants.utils import get_public_schema_name


class SchoolAccessMiddleware:
    """Keeps a signed-in person to the schools they may actually act at.

    On a school's own host, an authenticated user needs an **active** Membership
    there. Invited and suspended people are refused: their relationship exists,
    but it does not grant access. That rule is not spelled out below — it lives
    one step away in `User.roles_at()`, which is scoped to ACCESS_STATUSES.
    Deliberately not `.live()`, which is the wider set including invited and
    suspended.

    On the public portal host none of this applies: that is where a parent sees
    children from several schools at once, and where a login with no membership
    anywhere still has to be able to sign in.

    Sets `request.school` (None on the portal) and `request.school_roles`.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(connection, "tenant", None)
        on_portal = tenant is None or tenant.schema_name == get_public_schema_name()

        request.school = None if on_portal else tenant
        request.school_roles = frozenset()

        user = getattr(request, "user", None)
        if not on_portal and user is not None and user.is_authenticated:
            request.school_roles = frozenset(user.roles_at(tenant))
            if not request.school_roles and not user.is_platform_staff:
                raise PermissionDenied("You do not have access to this school.")

        return self.get_response(request)
