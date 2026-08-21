"""Every log line and every error report says which school it is about.

The property being defended is narrow and easy to lose: it must hold for lines
*nobody wrote for it*. A logger call inside `schools/` could always have added
the school by hand; `django.request`'s could not, and a third-party library's
never will. So the tests below drive Django's own logger and a bare
`logging.getLogger()` rather than any logger this codebase owns.

Real schools with real schemas, because the whole question is what
`connection.tenant` says, and the answer only differs from `None` when a schema
is really being connected to.
"""

import logging
from io import StringIO

from django.core import mail
from django.db import connection
from django.test import TestCase, override_settings

from schools.logging import (
    NO_SCHOOL,
    PORTAL,
    SchoolAdminEmailHandler,
    SchoolContextFilter,
    current_school,
)
from schools.tests.test_tenant_isolation import connected_to, make_school


def capture(logger_name, *, filters=(SchoolContextFilter,)):
    """A handler that formats with `%(school)s` and keeps the output."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("[%(school)s] %(message)s"))
    for filter_class in filters:
        handler.addFilter(filter_class())
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger, handler, stream


class SchoolContextFilterTests(TestCase):
    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        self.logger, self.handler, self.stream = capture("test.school.context")
        self.addCleanup(self.logger.removeHandler, self.handler)

    def test_a_line_logged_inside_a_school_names_it(self):
        with connected_to(self.stmarys):
            self.logger.info("something happened")
        self.assertIn("[St Mary's] something happened", self.stream.getvalue())

    def test_two_schools_are_told_apart(self):
        """The whole point: the same message from two schools reads differently."""
        with connected_to(self.stmarys):
            self.logger.info("IntegrityError on save")
        with connected_to(self.grace):
            self.logger.info("IntegrityError on save")

        output = self.stream.getvalue()
        self.assertIn("[St Mary's] IntegrityError on save", output)
        self.assertIn("[Grace Academy] IntegrityError on save", output)

    def test_the_public_schema_is_named_the_portal_not_a_school(self):
        """It is where a login with no membership lands, not a customer."""
        self.logger.info("signing in")
        self.assertIn(f"[{PORTAL}] signing in", self.stream.getvalue())

    def test_a_record_always_has_the_attribute(self):
        """A formatter using `%(school)s` must never be the thing that breaks.

        A `KeyError` raised while formatting a log record happens *during* the
        reporting of something else, and takes the something else with it. So
        the filter sets the attribute on every record, including when there is
        no answer.
        """
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "boom", None, None
        )
        self.assertTrue(SchoolContextFilter().filter(record))
        self.assertTrue(hasattr(record, "school"))
        self.assertTrue(record.school)

    def test_the_filter_never_raises_even_if_the_connection_misbehaves(self):
        """Logging must not be able to fail. Pinned rather than assumed."""

        class Exploding:
            @property
            def tenant(self):
                raise RuntimeError("connection is mid-teardown")

        import schools.logging as school_logging

        original = school_logging.connection
        school_logging.connection = Exploding()
        try:
            record = logging.LogRecord(
                "x", logging.ERROR, __file__, 1, "boom", None, None
            )
            self.assertTrue(SchoolContextFilter().filter(record))
            self.assertEqual(record.school, NO_SCHOOL)
        finally:
            school_logging.connection = original

    def test_current_school_reads_the_connection_not_a_request(self):
        """Which is what makes it work in a command or an on_commit callback."""
        self.assertEqual(current_school(), PORTAL)
        with connected_to(self.grace):
            self.assertEqual(current_school(), "Grace Academy")
        self.assertEqual(current_school(), PORTAL)


class DjangosOwnLoggerTests(TestCase):
    """The lines nobody can go and edit a call site for.

    `django.request` is the one that matters: it is where every unhandled 500
    is reported, it is written by Django, and it is exactly where "which
    school?" is worth most.
    """

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.logger, self.handler, self.stream = capture("django.request")
        self.addCleanup(self.logger.removeHandler, self.handler)

    def test_djangos_request_logger_carries_the_school(self):
        with connected_to(self.stmarys):
            self.logger.error("Internal Server Error: /api/schools/st-marys/x/")
        self.assertIn("[St Mary's] Internal Server Error", self.stream.getvalue())

    def test_a_library_logger_nobody_configured_carries_it_too(self):
        """The filter is on the handler, so it applies to whatever reaches it."""
        logger, handler, stream = capture("some.third.party.library")
        self.addCleanup(logger.removeHandler, handler)
        with connected_to(self.stmarys):
            logger.warning("retrying")
        self.assertIn("[St Mary's] retrying", stream.getvalue())


class SettingsWiringTests(TestCase):
    """That the configuration in settings.py actually installs all this.

    Worth its own tests because everything above would pass with a perfectly
    good filter that no handler in the real project ever used.
    """

    def test_every_configured_handler_has_the_school_filter(self):
        from django.conf import settings

        for name, handler in settings.LOGGING["handlers"].items():
            with self.subTest(handler=name):
                self.assertIn("school", handler.get("filters", []))

    def test_existing_loggers_are_not_disabled(self):
        """`disable_existing_loggers` silences the very logger this labels.

        Django configures `django` and `django.server` before this dictConfig
        runs, so switching this on would turn off `django.request` for the life
        of the process — and the section would then be labelling nothing.
        """
        from django.conf import settings

        self.assertIs(settings.LOGGING["disable_existing_loggers"], False)

    def test_the_formatter_prints_the_school(self):
        from django.conf import settings

        fmt = settings.LOGGING["formatters"]["school_aware"]["format"]
        self.assertIn("{school}", fmt)

    def test_the_error_report_handler_is_the_school_aware_one(self):
        from django.conf import settings

        self.assertEqual(
            settings.LOGGING["handlers"]["mail_admins"]["class"],
            "schools.logging.SchoolAdminEmailHandler",
        )


@override_settings(
    ADMINS=[("Ops", "ops@luffy.school")],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEBUG=False,
)
class ErrorReportTests(TestCase):
    """The subject line, which is the part anybody reads in a list of forty."""

    def setUp(self):
        self.stmarys = make_school("St Mary's", "st-marys", "st_marys")
        self.grace = make_school("Grace Academy", "grace", "grace")
        mail.outbox = []

    def report(self, message):
        handler = SchoolAdminEmailHandler()
        handler.addFilter(SchoolContextFilter())
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, message, None, None
        )
        # The filter is applied by the handler's own machinery in production;
        # `emit()` is called directly here, so apply it the same way.
        for log_filter in handler.filters:
            log_filter.filter(record)
        handler.emit(record)
        return mail.outbox[-1]

    def test_the_school_is_in_the_subject(self):
        with connected_to(self.stmarys):
            message = self.report("Internal Server Error: /api/x/")
        self.assertIn("St Mary's", message.subject)
        self.assertIn("Internal Server Error", message.subject)

    def test_two_schools_errors_have_different_subjects(self):
        """Forty of these all naming one school is itself the diagnosis."""
        with connected_to(self.stmarys):
            first = self.report("Internal Server Error: /api/x/")
        with connected_to(self.grace):
            second = self.report("Internal Server Error: /api/x/")

        self.assertNotEqual(first.subject, second.subject)
        self.assertIn("St Mary's", first.subject)
        self.assertIn("Grace Academy", second.subject)

    def test_a_report_from_the_portal_still_sends_and_says_so(self):
        """Platform-wide failures are real, must not be dropped, and are not a school."""
        message = self.report("Internal Server Error: /admin/")
        self.assertIn(PORTAL, message.subject)
        self.assertNotIn("St Mary's", message.subject)

    def test_the_handler_carries_the_school_without_the_filter_attached(self):
        """Reachable on its own, so it asks rather than trusting the record."""
        handler = SchoolAdminEmailHandler()
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "boom", None, None
        )
        with connected_to(self.grace):
            handler.emit(record)
        self.assertIn("Grace Academy", mail.outbox[-1].subject)

    def test_the_school_does_not_leak_between_reports(self):
        """The thread-local is cleared, so one error cannot label the next.

        One handler instance is shared process-wide. Leaving the school set
        would label a later platform-wide failure with whichever school
        happened to fail before it — a wrong name being worse than none.
        """
        handler = SchoolAdminEmailHandler()
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "boom", None, None
        )
        with connected_to(self.stmarys):
            handler.emit(record)
        self.assertIn("St Mary's", mail.outbox[-1].subject)

        handler.emit(record)
        self.assertNotIn("St Mary's", mail.outbox[-1].subject)
