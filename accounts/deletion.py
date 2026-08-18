"""Removing a person, and why the permanent version is not a method.

The supported way to take away someone's access is `deactivate_user()`, which
clears `is_active`. Nothing is erased: their memberships, their children, the
history of what they did at a school all stay exactly where they were, and
`ModelBackend.user_can_authenticate()` — which `IdentifierBackend` inherits —
refuses the sign-in from that moment on. Reversing it is one field.

Permanently deleting the row is a different thing, and `hard_delete_user()` is
the only way to do it. Two mechanisms, because either alone is not enough:

It is a plain function rather than a manager or queryset method, so a hard
delete cannot be reached by ordinary chaining. That shapes the API but enforces
nothing on its own — `.delete()` still exists on every instance and queryset,
and an earlier version of this module claimed the absence of a manager method as
a safety property when it was only a naming choice.

So `block_unsanctioned_delete()` below refuses any `pre_delete` on `User` unless
`hard_delete_user()` opened the door for it. That is what makes "the only way"
literally true: `user.delete()` in a view, `User.objects.filter(...).delete()`
in a shell, a cascade from somewhere else — all of them raise. The only bypass
is the thread-local sentinel, which is set around exactly one `delete()` call
and restored afterwards.

Why a hard delete needs a guard at all
--------------------------------------
`on_delete` is not a safety net in a schema-per-tenant database. The deletion
collector resolves every relation against the *currently connected* schema, so
deleting a user while pointed at St Mary's cannot see a row in Grace Academy's
schema that references them. `PROTECT` is the worst case: it works by querying
the referencing table, finds nothing from the wrong schema, and lets the delete
through. The transaction then fails at `COMMIT` as an `IntegrityError` naming a
table the connection was never pointed at. That is measured, not assumed — see
`schools/tests/test_cross_schema_fk.py` and the blocker in `docs/tenancy.md`.

So the scan below has to visit each school's schema itself. What it must NOT do
is scan every schema for *shared* models. `accounts` is in `SHARED_APPS` only,
so there is one `accounts_membership` table, in `public`; because
`schema_context` leaves `public` on the `search_path`, querying `Membership`
from inside a tenant just reads that same public table again. Counting it once
per school would report every school on the platform as an offender for a user
who holds a single membership. Hence the split: shared relations are counted
once in `public`, tenant-local relations once per school schema.
"""

import contextlib
import threading

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django_tenants.utils import (
    app_labels,
    get_public_schema_name,
    get_tenant_model,
    schema_context,
)

from .models import User

#: Which schema a model's table actually lives in, by app. An app listed in
#: both settings gets a table in `public` *and* in every tenant schema, so it
#: legitimately appears in both sets and is checked both ways.
SHARED_APP_LABELS = frozenset(app_labels(settings.SHARED_APPS))
TENANT_APP_LABELS = frozenset(app_labels(settings.TENANT_APPS))


#: Thread-local rather than a module-level flag, and deliberately so: a plain
#: bool would be shared by every worker thread, so one sanctioned delete would
#: hold the door open for anything another request happened to be deleting at
#: the same moment. The whole guarantee below rests on this being per-thread.
_sanctioned = threading.local()


@contextlib.contextmanager
def _sanctioned_delete():
    """Open the door for exactly one `delete()` call on this thread."""
    previous = getattr(_sanctioned, "allowed", False)
    _sanctioned.allowed = True
    try:
        yield
    finally:
        _sanctioned.allowed = previous


@receiver(pre_delete, sender=User, dispatch_uid="accounts.deletion.block_hard_delete")
def block_unsanctioned_delete(sender, instance, **kwargs):
    """Make the policy real instead of documented.

    Without this, `hard_delete_user()` is a function people are asked to
    remember. `user.delete()` and `User.objects.filter(...).delete()` still work
    from any view or shell, still cascade `Membership.user` away, and never run
    the scan. Being a module-level function keeps a hard delete off the end of a
    queryset chain; it does not stop `.delete()` existing.

    A second, quieter effect: `Collector.can_fast_delete()` returns False for a
    model with `pre_delete` listeners, so a bulk queryset delete can no longer
    take the fast path that skips per-object signals. Every route into deleting
    a user now passes through here.
    """
    if not getattr(_sanctioned, "allowed", False):
        raise ValidationError(
            f"Refusing to delete {instance}: deleting a User directly is not "
            "supported. Deactivate them with accounts.deletion.deactivate_user(), "
            "or call accounts.deletion.hard_delete_user(), which checks every "
            "schema for references first."
        )


class Reference:
    """One model's rows pointing at the user, found in one schema."""

    def __init__(self, schema_name, model, count):
        self.schema_name = schema_name
        self.model = model
        self.count = count

    def __str__(self):
        rows = "row" if self.count == 1 else "rows"
        return f"{self.schema_name}: {self.model._meta.label} ({self.count} {rows})"


def deactivate_user(user, *, save=True):
    """Take away a person's access without erasing anything. The standard path.

    This is what "removing" a user means here. `is_active=False` is refused at
    sign-in by `IdentifierBackend`, while every membership, guardianship and
    school record survives untouched — which is what makes it reversible, and
    what makes it safe in a database where a real delete cannot see across
    schemas.
    """
    user.is_active = False
    if save:
        user.save(update_fields=["is_active"])
    return user


def reactivate_user(user, *, save=True):
    """Undo `deactivate_user()`. Memberships decide what they can then reach."""
    user.is_active = True
    if save:
        user.save(update_fields=["is_active"])
    return user


def _reverse_relations_into_user():
    """Every relation pointing at `User`, hidden ones included.

    Not `_meta.related_objects`, which is the obvious choice and the wrong one:
    it drops relations declared `related_name="+"`, and its own docstring calls
    itself "Private API intended only to be used by Django itself". The deletion
    collector reads `get_fields(include_hidden=True)`, so a hidden ForeignKey is
    something Django will happily delete against while a scan built on
    `related_objects` cannot see it at all — the guard would pass and the commit
    would still fail. Read the same set the collector reads.

    `auto_created and not concrete` is what isolates reverse relations: forward
    fields are concrete, and the auto primary key is concrete too.

    Wider than the collector's candidate set on purpose — many-to-many relations
    are kept, because the question here is "does anything still reference this
    person", which is broader than "what would the collector delete".
    """
    return [
        field
        for field in User._meta.get_fields(include_hidden=True)
        if field.auto_created and not field.concrete
    ]


def _relations_into_user():
    """Every relation pointing at `User`, split by where its table lives.

    Read off the app registry rather than hard-coded, so a tenant model added
    later is covered the day it exists instead of the day someone remembers to
    add it here.

    An app in neither setting is an error rather than an empty result. Silently
    dropping it would mean a relation nobody scans, which is the one outcome
    this module must never produce — and installing an app by appending it to
    `INSTALLED_APPS` instead of to `SHARED_APPS` is exactly how that happens.
    """
    shared, tenant_local = [], []
    for relation in _reverse_relations_into_user():
        app_label = relation.related_model._meta.app_label
        if app_label in SHARED_APP_LABELS:
            shared.append(relation)
        if app_label in TENANT_APP_LABELS:
            tenant_local.append(relation)
        if app_label not in SHARED_APP_LABELS and app_label not in TENANT_APP_LABELS:
            raise ImproperlyConfigured(
                f"{relation.related_model._meta.label} references accounts.User, but "
                f"its app {app_label!r} is in neither SHARED_APPS nor TENANT_APPS, so "
                "there is no way to know which schema its table lives in. Add it to "
                "whichever it belongs to; appending it to INSTALLED_APPS alone leaves "
                "it unscanned by accounts.deletion."
            )
    return shared, tenant_local


def _count_references(user, relations, schema_name):
    """Rows in the *connected* schema that point at `user`, per relation.

    Deliberately unfiltered by status: an ended membership is still a row, and
    still the family history that makes this delete the wrong move.
    """
    found = []
    for relation in relations:
        # `relation.field.name` is the attribute on the referencing model, which
        # reads the same for a ForeignKey and a ManyToManyField.
        count = (
            relation.related_model._base_manager.filter(**{relation.field.name: user})
            .count()
        )
        if count:
            found.append(Reference(schema_name, relation.related_model, count))
    return found


def find_references(user):
    """Everything anywhere that still points at `user`.

    Visits `public` once for shared models, then every school's schema for
    tenant-local ones. A missing tenant table raises rather than being skipped:
    a schema this scan cannot read is a schema whose references it cannot rule
    out, and guessing "none" there is the failure this module exists to prevent.
    """
    shared, tenant_local = _relations_into_user()
    public_schema = get_public_schema_name()

    with schema_context(public_schema):
        found = _count_references(user, shared, public_schema)

    if tenant_local:
        schema_names = (
            get_tenant_model()
            .objects.exclude(schema_name=public_schema)
            .order_by("schema_name")
            .values_list("schema_name", flat=True)
        )
        for schema_name in schema_names:
            with schema_context(schema_name):
                found.extend(_count_references(user, tenant_local, schema_name))

    return found


def _deletion_schema(tenant_local_relations):
    """Where the delete itself has to run for every table to resolve.

    Not a detail. Django's collector walks *every* relation into `User` before
    it deletes anything, so each referencing table must exist on the
    `search_path` at that moment. From `public` a tenant-local table does not,
    and the delete dies with `relation "..." does not exist` — the guard would
    have passed and the delete would still fail.

    A tenant's `search_path` is `<tenant>, public`, which resolves tenant-local
    and shared tables alike, so it is the only place the whole collection can
    run. Which school it is does not matter: `find_references()` has already
    established there is nothing to collect in any of them. With no tenant
    models pointing here — which is where this codebase stands today — `public`
    is both sufficient and the honest answer.
    """
    public_schema = get_public_schema_name()
    if not tenant_local_relations:
        return public_schema
    schema_name = (
        get_tenant_model()
        .objects.exclude(schema_name=public_schema)
        .order_by("schema_name")
        .values_list("schema_name", flat=True)
        .first()
    )
    return schema_name or public_schema


@transaction.atomic
def hard_delete_user(user):
    """Permanently delete `user`, or refuse and say which schemas are holding it.

    The only sanctioned way to remove a `User` row. Refuses whenever anything
    still references the person — the remedy is `deactivate_user()`, or clearing
    those references first if the row genuinely must go (a duplicate account
    created by mistake, an erasure request).

    Raises `ValidationError` naming every schema that blocked it.
    """
    references = find_references(user)
    if references:
        schemas = {reference.schema_name for reference in references}
        # A list of messages, not one string with newlines in it. ValidationError
        # renders as `repr(self.messages)`, so an embedded "\n" comes back out as
        # a literal backslash-n inside a list repr — the formatting would survive
        # only until someone logged it. Each line is its own message instead, and
        # `exc.messages` hands them back cleanly.
        raise ValidationError(
            [f"Refusing to hard-delete {user}: still referenced in {len(schemas)} schema(s)."]
            + [
                str(reference)
                for reference in sorted(
                    references,
                    key=lambda reference: (reference.schema_name, reference.model._meta.label),
                )
            ]
            + [
                "Deactivate the user instead (accounts.deletion.deactivate_user), "
                "or remove those references first."
            ]
        )

    _, tenant_local = _relations_into_user()
    with schema_context(_deletion_schema(tenant_local)):
        with _sanctioned_delete():
            return user.delete()
