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
of being deleted.

A transfer is **two one-sided acts**, because no school may write a row at another:

| Act | Who calls it | Authority needed |
| --- | --- | --- |
| `release_student_as(actor, student)` | the school the child is leaving | that school only |
| `enroll_student_as(actor, user, school)` | the school admitting them | that school only |

`release_student_as()` takes no destination, deliberately — the moment it accepted a
`to_school` it would be a cross-school write wearing a one-sided signature. Ending the old
row is also what frees the one-school slot, since the partial index keys off
`status <> 'ended'`, so the ordering is forced rather than conventional: admission before
release is refused with `AlreadyEnrolled`, naming the school still holding the child.

Guardianship rows survive a release, still pointing at the ended membership. They are the
only record of who the child's guardians were, and the receiving school re-links them from
it (`student.guardianships`) with `link_guardian_as()` under its own authority. What does
not survive is the parents' *access*: a guardian with no other live child at the releasing
school loses their PARENT membership there.

`services.transfer_student_as()` is kept for platform staff and for the rare admin holding
memberships at both schools. It does both halves in one transaction — no window, and
guardians carried across rather than re-linked — but it is not the ordinary path, because
requiring authority at both ends is what made ordinary transfers impossible.

### The handshake

Two one-sided acts move a child, but nothing connects them: no link between a release and
the admission it was meant for, no record that anybody agreed, and a window in between
where the child belongs to no school. `TransferRequest` is what connects them, and the
idea fits in a sentence — **the handshake assembles two-sided authority out of two
one-sided acts.**

`transfer_student()` really does need authority at both ends. Requiring one *caller* to
hold both is what broke ordinary transfers. So one school signs by requesting, the other
by accepting, and only with both signatures does the transfer run — in one transaction,
which is what closes the window. Neither side ever acted at the other's school; the pair
of consents did.

| Act | Called by | Authority needed |
| --- | --- | --- |
| `request_transfer_as(actor, student, to_school)` | either school | that actor's own end |
| `accept_transfer_as(actor, request)` | the other school | the other end |
| `decline_transfer_as(actor, request)` | the other school | the other end |
| `withdraw_transfer_as(actor, request)` | the school that asked | its own end |

Either end may ask. "We are letting this child go to Grace" and "we would like to admit
this child from St Mary's" are one proposal seen from opposite sides, so `requested_side`
records which it was — "who asked" is the first question anyone has when a transfer is
disputed.

**One person may not sign both halves.** Platform staff pass every authority check, and an
admin can hold memberships at both schools, so nothing but an explicit rule stops one
person producing a row that claims two schools agreed. `SameSignatory` is that rule. It is
not a permissions check — the actor has already been found to hold the authority — it is a
check on what the record *means*. Somebody who genuinely holds both ends does not need a
handshake: `transfer_student_as()` is the one-caller path and says so in its signature.

**At most one open request per enrolment**, as a partial unique index rather than an
application check — the same spirit as the one-school slot it protects. Two open requests
would let one admin agree to Grace and another to Hillside for the same child, and the
second to land would find the enrolment gone. A second destination waits for the first to
be declined or withdrawn.

A request is a proposal about a relationship, and the relationship can move on while it
sits there — the child released without a destination, graduated, moved by platform staff.
Accepting then raises `EnrolmentMovedOn` rather than reviving an ended enrolment. Such a
request keeps its `pending` status, because nobody declined it and rewriting the status
would put a lie in the record this table exists to be; it simply drops out of
`transfers_awaiting()`, which filters on the enrolment still being live. Status and queue
answer different questions, and neither is allowed to fake the other's answer.

**Every transfer leaves a row, not just the handshakes.**
`services.transfer_student_as()` writes one too, marked `SINGLE_PARTY`: one actor held
both ends, so the row names that person as both signatures and carries no side. Without it
the table would have logged only the moves that went through a handshake, which is the
wrong half — the transfers with the least independent oversight would have been the ones
with no record. `route` distinguishes the two, and it is not forgeable in either
direction:

- no entry point takes `route` as an argument, so it follows from which function ran;
- an accepted or declined handshake row must name **two different** people;
- a single-party row must name **one** person twice, must be `accepted`, and must carry
  **no** side;
- a handshake row must carry a side.

The last four are `CheckConstraint`s, so relabelling an existing row either way fails in
Postgres, not merely in a code path that a shell session or a data migration could walk
around. `SameSignatory` and the two-signatory constraint say the same thing at different
layers on purpose.

The one place two identical names are correct is a **withdrawal** — the asking side
retracting its own proposal, often by the very person who made it. That is why the column
is `resolved_by` rather than `answered_by`: an accept or a decline is an answer and comes
from the other side by definition, a withdrawal is neither, and the misleading name bought
a constraint that forbade ordinary withdrawals until Postgres rejected one.

The primitive `transfer_student()` still records nothing, and deliberately: it takes no
actor, so there is no signature to record, and inventing an author would be worse than the
gap. It is for imports and fixtures — anything driven by a request goes through an `_as`
function, which is the same split the rest of `services.py` already documents.

There is no HTTP surface for any of this yet — it is a service-layer flow, like
`release_student_as()` beside it.

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

`POST /api/login/` is the door — one `identifier` field, not three, because asking
somebody to first classify what they are about to type is asking them to know something
about our schema. It answers with the person's name, the schools this login may act at
**and the host each one lives on**, and a CSRF token. The host is not decoration: sign-in
happens on the portal and the work happens on a school's own host, so a client told only
"St Mary's" still cannot get a teacher back to their marking sheet.

`POST /api/logout/` is authenticated on purpose, which is what puts it behind ninja's CSRF
check. An unauthenticated logout is a route any other origin can aim at a signed-in
teacher's browser to throw away the session they are marking with.

**`/api/login/` is CSRF-checked too, by hand.** ninja exempts its own views from Django's
CSRF middleware and does the check inside cookie authentication instead — which the one
route you use *before* you have a cookie does not have. So the view calls `check_csrf()`
itself, and `GET /api/csrf/` exists to give a client with no template somewhere to get a
token. The attack this closes runs the opposite way to the usual one: the attacker does
not steal a session, they give the victim one of *theirs*, and a teacher whose browser is
quietly signed in as somebody else marks a class of thirty into an account the attacker
reads at leisure. `SameSite` does not help, because the forged request needs no cookie of
ours to succeed — it sets one. The cost is one round trip before a client's first sign-in,
which is exactly what Django's own `LoginView` has always required.

**The admin is a sign-in door as well, and it is held to the same policy.** Django's admin
authenticates through `AuthenticationForm`, which never passes near `accounts/signin.py` —
so for as long as both existed, `/admin/login/` took unlimited guesses and recorded
nothing, against the `is_platform_staff` accounts that are the only ones able to reach
every school's data at once. `accounts/forms.py` now runs the same three throttle calls
the endpoint does, sharing one window per identifier rather than keeping a second count
that would make the limit quietly twice what it says.

It is also served **on the portal only** now, by routing rather than by a check inside a
view: `PUBLIC_SCHEMA_URLCONF` carries the admin and `ROOT_URLCONF` does not, so a school's
host has no such route. Serving it from a tenant host was worse than untidy — the admin
edits *shared* tables, so it meant privileged writes to platform-wide rows issued on a
connection whose `search_path` had been set to one school's schema.

**Sign-in is served on the portal host and nowhere else.** A school's host refuses anyone
without an active membership *there* (`SchoolAccessMiddleware`); the portal is the one
host where somebody with no membership anywhere is still let through the door, and where
a parent with children at three schools sees all of them. Serving sign-in from a school's
host would mean the same credentials worked on one hostname and not another, and that a
teacher who is also a parent elsewhere needed two sessions to see their own child. A
school host answers `/api/login/` with a **404** — the route does not exist here — matching
the gradebook's answer on the portal.

**A session belongs to the person, not to one school.** That is not a new decision; it is
the one this project has been relying on since `Membership` was made shared rather than
per-tenant, and `SchoolAccessMiddleware` re-derives what somebody may do from the host on
every request rather than from anything stored in the session. Writing the school into the
session would put the same fact in two places and make the session's copy the stale one
the moment a membership is suspended.

What that costs is `SESSION_COOKIE_DOMAIN`. A cookie set with no `Domain` goes back only
to the host that set it, so without a domain spanning the portal and every school, a
teacher signs in successfully on the portal and arrives at their own school's host as a
stranger — and every part of that looks like it worked. Left unset it is not merely
unconfigured, it is wrong in a way that only shows up on the second host, so
`accounts.checks.session_cookie_spans_every_host()` refuses a production deploy without it
rather than letting a teacher discover it. Unset is still right for single-host local
development.

### Guessing, and what happens to people who guess

**One refusal, whatever went wrong.** No account, wrong password, deactivated account, two
accounts matching one identifier: same status, same code, same sentence. Splitting them is
the standing temptation on a sign-in route and it is how a login endpoint becomes an
account-existence oracle — every email and phone number on the platform, one guess at a
time, no credential needed. `IdentifierBackend` already charges a missing account the same
password hash as a real one, so the four do not differ in timing either.

**Counted, not locked.** Ten failures per identifier and fifty per address, per
quarter-hour; reaching either closes the window for a few minutes and never disables
anything. A lockout looks like the stronger control and is in fact a weapon: identifiers
here are semi-public *by design* — a school publishes staff addresses, a parent's number is
on the enrolment form, a student's handle is printed on their report card — so anyone who
can read a school's website could hold a teacher out of the gradebook for as long as they
kept typing. Against the ten-character password floor a few guesses per quarter-hour buys
an attacker nothing, and costs a teacher who has forgotten their password a short wait
rather than a call to an administrator who may not be in the building.

**Failures, not attempts**, which is what lets the per-address limit survive contact with a
school: a staff room reaches the internet through one NAT address, and counting every
sign-in would refuse the fortieth teacher to arrive at 07:50 for being the fortieth. A
success clears the identifier's count and deliberately **not** the address's — clearing
the address on success would hand an attacker a reset button in the form of their own
account.

**The throttle is asked before the credentials**, so a closed window is closed to
everybody, including a caller whose next guess happens to be right. It counts against the
identifier *as typed*, whether or not it resolves to anybody — counted only for accounts
that exist, the 429 would be the oracle the 401 refuses to be.

Kept in Postgres rather than the cache, and the reason is worth repeating because it is
invisible: there is no `CACHES` entry in this project, so Django's default is `LocMemCache`
— per process. A cache-backed throttle would count separately in each worker, turning a
limit of ten into ten times however many processes are running, with the setting reading
correctly and the tests passing on a single process. What is stored is a **digest** of the
identifier, not the identifier: people type their password into that box, and a table of
those in plain text is a credential store nobody decided to build. The address is stored
in the clear, because it is not a credential and it is the one field an incident is
actually investigated from.

`X-Forwarded-For` is ignored unless `TRUSTED_PROXY_COUNT` says how many hops our own edge
wrote. Believing it by default would let any caller invent an address per guess and opt
out of the per-address limit entirely.

## Staying signed in, and being told when you are not

The case that decides session policy is a teacher marking a class of thirty. Each cell
saves as it loses focus, so a marking session is a long stretch of small writes, not one
form and one submit. Two Django defaults are wrong for that shape, and both are overridden
in `settings.py` with the reasoning kept beside them.

**The window is idle time, not total time.** Django's default
(`SESSION_SAVE_EVERY_REQUEST = False`) runs the clock from the moment of *login* and never
extends it, however hard the person is working. A teacher who signed in near the end of
the window is logged out mid-sheet with the cursor still in a cell, seconds after saving a
mark successfully. `SESSION_SAVE_EVERY_REQUEST = True` slides the expiry on every request,
so "expired" means "went away" — the only thing anybody expects it to mean. The cost is
one session write per request, which for a register is thirty rather than none; accepted,
and if it ever shows in database load the answer is a cached session backend, not turning
it back off.

`SESSION_COOKIE_AGE` is twelve hours, down from Django's two weeks. A school day with room
either side, so a normal working day never trips it and a session left open on a shared
staff-room computer is gone by morning. Two weeks of *idle* time was never a decision; it
was the default nobody had chosen.

**A 401 says which 401 it is.** ninja's stock answer is `{"detail": "Unauthorized"}`
whether the caller never signed in or signed in and has since been timed out. Those are
the same status code and completely different situations: the second means *your work is
still good, sign in again and send it*, and a client that cannot tell them apart has to
treat every 401 as fatal and discard whatever the teacher had typed.
`accounts.session.SchoolSessionAuth` stamps which case it is — a session cookie was
presented, or none was — and the handler in `api.py` turns that into `code`
(`session_expired` / `not_authenticated`), `detail` for a person, and `retryable`.

`retryable` rests on a premise worth stating: authentication runs *before* the view, so a
request answered with 401 did not happen. Sending it again after signing in applies it
once, not twice, on every endpoint rather than only the idempotent ones. It is a narrower
claim than "safe to repeat in general" — a write whose *response* was lost is a different
situation and is covered by the gradebook's version check, not by this flag.

Splitting one 401 into two is the kind of change that quietly becomes a disclosure, so:
this one is not a session-key oracle. A forged or random cookie is unusable for exactly
the same reason an expired one is and gets exactly the same answer. Nothing reports
whether the key was ever real — unlike the invitation routes, which must answer a bad
token with a flat 404 because there *is* something on the other side to leak.

**What the server deliberately does not do is hold the teacher's unsent marks.** Writing
them would need the authority that just lapsed, and a server-side draft store is a second
copy of the gradebook with none of its rules. Replaying is the client's job, and the
server's half of the bargain is that replaying is *safe*: every write is conditional on
the version the teacher was shown, and a repeat of the caller's own write is recognised
rather than counted twice. `gradebook/tests/test_session_expiry.py` holds that across a
re-login, which is the one place it could quietly stop being true — the retry is the same
person on a different session.

> The recovery this describes is only completable because there is now somewhere to sign
> back in: see [Signing in](#signing-in). Note the one case that is deliberately *not*
> `session_expired` — signing out on purpose deletes the cookie as well as the session, so
> the next request presents nothing and is told `not_authenticated`. Telling somebody who
> chose to sign out that their work "can be sent again" would invite a client to replay
> what they had just abandoned.

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

Deliberately deferred, not overlooked.

**1. ~~Transfers need a handshake.~~ Built.** `transfer_student_as()` required authority at
both ends, which an ordinary school admin never has, so every transfer routed through
platform staff. Now two one-sided acts move a child (`release_student_as()` /
`enroll_student_as()`), and `TransferRequest` connects them into a handshake that records
both consents and runs the move in one transaction. See
[The handshake](#the-handshake).

Rejected on principle, and still rejected: destination-only authority. It would let a
receiving school unilaterally end a membership at a school it has no relationship with,
which is exactly the cross-school write this model exists to prevent.

Left open on purpose: no HTTP surface, and the unconnected two-act path still has its
window. `release_student_as()` without a handshake is the right call when a child leaves
for a school that is not on the platform — there is no second party to sign — so the gap
is kept, and a test pins it rather than letting a caller assume it is not there.

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

**3. ~~There is no login endpoint.~~ Built.** `POST /api/login/` and `POST /api/logout/`,
on the portal host only. See [Signing in](#signing-in) — this item held the decisions
rather than the code, and all three have been made:

*Where sign-in happens:* the portal host, because a school's host is defined by refusing
anybody without an active membership there, and that is the wrong door for a parent whose
children are at two schools.

*Whether a session is scoped to one school:* it is not, and that was never really open —
`Membership` is shared and `SchoolAccessMiddleware` re-derives access per request from the
host, so the session has never held a school and should not start. The work was the
consequence: `SESSION_COOKIE_DOMAIN`, and a deploy check that refuses to let it go unset.

*Rate limiting and lockout:* throttled, never locked, in Postgres rather than the cache —
with the reasoning for each in [Guessing, and what happens to people who
guess](#guessing-and-what-happens-to-people-who-guess). The short version is that a
lockout on a semi-public identifier is a weapon rather than a defence, and that a
cache-backed throttle in a project with no `CACHES` entry would silently be per-worker.

Still open, and now the only thing between a browser and this API: **there is no password
reset.** A teacher who forgets theirs has no self-service path, and the invitation flow
only sets a *first* password. It is the same shape as the invitation flow — a one-time
token delivered over a channel — and should probably reuse `schools/delivery.py` rather
than grow a second mechanism beside it.

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

`validate_token()` answers `None` for unknown, spent, revoked, expired, no-longer-invited
**and deactivated-invitee** alike, and the endpoints turn all six into the same 404 —
telling a guesser that a token was *once* real is telling them they guessed a real one.
Nor does the holder of a real token learn that the account behind it was disabled; that
is not something a link should explain. Expiry is
settled lazily on lookup rather than by a scheduled job, so a row cannot sit in `pending`
past its date because a cron job is broken.

A resend revokes the old invitation and issues a second row rather than updating one in
place, so the previous link dies the moment the new one is minted and both stay in the
audit trail.

**At most one invitation is pending per membership**, and that is enforced at the mint —
`invitations._issue()` revokes every live invitation for the membership before creating
the next one. Putting it there rather than in `resend_invitation()`, where it started, is
what makes it an invariant instead of one path's behaviour: a second *invite* used to
leave both links working, and reviving an ended membership used to bring its old pending
invitation back with it. It is application-level, not a database constraint, so two
callers minting for one membership at the same instant can still leave two live tokens;
both are for the same person, school and role and both still expire, so the failure is
benign. A partial unique index on `(membership)` where `status = 'pending'` is the
airtight version, in the same spirit as the one-school-per-student index.

Nothing else is unique but `token_hash` — deliberately no "one invitation per person" or
per contact detail, because a shared phone number must not collide. Note the distinction:
per-*membership* is a different claim, since a membership is already one person at one
school in one role.

**A deactivated account cannot be invited, and cannot accept.** `deactivate_user()` is how
access is taken away and it erases nothing, so the row is still there for
`matching_identifier()` to find — which is exactly why the flow has to ask rather than
assume a match is a person to invite. Nothing did, and the consequence ran the length of
the flow: the membership went to `INVITED`, mail went out, and acceptance promoted it to
`ACTIVE` while writing a fresh global password onto an account the platform had disabled.
The school was left with a teacher on its roster and in `active_staff()` who could never
sign in, because `is_active` is refused at the door by `IdentifierBackend`. The rule is
asked at four points, because each is reachable alone: `resolve_invitee()` (inviting),
`_issue()` (minting, which is what covers a resend), `validate_token()` (looking up) and
`accept()` (redeeming). Reinstating somebody is `reactivate_user()` — a decision worth
making deliberately rather than one an invitation makes on the platform's behalf.

**Acceptance decides on state it has locked.** `accept()` re-reads the membership, the
invitation and the user under `select_for_update` before it checks anything, because the
objects it was handed were loaded by `validate_token()` in an earlier transaction and
every guard is a question about the present. Reading the in-memory copies was a lost
update twice over: an acceptance that began before an admin's `end()` committed overwrote
the just-written `ENDED` with `ACTIVE` — leaving `ended_on` set on a live row — and two
clicks on one link both passed "is it still pending" before either reached the lock, so
the second spent an already-spent token. The rows are taken in the order
membership → invitation → user, which is the order `invite_staff()` takes the first two;
two transactions taking one pair in opposite orders is a deadlock.

Those locks are also deliberately narrow. `Membership.Meta.ordering` sorts by
`school__name` and `user__full_name`, and Postgres locks a row in *every* joined table
when `FOR UPDATE` meets a join — so the default ordering silently put an exclusive lock on
the **School** row into every membership lookup that took one — in `invite_staff()`, in
`grant_membership()` and in `accept()` alike. Two admins inviting two different teachers
at one school queued behind a row neither was writing. `.order_by()` on
those lookups drops the joins and the lock scope with them; `select_for_update(of=("self",))`
narrows it the same way. `(user, school, role)` is uniquely constrained, so there is at
most one row and no ordering to apply in the first place.

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
  membership is not in the way — re-hiring revives the row to `INVITED`. The old link
  does not come back with it: minting revokes whatever was still pending, so re-hiring
  issues a fresh credential rather than resurrecting one raised for a relationship that
  has since ended.

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
`check_deliverable(invitation)` and `check_configured()`. Because `send()` runs *after*
the commit, a failure inside it cannot undo anything — the caller saw an error while the
placeholder user, the membership and an undeliverable invitation all survived, one more
orphaned set per retry. The deterministic half of that failure is asked before the commit
instead, and it is two questions rather than one because the answers have different
audiences: `check_configured()` asks "can this deploy send *anything*?" (a missing
`EMAIL_HOST` is nobody's fault but the operator's), `check_deliverable()` asks "can it
reach *this person*?" (a teacher with no email address is something the admin can fix by
typing one). Both are optional, so a channel that cannot answer without sending — and a
test double that is a plain object with a `send` — remains valid.

**The accept link's origin is a setting, not the request.** `INVITATION_ACCEPT_URL` is a
template containing `{token}`, and it is the only place that decides where an invitation
points. The two API call sites used to build it from `request.build_absolute_uri()`, which
made the origin of a live credential a property of whichever host the *issuing admin*
happened to be signed in on: `TenantMainMiddleware` resolves the portal host and a
school's own host differently, so inviting the same teacher from two places produced links
on two different origins — for a page that is meant to live on a frontend which may be on
neither, and which no urlconf in this project serves. Nothing pinned the path either; the
service tests used `https://portal/i/{token}/` and `api.py` emitted `/invitations/{token}/`.

The setting has **no default**, which is deliberate: a hard-coded origin is wrong for
every deploy but one, and falling back to the request host is the bug being removed. An
unset or placeholder-less value is refused by `invitations.configured_accept_url()`, and
refused *before the commit*, so a deploy that has not been set up creates no orphaned
placeholder accounts on the way to saying so.

Note what this does **not** decide: whether Django should eventually serve the accept page
itself. It stays a frontend route, and the setting is what makes that choice reversible —
pointing it at a Django-served path later is a settings change and a urlconf entry, not an
edit to the flow.

**A mail deploy fails in two different ways and gets two different answers.** SMTP is the
`EMAIL_BACKEND` default, but Django's own SMTP defaults are `localhost:25` with no
credentials — not a mail server on any host this runs on — so a deploy that configured
nothing raised `ConnectionRefusedError` from inside an `on_commit` callback, after
everything had committed, and reached the admin as an unexplained 500.

- **No `EMAIL_HOST` at all** is a misconfiguration: `EmailChannel.check_configured()`
  refuses pre-commit and the API answers **503**, with nothing left behind. Nobody can be
  invited until an operator sets one, and that is what the response says.
- **An outage** — host set, connection refused or timed out — is caught in `send()` and
  re-raised as `DeliveryFailed`, which the API answers **502**. This one is post-commit
  and stays that way: the invitation exists, and losing it would be worse than keeping it.
  Re-inviting is idempotent (the account is reused, the membership is already `INVITED`,
  and minting revokes the stale token), so retrying through an outage leaves one account
  and one live invitation rather than one per attempt.

The `except` around the send is `(OSError, SMTPException)` and not `Exception`, on purpose:
folding a `TypeError` in the body template into "the mail server is down" would turn a bug
report into an outage nobody could reproduce.

**`EMAIL_BACKEND` must not default to the console backend.** It once did, and that failed
open in both directions: nothing was delivered, nothing raised, and the whole message —
accept URL and live token — went to stdout, which in a container is the application log.
The default is now SMTP, which fails loudly; local development opts into the console
backend explicitly in `docker-compose.yml`. Anything that renders `expires_at` for a
human goes through `timezone.localtime()` first — the column is UTC and the reader is in
`TIME_ZONE`, so an expiry at 23:30 UTC is already tomorrow in Lagos.
