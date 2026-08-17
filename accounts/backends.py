from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

from .models import User


class IdentifierBackend(ModelBackend):
    """One sign-in path for every role, three ways to name yourself.

    Staff type their email, parents often reach for the phone number the school
    has on file, and students use a school-issued handle and may have neither
    email nor phone. All three resolve to the same User row.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = username or kwargs.get("email") or kwargs.get("phone")
        if identifier is None or password is None:
            return None

        identifier = identifier.strip()
        user = (
            User.objects.filter(
                Q(username__iexact=identifier)
                | Q(email__iexact=identifier)
                | Q(phone=identifier)
            )
            .order_by("pk")
            .first()
        )
        if user is None:
            # Same work as a real check, so a missing account is not faster.
            User().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
