from django.contrib.auth.backends import ModelBackend

from .models import User


class IdentifierBackend(ModelBackend):
    """One sign-in path for every role, three ways to name yourself.

    Staff type their email, parents often reach for the phone number the school
    has on file, and students use a school-issued handle and may have neither
    email nor phone. All three resolve to the same User row via
    User.matching_identifier(), the same method the model uses to enforce that
    an identifier can't be ambiguous in the first place.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = username or kwargs.get("email") or kwargs.get("phone")
        if identifier is None or password is None:
            return None

        matches = list(User.objects.matching_identifier(identifier.strip()))
        if len(matches) > 1:
            # Genuine ambiguity: refuse rather than guess which person was
            # meant. assert_identifiers_unambiguous() should make this
            # unreachable in practice, but if it ever happens, failing closed
            # is the only safe move — silently picking one is how a parent
            # ends up signed into a stranger's account.
            User().set_password(password)
            return None

        user = matches[0] if matches else None
        if user is None:
            # Same work as a real check, so a missing account is not faster.
            User().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
