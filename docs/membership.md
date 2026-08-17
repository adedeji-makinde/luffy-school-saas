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

## Students

A student has exactly one school, enforced in Postgres as a partial unique index:

```python
UniqueConstraint(fields=["user"], condition=Q(role="student") & ~Q(status="ended"))
```

Two things follow. It is global rather than per-school — possible only because
`Membership` is shared — so a second live student row *anywhere* is rejected. And
`status="ended"` releases it, so graduations and transfers keep their history instead
of being deleted. Use `services.transfer_student()`, which ends the old membership,
opens the new one, and carries the guardians across.

## Signing in

One backend, `accounts.backends.IdentifierBackend`, resolves username, email or phone
to the same user. `username` is the required, unique sign-in field; email and phone are
optional and unique when present, stored as `NULL` rather than `""` so the unique
indexes don't collide. Students get a school-issued handle (`STM/2026/0042`) because
many have no email address; parents commonly use the phone number the school has on
file.
