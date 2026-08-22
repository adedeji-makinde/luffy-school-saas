"""Make "release is terminal" a rule about the *sheet*, not only about the log.

`nothing_moves_out_of_released` — the check constraint added in 0001 — is on
`results_resultsheettransition`. It refuses a row that says a sheet moved out of
`released`, and that is a real guarantee about the audit. It is not the
guarantee the module claimed.

The gap, stated plainly because it is the sort a docstring hides:

    ResultSheet.objects.filter(state="released").update(state="draft")

writes no transition row, so it meets no constraint on the transition table, and
migration 0002's trigger fires only on the transition table. Nothing in the
schema touched `results_resultsheet` at all. The sheet reverts, the audit shows
no retraction — and that is *worse* than an unguarded revert, because the log
now reads as though the result is still released while the sheet says it is a
draft somebody can edit. A parent is holding a card the system no longer agrees
it issued.

A check constraint cannot express this: the rule is about the transition from
one row-version to another, and a CHECK sees only the row in front of it. So it
is a trigger, which is also what makes it hold for the callers that matter — the
psql session, the import, the bulk `.update()` that never goes near
`services._move()`.

**BEFORE UPDATE only, and only on a change of `state`.** Not DELETE: a released
sheet always has transitions, and `ResultSheetTransition.sheet` is `PROTECT`, so
the row cannot be deleted while its own audit points at it. Not on every UPDATE
either — `updated_at` and the fields a revision will add must stay writable, and
a guard broader than the rule is one somebody eventually disables wholesale.

Created unqualified so it lands in whichever schema is first on the
`search_path`, which during `migrate_schemas` is the school's own — the same
note 0002 carries. Each school gets its own trigger and function, and dropping a
school's schema takes them with it.
"""

from django.db import migrations

FUNCTION = """
CREATE OR REPLACE FUNCTION results_release_is_final() RETURNS trigger AS $$
BEGIN
    IF OLD.state = 'released' AND NEW.state IS DISTINCT FROM OLD.state THEN
        RAISE EXCEPTION
            'result sheet % has been released to parents and cannot be moved '
            'to %. Issue a revision, which makes a new version and leaves this '
            'one standing.', OLD.id, NEW.state
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TRIGGER = """
CREATE TRIGGER results_release_is_final
BEFORE UPDATE ON results_resultsheet
FOR EACH ROW EXECUTE FUNCTION results_release_is_final();
"""

DROP_TRIGGER = "DROP TRIGGER IF EXISTS results_release_is_final ON results_resultsheet;"
DROP_FUNCTION = "DROP FUNCTION IF EXISTS results_release_is_final();"


class Migration(migrations.Migration):

    dependencies = [
        ("results", "0002_transitions_are_append_only"),
    ]

    operations = [
        migrations.RunSQL(sql=FUNCTION, reverse_sql=DROP_FUNCTION),
        migrations.RunSQL(sql=TRIGGER, reverse_sql=DROP_TRIGGER),
    ]
