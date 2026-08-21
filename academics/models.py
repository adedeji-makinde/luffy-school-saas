"""School-owned academic records. The first app that lives in TENANT_APPS.

Everything here is created once **per school schema**, not once for the
platform. That is the whole difference from `accounts`: an `accounts.User`
row is one person across every school, while a `Term` row below belongs to
exactly one school and is not merely hidden from the others — it is in a
different Postgres schema and therefore not on their connection's
`search_path` at all. See docs/tenancy.md.

Deliberately holds **no foreign keys**, including none back to `accounts`.
That is not an oversight: a tenant→shared foreign key is an unresolved
design question with a real failure mode, and it is a hard blocker on the
next tenant model rather than something to settle in passing. docs/tenancy.md
records what was measured and what has to be decided first.
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
