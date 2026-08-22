"""Three corrections to the transition log, all found by review.

- **Ordering on `sheet_id`, not `sheet`.** Ordering by the relation made Django
  sort by `ResultSheet.Meta.ordering`, which sorts by two *more* relations — so
  `history()` and the same-signatory lookup each compiled to a four-table join
  sorted by the term's session and the class's level. One of those runs inside
  the row lock, where every joined table lengthens the hold.
- **The `(sheet, cycle)` index removed.** `one_signature_per_person_per_review_cycle`
  and `one_transition_to_each_state_per_cycle` already build btrees led by
  exactly those columns. It was a third index per tenant schema — one per school
  on the platform — maintained on every insert and answering nothing.
- **`a_send_back_says_why` now tests for a non-whitespace character.** It was
  `~Q(reason="")`, so a reason of three spaces passed the database and was
  refused by `send_back()`, which compares `reason.strip()`. The gap was exactly
  the caller the constraint exists for — the import and the psql session — and
  what reached the teacher was a send-back whose reason renders blank.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("results", "0003_a_released_sheet_stays_released"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="resultsheettransition",
            options={"ordering": ["sheet_id", "created_at", "pk"]},
        ),
        migrations.RemoveConstraint(
            model_name="resultsheettransition",
            name="a_send_back_says_why",
        ),
        migrations.RemoveIndex(
            model_name="resultsheettransition",
            name="results_res_sheet_i_54b604_idx",
        ),
        migrations.AddConstraint(
            model_name="resultsheettransition",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("to_state", "draft"), _negated=True),
                    ("reason__regex", "\\S"),
                    _connector="OR",
                ),
                name="a_send_back_says_why",
            ),
        ),
    ]
