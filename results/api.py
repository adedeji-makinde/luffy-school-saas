"""The broadsheet: a class's marks, averages and positions, for staff.

One endpoint, and its whole design is the audience. **Position is staff-only**,
and that is a rule about who may see the number rather than about where it is
printed:

> Nigerian secondary schools do not print position on report cards. Parents and
> students see the cumulative average only. Schools use position internally, so
> it is computed and (at release) frozen — but it must not reach a parent or a
> student, in a card *or in a payload*.

Enforced here at the **router**, which is the earliest place it can be. A field
omitted from a template while the JSON still carries it is the same leak with an
extra step, and the way that happens is a family-facing view reusing a schema
that was written for staff. There is no schema in this module a family-facing
route could reuse: `PositionOut` and `BroadsheetOut` are only ever produced
behind `_require_position_authority()`.

The parent- and student-facing surfaces — the report card (task 6) and the
unauthenticated result checker — do not exist yet, and when they do they must be
built from their own schemas rather than by filtering these. That is
[issue #21](https://github.com/adedejimakinde/luffy-school-saas/issues/21).

**No school slug in the path**, for the reason `gradebook.api` sets out at
length: these are tenant tables, `TenantMainMiddleware` has already chosen the
schema from the hostname, and a slug would be a second opinion free to disagree
with the connection.
"""

from typing import List, Optional

from django.http import Http404
from django.shortcuts import get_object_or_404
from ninja import Router, Schema

from academics.models import ClassGroup, Term
from accounts.models import Membership, Role
from accounts.session import session_auth
from gradebook.models import Subject

from . import positions

router = Router(auth=session_auth)


#: Who may see a position or a class average.
#:
#: Teachers, the academic vice principal (this codebase's HOD-shaped role),
#: principals and school administrators — the four the decision names. The
#: load-bearing half is who is absent: a **parent** and a **student** are the
#: subjects of this number, not its audience, and a **bursar** keeps the books.
#:
#: Wider than `results.services.APPROVING_ROLES` and narrower than "staff",
#: deliberately. It is a reading right over the whole class, so it is not the
#: same question as who may move the sheet along the chain.
POSITION_VIEWING_ROLES = frozenset(
    {
        Role.TEACHER.value,
        Role.VICE_PRINCIPAL_ACADEMIC.value,
        Role.PRINCIPAL.value,
        Role.ADMIN.value,
    }
)


class SubjectPlacingOut(Schema):
    """One child in one subject. `position` is the staff-only half."""

    subject_id: int
    subject: str
    percentage: Optional[str]
    position: Optional[int]


class PlacingOut(Schema):
    """One child's row on the broadsheet.

    `percentage` and `position` are `Optional` together and are null together:
    that pair is how "this child has no marks" reaches the screen, rather than a
    zero that would claim they sat exams and scored nothing. `gradebook.ScoreOut`
    keeps the same distinction for the same reason.

    Percentages are strings, not floats. They are `Decimal` all the way through
    `positions` because ties are decided by equality, and handing them to JSON
    as floats would undo that at the last step for anybody who compares them.
    """

    student_membership_id: int
    student: str
    average: Optional[str]
    position: Optional[int]
    subjects: List[SubjectPlacingOut]


class BroadsheetOut(Schema):
    class_group: str
    term: str
    #: Computed on demand and never stored — see `positions.class_average()`.
    #: Staff-only, like `position`, and for the same reason.
    class_average: Optional[str]
    rows: List[PlacingOut]


def _school_of(request):
    """The school whose schema this request is already on.

    `None` is the portal host, where these tables do not exist at all — so a
    404 rather than a 403, the answer `gradebook.api._school_of()` gives and for
    the same reason: on the portal there is no such route, because there is no
    such broadsheet.
    """
    school = getattr(request, "school", None)
    if school is None:
        raise Http404("No results on this host.")
    return school


def _require_position_authority(actor, school):
    """A flat 404, not a 403, and that is the disclosure decision.

    `gradebook.api`'s `ExistenceOracleTests` settled this for the codebase: a
    caller who may not read this must not be able to tell a class that exists
    from one that does not by which refusal comes back. A parent probing class
    ids would otherwise map the school's whole class list.

    Asked before the class or term is looked up, so the refusal cannot depend on
    whether they exist.

    The unauthenticated branch is belt and braces: the router's `session_auth`
    answers first, with a 401, and never reaches this. It stays because the
    result checker this module will grow is `auth=None` by definition, and the
    day somebody adds it is the day this function stops being unreachable. A 401
    is no oracle either — it comes back whether or not the class exists.
    """
    if not getattr(actor, "is_authenticated", False):
        raise Http404("No such broadsheet.")
    if not set(actor.roles_at(school)) & POSITION_VIEWING_ROLES:
        raise Http404("No such broadsheet.")


def _named(student_ids) -> dict:
    """`membership id -> the child's name`, in one query."""
    return {
        membership.pk: membership.user.full_name or membership.user.username
        for membership in Membership.objects.select_related("user").filter(
            pk__in=student_ids
        )
    }


def _as_text(value) -> Optional[str]:
    return None if value is None else str(value)


@router.get(
    "/classes/{class_group_id}/broadsheet/",
    response=BroadsheetOut,
    tags=["results"],
)
def broadsheet(request, class_group_id: int, term_id: int):
    """A class's term: every child's subject percentages, averages and positions.

    Read from live marks. There is no frozen snapshot yet — that is task 3 — and
    once there is, a *released* term must be served from it rather than from
    here, because a position recomputed after release can silently disagree with
    the card a parent is holding. `positions.class_average()` says which of the
    two numbers is frozen and which is recomputed, and why they differ.
    """
    school = _school_of(request)
    _require_position_authority(request.user, school)

    class_group = get_object_or_404(ClassGroup, pk=class_group_id)
    term = get_object_or_404(Term, pk=term_id)

    student_ids = positions.roster_ids(class_group, term)
    names = _named(student_ids)
    averages = positions.overall_percentages(class_group, term)
    overall = positions.dense_positions(averages)

    subjects = list(Subject.objects.all())
    per_subject = {
        subject.pk: (
            positions.subject_percentages(class_group, term, subject.pk),
            positions.subject_positions(class_group, term, subject.pk),
        )
        for subject in subjects
    }

    rows = []
    for student_id in student_ids:
        rows.append(
            PlacingOut(
                student_membership_id=student_id,
                student=names.get(student_id, "—"),
                average=_as_text(averages.get(student_id)),
                position=overall.get(student_id),
                subjects=[
                    SubjectPlacingOut(
                        subject_id=subject.pk,
                        subject=subject.name,
                        percentage=_as_text(per_subject[subject.pk][0].get(student_id)),
                        position=per_subject[subject.pk][1].get(student_id),
                    )
                    for subject in subjects
                ],
            )
        )

    return BroadsheetOut(
        class_group=str(class_group),
        term=str(term),
        class_average=_as_text(positions.class_average(class_group, term)),
        rows=rows,
    )


__all__ = ["POSITION_VIEWING_ROLES", "router"]
