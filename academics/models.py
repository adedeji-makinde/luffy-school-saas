"""School-owned academic records. The first app that lives in TENANT_APPS.

Everything here is created once **per school schema**, not once for the
platform. That is the whole difference from `accounts`: an `accounts.User`
row is one person across every school, while a `Term` row below belongs to
exactly one school and is not merely hidden from the others — it is in a
different Postgres schema and therefore not on their connection's
`search_path` at all. See docs/tenancy.md.

Holds **no foreign key into `accounts`**, and never will. That is not an
oversight: `on_delete` is resolved against whichever schema the connection is
on, so a tenant→shared key gives `PROTECT` that does not protect and `CASCADE`
that cascades one school's rows only. docs/tenancy.md records what was measured.

`Term` originally held no foreign keys *at all*, which was true of it because
it pointed at nothing. `ClassPlacement` below points at two things and settles
the question the way `fees.FeeLedgerEntry` and `gradebook.Score` already had:
**real foreign keys between tables in the same schema, a bare id across the
schema boundary**, with the check that earns the bare id made in
`academics.services` before anything is written.
"""

from django.db import models
from django.db.models import F, Func, IntegerField, Q

class DaysBetween(Func):
    """`later - earlier` for two dates, as the integer Postgres returns.

    Not `F("later") - F("earlier")`, which is the obvious spelling and does not
    work here. Django reads a subtraction of two `DateField`s as a *duration*
    and renders `interval '1 day' * ("later" - "earlier")` — even wrapped in
    `ExpressionWrapper(output_field=IntegerField())`, which looks like it should
    settle the question and does not. Postgres then refuses the surrounding
    arithmetic outright (`operator does not exist: interval + integer`), so a
    check constraint written that way fails when the *schema* is created, taking
    every term at every school with it.

    A subclass rather than a `Func(...)` instance with `template=` and
    `arg_joiner=` passed in, and that is not style. Those two arrive as
    `**extra`, a dict whose key order lands in the expression's identity — so an
    instance built here and the identical instance reconstructed from a
    migration compare **unequal**, and `makemigrations` proposes dropping and
    recreating the constraint on every single run, forever. As class attributes
    they never enter `extra` at all, and the round-trip is stable. CI runs
    `makemigrations --check`, so the symptom would have been a permanently red
    build rather than anything subtle.
    """

    template = "(%(expressions)s)"
    arg_joiner = " - "
    output_field = IntegerField()


class TermName(models.TextChoices):
    FIRST = "first", "First term"
    SECOND = "second", "Second term"
    THIRD = "third", "Third term"


class Term(models.Model):
    """One school's slice of one academic session.

    The natural first tenant-scoped table: attendance, fees and report cards
    are all reckoned per term, so nearly every school-owned record that comes
    later hangs off this one. It is also genuinely school-owned — two schools
    run the same 2025/2026 session on different dates, and neither has any
    business seeing the other's calendar.
    """

    session = models.CharField(
        max_length=9, help_text="Academic session this term belongs to, e.g. 2025/2026."
    )
    name = models.CharField(max_length=16, choices=TermName)
    starts_on = models.DateField()
    ends_on = models.DateField()

    # A date this term *announces*, not a pointer to the next Term row — which
    # is why it is a column here and not a lookup. A school prints "Next term
    # begins: 8 January" on the report card it hands out in December, and at
    # that moment next term's row usually does not exist yet. Deriving it would
    # leave the field empty at exactly the moment it is wanted.
    #
    # It also cannot be derived reliably even later: the term after 2025/2026
    # Third is 2026/2027 First, so "the next term" crosses sessions, and session
    # is a formatted string rather than anything with an ordering. Nullable
    # because "not announced yet" is the honest answer for most of a term.
    next_term_starts_on = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "When the next term begins, as announced during this one. "
            "Blank until the school says."
        ),
    )

    # The count the *school* declares, not one computed from the dates. Weekends
    # come out, but so do mid-term break, public holidays that move year to year
    # (Eid, Easter), sports day, and any day the school closed for weather or a
    # local event. This number is the denominator of the attendance percentage
    # on a report card, so a computed one that disagreed with the school's own
    # register would make every percentage wrong in a way nobody could explain.
    # Nullable for the same reason as above: it is often not settled on the day
    # the term record is created.
    school_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Teaching days in this term, as the school counts them. The "
            "denominator of every attendance figure."
        ),
    )

    # Which term the school is currently teaching. At most one, enforced below.
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["-session", "starts_on"]
        constraints = [
            # Per-schema, so "2025/2026 First term" existing at St Mary's does
            # not stop Grace Academy having its own. That is the isolation.
            models.UniqueConstraint(
                fields=["session", "name"], name="uniq_term_session_name"
            ),
            models.UniqueConstraint(
                fields=["is_current"],
                condition=Q(is_current=True),
                name="one_current_term",
            ),
            models.CheckConstraint(
                condition=Q(ends_on__gt=F("starts_on")),
                name="term_ends_after_it_starts",
            ),
            # A next term that begins before this one ends is a typo, not a
            # calendar. `__gt` rather than `__gte`: the new term starting the
            # same day the old one ends would mean a day belonging to both.
            models.CheckConstraint(
                condition=Q(next_term_starts_on__isnull=True)
                | Q(next_term_starts_on__gt=F("ends_on")),
                name="next_term_starts_after_this_one_ends",
            ),
            # A term cannot contain more school days than it contains days.
            # `+ 1` because both endpoints are teaching days — a Monday-to-Friday
            # term is five days, not four. See `DaysBetween` for why the
            # subtraction is spelled out rather than written with `F() - F()`.
            models.CheckConstraint(
                condition=Q(school_days__isnull=True)
                | Q(
                    school_days__lte=DaysBetween(F("ends_on"), F("starts_on")) + 1
                ),
                name="school_days_fit_inside_the_term",
            ),
            # Zero school days is not a term. Guarded separately from the upper
            # bound so a violation says which end was wrong.
            models.CheckConstraint(
                condition=Q(school_days__isnull=True) | Q(school_days__gte=1),
                name="a_term_has_at_least_one_school_day",
            ),
        ]

    def __str__(self):
        return f"{self.get_name_display()} {self.session}"

    @property
    def calendar_days(self) -> int:
        """Days from the first to the last, inclusive of both.

        Not the same question as `school_days` and deliberately not a default
        for it — this is the ceiling the constraint above checks against, which
        is a different thing from what the school actually taught.
        """
        return (self.ends_on - self.starts_on).days + 1


class ClassGroup(models.Model):
    """One teaching group a school puts children in: "JSS 1A", "Primary 4".

    `gradebook.Assessment` refused to name one of these, and said why: there was
    no class model, and inventing one there would have been guessing at how a
    school groups its children. It is no longer a guess — a position in class
    and a class average are the two numbers a report card is judged on, and
    neither has a denominator without this table.

    Deliberately *not* tied to a session or a term. "JSS 1A" is the same group
    year after year; who sits in it is what changes, and that is
    `ClassPlacement`. Putting a session on the group instead would mean a new
    row per year, and every historical placement pointing at a different one —
    so "how did JSS 1A do last year" would have to be asked of a different
    class, which is not what anybody means by the question.
    """

    #: What the school prints. Its own name rather than a level plus an arm,
    #: because schools spell it their own way — "JSS 1A", "Primary 4 Gold",
    #: "Year 7 Blue" — and a composed one would fight them, exactly as
    #: `Subject.code` is stored rather than slugged from `Subject.name`.
    name = models.CharField(max_length=64)

    #: Where the group sits in the school's own order, smallest first. Not
    #: derived from `name`: "JSS 1A" sorts before "JSS 10A" as text, and no
    #: string rule survives a school that runs Nursery, Primary and Senior in
    #: one place. Used for ordering a broadsheet and, later, for what promotion
    #: means. Not unique — a year with three arms has three groups at one level.
    level = models.PositiveSmallIntegerField(
        default=0,
        help_text="The school's own ordering, smallest first. Arms of one year share a level.",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="A group no longer taught. Kept, because old placements name it.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["level", "name"]
        constraints = [
            # Per schema, so St Mary's having a "JSS 1A" does not stop Grace
            # Academy having one. Same isolation as `uniq_term_session_name`.
            models.UniqueConstraint(fields=["name"], name="uniq_class_group_name"),
        ]

    def __str__(self):
        return self.name


class ClassPlacementQuerySet(models.QuerySet):
    def for_term(self, term):
        return self.filter(term=term)

    def roster(self, class_group, term):
        """Who sat in this group this term."""
        return self.filter(class_group=class_group, term=term)

    def student_ids(self, class_group, term) -> list[int]:
        return list(
            self.roster(class_group, term).values_list(
                "student_membership_id", flat=True
            )
        )


class ClassPlacement(models.Model):
    """Which group one child sat in for one term.

    **Per term, not per session**, and that is the decision this table turns on.
    Everything a report card is reckoned from is already per term — attendance,
    fees, assessments — and a child who moves from JSS 1A to JSS 1B in January
    must be ranked in the group they were actually taught in, not the one they
    started the year in. A session-scoped placement would have to be edited to
    describe that, which would silently rewrite the position printed on a
    report card that had already gone home.

    Named `ClassPlacement` rather than `Enrolment` on purpose.
    `accounts.services.enroll_student()` already means something else — becoming
    a student of the school at all — and two words one letter apart, meaning
    two different things, is how the wrong one gets called.
    """

    # Both tenant-local, so both are real foreign keys with real integrity. The
    # cross-schema problem docs/tenancy.md describes does not arise between two
    # tables in one schema — the same note `gradebook.Assessment` carries.
    class_group = models.ForeignKey(
        ClassGroup, related_name="placements", on_delete=models.PROTECT
    )
    term = models.ForeignKey(
        Term, related_name="placements", on_delete=models.PROTECT
    )

    # A bare id, not a ForeignKey, pointing at the child's STUDENT membership in
    # the shared `accounts` app — the policy settled in docs/tenancy.md and
    # applied by `fees.FeeLedgerEntry` and `gradebook.Score` before this.
    # `academics.services` checks the id names a student *of this school* before
    # anything is written; see `accounts.students.why_not_a_student_here()`,
    # which this table is the third caller of and the reason it exists.
    student_membership_id = models.PositiveBigIntegerField(db_index=True)

    placed_by_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ClassPlacementQuerySet.as_manager()

    class Meta:
        ordering = ["class_group", "student_membership_id"]
        indexes = [
            # The roster query, which every position and every class average
            # runs: "who was in this group this term".
            models.Index(fields=["class_group", "term"]),
        ]
        constraints = [
            # **One group per child per term**, and this is the load-bearing
            # line in the file. Without it a child can sit in two groups at
            # once, which does not read as corrupt anywhere — it reads as two
            # perfectly ordinary rows — and produces two positions in class and
            # two class averages for one term, with nothing to say which is the
            # one to print.
            #
            # It is also the backstop for the race: two administrators placing
            # the same child into different arms at the same instant both find
            # no row and both insert. `services.place_student()` turns the
            # resulting IntegrityError into a refusal naming the group that won.
            models.UniqueConstraint(
                fields=["term", "student_membership_id"],
                name="one_class_placement_per_student_per_term",
            ),
        ]

    def __str__(self):
        return f"membership {self.student_membership_id} in {self.class_group} ({self.term})"
