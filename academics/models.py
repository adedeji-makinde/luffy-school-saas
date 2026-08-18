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
from django.db.models import F, Q


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
        ]

    def __str__(self):
        return f"{self.get_name_display()} {self.session}"
