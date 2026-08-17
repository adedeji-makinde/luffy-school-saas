"""Write operations on memberships.

The model enforces what Postgres can enforce. The rules that span tables live
here, so callers never have to remember them:

  * linking a child to a parent gives that parent a PARENT membership at the
    child's school, creating it if this is their first child there;
  * unlinking their last child at a school ends that membership;
  * transferring a student carries the guardians across.
"""

from django.db import transaction

from .models import (
    LIVE_STATUSES,
    Guardianship,
    Membership,
    MembershipStatus,
    Relationship,
    Role,
    User,
)


class MembershipError(Exception):
    """A membership rule was violated."""


class AlreadyEnrolled(MembershipError):
    pass


class NotAStudent(MembershipError):
    pass


@transaction.atomic
def grant_membership(user, school, role, *, status=MembershipStatus.ACTIVE, **fields):
    """Give `user` a `role` at `school`, reviving an ended membership if there is one.

    Idempotent: calling it twice returns the same row.
    """
    membership = (
        Membership.objects.select_for_update().filter(user=user, school=school, role=role).first()
    )
    if membership is None:
        return Membership.objects.create(
            user=user, school=school, role=role, status=status, **fields
        )
    if not membership.is_live:
        membership.status = status
        membership.ended_on = None
        for key, value in fields.items():
            setattr(membership, key, value)
        membership.save()
    return membership


@transaction.atomic
def enroll_student(user, school, *, reference="", **fields):
    """Enrol `user` as a student at `school`.

    A student has exactly one school, so this refuses if they are already
    enrolled somewhere else. Use `transfer_student` for a move.
    """
    existing = (
        Membership.objects.select_for_update()
        .filter(user=user, role=Role.STUDENT, status__in=LIVE_STATUSES)
        .first()
    )
    if existing is not None and existing.school_id != school.pk:
        raise AlreadyEnrolled(
            f"{user} is already enrolled at {existing.school}. Transfer them instead."
        )
    return grant_membership(user, school, Role.STUDENT, reference=reference, **fields)


@transaction.atomic
def link_guardian(
    guardian,
    student,
    *,
    relationship=Relationship.GUARDIAN,
    is_primary_contact=False,
    receives_invoices=True,
    can_collect=True,
):
    """Link a parent's login to one child.

    Also ensures the parent holds a PARENT membership at that child's school —
    this is how one login comes to span several schools: each linked child adds
    the school it belongs to.
    """
    if student.role != Role.STUDENT:
        raise NotAStudent("A guardianship must point at a STUDENT membership.")
    if guardian.pk == student.user_id:
        raise MembershipError("A student cannot be their own guardian.")

    grant_membership(guardian, student.school, Role.PARENT)

    if is_primary_contact:
        Guardianship.objects.filter(student=student, is_primary_contact=True).update(
            is_primary_contact=False
        )

    link, created = Guardianship.objects.get_or_create(
        guardian=guardian,
        student=student,
        defaults={
            "relationship": relationship,
            "is_primary_contact": is_primary_contact,
            "receives_invoices": receives_invoices,
            "can_collect": can_collect,
        },
    )
    if not created and is_primary_contact and not link.is_primary_contact:
        link.is_primary_contact = True
        link.save(update_fields=["is_primary_contact"])
    return link


@transaction.atomic
def unlink_guardian(guardian, student):
    """Remove a parent's link to a child.

    If that was their last child at the school, their PARENT membership there
    ends too — a login should not retain access to a school it has no reason
    to reach. Memberships in other roles at that school are left alone.
    """
    Guardianship.objects.filter(guardian=guardian, student=student).delete()

    still_has_children = Guardianship.objects.filter(
        guardian=guardian,
        student__school=student.school,
        student__status__in=LIVE_STATUSES,
    ).exists()
    if not still_has_children:
        Membership.objects.filter(
            user=guardian, school=student.school, role=Role.PARENT, status__in=LIVE_STATUSES
        ).update(status=MembershipStatus.ENDED)


@transaction.atomic
def transfer_student(student, to_school, *, reference=""):
    """Move a student to another school, carrying their guardians with them.

    Ends the old membership (keeping it as history), opens a new one, and
    re-links every guardian — which in turn grants them a PARENT membership at
    the new school and drops the old one if no other child keeps them there.
    """
    if student.role != Role.STUDENT:
        raise NotAStudent("Only a STUDENT membership can be transferred.")
    if student.school_id == to_school.pk:
        return student

    guardians = list(
        Guardianship.objects.filter(student=student).select_related("guardian")
    )

    # End first: the partial unique index allows only one live STUDENT row.
    student.end()

    new_membership = grant_membership(
        student.user,
        to_school,
        Role.STUDENT,
        reference=reference or student.reference,
        display_name=student.display_name,
    )

    for link in guardians:
        link_guardian(
            link.guardian,
            new_membership,
            relationship=link.relationship,
            is_primary_contact=link.is_primary_contact,
            receives_invoices=link.receives_invoices,
            can_collect=link.can_collect,
        )
        # Drop access to the old school unless another child is still there.
        unlink_guardian(link.guardian, student)

    return new_membership


def parent_dashboard(guardian):
    """Every child of `guardian`, grouped by school — the one-login view.

    Returns [(school, [student memberships]), ...] ordered by school name.
    A single query against the public schema, so a parent with children at
    three schools pays the same cost as a parent with one.
    """
    children = list(guardian.children())
    grouped: dict = {}
    for child in children:
        grouped.setdefault(child.school, []).append(child)
    return sorted(grouped.items(), key=lambda pair: pair[0].name)


def school_directory(school, *, role=None):
    """Everyone at one school, optionally filtered to a single role."""
    qs = Membership.objects.for_school(school).live().select_related("user")
    return qs.filter(role=role) if role else qs
