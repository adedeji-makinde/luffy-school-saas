"""Make "the ledger is append-only" a rule Postgres holds, not one Python asks for.

`FeeLedgerEntry.save()` and `.delete()` already refuse, and that is the error a
developer sees. This is the one a `psql` session, a data import, a bulk
`.update()` or a future service function written in a hurry runs into — none of
which go anywhere near the model's methods. It is the same reasoning
docs/tenancy.md gives for preferring a constraint to a `clean()`, applied to the
one table in this project where "what did the books say last week" has to have an
answer.

Both objects are created unqualified, so they land in whichever schema is first
on the `search_path` — which during `migrate_schemas` is the school's own. Each
school therefore gets its own trigger and its own function, exactly as it gets
its own table, and dropping a school's schema takes all three with it.

Note the deliberate gap: this fires per row on UPDATE and DELETE, and not on
TRUNCATE. A statement-level TRUNCATE guard would also block Django's test
teardown, and a schema being emptied wholesale is a different act from a row
being quietly rewritten — the second is the one that corrupts a story.
"""

from django.db import migrations

FUNCTION = """
CREATE OR REPLACE FUNCTION fees_ledger_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'fees_feeledgerentry is append-only; % is not allowed. '
        'Post a reversal naming the entry instead.', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

TRIGGER = """
CREATE TRIGGER fees_ledger_append_only
BEFORE UPDATE OR DELETE ON fees_feeledgerentry
FOR EACH ROW EXECUTE FUNCTION fees_ledger_is_append_only();
"""

DROP_TRIGGER = "DROP TRIGGER IF EXISTS fees_ledger_append_only ON fees_feeledgerentry;"
DROP_FUNCTION = "DROP FUNCTION IF EXISTS fees_ledger_is_append_only();"


class Migration(migrations.Migration):

    dependencies = [
        ("fees", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=FUNCTION, reverse_sql=DROP_FUNCTION),
        migrations.RunSQL(sql=TRIGGER, reverse_sql=DROP_TRIGGER),
    ]
