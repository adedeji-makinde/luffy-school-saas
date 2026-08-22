"""Identity and school access.

Everything here lives in the public schema, once for the whole platform.

The shape to keep in mind:

    User            a person, one login, no role of its own
    Membership      (person, school, role) — the only place a role exists
    Guardianship    (parent user, a child's STUDENT membership)
    TransferRequest one school's proposal to move a child to another, and the
                    other school's answer — two signatures, one transfer

A person is not "a teacher"; a person *is a teacher at a school*, and may
simultaneously be a parent at that school and at two others. So role is an
attribute of the relationship, never of the user.
"""

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

from .identifiers import canonical_username, normalize_email, normalize_phone, try_normalize_phone


class Role(models.TextChoices):
    """Every role that gets a login. There is no flat "staff" role."""

    ADMIN = "admin", "School administrator"
    PRINCIPAL = "principal", "Principal"
    #: The academic checker between the class teacher and the principal in the
    #: results approval chain. Named for the scope that exists: the chain runs
    #: per (class, term) and carries no subject, so a head of department — who
    #: is head *of a subject area* — would have nothing here to be head of.
    #:
    #: The stored value is short because `Membership.role` is `max_length=16`
    #: and "vice_principal_academic" is 23 characters. The value is an internal
    #: key like "admin" and "bursar"; the label is what anybody actually reads.
    VICE_PRINCIPAL_ACADEMIC = "vp_academic", "Vice Principal (Academic)"
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
    {
        Role.ADMIN.value,
        Role.PRINCIPAL.value,
        Role.VICE_PRINCIPAL_ACADEMIC.value,
        Role.TEACHER.value,
        Role.BURSAR.value,
    }
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


class TransferError(Exception):
    """A transfer could not be requested, accepted or called off as asked.

    The base for every refusal in the handshake, on both sides of the seam: the
    states this module enforces (already resolved, the enrolment moved on) and
    the ones `transfers.py` enforces (wrong side, nothing to answer). It lives
    here rather than there because the dependency already runs that way —
    `transfers.py` imports this module, not the reverse — so one
    `except TransferError` catches the whole flow.

    Deliberately not a subclass of `services.MembershipError`: that hierarchy
    lives in `services.py`, which imports *this* module, and reaching the other
    way for a base class would be a circular import. `NotPermitted` is still
    raised from `services` for authority, exactly as the invitation flow does.
    """


class TransferAlreadyResolved(TransferError):
    """This request has already been accepted, declined or withdrawn."""


class EnrolmentMovedOn(TransferError):
    """The enrolment this request would move is no longer live.

    Its own type because the fix differs: nothing is wrong with the request, and
    nobody did anything wrong. The child left by another route — released
    without a destination, graduated, transferred by platform staff — while this
    sat pending, so there is nothing left to hand over.
    """


class TransferSide(models.TextChoices):
    """Which end of a transfer somebody is acting from.

    Two schools, and every act in the handshake belongs to exactly one of them.
    Naming the side rather than the school is what keeps the rules readable: the
    requester signs for their own side, the answerer for the other, and neither
    statement has to mention which school is which.
    """

    RELEASING = "releasing", "Releasing school"
    RECEIVING = "receiving", "Receiving school"

    @property
    def other(self) -> "TransferSide":
        return (
            TransferSide.RECEIVING
            if self == TransferSide.RELEASING
            else TransferSide.RELEASING
        )


class TransferRoute(models.TextChoices):
    """How a transfer came to happen — and therefore what this row is evidence of.

    Set by the code path that wrote the row, never by a caller: neither
    `request_transfer_as()` nor `transfer_student_as()` accepts it as an
    argument, so a row cannot claim to be something it is not. The check
    constraints below make the same guarantee at the database level, so it holds
    against a shell session and a data migration too.

    HANDSHAKE     Two schools agreed. Two distinct signatories, `requested_side`
                  says who spoke first, and the row passed through `pending`.
    SINGLE_PARTY  One actor held authority at both ends and did the whole thing
                  through `services.transfer_student_as()`. There was never a
                  second party, so the row names one person twice and has no
                  side — and says so rather than dressing itself up as consent
                  that nobody gave.
    """

    HANDSHAKE = "handshake", "Agreed between two schools"
    SINGLE_PARTY = "single_party", "Carried out by one authority"


class TransferRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    WITHDRAWN = "withdrawn", "Withdrawn"


class TransferRequestQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=TransferRequestStatus.PENDING)

    def for_school(self, school):
        """Everything either side of `school` — outgoing and incoming alike."""
        return self.filter(Q(student__school=school) | Q(to_school=school))

    def awaiting(self, school):
        """Pending requests it is `school`'s turn to answer, and can still answer.

        The other side asked; this side has not replied. Note it is keyed off
        which side *requested*, not off which school appears where: a school
        both receives requests to admit and receives requests to release, and
        the only thing separating them is who spoke first.

        The live-enrolment filter is what keeps this list honest. A request whose
        child left by another route stays PENDING — nobody declined it, and
        rewriting its status to pretend otherwise would put a lie in the record
        this table exists to be. But it can never be accepted again, so showing
        it as work waiting on a school would be its own kind of lie. It drops
        out of the queue and keeps its status.
        """
        return self.pending().filter(
            Q(student__status__in=LIVE_STATUSES)
            & (
                Q(requested_side=TransferSide.RELEASING, to_school=school)
                | Q(requested_side=TransferSide.RECEIVING, student__school=school)
            )
        )


class TransferRequest(models.Model):
    """One school's proposal to move a child to another, and the other's answer.

    `release_student_as()` and `enroll_student_as()` already let two schools move
    a child without either writing at the other, but they are two unconnected
    acts: nothing ties a release to the admission it was meant for, nothing
    records that anyone agreed, and between them the child belongs to no school.
    This is what connects them.

    **The handshake assembles two-sided authority out of two one-sided acts.**
    That is the whole idea, and it is worth stating plainly. `transfer_student()`
    genuinely needs authority at both ends — it ends a membership at one school
    and opens one at the other. Requiring one caller to hold both is what made
    ordinary transfers impossible. So one school signs by *requesting*, the other
    signs by *accepting*, and only when both signatures exist does the transfer
    run — in a single transaction, which is what closes the window the two-act
    path leaves open. Neither side ever acted at the other's school; the pair of
    consents did.

    Either side may initiate. A releasing school saying "we are letting this
    child go to Grace Academy" and a receiving school saying "we would like to
    admit this child from St Mary's" are the same proposal from opposite ends,
    and which one happens first is a fact about the family, not about the model.
    `requested_side` records which it was, because "who asked" is the first
    question anyone will have when a transfer is disputed.

    Points at the child's STUDENT `Membership` rather than repeating the child
    and the school they are leaving. That one foreign key pins both, and it
    cannot drift out of step with the enrolment it is proposing to move — which
    two loose columns could. `student_user` and `from_school` read them back off
    it.
    """

    student = models.ForeignKey(
        Membership,
        related_name="transfer_requests",
        on_delete=models.PROTECT,
        help_text="The child's STUDENT membership, which pins the leaving school too.",
    )
    to_school = models.ForeignKey(
        "schools.School", related_name="incoming_transfer_requests", on_delete=models.PROTECT
    )

    requested_by = models.ForeignKey(
        User, related_name="transfer_requests_made", on_delete=models.PROTECT
    )
    #: Which end asked. Not derivable after the fact: an admin can hold
    #: authority at both schools, and by the time anyone reads this row the
    #: memberships that granted it may have changed.
    #:
    #: Null on a SINGLE_PARTY row, and null rather than a default, because there
    #: was no side: one actor held both ends. Writing RELEASING there to avoid a
    #: nullable column would be inventing a fact, and this table's whole purpose
    #: is to be believed. A constraint below ties the two fields together.
    requested_side = models.CharField(
        max_length=16, choices=TransferSide, null=True, blank=True
    )
    requested_at = models.DateTimeField(auto_now_add=True)

    #: Whether two schools agreed or one authority acted alone. See TransferRoute.
    route = models.CharField(
        max_length=16, choices=TransferRoute, default=TransferRoute.HANDSHAKE
    )

    #: Who took this out of `pending`, and when. Null while it is still open.
    #:
    #: "Resolved", not "answered", and the difference is load-bearing. An accept
    #: or a decline is an *answer*, and comes from the other side of the table by
    #: definition. A withdrawal comes from the side that asked, and may well be
    #: the same person who asked — so a column called `answered_by` invited the
    #: rule "these two names always differ", which is false for exactly that
    #: case. The constraint below states the narrower thing that is actually
    #: true: an *answered* transfer names two people.
    resolved_by = models.ForeignKey(
        User,
        related_name="transfer_requests_answered",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=16, choices=TransferRequestStatus, default=TransferRequestStatus.PENDING
    )
    #: What the receiving school will call the child. Carried on the request so
    #: it can be offered when the receiving school initiates, and supplied at
    #: acceptance otherwise; either way it is the receiving school's to set.
    reference = models.CharField(max_length=64, blank=True)
    note = models.TextField(blank=True, help_text="Free text, shown to both schools.")

    objects = TransferRequestQuerySet.as_manager()

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            # At most one pending request per enrolment, in the database rather
            # than in application code — the same spirit as the one-school slot
            # it protects. Two pending requests would let one school agree to
            # Grace and another admin agree to Hillside for the same child, and
            # whichever landed second would find the enrolment already gone.
            # A second destination has to wait for the first to be declined or
            # withdrawn, which is a real constraint on schools and the honest
            # one: a child is transferring to one place.
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(status="pending"),
                name="one_pending_transfer_request_per_student",
            ),
            # The next four are what stop a row lying about how it came about.
            # Application code already writes `route` itself rather than taking
            # it from a caller, but "the only code that writes this is careful"
            # is a property of today's code; these are properties of the table.
            #
            # An answered handshake names two different people. This is
            # `SameSignatory` again, one layer down — and the layer that also
            # catches a shell session, a data migration, or a future endpoint
            # that forgets.
            #
            # Scoped to accepted and declined, which are the outcomes that claim
            # something about the *other* school. A withdrawal is the asking
            # side retracting its own proposal, so it names its own people and
            # frequently the very person who asked; demanding two names there
            # would forbid the most ordinary thing a school can do with a
            # request it no longer wants.
            models.CheckConstraint(
                condition=Q(route=TransferRoute.SINGLE_PARTY)
                | Q(
                    status__in=[
                        TransferRequestStatus.PENDING,
                        TransferRequestStatus.WITHDRAWN,
                    ]
                )
                | (
                    Q(resolved_by__isnull=False)
                    & ~Q(requested_by=F("resolved_by"))
                ),
                name="answered_transfer_names_two_signatories",
            ),
            # A single-party row names one person, twice, and never nobody: it
            # is written only after the transfer has happened, so an unanswered
            # one would describe an event with no author.
            models.CheckConstraint(
                condition=Q(route=TransferRoute.HANDSHAKE)
                | (Q(resolved_by__isnull=False) & Q(requested_by=F("resolved_by"))),
                name="single_party_transfer_names_one_signatory",
            ),
            # ...and only after it succeeded, so it is never pending, declined
            # or withdrawn. There was no proposal to answer.
            models.CheckConstraint(
                condition=Q(route=TransferRoute.HANDSHAKE)
                | Q(status=TransferRequestStatus.ACCEPTED),
                name="single_party_transfer_is_always_accepted",
            ),
            # A side is exactly the thing a single-party transfer does not have,
            # and exactly the thing a handshake must record.
            models.CheckConstraint(
                condition=Q(route=TransferRoute.HANDSHAKE, requested_side__isnull=False)
                | Q(route=TransferRoute.SINGLE_PARTY, requested_side__isnull=True),
                name="transfer_side_matches_route",
            ),
        ]
        indexes = [
            models.Index(fields=["to_school", "status"]),
            models.Index(fields=["student", "status"]),
        ]

    def __str__(self):
        return (
            f"{self.get_status_display()} transfer of {self.student.name} "
            f"to {self.to_school}"
        )

    # -- read the pair back off the membership --------------------------------

    @property
    def student_user(self):
        return self.student.user

    @property
    def from_school(self):
        return self.student.school

    @property
    def is_pending(self) -> bool:
        return self.status == TransferRequestStatus.PENDING

    def school_for(self, side):
        """The school somebody acting for `side` must hold authority at.

        Asked as a question about a side rather than about a user, because a
        person may hold authority at both ends and the model should not have to
        care which. `school_for(requested_side.other)` is therefore "who still
        has to sign this".
        """
        return self.from_school if side == TransferSide.RELEASING else self.to_school

    # -- answering ------------------------------------------------------------

    def _lock_pending(self):
        """This row, locked, if it is still open to an answer.

        Every answer starts here, so "is this still pending?" is asked once, of
        a row this transaction holds, rather than of whatever the caller happened
        to load. The invitation flow learned that the expensive way: a guard
        reading an in-memory copy is answering a question about the past, and two
        answers arriving together both passed it before either wrote.

        `.order_by()` for the reason `Meta.ordering` always needs it under
        `FOR UPDATE` — this one sorts by a local column, but the habit is what
        keeps the next person from adding a joined sort and locking two more
        tables without noticing.
        """
        locked = (
            TransferRequest.objects.select_for_update().order_by().get(pk=self.pk)
        )
        if locked.status != TransferRequestStatus.PENDING:
            # Bring the caller's object in line with what the lock read, so it
            # stops asserting the state this transaction has just disproved.
            self.status = locked.status
            self.resolved_by_id = locked.resolved_by_id
            self.resolved_at = locked.resolved_at
            raise TransferAlreadyResolved(
                f"This transfer request was already "
                f"{locked.get_status_display().lower()}."
            )
        return locked

    def _record_answer(self, by, status, *, extra_fields=()):
        self.status = status
        self.resolved_by = by
        self.resolved_at = timezone.now()
        self.save(
            update_fields=["status", "resolved_by", "resolved_at", *extra_fields]
        )
        return self

    @transaction.atomic
    def accept(self, by, *, reference=""):
        """Both signatures are now in. Runs the transfer, returns the new membership.

        The transfer itself is `services.transfer_student()`, unchanged, and
        called here for the first time by something other than an admin who
        holds both schools. That is the point: the two consents recorded on this
        row *are* the two-sided authority it has always needed, so it can run in
        one transaction — no window where the child belongs to nowhere, and the
        guardians carried across rather than re-linked by hand.

        The enrolment is re-checked rather than assumed. A pending request is a
        proposal about a relationship, and the relationship may have moved on
        while the request sat there — the child released without a destination,
        graduated, or moved by platform staff. Accepting then would either fail
        on the one-live-student index or quietly revive an ended enrolment, so
        `EnrolmentMovedOn` says what actually happened instead.

        The answer is written **last**, after everything that could refuse has
        refused. A row reading "accepted" beside an enrolment that never moved
        would be a worse record than no row at all, and this is a record whose
        whole purpose is to be trusted when a transfer is disputed.
        """
        # Imported here, not at module scope: `services` imports this module, so
        # a module-level import would close the loop.
        from .services import transfer_student

        self._lock_pending()

        student = (
            Membership.objects.select_for_update().order_by().get(pk=self.student_id)
        )
        if not student.is_live:
            raise EnrolmentMovedOn(
                f"{student.user}'s enrolment at {student.school} is "
                f"{student.get_status_display().lower()}, so there is nothing to "
                f"hand over. A fresh admission is the way in now."
            )

        if reference:
            self.reference = reference

        moved = transfer_student(student, self.to_school, reference=self.reference)
        self._record_answer(by, TransferRequestStatus.ACCEPTED, extra_fields=["reference"])
        return moved

    @transaction.atomic
    def decline(self, by):
        """The other side says no. The enrolment is untouched."""
        self._lock_pending()
        return self._record_answer(by, TransferRequestStatus.DECLINED)

    @transaction.atomic
    def withdraw(self, by):
        """The side that asked calls it off. The enrolment is untouched."""
        self._lock_pending()
        return self._record_answer(by, TransferRequestStatus.WITHDRAWN)


class SignInScope(models.TextChoices):
    """The two things a run of failed sign-ins is counted against.

    Two, not one, because they answer different questions. IDENTIFIER bounds
    how many guesses one account can absorb; ADDRESS bounds how many guesses
    one machine can make across all accounts, which is the credential-stuffing
    shape the first one cannot see.
    """

    IDENTIFIER = "identifier", "Identifier"
    ADDRESS = "address", "Network address"


class SignInAttempts(models.Model):
    """One row per throttled key, holding a count and the window it counts in.

    A counter rather than a row per attempt, so the table is bounded by the
    number of distinct keys seen rather than by how hard somebody is trying.
    That trades away an audit trail; if one is wanted later it belongs in the
    log stream, not here, where it would be a second copy with different
    retention.

    **In the public schema, like everything else in this app.** A throttle that
    lived per-tenant would count each school separately, and sign-in does not
    happen at a school — it happens on the portal, before any school is chosen.

    **The identifier is stored as a digest, the address in the clear**, and the
    asymmetry is deliberate. Whatever was typed into the identifier box is not
    always an identifier: people type their password into it, and a table of
    those in plain text is a credential store nobody decided to build. Hashing
    costs nothing here because the key is only ever compared for equality — the
    same reasoning `schools.models.hash_token()` gives for invitation tokens.
    An address is not a credential and is the one field an incident is actually
    investigated from, so it stays readable.
    """

    scope = models.CharField(max_length=16, choices=SignInScope.choices)
    key = models.CharField(
        max_length=255,
        help_text="SHA-256 of the normalized identifier, or the address itself.",
    )
    failures = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "key"], name="one_signin_counter_per_key"
            )
        ]

    def __str__(self):
        return f"{self.scope}:{self.key} ({self.failures})"
