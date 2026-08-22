"""Deployment checks for things that are only wrong on the second host.

Registered with `deploy=True`, so these run under `manage.py check --deploy`
and stay out of the way of ordinary development and the test suite — which is
the right trade for a rule about production hostnames.
"""

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def session_cookie_spans_every_host(app_configs, **kwargs):
    """`SESSION_COOKIE_DOMAIN` must be set once there is more than one host.

    Sign-in happens on the portal host and the work happens on a school's host.
    A cookie set without a Domain attribute goes back only to the host that set
    it, so leaving this unset produces the most expensive kind of bug: sign-in
    succeeds, returns 200, sets a cookie, and the very next request — to the
    school the person was signing in to reach — arrives unauthenticated. Every
    part of that looks like it worked.

    Refused at deploy rather than caught at runtime because the platform cannot
    tell the two situations apart from inside a single request: "no cookie yet"
    and "cookie that will never be sent here" are the same absence.
    """
    if settings.SESSION_COOKIE_DOMAIN:
        return []
    return [
        Error(
            "SESSION_COOKIE_DOMAIN is not set, so a session opened on the "
            "portal host will not be sent to any school's host.",
            hint=(
                "Set it to the parent domain of every host this platform "
                "answers on, with a leading dot — for example '.luffy.school'. "
                "A deployment that genuinely serves one host only may set it to "
                "that host."
            ),
            id="accounts.E001",
        )
    ]
