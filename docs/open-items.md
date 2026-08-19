# Open items: staff invitation flow

The findings from the review of this branch now live in
**[issue #6](https://github.com/adedejimakinde/luffy-school-saas/issues/6)**, which is the
one live copy. This file used to hold them in full; it was cut down once the issue existed,
because a findings list tracked beside the code goes stale the moment somebody fixes an
item without updating both.

Three were fixed on this branch, and their commit messages carry the reproduction detail:

| | | |
| --- | --- | --- |
| `034b6b3` | Invite path locked the School row it never writes | `Meta.ordering` joined, and a joined `FOR UPDATE` locks every joined table |
| `05330ad` | A deactivated account could be invited and re-activated | now refused at all four points that are reachable alone |
| `0f75dfb` | `accept()` decided on state it had not locked | membership, invitation and user are re-read under lock first |

Two of the remaining items are **decisions, not defects**, and are called out as such in the
issue: where the invitation accept page lives (the link the mail carries resolves nowhere
today, deliberately — it is meant to be a frontend route that does not exist yet), and how
SMTP is configured for a real deploy. Neither should be guessed at in code.

> Not the same list as the [Open items](membership.md#open-items) section in
> `membership.md`. That one holds design work deliberately deferred — the transfer
> handshake, invitations for parents and students. This one held defects.
>
> And note the collision: issue **#6** is the issue as a whole, while the finding numbered
> **6** inside it is the accept-page decision. Different things.
