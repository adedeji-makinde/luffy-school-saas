# Open items: staff invitation flow

**Not the same list as [membership.md](membership.md#open-items).** That section holds *design*
deferrals — work consciously scoped out, like the transfer handshake and the parent/student
invitation flow. This file holds *defects*: things the code gets wrong, found by review. An entry
here is a bug to fix or a decision to make, not a feature not yet built.

Findings from a review of `main..staff-invitation-flow` at commit `705dc78` (7 commits, 2,687 lines
across 12 files). All 103 tests passed at the time; these were things the tests did not cover.

**✅ verified** items were reproduced with throwaway probe tests (since deleted; suite still green,
tree clean). Items #2, #6 and #10 were re-verified in a second pass with real two-connection
concurrency and the real delivery path — see the evidence blocks inline.

**Status: #1, #2 and #10 are fixed** — 14 regression tests added, all of which fail against the
code as it was and pass against the fix; suite is 226 green. One commit per finding, each green on
its own: `034b6b3` (#10, 214 tests), `05330ad` (#1, 223), `0f75dfb` (#2, 226). Their sections below
are kept for the record with a note on what changed. **#6 and #7 are deliberately left open**: both turn on a
deployment decision (where the accept page lives, and how mail is configured) that should not be
guessed at. Everything else is untouched.

---

## Correctness

### 1. A deactivated user can be invited and re-activated as staff — ✅ verified · **FIXED**

> **Fixed.** New `InviteeDeactivated(InvitationError)` in `schools/models.py`, refused at the four
> points each reachable on its own: `resolve_invitee()` (inviting), `_issue()` (minting, which is
> what covers a resend), `validate_token()` (lookup — falls into the same flat 404 as every other
> dead token, so the holder learns nothing about the account), and `accept()` (redeeming directly).
> The API maps it to 409 beside `AlreadyAMember`. 8 tests in `DeactivatedInviteeTests` and
> `InvitationApiTests`, plus a concurrency test for deactivation racing an in-flight accept.


`schools/invitations.py:151`

Neither `resolve_invitee()`, `invite_staff()`, nor `accept()` checks `User.is_active`.

`accounts.deletion.deactivate_user()` is the documented way to remove someone's access. If an
admin then invites that email, `matching_identifier()` returns the deactivated row, an `INVITED`
membership is created, mail goes out, and `accept()` flips the membership to `ACTIVE` — observed
state: `membership='active', is_active=False`. The roster and `active_staff()` now show a working
teacher who can never sign in. Worse: if the deactivated account had an unusable password,
`accept()` writes a new **platform-wide** credential onto an account the platform deliberately
disabled.

### 2. `accept()` validates stale reads under the User lock — ✅ verified, both halves · **FIXED**

> **Fixed.** `accept()` now locks and re-reads membership, invitation and user with
> `select_for_update` *before* any guard runs, and syncs the caller's instance from what the lock
> read. Rows are taken in the order membership → invitation → user, matching the order
> `invite_staff()` takes the first two — taking one pair in opposite orders would be a deadlock.
> 3 threaded regression tests in `test_invitation_concurrency.py`, plus an assertion that
> `accept()` takes exactly three row locks and none of them joins.


`schools/models.py:312`

`select_for_update()` is taken on `User`, but every guard reads off `Invitation`/`Membership`
objects loaded in `_validated()` *before* the lock. Reproduced with two real DB connections,
interleaved at the exact window between the endpoint's two lines
(`invitation = _validated(token)` / `invitation.accept(...)`).

**2a — an ended membership is resurrected.** Admin calls `membership.end()` mid-accept:

    admin committed end():   membership -> ended
    accept() raised:         nothing
    membership status now:   active
    membership ended_on:     2026-08-19
    invitation status now:   accepted

Note the last two lines together: the row ends up **`active` while still carrying `ended_on`**.
`end()` writes `status` + `ended_on`; `accept()` writes only `status` via `update_fields`. That is
a state no code path checks for — `is_live` and `grants_access` both read `status` alone, so the
membership reads as fully live with an end date attached.

**2b — one token accepted twice.** Two clicks, both loaded before either committed:

    click-1: ACCEPTED ok
    click-2: ACCEPTED ok
    accepts that succeeded:  2 of 2

Both passed the `status != PENDING` check at line 277 *before* either reached the lock, so the
`User` lock serialised them without refusing either. The docstring at line 310 — "the loser finds
the row no longer PENDING and is refused above" — is false: the loser checked before it locked.

Fix: re-read invitation and membership under the lock (or lock the `Membership`/`Invitation` row
rather than the `User`), and make the status writes conditional (`filter(status=...).update(...)`)
so a lost update is impossible rather than unlikely.

### 3. No validation on `email` / `full_name` → 500s and garbage rows — ✅ verified

`api.py:45`

`InviteIn` declares both as bare `str`; nothing downstream validates them.

- `email` of 160 chars → `resolve_invitee` sets `username=email` (varchar(150)) →
  `django.db.utils.DataError`, uncaught in `create_invitation` → **500**.
- `full_name` of 400 chars → same, on varchar(255).
- `email='not an email at all'` **succeeds**: `User.save()` never runs `full_clean`,
  `normalize_email` only lowercases, and the row persists with that string as both email and
  username. `EmailChannel.check_deliverable` only tests truthiness, so it passes the pre-commit
  check and the SMTP failure lands *post*-commit — bypassing the orphan prevention `_deliver()`
  exists to provide.

### 4. Invalid phone numbers are silently discarded — ✅ verified

`schools/invitations.py:81`

`resolve_invitee()` uses `try_normalize_phone()`, which swallows an invalid number and returns
`None`.

- `invite_staff(email='ok@example.com', phone='12')` stores `user.phone=None` with no error — the
  admin believes they recorded a number that does not exist.
- `invite(email=None, phone='0803123456')` (one digit short) leaves both identifiers `None` and
  raises the misleading *"An email address or a phone number is required."* → 400, even though a
  phone number was supplied.

Everywhere else (`User.normalize_identifiers` → `normalize_phone`) an invalid number raises
`ValidationError`. This is the one path that drops it silently.

### 5. `_deliver()` silently does nothing when `accept_url_for` is omitted — ✅ verified

`schools/invitations.py:284`

It returns early when `accept_url_for is None` — minting a live token, skipping
`check_deliverable()` entirely, and delivering nothing, with a successful return value.

`invite_staff(actor, school, role, email=None, phone='08031234567')` with no `accept_url_for`
creates a User with `email=None`, an `INVITED` membership and a `PENDING` Invitation, while
`RecordingChannel.sent` stays `[]`. The EmailChannel's "this person has no address" guard is never
asked. Any management command, data import, or future caller that forgets one kwarg produces
orphaned placeholder accounts and dead tokens. `resend_invitation()` has the same default and the
same hole.

### 6. Invitation links have no configured origin, and follow whichever host the admin used — ✅ verified · **DECISION NEEDED**

> **Left open deliberately.** The fix depends on where the accept page is meant to live — a
> separate frontend, or a Django-served route — and that is an architecture decision, not a
> defect to patch. Both branches are sketched at the end of this section.


`api.py:142`, `api.py:187`

**Today every invitation email contains a dead link.** Captured from the real `EmailChannel`
through the real middleware stack (locmem outbox, not a test double):

    Accept the invitation:
    http://testserver/invitations/D_qaW9EMZW2dHGq5J-YdCmReQzl-U4fxjhXJYoLXr04/

    GET /invitations/<token>/       -> 404
    GET /api/invitations/<token>/   -> 200

But the missing route is *intentional* — `api.py:139-141` says so: "The link the invitee clicks.
A frontend route, not this API." So "no urlconf serves this path" is not itself the defect, and
adding a Django route is not automatically the fix. There is no frontend anywhere in this repo and
no `FRONTEND_ORIGIN`/`INVITATION_ACCEPT_URL` setting, so the intended frontend origin is nowhere
expressed. Two concrete defects follow:

**6a — the origin is whatever host the issuing admin was on.** Verified with two POSTs from the
same admin, differing only in `Host`:

    admin on testserver            -> http://testserver/invitations/GTpS4EW...
    admin on stmarys.luffy.school  -> http://stmarys.luffy.school/invitations/HWmNPK6...

`TenantMainMiddleware` resolves those two hosts differently, so the same flow emits portal-host and
tenant-host links depending on where the admin happened to be standing — for a route that is meant
to live on a frontend which may be on neither.

**6b — nothing pins the delivered path.** The service tests use `https://portal/i/{token}/`
(`test_invitations.py:88`); `api.py` emits `/invitations/{token}/`. Two different shapes, and no
test asserts either resolves. `test_the_raw_token_is_handed_to_the_channel` only checks that the
token appears somewhere in the string.

Fix: a single `INVITATION_ACCEPT_URL` (or frontend-origin) setting that both call sites build from,
so the link is deterministic regardless of the issuing host — *or*, if Django is meant to serve the
accept page after all, add the route and say so. Either way, a test that asserts the delivered link
actually resolves.

### 7. SMTP default has nowhere to connect, and failures escape as 500s — **DECISION NEEDED**

> **Left open deliberately.** Which SMTP host/credentials the platform uses, and whether a
> post-commit delivery failure should surface as a 500 or be swallowed and retried, are both
> deployment calls. The code change is small once those are settled.


`settings.py:107`

The new SMTP default ships with no `EMAIL_HOST`/`EMAIL_PORT`/credential settings. A deploy that
sets `EMAIL_BACKEND` nowhere (the intended path) gets Django's default `localhost:25`, so every
invite raises `ConnectionRefusedError`.

Because `invite_staff()` is the outermost atomic block, its `on_commit` callbacks run inside
`create_invitation`'s try/except — which handles only `NotPermitted`, `InvitationError` and
`NoDeliveryAddress`. An `OSError`/`SMTPException` propagates to a 500 with the invitation,
membership and placeholder user **already committed**: exactly the "one more orphan per retry"
outcome `_deliver()`'s docstring says it prevents.

### 8. Password floor is one validator on the path that writes a global credential

`settings.py:65`

`AUTH_PASSWORD_VALIDATORS` contains `MinimumLengthValidator` alone. `{"password": "1234567890"}`
or `"password12"` both pass `validate_password()` at `/api/invitations/{token}/accept/` and write
a credential that signs the user in at **every** school they hold a membership at.

Django's stock list also ships `CommonPasswordValidator`, `NumericPasswordValidator` and
`UserAttributeSimilarityValidator`. The comment's own reasoning ("what it writes is a *global*
credential, so it is worth a floor") argues for at least the first two.

---

## Correctness (lower severity)

### 9. `pending_invitations()` reports expired invitations as live — and has no callers

`schools/invitations.py:302`

It filters only on `status=PENDING`, but expiry is settled lazily: `validate_token()` is the only
thing that flips `PENDING`→`EXPIRED`. An invitation nobody clicks stays `PENDING` forever, so a
function documented as "Every live invite at one school" lists dead links as outstanding.

Related: `_issue()`'s `stale` query sweeps those same rows to `REVOKED` rather than `EXPIRED`,
mislabelling the audit trail.

Fix: add `.filter(expires_at__gt=timezone.now())` (or an `InvitationQuerySet.live()`) — or delete
the function. Nothing in `api.py` or the tests uses it.

### 10. `select_for_update()` serialises every invite at a school on the School row — ✅ verified · **FIXED**

> **Fixed.** `.order_by()` added to the locking lookup in `invitations.invite_staff()` **and** to
> `accounts.services.grant_membership()`. The second one was not optional: `grant_membership()`
> re-takes the same lock one line later, so fixing only the call in `invitations.py` would have
> left the School row locked anyway and made the fix cosmetic. Note this puts a change in
> `accounts/services.py`, which is otherwise untouched by this branch. Guarded by a test asserting
> that no `FOR UPDATE` the invite path emits contains a `JOIN`, and by a contention test that
> fails if a grant locks the School row.


`schools/invitations.py:156`

`Membership.objects.select_for_update().filter(...).first()` inherits `Membership.Meta.ordering`
(`["school__name", "role", "user__full_name"]`), which JOINs `schools_school` and `accounts_user`.
Without `of=('self',)`, Postgres locks a row in **every joined table**. Captured SQL:

    ... FROM "accounts_membership"
        INNER JOIN "schools_school" ON (...) INNER JOIN "accounts_user" ON (...)
        WHERE ... ORDER BY "schools_school"."name" ASC, "accounts_membership"."role" ASC,
                           "accounts_user"."full_name" ASC
        LIMIT 1 FOR UPDATE

Contention measured with two connections — one holding the invite-path lock, one probing with
`NOWAIT`. (The probe only means anything when the filter matches an existing `Membership`: against
zero rows `FOR UPDATE` locks nothing and everything looks fine.)

    AS WRITTEN         second txn locking schools_school: BLOCKED -> could not obtain lock
    WITH .order_by()   second txn locking schools_school: acquired freely

    AS WRITTEN         admin two's invite: BLOCKED -> could not obtain lock
    WITH .order_by()   admin two's invite: proceeded freely

That last pair is the real cost: **two admins inviting two different teachers at the same school
serialise**, and any other transaction touching that School row queues behind them for the life of
the invite.

Fix: `.order_by()` (or `select_for_update(of=('self',))`) — either drops the JOINs and the lock
scope with them. `grant_membership()` also re-runs the identical query one line later
(`services.py:71`), so folding the check into it removes the duplicate round-trip entirely.

### 16. `enroll_student()` has the identical lock-scope defect — **not fixed, out of scope**

`accounts/services.py:93`

    existing = (
        Membership.objects.select_for_update()
        .filter(user=user, role=Role.STUDENT, status__in=LIVE_STATUSES)
        .first()
    )

Same missing `.order_by()`, same joined `Meta.ordering`, same consequence: a `FOR UPDATE` that
locks rows in `schools_school` and `accounts_user` as a side effect of sorting. Found while fixing
#10 and deliberately left alone — it is not on the invitation path and `accounts/services.py` is
otherwise untouched by this branch, so widening the diff further needs a decision. The fix is the
one word `.order_by()`.

Worth a sweep for the pattern generally: `select_for_update()` on any model whose `Meta.ordering`
crosses a relation is over-locking, and `Membership` and `Guardianship` both order across joins.

### 17. `revoke()` has the same stale-read shape `accept()` had — **not fixed**

`schools/models.py:408`, `schools/invitations.py:224`

`revoke_invitation()` is `@transaction.atomic` but takes no lock, and `revoke()` checks
`self.status` on an instance the view loaded earlier. So an admin revoking while an invitee accepts
can write `REVOKED` over a just-committed `ACCEPTED`, leaving the membership `ACTIVE` with its
invitation marked revoked — a row that says the credential was cancelled and a relationship that
says it was redeemed.

Milder than #2: nothing is resurrected and no password is written, the audit trail is just wrong.
Not fixed because #2 as filed was scoped to `accept()`, and the same lock-and-re-read treatment
here is a separate change with its own tests. Mentioned because it is the same bug class and will
read as an oversight otherwise.

---

## Documentation

### 11. `docs/membership.md` still says the invitation flow does not exist

`docs/membership.md:90`

Line 90 reads: *"There is no invitation flow yet: nothing sets `invited` except an explicit
`status=` argument. No tokens, no email, no acceptance step."*

The diff updated the Open-items entry at line 302 and added the whole "Inviting staff" chapter 200
lines below, but left this paragraph untouched. A reader reaching the predicate table — the
section most likely to be consulted when touching `MembershipStatus` — is told the feature does
not exist, and that nothing but an explicit `status=` writes `INVITED`, which is now false in
three places (`invite_staff`, `grant_membership`'s revive path, `accept`).

---

## Cleanups

### 12. `active_staff()` re-implements existing queryset helpers

`schools/invitations.py:320`

`.filter(role__in=STAFF_ROLES)` duplicates `MembershipQuerySet.staff()`
(`accounts/models.py:305`), and the `return qs.filter(role=role) if role else qs` tail is
copy-pasted verbatim from `services.school_directory()` (`accounts/services.py:284`). That is a
second place `STAFF_ROLES` is spelled out for the same question — if the grouping gains a member,
this function silently keeps the old behaviour.

`Membership.objects.for_school(school).with_access().staff().select_related('user')` says the same
thing in the existing vocabulary.

### 13. `_issue()` builds a kwargs dict to work around a `None` sentinel

`schools/invitations.py:262`

`kwargs = {"ttl": ttl} if ttl is not None else {}` exists only because callers pass `ttl=None` to
mean "use the default". Signing it `def _issue(membership, actor, *, ttl=DEFAULT_INVITATION_TTL)`
removes the dict, the conditional and the `**`-unpacking, and puts the default in the signature
instead of behind a sentinel re-derived at each of the two call sites.

### 14. `resolve_invitee()` has a dead parameter and a dead return value

`schools/invitations.py:58`

One call site (line 151) passes only email/phone/full_name and binds the second return value to
`_created`. So the `username or email or phone` fallback at line 101 always resolves to
email-or-phone, and the documented `(user, created)` contract is dead weight the docstring spends
a paragraph on. Both are surface area a future caller can misuse — passing `username=` bypasses
the `matching_identifier` lookup the module docstring calls the load-bearing part of the design.

### 15. `get_channel()` duplicates the default already in settings

`schools/delivery.py:108`

`settings.py:90` sets `INVITATION_CHANNEL = os.environ.get('INVITATION_CHANNEL',
'schools.delivery.EmailChannel')`, so the `getattr` default here can only fire if the setting is
deleted entirely — it is a second copy of a string that must be kept in step. When parents get a
WhatsApp channel and the platform default changes, whichever copy is missed becomes a silent
fallback to email.

A plain `settings.INVITATION_CHANNEL` keeps one source of truth. Same pattern at line 81, where
`getattr(settings, 'DEFAULT_FROM_EMAIL', None)` guards a setting Django always defines.

---

## Checklist

- [x] 1. Deactivated user can be invited and re-activated ✅ **fixed**
- [x] 2. `accept()` validates stale reads under lock ✅ **fixed**
- [ ] 3. No validation on `email`/`full_name` ✅ (`api.py:45`)
- [ ] 4. Invalid phone silently discarded ✅ (`invitations.py:81`)
- [ ] 5. `_deliver()` silent no-op without `accept_url_for` ✅ (`invitations.py:284`)
- [ ] 6. Accept-URL origin unconfigured and host-dependent ✅ — **decision needed**
- [ ] 7. SMTP default unreachable; failures escape as 500 — **decision needed**
- [ ] 8. Password validators too weak for a global credential (`settings.py:65`)
- [ ] 9. `pending_invitations()` reports expired as live (`invitations.py:302`)
- [x] 10. `select_for_update()` serialises invites on the School row ✅ **fixed**
- [ ] 11. `docs/membership.md:90` contradicts the new chapter
- [ ] 12. `active_staff()` duplicates queryset helpers (`invitations.py:320`)
- [ ] 13. `_issue()` kwargs-dict sentinel (`invitations.py:262`)
- [ ] 14. `resolve_invitee()` dead param and return value (`invitations.py:58`)
- [ ] 15. `get_channel()` duplicated default (`delivery.py:108`)
- [ ] 16. `enroll_student()` has the same lock-scope defect (`services.py:93`) — found while fixing #10
- [ ] 17. `revoke()` has the same stale-read shape `accept()` had (`models.py:408`)
