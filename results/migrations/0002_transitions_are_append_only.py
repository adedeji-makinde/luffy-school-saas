"""Make "the approval chain is append-only" a rule Postgres holds.

`ResultSheetTransition.save()` and `.delete()` already refuse, and that is the
error a developer sees. This is the one a `psql` session, a data import, a bulk
`.update()` or a service function written in a hurry runs into — none of which
go anywhere near the model's methods.

Deliberately copied in shape from `fees/migrations/0002_ledger_is_append_only`
rather than factored into something shared. The two tables are in different apps
with different migration histories, and a helper imported across app boundaries
to generate SQL would couple their migrations forever to save eight lines.

Both objects are created unqualified, so they land in whichever schema is first
on the `search_path` — during `migrate_schemas` that is the school's own. Each
school therefore gets its own trigger and its own function, exactly as it gets
its own table, and dropping a school's schema takes all three with it.

The same deliberate gap as the ledger's: this fires per row on UPDATE and
DELETE, not on TRUNCATE. A statement-level TRUNCATE guard would also block
Django's test teardown, and a schema emptied wholesale is a different act from a
row quietly rewritten — the second is the one that corrupts a story.
"""

from django.db import migrations

FUNCTION = """
CREATE OR REPLACE FUNCTION results_transitions_are_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'results_resultsheettransition is append-only; % is not allowed. '
        'Record the next step instead.', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

TRIGGER = """
CREATE TRIGGER results_transitions_append_only
BEFORE UPDATE OR DELETE ON results_resultsheettransition
FOR EACH ROW EXECUTE FUNCTION results_transitions_are_append_only();
"""

DROP_TRIGGER = (
    "DROP TRIGGER IF EXISTS results_transitions_append_only "
    "ON results_resultsheettransition;"
)
DROP_FUNCTION = "DROP FUNCTION IF EXISTS results_transitions_are_append_only();"


class Migration(migrations.Migration):

    dependencies = [
        ("results", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=FUNCTION, reverse_sql=DROP_FUNCTION),
        migrations.RunSQL(sql=TRIGGER, reverse_sql=DROP_TRIGGER),
    ]
