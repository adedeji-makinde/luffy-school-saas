"""The admin's sign-in form, held to the same policy as the API's.

The admin was a second front door, and it was the unguarded one. `/api/login/`
counts failures, refuses on the portal host only, and answers every failure
identically; `/admin/login/` did none of that, because Django's admin
authenticates through `AuthenticationForm` and never passes anywhere near
`accounts/signin.py`. Forty wrong passwords at that URL recorded nothing.

Worse than a gap in coverage, it was a gap over the *most* privileged accounts
on the platform: the admin only lets `is_platform_staff` through, so the door
without a counter on it was the one guarding the operator's own logins.

What this does not do is duplicate the policy. The throttle's limits, its keys,
what a success forgives and what it does not, all still live in
`accounts/signin.py` — this form calls the same three functions the endpoint
does. A second copy of the numbers would drift, and the drift would be silent.

Two things deliberately stay different from the API's answer, because the admin
is a rendered form rather than a JSON API:

* The refusal is a form error rather than a 429 with `Retry-After`. Nothing here
  is a programmatic client.
* Django's own "please enter the correct username and password for a staff
  account" is left as it is. It is already one message for every failure, which
  is the property that matters, and rewording it would be a change to Django's
  admin for no gain.
"""

from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError

from . import signin, throttling


class ThrottledAdminAuthenticationForm(AdminAuthenticationForm):
    """`AdminAuthenticationForm`, plus the counter the API has always had."""

    def _address(self) -> str:
        """The address to count against.

        `AuthenticationForm` is documented as taking a request and the admin
        always passes one, but it is an optional argument and this is a security
        control — so an absent request falls into the shared empty-address
        bucket rather than skipping the count.
        """
        return throttling.client_address(self.request) if self.request else ""

    def clean(self):
        """Ask the throttle first, then let Django do the authenticating.

        Ordered exactly as `signin.sign_in()` is, and for the same reason: a
        closed window has to be closed to everybody, including whoever's next
        guess happens to be right.
        """
        identifier = self.cleaned_data.get("username") or self.data.get("username") or ""
        address = self._address()

        wait = signin.wait_before_retrying(identifier, address)
        if wait is not None:
            raise ValidationError(signin.THROTTLED, code=signin.TOO_MANY_ATTEMPTS)

        try:
            cleaned = super().clean()
        except ValidationError:
            # Every way the admin says no lands here: no such account, wrong
            # password, and — via `confirm_login_allowed()` — a correct password
            # for somebody who is not platform staff. All three are a failed
            # attempt at this door and all three are counted.
            signin.note_failure(identifier, address)
            raise

        # Not "no exception was raised": a submission missing a field never
        # reaches the authentication step at all, and forgiving the count on
        # that would hand anybody a free reset with an empty form.
        if self.get_user() is not None:
            signin.note_success(identifier)
        return cleaned
