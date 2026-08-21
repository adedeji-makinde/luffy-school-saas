"""A teacher whose session lapses in the middle of a marking sheet.

The story this branch exists for, told end to end: a teacher marks a few cells,
their session goes, the next blur is refused, they sign in again, and the browser
sends the refused mark a second time. Nothing may be lost and nothing may be
counted twice.

The server does not hold the unsent mark anywhere, and deliberately so — writing
it would need the authority that just lapsed, and a server-side draft store is a
second copy of the gradebook with none of its rules. What the server owes instead
is that **replaying is safe**, which is not a new promise: every write is already
conditional on the version the teacher was shown, and
`_is_our_write_arriving_twice()` already recognises a repeat of the caller's own
write. This module holds that line across a re-login, which is the one place it
could quietly stop being true — the retry is the *same person* but a *different
session*, and a rule keyed on the session rather than the person would break here
and nowhere else.

Session policy itself — how long a session lasts, what the 401 body says — is in
`accounts/tests/test_session.py`.
"""

from django.contrib.sessions.models import Session

from accounts.session import SESSION_EXPIRED
from gradebook.models import Score
from gradebook.tests.test_api import GradebookApiSetUp
from gradebook.tests.test_scores import connected_to


class MarkingThroughASessionExpiryTests(GradebookApiSetUp):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher.user)

    def expire_the_session(self):
        """What a timed-out browser looks like: cookie kept, session gone."""
        Session.objects.all().delete()

    def test_a_lapsed_session_refuses_the_save_and_says_it_is_retryable(self):
        first = self.save(self.ada.pk, 15)
        self.assertEqual(first.status_code, 200)

        self.expire_the_session()
        refused = self.save(self.emeka.pk, 12)

        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.json()["code"], SESSION_EXPIRED)
        self.assertTrue(refused.json()["retryable"])

    def test_the_refused_mark_was_not_written(self):
        """A 401 must not be a half-write.

        Authentication happens before the view, so this could hardly be
        otherwise — but "the mark you were told was refused is definitely not in
        the book" is the premise the whole replay is built on, and a premise
        worth a test is worth this one.
        """
        self.expire_the_session()
        self.save(self.emeka.pk, 12)

        with connected_to(self.stmarys):
            self.assertFalse(
                Score.objects.filter(student_membership_id=self.emeka.pk).exists()
            )

    def test_signing_in_again_and_resending_writes_the_mark(self):
        self.expire_the_session()
        self.assertEqual(self.save(self.emeka.pk, 12).status_code, 401)

        self.client.force_login(self.teacher.user)
        replayed = self.save(self.emeka.pk, 12)

        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(replayed.json()["value"], 12)

    def test_a_mark_that_did_land_is_not_doubled_by_the_replay(self):
        """The other order, and the one that actually bites.

        A blur that reached the server and whose *response* was lost looks
        identical from the browser to one that never arrived — so the teacher
        signs in again and resends a mark that is already in the book, carrying
        the version they were shown before it was written. That is a stale
        version by the letter of the rule, and calling it a conflict would tell
        a teacher working alone that somebody else is in their sheet.

        It is swallowed because the row already says exactly what this request
        asked for *and this same person wrote it*. Crossing a session boundary
        must not change that: the test is here because a check written against
        the session rather than the user would pass everywhere except here.
        """
        self.assertEqual(self.save(self.ada.pk, 15).status_code, 200)
        self.expire_the_session()
        self.client.force_login(self.teacher.user)

        # `expected_version=None` — "I was shown no mark" — which is what the
        # browser still believes, because the response never came back.
        replayed = self.save(self.ada.pk, 15)

        self.assertEqual(replayed.status_code, 200)
        with connected_to(self.stmarys):
            marks = Score.objects.filter(student_membership_id=self.ada.pk)
            self.assertEqual(marks.count(), 1)
            self.assertEqual(marks.get().value, 15)

    def test_somebody_elses_mark_is_still_a_conflict_across_the_boundary(self):
        """Replay-safety is not "the second write always wins quietly".

        If the cell moved while the teacher was signed out, the refusal has to
        survive the re-login. Otherwise the session boundary becomes a way to
        launder a stale write into an overwrite.
        """
        self.assertEqual(self.save(self.ada.pk, 15).status_code, 200)

        self.expire_the_session()
        # Another teacher corrects the mark while the first one is signed out.
        self.client.force_login(self.other_teacher.user)
        corrected = self.save(self.ada.pk, 17, expected_version=1)
        self.assertEqual(corrected.status_code, 200)

        self.client.force_login(self.teacher.user)
        replayed = self.save(self.ada.pk, 15)

        self.assertEqual(replayed.status_code, 409)
        self.assertEqual(replayed.json()["current"]["value"], 17)

    def test_reading_the_sheet_is_refused_the_same_way(self):
        """Whatever the teacher does next, not only a save.

        A lapsed session usually surfaces on a read — the teacher tabs back and
        the sheet reloads — so the read has to carry the same recoverable answer
        as the write, or the client learns the situation from whichever request
        happened to go first.
        """
        self.expire_the_session()
        refused = self.sheet()

        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.json()["code"], SESSION_EXPIRED)
