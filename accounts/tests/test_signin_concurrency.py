"""Two wrong passwords arriving at the same instant.

A throttle that loses a count is not a throttle. `record_failure()` is
read-modify-write on one row, which is the shape that quietly does the wrong
thing under load: both requests read `failures = 9`, both write `10`, and a
limit of ten has just become eleven — repeatably, for anyone who sends their
guesses in pairs. `select_for_update()` is what makes the second one wait, and
this is the test that says so.

`TransactionTestCase` and real threads, on the same reasoning as
`test_transfer_concurrency.py`: two connections whose commits are visible to
each other, interleaved by a barrier rather than by sleeps, so the two attempts
are provably in flight together.

The counterpart is `blocked_for()`, which deliberately does *not* lock — the
docstring there explains what that costs and why it is the right trade. Nothing
here contradicts it: reading a count one low can let a single extra guess
through, whereas losing a write corrupts the count for the rest of the window.
"""

import threading

from django.db import connections
from django.test import TransactionTestCase

from accounts import throttling
from accounts.models import SignInAttempts, SignInScope

IDENTIFIER = "tayo@st-marys.school"


class ConcurrentFailuresTests(TransactionTestCase):
    def _fail_together(self, count):
        """`count` failures against one identifier, all released at once."""
        ready = threading.Barrier(count, timeout=15)
        errors = []

        def run():
            try:
                ready.wait()
                throttling.record_failure(SignInScope.IDENTIFIER, IDENTIFIER)
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, [], f"a thread failed: {errors}")

    def test_simultaneous_failures_are_all_counted(self):
        self._fail_together(4)

        row = SignInAttempts.objects.get(scope=SignInScope.IDENTIFIER)
        self.assertEqual(row.failures, 4)

    def test_the_first_two_do_not_collide_creating_the_row(self):
        """There is no row yet, so both threads try to insert one.

        One of them loses on `one_signin_counter_per_key`, and losing has to
        mean "take the lock on theirs" rather than "raise" — a throttle that
        500s on its own first two requests would be turned off within a day.
        """
        self._fail_together(2)

        self.assertEqual(
            SignInAttempts.objects.filter(scope=SignInScope.IDENTIFIER).count(), 1
        )
        self.assertEqual(
            SignInAttempts.objects.get(scope=SignInScope.IDENTIFIER).failures, 2
        )
