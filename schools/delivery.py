"""How an invite token reaches the person it was minted for.

Deliberately not on `Invitation`. The model's job is to know whether a token is
good; getting it to a human is a different concern with a different lifetime —
this pass sends staff invitations by email, and parents will want WhatsApp,
which is a channel with its own templates, credentials, failure modes and
regulatory rules. If `Invitation.send()` existed, adding that second channel
would mean opening the model.

So a channel is anything with `send(invitation, raw_token, *, accept_url)`, and
optionally a `check_deliverable(invitation)` that answers whether this person is
reachable at all without sending anything. `invitations.py` resolves one and
calls it; nothing in `models.py` imports this module at all, and the dependency
deliberately points this way only.

Note what a channel is handed and what it is not: the **raw token**, which is
never persisted, and never the `Invitation` row's `token_hash`. A channel is the
one place the raw token legitimately exists outside the mint, which is also why
`EmailChannel` puts it in a link and keeps no copy.
"""

import logging
import smtplib

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


class NoDeliveryAddress(Exception):
    """The invitee has no address the selected channel can reach."""


class DeliveryNotConfigured(Exception):
    """This deploy cannot send invitations at all.

    About the *installation*, not about the invitee — which is the whole reason
    it is not a `NoDeliveryAddress`. Nobody can be invited until somebody fixes
    a setting, so the answer to the admin who tried is "this platform is not
    finished being set up", not "that person is unreachable".

    Deliberately not an `InvitationError` either. That hierarchy means "the flow
    refuses this request on state grounds", and `api.py` maps it to 4xx: the
    caller did something that cannot be done. This is the opposite — the request
    was fine and the server is not.
    """


class DeliveryFailed(Exception):
    """The channel was configured and reachable-looking, and the send failed.

    Genuine infrastructure trouble: the mail host is down, refuses the
    credentials, or times out. Raised from `send()`, which `invitations.py`
    dispatches through `transaction.on_commit` — so by the time this is raised
    the invitation, its membership and any placeholder account are committed and
    cannot be undone.

    That is the right outcome rather than a regrettable one: the invitation
    exists and can be resent once mail is healthy, and re-running the invite is
    idempotent (`_issue()` revokes the stale token and mints a fresh one). The
    thing worth being careful about is only that the admin is *told*, which is
    why this is a type of its own rather than an `SMTPException` escaping as a
    generic 500.
    """


class Channel:
    """The seam. Implement `send()` and register the path in settings.

    Not an ABC on purpose — a test double should be able to be a plain object
    with a `send` method, without inheriting anything.

    `check_configured()` and `check_deliverable()` are the optional rest of the
    contract, and they are optional precisely so that "a plain object with a
    `send`" stays true. Both are asked by `invitations.py` while the transaction
    is still open, so that raising rolls the whole invitation back; `send()`
    itself runs after commit, far too late to undo anything.

    They ask two different questions, and the split matters because the answers
    have different audiences:

    - `check_configured()` — "can this channel send *anything*, in this deploy?"
      A missing SMTP host is nobody's fault but the operator's.
    - `check_deliverable()` — "could this channel reach *this person*?" A staff
      member with no email address is something the admin can fix by typing one.
    """

    def check_configured(self):
        """Raise `DeliveryNotConfigured` if this deploy cannot send at all."""
        return None

    def check_deliverable(self, invitation):
        """Raise `NoDeliveryAddress` if this channel has no way to reach them."""
        return None

    def send(self, invitation, raw_token, *, accept_url):  # pragma: no cover
        raise NotImplementedError


class EmailChannel(Channel):
    """Staff invitations, by email.

    Email only, and only for staff, per the messaging policy for this pass. A
    student or parent channel is not simply this class with a different address:
    it needs its own consent handling, so it belongs beside this one rather than
    inside it.
    """

    subject_template = "You have been invited to join {school}"

    #: Backends that talk to a mail server over the network and therefore need
    #: `EMAIL_HOST` to point somewhere. The console, locmem and filebased
    #: backends do not, and neither does anything a deploy substitutes for local
    #: development — so the check below applies to this list rather than to
    #: "anything that is not locmem", which would fail the wrong deploys.
    NETWORK_BACKENDS = ("django.core.mail.backends.smtp.EmailBackend",)

    def check_configured(self):
        """Refuse to mint an invitation this deploy has nowhere to send.

        `settings.py` defaults `EMAIL_BACKEND` to SMTP deliberately, so that a
        deploy which configures nothing fails closed rather than silently
        printing live tokens to the application log. But Django's *own* SMTP
        defaults are `localhost:25` with no credentials, which is not a mail
        server on any host this runs on — so failing closed took the shape of a
        `ConnectionRefusedError` raised from inside an `on_commit` callback,
        after the invitation, the membership and the placeholder account had all
        committed, and it reached the admin as an unexplained 500.

        Asked here, while the transaction is still open, the same
        misconfiguration refuses the invitation and leaves nothing behind.
        """
        if settings.EMAIL_BACKEND not in self.NETWORK_BACKENDS:
            return None
        # Note what this depends on: Django's *own* `EMAIL_HOST` default is the
        # truthy string `"localhost"`, which would sail through here. It reads
        # as empty only because `settings.py` deliberately defaults it to `""`
        # when the environment does not set it. That coupling is load-bearing
        # and easy to delete by accident, so a test pins it — see
        # `MailConfigurationTests.test_the_email_host_default_is_empty_not_localhost`.
        if not getattr(settings, "EMAIL_HOST", ""):
            raise DeliveryNotConfigured(
                "Invitations are sent by email, and no EMAIL_HOST is configured "
                "for this deploy — so there is nowhere to send them. Set "
                "EMAIL_HOST (and its credentials), or select a different "
                "EMAIL_BACKEND."
            )
        return None

    def check_deliverable(self, invitation):
        recipient = invitation.user.email
        if not recipient:
            raise NoDeliveryAddress(
                f"{invitation.user} has no email address, and email is the only "
                "channel enabled for staff invitations in this pass."
            )
        return recipient

    def send(self, invitation, raw_token, *, accept_url):
        # Asked again rather than trusted: `send()` is reachable on its own, and
        # the pre-commit check happened in a different transaction state.
        self.check_configured()
        recipient = self.check_deliverable(invitation)
        try:
            send_mail(
                subject=self.subject_template.format(school=invitation.school.name),
                message=self._body(invitation, accept_url),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[recipient],
                fail_silently=False,
            )
        except (OSError, smtplib.SMTPException) as exc:
            # Narrow on purpose. `OSError` is what a refused connection, a DNS
            # failure and a socket timeout all arrive as, and `SMTPException` is
            # a subclass of it — named anyway, because the protocol half of the
            # contract is worth stating rather than leaving a reader to know
            # that. Catching `Exception` would fold a `TypeError` in `_body()`
            # into "the mail server is down": a bug report nobody would ever
            # receive, and an admin retrying an outage that does not exist.
            #
            # `fail_silently=False` above is the other half: with it True this
            # would swallow the same failures and report success, which is the
            # silent non-delivery the SMTP default in settings.py exists to
            # prevent.
            #
            # The cause goes to the log and not into the message. This message
            # reaches a school admin over HTTP, and `SMTPAuthenticationError`
            # in particular carries the mail server's own response — a hostname
            # and a credential complaint are an operator's business, not a
            # customer's. `from exc` keeps the traceback whole for whoever reads
            # the log.
            logger.exception(
                "Invitation %s to %s could not be delivered by email",
                invitation.pk,
                invitation.school.name,
            )
            raise DeliveryFailed(
                f"The invitation to {invitation.school.name} was created, but "
                f"could not be emailed. Resend it once mail is working."
            ) from exc
        return recipient

    def _body(self, invitation, accept_url):
        # localtime(), not the stored value: `expires_at` is UTC and the reader
        # is in TIME_ZONE. An expiry at 23:30 UTC is already the next day in
        # Lagos, so formatting the raw value advertised the wrong deadline for
        # every invitation whose link died in the last hour of the UTC day.
        expires_at = timezone.localtime(invitation.expires_at)
        return (
            f"{invitation.invited_by.get_full_name()} has invited you to join "
            f"{invitation.school.name} as {invitation.membership.get_role_display()}.\n\n"
            f"Accept the invitation:\n{accept_url}\n\n"
            f"The link stops working on {expires_at.strftime('%d %B %Y')}."
        )


def get_channel():
    """The configured channel.

    A dotted path in settings rather than a hard-coded class, so tests can
    substitute a recorder and a future WhatsApp channel is a settings change
    rather than an edit here.
    """
    path = getattr(
        settings, "INVITATION_CHANNEL", "schools.delivery.EmailChannel"
    )
    return import_string(path)()
