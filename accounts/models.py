"""Identity and school access.

Everything here lives in the public schema, once for the whole platform.

The shape to keep in mind:

    User            a person, one login, no role of its own
    Membership      (person, school, role) — the only place a role exists
    Guardianship    (parent user, a child's STUDENT membership)

A person is not "a teacher"; a person *is a teacher at a school*, and may
simultaneously be a parent at that school and at two others. So role is an
attribute of the relationship, never of the user.
"""

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Role(models.TextChoices):
    """Every role that gets a login. There is no flat "staff" role."""

    ADMIN = "admin", "School administrator"
    PRINCIPAL = "principal", "Principal"
    TEACHER = "teacher", "Teacher"
    BURSAR = "bursar", "Bursar"
    PARENT = "parent", "Parent or guardian"
    STUDENT = "student", "Student"


# Role groupings for permission checks — not a second source of truth.
# These hold role *values*, which is what the database hands back. Role members
# are interchangeable with them: TextChoices mixes in str, so a member hashes
# and compares by value. (A plain enum.Enum would hash by name and silently
# fail set membership here.)
STAFF_ROLES = frozenset(
    {Role.ADMIN.value, Role.PRINCIPAL.value, Role.TEACHER.value, Role.BURSAR.value}
)
FAMILY_ROLES = frozenset({Role.PARENT.value, Role.STUDENT.value})
# Every role except STUDENT may be held at several schools at once.
SINGLE_SCHOOL_ROLES = frozenset({Role.STUDENT.value})
# Roles that may hand out memberships — at their own school only, never
# platform-wide. Principals are deliberately not included; add them here if
# that changes. Cross-school authority belongs to User.is_platform_staff.
MEMBERSHIP_GRANTING_ROLES = frozenset({Role.ADMIN.value})


def is_staff_role(role) -> bool:
    return role in STAFF_ROLES


def is_family_role(role) -> bool:
    return role in FAMILY_ROLES


class MembershipStatus(models.TextChoices):
    INVITED = "invited", "Invited"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    ENDED = "ended", "Ended"


#: Statuses that still tie a person to a school. `ENDED` is history: a
#: graduated or transferred student keeps the row but frees the constraint.
LIVE_STATUSES = frozenset(
    {MembershipStatus.INVITED.value, MembershipStatus.ACTIVE.value, MembershipStatus.SUSPENDED.value}
)


class Relationship(models.TextChoices):
    MOTHER = "mother", "Mother"
    FATHER = "father", "Father"
    GUARDIAN = "guardian", "Guardian"
    OTHER = "other", "Other"


class UserManager(BaseUserManager):
    def _normalize(self, email, phone):
        # Blank strings would collide under the unique indexes; NULLs do not.
        return (self.normalize_email(email).lower() or None if email else None, phone or None)

    def create_user(self, username, password=None, *, email=None, phone=None, **extra):
        if not username:
            raise ValueError("A username is required.")
        email, phone = self._normalize(email, phone)
        user = self.model(username=username, email=email, phone=phone, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, *, email=None, phone=None, **extra):
        extra.setdefault("is_platform_staff", True)
        extra.setdefault("is_superuser", True)
        if not extra["is_platform_staff"] or not extra["is_superuser"]:
            raise ValueError("A superuser must be platform staff and a superuser.")
        return self.create_user(username, password, email=email, phone=phone, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """One person, one login, for the life of their relationship with the platform.

    Carries no role and no school. A student who becomes a teacher years later
    keeps this row; a parent with children at three schools has exactly one.
    """

    username = models.CharField(
        max_length=150,
        unique=True,
        help_text=(
            "Stable sign-in handle. Staff and parents usually get their email; "
            "students get a school-issued handle such as STM/2026/0042."
        ),
    )
    # Optional: a young student may have neither. Unique when present.
    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=32, unique=True, null=True, blank=True)

    full_name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=100, blank=True)

    is_active = models.BooleanField(default=True)
    # The SaaS operator — us, not a school. School authority comes from
    # Membership.role; this flag is only for platform-wide access.
    is_platform_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name or self.username

    def save(self, *args, **kwargs):
        self.email = (self.email or "").lower() or None
        self.phone = self.phone or None
        super().save(*args, **kwargs)

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        if self.short_name:
            return self.short_name
        return self.full_name.split(" ")[0] if self.full_name else self.username

    @property
    def is_staff(self):
        """Only for django.contrib.admin's login check.

        "Staff" in this codebase means a school staff role (see STAFF_ROLES),
        which is a Membership concern and has nothing to do with this.
        """
        return self.is_platform_staff

    # -- access questions, all answerable without leaving the public schema --

    def live_memberships(self):
        return self.memberships.live().select_related("school")

    def schools(self):
        """Every school this login can reach, in any capacity."""
        from schools.models import School

        return School.objects.filter(
            memberships__user=self, memberships__status__in=LIVE_STATUSES
        ).distinct()

    def roles_at(self, school) -> set:
        return set(
            self.memberships.live().filter(school=school).values_list("role", flat=True)
        )

    def has_access_to(self, school) -> bool:
        return self.is_platform_staff or self.memberships.live().filter(school=school).exists()

    def children(self):
        """Every child this login guards, across every school.

        One query, no schema switching — this is what lets a parent with kids
        at two schools see all of them from one login.
        """
        return (
            Membership.objects.filter(
                guardianships__guardian=self, status__in=LIVE_STATUSES
            )
            .select_related("user", "school")
            .order_by("school__name", "user__full_name")
        )

    def student_membership(self):
        """A student has exactly one school, so this is singular by design."""
        return self.memberships.live().filter(role=Role.STUDENT).select_related("school").first()


class MembershipQuerySet(models.QuerySet):
    def live(self):
        """Memberships that still grant access."""
        return self.filter(status__in=LIVE_STATUSES)

    def active(self):
        return self.filter(status=MembershipStatus.ACTIVE)

    def staff(self):
        return self.filter(role__in=STAFF_ROLES)

    def family(self):
        return self.filter(role__in=FAMILY_ROLES)

    def students(self):
        return self.filter(role=Role.STUDENT)

    def parents(self):
        return self.filter(role=Role.PARENT)

    def for_school(self, school):
        return self.filter(school=school)


class Membership(models.Model):
    """One person's standing at one school, in one capacity.

    Multiple rows per (user, school) are expected and correct: the maths
    teacher whose daughter attends the same school holds a TEACHER membership
    and a PARENT membership there.
    """

    user = models.ForeignKey(User, related_name="memberships", on_delete=models.CASCADE)
    school = models.ForeignKey(
        "schools.School", related_name="memberships", on_delete=models.CASCADE
    )
    role = models.CharField(max_length=16, choices=Role)
    status = models.CharField(
        max_length=16, choices=MembershipStatus, default=MembershipStatus.ACTIVE
    )

    # What this school calls them — a school may know a parent by a different
    # name than the one on their login.
    display_name = models.CharField(max_length=255, blank=True)
    # School-issued identifier: admission number, staff number.
    reference = models.CharField(max_length=64, blank=True)

    started_on = models.DateField(default=timezone.localdate)
    ended_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = MembershipQuerySet.as_manager()

    class Meta:
        ordering = ["school__name", "role", "user__full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "school", "role"],
                name="uniq_membership_user_school_role",
            ),
            # A student has exactly one school. Enforced in the database, and
            # global rather than per-school precisely because Membership is
            # shared: a second live STUDENT row anywhere is rejected. Ended
            # rows are excluded so transfers and graduations keep their history.
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(role="student") & ~Q(status="ended"),
                name="one_live_student_membership_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "role", "status"]),
            models.Index(fields=["user", "role"]),
            models.Index(fields=["school", "reference"]),
        ]

    def __str__(self):
        return f"{self.user} — {self.get_role_display()} at {self.school}"

    def clean(self):
        if self.role and not is_staff_role(self.role) and not is_family_role(self.role):
            raise ValidationError({"role": f"Unknown role {self.role!r}."})

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def is_staff_role(self) -> bool:
        return is_staff_role(self.role)

    @property
    def name(self) -> str:
        return self.display_name or self.user.full_name

    def guardians(self):
        """Who may see this student. Empty for non-student memberships."""
        return User.objects.filter(guardianships__student=self).distinct()

    def end(self, on=None, *, save=True):
        self.status = MembershipStatus.ENDED
        self.ended_on = on or timezone.localdate()
        if save:
            self.save(update_fields=["status", "ended_on", "updated_at"])
        return self


class Guardianship(models.Model):
    """Links a parent's login to one child.

    Points at the child's STUDENT *membership* rather than their user, because
    that single foreign key pins both the child and the school they attend.
    A parent with three children at two schools has three rows here and two
    PARENT memberships.
    """

    guardian = models.ForeignKey(
        User, related_name="guardianships", on_delete=models.CASCADE
    )
    student = models.ForeignKey(
        Membership,
        related_name="guardianships",
        on_delete=models.CASCADE,
        help_text="The child's STUDENT membership, which pins the school too.",
    )
    relationship = models.CharField(
        max_length=16, choices=Relationship, default=Relationship.GUARDIAN
    )
    is_primary_contact = models.BooleanField(default=False)
    receives_invoices = models.BooleanField(default=True)
    can_collect = models.BooleanField(default=True, help_text="Authorised for pickup.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["student__user__full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["guardian", "student"], name="uniq_guardianship_guardian_student"
            ),
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(is_primary_contact=True),
                name="one_primary_contact_per_student",
            ),
        ]

    def __str__(self):
        return f"{self.guardian} → {self.student.name}"

    def clean(self):
        # Cross-table rules Postgres cannot express as a constraint.
        if self.student_id and self.student.role != Role.STUDENT:
            raise ValidationError(
                {"student": "A guardianship must point at a STUDENT membership."}
            )
        if self.guardian_id and self.student_id and self.guardian_id == self.student.user_id:
            raise ValidationError({"guardian": "A student cannot be their own guardian."})

    @property
    def school(self):
        return self.student.school
