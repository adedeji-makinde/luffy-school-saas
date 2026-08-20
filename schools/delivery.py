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

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.module_loading import import_string


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


class Channel:
    """The seam. Implement `send()` and register the path in settings.

    Not an ABC on purpose — a test double should be able to be a plain object
    with a `send` method, without inheriting anything.

    `check_deliverable()` is the optional second half of the contract, and it is
    optional precisely so that "a plain object with a `send`" stays true. It
    answers "could this channel reach this person?" *without* sending, which
    lets `invitations.py` ask the question while the transaction is still open
    and roll the whole invitation back if the answer is no. `send()` itself runs
    after commit, far too late to undo anything.
    """

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
        recipient = self.check_deliverable(invitation)
        send_mail(
            subject=self.subject_template.format(school=invitation.school.name),
            message=self._body(invitation, accept_url),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[recipient],
            fail_silently=False,
        )
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
