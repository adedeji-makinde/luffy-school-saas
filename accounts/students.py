"""One definition of "is this child ours?", for every tenant app that asks it.

`fees` and `gradebook` each carried their own copy of this rule, and each
copy's docstring said the same thing: the check is what earns the bare
`student_membership_id`, and it moves here when a third tenant app needs it.
`academics.ClassPlacement` is the third, so here it is.

It lives in `accounts` because that is where `Membership` lives. The
alternative — one tenant app importing another's services — is the dependency
this codebase has refused twice already, in `fees.services.NotThisSchoolsStudent`
and again in `gradebook.services.NotThisSchoolsStudent`: a gradebook that
imports the bursar's module in order to score a child has it backwards, and the
two answer to different people.

**This module raises nothing.** It answers a question and returns a sentence, or
`None`. That is not squeamishness about exceptions — it is what lets each app
keep its own hierarchy. `fees` raises `NotThisSchoolsStudent(FeeLedgerError)`
and `gradebook` raises `NotThisSchoolsStudent(GradebookError)`, and callers
catch `except FeeLedgerError` meaning "the entry was not posted". A shared
exception type would either break those catches or force every app to catch a
foreign base class, and both are worse than returning a string.

So the rule is shared and the raising is not, which is the split that actually
matters: there is now one place where "a student of this school" is *defined*,
and each app still refuses in its own words and its own type.
"""

from django.db import connection

from .models import Role


def why_not_a_student_here(membership, *, subject: str, holder: str) -> str | None:
    """Why `membership` may not have `subject` written against it here, or None.

    "Here" is the schema the connection is on, read from the connection rather
    than passed in. That is the load-bearing detail and it is deliberate: the
    tenant table about to be written has *already* been chosen by the
    `search_path`, so a school passed in as an argument is a second opinion that
    can disagree with it — and when it does, the row lands in one school's
    tables having been checked against another's. There is only ever one right
    answer to "which school is this", and the connection is holding it.

    Two questions, not one, because they fail differently:

    1. **Is this a STUDENT membership?** A `student_membership_id` pointing at a
       teacher's membership is not a near miss; it is a row about the wrong
       person entirely. The student's STUDENT membership is what pins both the
       child *and* their school, which is why nothing here takes a `User`.
    2. **Is that student ours?** `student_membership_id` carries no foreign key
       (docs/tenancy.md), so the column will accept any integer, including a
       child at another school. Nothing about that would look wrong afterwards:
       the row would sit in St Mary's tables, count towards St Mary's numbers,
       and name a child St Mary's has never taught.

    A foreign key would not have caught the second one either. `Membership` is
    shared, so a key into it constrains only that the row *exists* — every
    school's students are in that one table. The school half has to be asked in
    code however the column is declared.

    `subject` and `holder` are the two nouns each caller phrases it with — "a
    mark" and "the gradebook", "a fee entry" and "the books" — so that a refusal
    reads like the app that made it rather than like a shared utility.
    """
    if membership.role != Role.STUDENT:
        return (
            f"{membership} is not a student membership. {subject.capitalize()} is "
            f"keyed on a student's STUDENT membership, which is what pins both "
            f"the child and their school."
        )

    if membership.school.schema_name != connection.schema_name:
        return (
            f"{membership.user} is a student at {membership.school}, and this is "
            f"another school's {holder}. {subject.capitalize()} belongs to the "
            f"school the child attends."
        )

    return None


__all__ = ["why_not_a_student_here"]
