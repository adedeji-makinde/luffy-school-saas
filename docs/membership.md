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
(`accounts.middleware.SchoolAccessMiddleware`).

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
| Does the relationship exist? | `LIVE_STATUSES` / `.live()` | invited, active, suspended | the one-school slot, `children()`, `student_membership()` |
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
