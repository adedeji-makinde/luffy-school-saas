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
    MEMBERSHIP_GRANTING_ROLES,
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


class NotPermitted(MembershipError):
    """The actor has no authority to grant memberships at that school."""


def can_grant_memberships(actor, school) -> bool:
    """May `actor` hand out memberships at `school`?

    A school administrator's authority stops at their own school — an admin at
    St Mary's cannot enrol anyone at Grace Academy. Only platform staff act
    across schools.

    Access-scoped: an admin who is merely invited, or who has been suspended,
    grants nothing.
    """
    if getattr(actor, "is_platform_staff", False):
        return True
    return actor.memberships.with_access().filter(
        school=school, role__in=MEMBERSHIP_GRANTING_ROLES
    ).exists()


def _require_grant_authority(actor, school):
    if not can_grant_memberships(actor, school):
        raise NotPermitted(f"{actor} cannot grant memberships at {school}.")


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


# ---------------------------------------------------------------------------
# Actor-checked entry points.
#
# The functions above are primitives: they keep the data consistent but ask no
# questions about who is calling, which is what lets link_guardian() grant a
# PARENT membership on its own. Anything driven by a request should come
# through here instead, so a school administrator's reach stays inside their
# own school.
# ---------------------------------------------------------------------------


@transaction.atomic
def grant_membership_as(actor, user, school, role, **kwargs):
    _require_grant_authority(actor, school)
    return grant_membership(user, school, role, **kwargs)


@transaction.atomic
def enroll_student_as(actor, user, school, **kwargs):
    _require_grant_authority(actor, school)
    return enroll_student(user, school, **kwargs)


@transaction.atomic
def link_guardian_as(actor, guardian, student, **kwargs):
    # Authority is needed at the child's school, because linking grants the
    # parent a membership there.
    _require_grant_authority(actor, student.school)
    return link_guardian(guardian, student, **kwargs)


@transaction.atomic
def unlink_guardian_as(actor, guardian, student):
    _require_grant_authority(actor, student.school)
    return unlink_guardian(guardian, student)


@transaction.atomic
def transfer_student_as(actor, student, to_school, **kwargs):
    # A transfer ends a membership at one school and opens one at another, so
    # it needs authority at both ends — in practice platform staff, or an admin
    # who happens to hold a membership at both schools.
    _require_grant_authority(actor, student.school)
    _require_grant_authority(actor, to_school)
    return transfer_student(student, to_school, **kwargs)


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
    """Everyone at one school, optionally filtered to a single role.

    Relationship-scoped on purpose, so invited and suspended people appear
    alongside active ones. A school's own directory should show who is pending
    and who is suspended rather than hiding them — that is the roster the office
    works from. Do not narrow this to `with_access()`; access and visibility are
    different questions.
    """
    qs = Membership.objects.for_school(school).live().select_related("user")
    return qs.filter(role=role) if role else qs
