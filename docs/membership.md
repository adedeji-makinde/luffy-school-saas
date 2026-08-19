# Identity, membership and why they are not per-tenant

## The one rule to not undo

`accounts.User`, `accounts.Membership` and `accounts.Guardianship` live in the
**public** schema (`SHARED_APPS`). Everything a school owns — academics, attendance,
fees, report cards — belongs in `TENANT_APPS`, one copy per school.

It is tempting to "improve" the isolation by moving `User` into each tenant schema.
Don't. A parent may have children at more than one school and must see all of them
from a single login. Per-tenant users would mean one account per school, which is
exactly the thing this model exists to avoid.

The isolation that matters is still intact: a school's records never leave its schema,
and reaching them requires a live `Membership` for that school
(`accounts.middleware.SchoolAccessMiddleware`). That half of the claim is no longer
taken on trust — see [tenancy.md](tenancy.md) for what was verified against a real
Postgres, and for the open blocker on foreign keys from a tenant table back to
anything in here.

## Everyone gets an account

Six roles, none of them privileged in the schema: `admin`, `principal`, `teacher`,
`bursar`, `parent`, `student`. There is no flat "staff" flag on a person. Role is an
attribute of the *relationship* between a person and a school:

```
Membership(user, school, role, status)
```

So a person is never "a teacher" in the abstract — they are a teacher *at St Mary's*,
and possibly a parent there too, and a parent at Grace Academy as well. Three rows,
one login.

`STAFF_ROLES` and `FAMILY_ROLES` in `accounts/models.py` are groupings for permission
checks, not a second source of truth. They hold role *values*, which is what the
database hands back, and `Role` members work against them interchangeably —
`TextChoices` mixes in `str`, so a member hashes and compares by value. Worth knowing
if you ever swap in a plain `enum.Enum`: that one hashes by *name*, and
`"admin" in {Role.ADMIN}` would quietly become `False`.

Django's `is_staff` is a red herring here. The only field is `User.is_platform_staff`,
meaning the SaaS operator — us — not school staff. `User.is_staff` exists solely as a
property so `django.contrib.admin`'s login check keeps working.

`PermissionsMixin` is kept for that admin integration, so `User.groups` and
`User.user_permissions` exist. **They are deliberately unused as authority.** Every
"may this person do this?" question is answered from `Membership.role` for the school in
question. Global groups cannot express "teacher at St Mary's, parent at Grace Academy",
so granting school authority through them would quietly break the one-login-many-schools
model. Use `request.school_roles`, `user.roles_at(school)` or `user.has_access_to(school)`.

## Parents

A parent's reach is derived, never stored twice:

- `Guardianship(guardian, student)` points at the child's **STUDENT membership**, not
  the child's user. That single foreign key pins both the child and their school.
- `services.link_guardian()` grants the parent a `PARENT` membership at that child's
  school, creating it on the first child there. This is how one login comes to span
  several schools.
- `services.unlink_guardian()` ends that membership when the last child at the school
  is unlinked — and leaves any other role at that school alone, so a teacher whose
  child graduates is still a teacher.
- `User.children()` returns every child across every school in one query;
  `services.parent_dashboard()` groups them by school for the portal view.

## Two predicates, not one

`Membership.status` answers two different questions, and conflating them causes bugs in
both directions. Keep them apart:

| Question | Predicate | Statuses | Used by |
| --- | --- | --- | --- |
| Does the relationship exist? | `LIVE_STATUSES` / `.live()` | invited, active, suspended | the one-school slot, `children()`, `student_membership()`, `school_directory()` |
| May they act at the school? | `ACCESS_STATUSES` / `.with_access()` | active only | `has_access_to()`, `roles_at()`, the middleware, `can_grant_memberships()` |

An invitation is an offer, not access, and a suspension withdraws access without
dissolving the relationship. So an invited or suspended person cannot sign in, while
still occupying their place — an invited student holds their single school slot and
cannot be enrolled elsewhere.

The important consequence runs the other way too: **`children()` is deliberately
relationship-scoped**, so a parent sees an invited child on their dashboard before that
child can sign in themselves. Do not "tidy" it to use `with_access()`.

The database index is independent of both — it keys off `status <> 'ended'` directly, so
changing either frozenset cannot weaken the one-school-per-student guarantee.

There is no invitation *flow* yet: nothing sets `invited` except an explicit `status=`
argument. No tokens, no email, no acceptance step. Note that a placeholder account made
with `create_user(username, None)` has an unusable password and cannot authenticate,
which is the natural building block when that flow gets built.

## Who may grant memberships

A school administrator's authority stops at their own school. An admin at St Mary's
cannot enrol a child, hire a teacher or link a parent at Grace Academy; only
`is_platform_staff` acts across schools. `MEMBERSHIP_GRANTING_ROLES` holds the roles
that may grant — currently `admin` alone. Principals are deliberately excluded; add
them to that frozenset if the policy changes.

The functions in `services.py` come in two layers, and the distinction matters:

- **Primitives** — `grant_membership`, `enroll_student`, `link_guardian`,
  `unlink_guardian`, `transfer_student`. They keep the data consistent but ask nothing
  about the caller, which is what lets `link_guardian` grant a `PARENT` membership by
  itself. Use these for imports, fixtures and internal calls.
- **Actor-checked** — the same names with an `_as` suffix, taking the acting user
  first. They call `can_grant_memberships(actor, school)` and raise `NotPermitted`.
  **Anything driven by a request goes through these.**

`transfer_student_as` needs authority at *both* schools, since a transfer ends a
membership at one and opens one at the other. In practice that means platform staff, or
an admin who holds a membership at both.

## Students

Students do not see siblings — that is a parent-only view. It follows from the model
rather than needing a rule: `User.children()` returns children of a *guardian*, and a
student is never a guardian, so a student sees only their own membership. Tests pin
this so it does not become true by accident and then quietly stop being true.

A student has exactly one school, enforced in Postgres as a partial unique index:

```python
UniqueConstraint(fields=["user"], condition=Q(role="student") & ~Q(status="ended"))
```

Two things follow. It is global rather than per-school — possible only because
`Membership` is shared — so a second live student row *anywhere* is rejected. And
`status="ended"` releases it, so graduations and transfers keep their history instead
of being deleted. Use `services.transfer_student()`, which ends the old membership,
opens the new one, and carries the guardians across.

## Deleting things

`Membership.school` is **`PROTECT`**. Memberships and the guardianships hanging off
them are the family history, and losing them as a side effect of deleting a school is
the kind of bug that should be impossible rather than merely unlikely. Deleting a
school with any membership — *including ended ones* — raises `ProtectedError`. Ended
rows are the history, so they keep protecting the school; that is deliberate, not an
oversight.

Close a relationship with `membership.end()`, never `delete()`. `transfer_student()`
already does this.

**Both `Guardianship` foreign keys are `PROTECT` as well** — `guardian` and `student`.
Nothing deletes a family link as a side effect: a guardian cannot be deleted while a
link remains, and neither can a child, because their membership would cascade and the
guardianship protects it. Call `services.unlink_guardian()` first; it keeps both sides
in step. That leaves `Membership.user` as the one remaining cascade, which is a deletion
*about* that person rather than a side effect — and it is still blocked while any
guardianship references them.

### Removing a person

Deactivate, don't delete. `accounts.deletion.deactivate_user()` clears `is_active`,
which `IdentifierBackend` refuses at sign-in via the inherited
`user_can_authenticate()`, while every membership, guardianship and school record
stays where it was. It is reversible with `reactivate_user()`.

Permanently removing the row goes through `accounts.deletion.hard_delete_user()` and
nothing else, and that is enforced rather than asked for. A `pre_delete` receiver on
`User` refuses every delete that `hard_delete_user()` did not open the door for, so
`user.delete()` in a view and `User.objects.filter(...).delete()` in a shell both
raise. Registering the receiver also costs the collector its fast path — Django
disables `can_fast_delete()` for a model with `pre_delete` listeners — so a bulk
delete can no longer skip per-object signals.

Being a plain function rather than a manager or queryset method still matters, but
only for API shape: it keeps a hard delete off the end of a chain. The receiver is
what makes it a guarantee.

`hard_delete_user()` refuses — `ValidationError`, naming each schema — while anything
still references the user, ended memberships included. Tests that need the raw
behaviour underneath the policy lift it with `_sanctioned_delete()` — three test
files do, each saying why at the point it does.

The guard exists because `on_delete` cannot see across schemas: `PROTECT` queries the
referencing table in the *connected* schema only, so a reference held by another
school raises nothing and the transaction fails later at `COMMIT`. See
[tenancy.md](tenancy.md#hard-blocker-tenant--shared-foreign-keys) and
`accounts/tests/test_deletion.py`.

Worth knowing why both sides need protecting rather than just one: `Guardianship.guardian`
points at the parent's **User**, not their PARENT membership. Before `PROTECT`, deleting a
parent's membership left the link behind, so `children()` still listed the child while
`has_access_to()` returned `False`.

## Signing in

One backend, `accounts.backends.IdentifierBackend`, resolves username, email or phone
to the same user. `username` is the required, unique sign-in field; email and phone are
optional and unique when present, stored as `NULL` rather than `""` so the unique
indexes don't collide. Students get a school-issued handle (`STM/2026/0042`) because
many have no email address; parents commonly use the phone number the school has on
file.

## Identifiers: one phone number, one account

Before this, `phone` was stored as whatever string was typed. `08031234567`
and `+2348031234567` are the same person's number, but as exact strings they
are different — so one family could show up as two accounts, one holding the
mother's PARENT membership and the other holding half the children, split by
nothing but which form of their own phone number they happened to type on
which form. `accounts/identifiers.py` fixes this by normalizing every phone
number to E.164 (`+2348031234567`) via the `phonenumbers` library before it
is saved or compared, rather than hand-rolling that parsing.

`settings.PHONE_DEFAULT_REGION` (`NG`) is a **parsing default, not a
restriction**: a number typed with no country code is read as Nigerian, but
any valid number from anywhere — another country, a landline, a toll-free
line — is accepted as-is. This is enforced with `is_valid_number`, never
`is_valid_number_for_region`.

**Usernames are normalized too, but only under a strict gate.** A naive
normalizer that tried to phone-ify every username would rewrite a
school-issued handle like `STM-08031234567` into an actual phone number, and
worse, could collide two different students' handles that differ only in
punctuation. So `canonical_username()` only normalizes a username when the
**entire string** is made of digits and phone punctuation
(`^[+()\d\s.\-]+$`) — one letter, colon, or slash disqualifies it outright.
`STM-08031234567` and `tel:08031234567` are left untouched; `0803 123 4567`
becomes `+2348031234567`. When a username *is* phone-shaped, its stored form
matches `User.phone`'s E.164 form exactly, so the two can agree without
colliding on the same account (see `User.assert_identifiers_unambiguous()`).

## Sign-in collisions: Option C, not a database constraint yet

Same-column uniqueness — two users sharing one `phone`, one `email`, or one
`username` — is enforced by real unique indexes and always has been. The gap
this closes is **cross-column**: nothing stopped one user's `username` from
being identical to a different user's `phone`, since they live in different
columns with different indexes. `IdentifierBackend` used to resolve that
case with `order_by("pk").first()` — an arbitrary pick with no real policy,
silently signing whoever typed that identifier into one of two unrelated
accounts.

The fix, deliberately **Option C**: an application-level check,
`User.assert_identifiers_unambiguous()`, rather than a database-level
constraint. It runs on save via `User.matching_identifier()` — a single
query, on `UserManager`, that resolves an identifier against
username/email/phone in one shot. Both the model's check and
`IdentifierBackend.authenticate()` call this same method, on purpose: the
collision rule and the sign-in resolution read from one place, so they
cannot drift apart the way the old `order_by("pk")` guess could from
whatever the "real" rule was supposed to be.

**This is racy by construction, and that is a known, accepted limitation.**
Two concurrent transactions can both run `assert_identifiers_unambiguous()`,
both see no conflicting row yet, and both commit — because a `SELECT`
against a row that doesn't exist yet locks nothing. Only the cross-column
comparison rides on this gap; same-column uniqueness is still a real
database constraint regardless of this check's outcome. It is acceptable
pre-launch because the failure mode is safe: a genuinely ambiguous
identifier is **refused at sign-in**, never silently resolved to the wrong
person. `IdentifierBackend` refuses to authenticate rather than pick one
when `matching_identifier()` returns more than one user — this replaces
`order_by("pk").first()` entirely, not just for the cross-column case.

The check is skipped when a save's `update_fields` excludes all three
identifier columns, so `update_last_login` — which fires on every sign-in
and only ever touches `last_login` — pays no extra query for a check that
cannot possibly apply to it. A save with `update_fields=None`, or one that
touches `username`/`email`/`phone`, still runs it.

**The upgrade path, for later:** a `UserIdentifier(kind, canonical_value)`
table with one unique index spanning all three kinds, so the database
itself makes cross-column collisions impossible instead of merely unlikely.
That is a change of *mechanism* — swapping the racy check for a real
constraint — not a change of *rule*: an ambiguous identifier is refused
either way. Nothing about the current `User.phone`/`User.email`/
`User.username` shape needs to change to get there later.

## Open items

Both deliberately deferred, not overlooked. Neither is started.

**1. Transfers need a handshake.** `transfer_student_as()` requires authority at both the
old and the new school. Since an admin normally holds authority at one school only, that
means **an ordinary school admin can never transfer a student** — in practice every
transfer routes through platform staff, which does not scale.

The agreed direction is to stop treating it as one operation and split it into two
one-sided acts: `release_student_as(actor, student)` (old school ends the membership,
authority at the old school only) followed by the existing `enroll_student_as()` (new
school admits). Neither school ever writes at the other, and the partial index already
allows it — once the old row is `ended`, the slot is free. `Membership.end()` and
`enroll_student_as()` already exist, so this is a small change with no migration.

Two costs to handle: guardians must be re-linked by the receiving school, and there is a
window between release and admission where the child belongs to no school, so
`student_membership()` returns `None`. A `TransferRequest` handshake — either side
initiates, the other accepts, then `transfer_student()` runs — would close the window and
record who agreed, which matters the first time a transfer is disputed.

Rejected on principle: destination-only authority. It would let a receiving school
unilaterally end a membership at a school it has no relationship with, which is exactly
the cross-school write this model exists to prevent.

**2. ~~No invitation flow.~~ Built for staff; parents and students still open.**
See [Inviting staff](#inviting-staff) below. The warning this item carried turned out to
be the load-bearing part of the design and survives intact: identity is global, so the
two states are orthogonal — whether the *person* has a usable credential (`User`-level)
and whether *this school's* relationship has been accepted (`Membership`-level).
`Invitation.needs_password` is exactly that distinction, and the preview endpoint reports
it so a teacher joining their second school is never asked for a second password.

What remains open is the other half of the audience: parents and students. That is not
the same flow with a different role value. Parents commonly share one phone between two
guardians, students often have neither email nor phone, and both need a channel that is
not email — which is why delivery is a seam rather than a method (see below).

## Inviting staff

An admin invites by email or phone plus an intended role. The person is resolved through
`User.objects.matching_identifier()` — the same lookup sign-in uses, so an invite can
never find somebody a later login would not — and an existing account is **reused**, not
duplicated. Only if there is no match at all is a placeholder created with
`create_user(username, None)`, whose unusable password cannot authenticate until
acceptance sets one.

The `Membership` is created immediately at `INVITED`. That is a real relationship which
grants no access: it appears on `services.school_directory()` and is absent from
`invitations.active_staff()`. Acceptance promotes it to `ACTIVE`. So an `Invitation` is a
credential for a relationship that already exists in the data, not a promise of one —
which is why it holds a single foreign key to the membership rather than repeating the
person, school and role as three columns that could drift.

**Tokens are stored as SHA-256 digests and never in the clear**, on the same reasoning as
a password: whoever can read the table cannot mint a working link from it, so a leaked
backup is not a set of live invitations. `create_with_token()` returns the raw token to
its caller once; after that it exists only in whatever the recipient received. A lost
token is reissued, never recovered.

`validate_token()` answers `None` for unknown, spent, revoked, expired **and
no-longer-invited** alike, and the endpoints turn all five into the same 404 — telling a
guesser that a token was *once* real is telling them they guessed a real one. Expiry is
settled lazily on lookup rather than by a scheduled job, so a row cannot sit in `pending`
past its date because a cron job is broken.

A resend revokes the old invitation and issues a second row rather than updating one in
place, so the previous link dies the moment the new one is minted and both stay in the
audit trail. Nothing is unique but `token_hash` — deliberately no "one invitation per
person" or per contact detail, because a shared phone number must not collide.

Acceptance is the only path that sets a password, and what it writes is a *global*
credential — it signs the person in at every school they hold a membership at, not just
the one that invited them. `AUTH_PASSWORD_VALIDATORS` therefore has to be non-empty for
that path to mean anything; Django ships it empty. `accept()` calls `validate_password()`
itself rather than leaving it to the endpoint, and raises `WeakPassword`, which the API
renders as 422 beside `PasswordRequired` — both are things the invitee can fix and
resubmit with the same link.

### Every rule is asked of the membership, not of the row

An `Invitation` is a credential for a relationship, so the relationship is what decides
whether the credential is still good. This is not a stylistic preference; asking the
invitation row instead is wrong in ways that are easy to miss, because a membership has
more than one invitation over its life and each row's status describes only itself:

- **A token dies with its membership.** `Membership.end()` and a suspension leave any
  outstanding invitation `pending`, so `validate_token()` requires the membership to be
  `INVITED` as well. Without that, a withdrawn relationship left a working link behind —
  and redeeming it still set that global password. `accept()` re-checks rather than
  trusting the lookup, because it is callable directly.
- **A resend asks the membership too.** After `invite → resend → accept`, row one is
  `REVOKED` and the membership is `ACTIVE`. "Revoked" is a resendable state, so a rule
  keyed off row one would happily mint a live token for somebody already in — the exact
  thing the rule exists to prevent. `AlreadyAccepted` (409) is raised off
  `membership.status`; suspended and ended give `MembershipNotOpen`.
- **Inviting an existing member is refused.** `grant_membership()` is idempotent and
  returns a live row *untouched*, so a requested `status=INVITED` is silently dropped
  against an `ACTIVE` membership. `invite_staff()` checks for a live membership under the
  same lock `grant_membership()` takes, and raises `AlreadyAMember` (409). An **ended**
  membership is not in the way — re-hiring revives the row to `INVITED`, which also
  revives any invitation still pending against it.

All of these refusals share one base class, and that is worth stating because it briefly
was not true: `schools.models.InvitationError`, re-exported from `schools.invitations`.
The model's own refusals (`PasswordRequired`, `WeakPassword`, a spent or expired link) and
the service's (`NotStaffRole`, `AmbiguousInvitee`, `AlreadyAccepted`, `AlreadyAMember`,
`MembershipNotOpen`) all descend from it, so `except InvitationError` catches the whole
flow no matter which of the two modules you imported it from.

The API answers back with none of this. `InvitationOut` deliberately carries no invitee
name: identity is global, so a matching account may belong to somebody this school has no
relationship with, and echoing the *resolved* account's name both leaked a stranger's real
name across schools and made the endpoint an exists/does-not-exist oracle for any email or
phone on the platform — one unsolicited invitation email per probe. The invitee sees their
own name on the preview, where the token proves who they are.

`role` is the stored value on every response — `"teacher"`, never `"Teacher"`. The preview
carries the label separately as `role_display`, because it is the one endpoint rendered to
somebody who is not signed in and has no role vocabulary of their own. Keep them apart: a
`role` that means the database value on some endpoints and the display label on others is
a field a client cannot key off at all.

**Delivery is a seam, not a method.** `schools/delivery.py` defines a channel as anything
with `send(invitation, raw_token, *, accept_url)`, resolved from `INVITATION_CHANNEL`.
`schools/models.py` does not import it and a test enforces that. Adding WhatsApp for
parents is a new class beside `EmailChannel` and a settings value — not an edit to
`Invitation`. Sending is dispatched through `transaction.on_commit`, so no link is ever
delivered for an invitation whose transaction rolled back.

That dispatch cuts both ways, which is why a channel may also answer
`check_deliverable(invitation)`. Because `send()` runs *after* the commit, a failure
inside it cannot undo anything — the caller saw an error while the placeholder user, the
membership and an undeliverable invitation all survived, one more orphaned set per retry.
The deterministic half of that failure, "there is no address to reach them at", is asked
before the commit instead. It is optional, so a channel that cannot answer without
sending — and a test double that is a plain object with a `send` — remains valid.

**`EMAIL_BACKEND` must not default to the console backend.** It once did, and that failed
open in both directions: nothing was delivered, nothing raised, and the whole message —
accept URL and live token — went to stdout, which in a container is the application log.
The default is now SMTP, which fails loudly; local development opts into the console
backend explicitly in `docker-compose.yml`. Anything that renders `expires_at` for a
human goes through `timezone.localtime()` first — the column is UTC and the reader is in
`TIME_ZONE`, so an expiry at 23:30 UTC is already tomorrow in Lagos.
