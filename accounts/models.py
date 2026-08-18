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

from .identifiers import canonical_username, normalize_email, normalize_phone, try_normalize_phone


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


#: Statuses that still tie a person to a school — everything but ended history.
#: A graduated or transferred student keeps the row but frees the constraint.
#: This is the "does the relationship exist?" predicate: it decides whether a
#: student's single-school slot is occupied and whether a child shows up on
#: their parent's dashboard.
LIVE_STATUSES = frozenset(
    {MembershipStatus.INVITED.value, MembershipStatus.ACTIVE.value, MembershipStatus.SUSPENDED.value}
)

#: Statuses that let a person actually act at a school. Deliberately narrower:
#: an invitation is an offer rather than access, and a suspension withdraws it.
#: Both still occupy the relationship above — see LIVE_STATUSES. Keeping these
#: two predicates apart is what lets a parent see an invited child before that
#: child can sign in.
ACCESS_STATUSES = frozenset({MembershipStatus.ACTIVE.value})


class Relationship(models.TextChoices):
    MOTHER = "mother", "Mother"
    FATHER = "father", "Father"
    GUARDIAN = "guardian", "Guardian"
    OTHER = "other", "Other"


class UserManager(BaseUserManager):
    def create_user(self, username, password=None, *, email=None, phone=None, **extra):
        if not username:
            raise ValueError("A username is required.")
        # save() normalizes username/email/phone, so raw input is fine here.
        user = self.model(username=username, email=email, phone=phone, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def matching_identifier(self, identifier):
        """Every user `identifier` could refer to, as one query.

        Checks username and email case-insensitively, and phone by its
        normalized value when `identifier` is phone-shaped. This is the
        single source of truth for identifier resolution: both
        User.assert_identifiers_unambiguous() and IdentifierBackend call
        this, so the collision rule and the sign-in resolution can never
        drift apart.
        """
        identifier = (identifier or "").strip()
        if not identifier:
            return self.none()
        query = Q(username__iexact=identifier) | Q(email__iexact=identifier)
        phone = try_normalize_phone(identifier)
        if phone:
            query |= Q(phone=phone)
        return self.filter(query)

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
            "students get a school-issued handle such as STM/2026/0042. A "
            "username that is itself a phone number is stored in E.164, "
            "matching the phone field."
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
        self.normalize_identifiers()
        update_fields = kwargs.get("update_fields")
        # update_last_login fires on every sign-in and only ever touches
        # last_login, so it must not pay for a collision check that can't
        # possibly apply to it. Any save that could touch an identifier
        # column — including a plain save() with no update_fields at all —
        # still runs the check.
        if update_fields is None or set(update_fields) & {"username", "email", "phone"}:
            self.assert_identifiers_unambiguous()
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        self.normalize_identifiers()
        self.assert_identifiers_unambiguous()

    def normalize_identifiers(self):
        self.username = canonical_username(self.username)
        self.email = normalize_email(self.email)
        self.phone = normalize_phone(self.phone)

    def assert_identifiers_unambiguous(self):
        """Option C: refuse to save if an identifier of this user's already
        resolves to a different account.

        Application-level, not (yet) a database constraint, and racy by
        construction: two concurrent saves can both pass this SELECT before
        either commits, because there is no row yet for either to lock
        against. Only this cross-column comparison rides on that race —
        same-column uniqueness (two users sharing one phone, say) is still
        enforced by real unique indexes regardless of this check. Acceptable
        pre-launch because the failure mode is safe: a genuinely ambiguous
        identifier is refused at sign-in (see IdentifierBackend), never
        silently resolved to the wrong person. The upgrade path is a
        UserIdentifier(kind, canonical_value) table with one unique index
        spanning all three kinds — a change of mechanism, not of rule.
        """
        for value in filter(None, {self.username, self.email, self.phone}):
            matches = User.objects.matching_identifier(value)
            if self.pk is not None:
                matches = matches.exclude(pk=self.pk)
            if matches.exists():
                raise ValidationError(f"{value!r} is already in use by another account.")

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
        """Every relationship that still exists, invited and suspended included."""
        return self.memberships.live().select_related("school")

    def schools(self):
        """Every school this login can act at."""
        from schools.models import School

        return School.objects.filter(
            memberships__user=self, memberships__status__in=ACCESS_STATUSES
        ).distinct()

    def roles_at(self, school) -> set:
        """Roles this login may currently exercise at `school`.

        Access-scoped, so an invited or suspended person has no roles here even
        though the membership exists. Authorisation reads this.
        """
        return set(
            self.memberships.with_access()
            .filter(school=school)
            .values_list("role", flat=True)
        )

    def has_access_to(self, school) -> bool:
        return (
            self.is_platform_staff
            or self.memberships.with_access().filter(school=school).exists()
        )

    def children(self):
        """Every child this login guards, across every school.

        One query, no schema switching — this is what lets a parent with kids
        at two schools see all of them from one login.

        Scoped to LIVE_STATUSES rather than ACCESS_STATUSES on purpose: a parent
        should see an invited child on their dashboard before that child can
        sign in themselves.
        """
        return (
            Membership.objects.filter(
                guardianships__guardian=self, status__in=LIVE_STATUSES
            )
            .select_related("user", "school")
            .order_by("school__name", "user__full_name")
        )

    def student_membership(self):
        """A student has exactly one school, so this is singular by design.

        Relationship-scoped: an invited student already belongs to their school.
        """
        return self.memberships.live().filter(role=Role.STUDENT).select_related("school").first()


class MembershipQuerySet(models.QuerySet):
    def live(self):
        """Relationships that still exist — everything but ended history.

        Includes invited and suspended people, who hold a place at the school
        without being able to act there. For "may they do things?" use
        `with_access()`.
        """
        return self.filter(status__in=LIVE_STATUSES)

    def with_access(self):
        """Memberships that let the person act at the school. Active only."""
        return self.filter(status__in=ACCESS_STATUSES)

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
    # PROTECT, not CASCADE: these rows are the family history. Deleting a school
    # must not quietly take enrolments and guardianships with it as a side
    # effect. Ending a membership (status='ended') is the supported way to close
    # a relationship, and ended rows still block the delete — that is the point.
    school = models.ForeignKey(
        "schools.School", related_name="memberships", on_delete=models.PROTECT
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
        """The relationship exists — not necessarily usable. See grants_access."""
        return self.status in LIVE_STATUSES

    @property
    def grants_access(self) -> bool:
        return self.status in ACCESS_STATUSES

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

    # Both sides PROTECT. These rows are family history, and no delete of a
    # person or a membership should erase them as a side effect. Call
    # services.unlink_guardian() first — it keeps both sides in step.
    guardian = models.ForeignKey(
        User, related_name="guardianships", on_delete=models.PROTECT
    )
    student = models.ForeignKey(
        Membership,
        related_name="guardianships",
        on_delete=models.PROTECT,
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
