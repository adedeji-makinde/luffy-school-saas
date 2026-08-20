# Open items: staff invitation flow

The findings from the review of this branch now live in
**[issue #6](https://github.com/adedejimakinde/luffy-school-saas/issues/6)**, which is the
one live copy. This file used to hold them in full; it was cut down once the issue existed,
because a findings list tracked beside the code goes stale the moment somebody fixes an
item without updating both.

Three were fixed on the `staff-invitation-flow` branch, and their commit messages carry the
reproduction detail:

| | | |
| --- | --- | --- |
| `034b6b3` | Invite path locked the School row it never writes | `Meta.ordering` joined, and a joined `FOR UPDATE` locks every joined table |
| `05330ad` | A deactivated account could be invited and re-activated | now refused at all four points that are reachable alone |
| `0f75dfb` | `accept()` decided on state it had not locked | membership, invitation and user are re-read under lock first |

Two more were **decisions, not defects** — where the invitation accept page lives, and how
SMTP is configured for a real deploy. Both are now settled, on the
`invitation-delivery-config` branch, and the choice made is written down rather than left
to be re-derived from the code:

| | | |
| --- | --- | --- |
| finding 6 | The accept link followed whichever host the issuing admin was on | one `INVITATION_ACCEPT_URL` template, with no default, refused pre-commit when unset. The page stays a **frontend** route; Django serving it was considered and rejected — see the PR |
| finding 7 | SMTP defaulted to `localhost:25`, and failures escaped as 500s | mail settings read from the environment; no host is a pre-commit **503** with nothing committed, an outage is a post-commit **502** with a resendable invitation |

The distinction those two rows draw is the reusable part: a *misconfiguration* can be
refused while the transaction is still open, so it costs nothing; an *outage* cannot, so
the row survives and the admin is told which of the two happened. Everything else on the
list is untouched.

> Not the same list as the [Open items](membership.md#open-items) section in
> `membership.md`. That one holds design work deliberately deferred — the transfer
> handshake, invitations for parents and students. This one held defects.
>
> And note the collision: issue **#6** is the issue as a whole, while the finding numbered
> **6** inside it is the accept-page decision. Different things.
