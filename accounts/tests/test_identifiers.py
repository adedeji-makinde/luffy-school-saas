"""Phone/username normalization and the Option C ambiguity check.

See accounts/identifiers.py for the normalization rules and
docs/membership.md for the collision policy these tests pin.
"""

from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from accounts.identifiers import (
    canonical_username,
    normalize_email,
    normalize_phone,
    try_normalize_phone,
)
from accounts.models import User
from accounts.tests.test_membership import PASSWORD, make_school, make_user


class NormalizePhoneTests(TestCase):
    def test_a_local_number_is_read_as_nigerian_by_default(self):
        self.assertEqual(normalize_phone("08031234567"), "+2348031234567")

    def test_an_already_international_number_round_trips(self):
        self.assertEqual(normalize_phone("+2348031234567"), "+2348031234567")

    def test_a_foreign_number_is_accepted_as_is(self):
        self.assertEqual(normalize_phone("+14155552671"), "+14155552671")

    def test_garbage_raises(self):
        with self.assertRaises(ValidationError):
            normalize_phone("not-a-phone")

    def test_blank_is_none(self):
        self.assertIsNone(normalize_phone(""))
        self.assertIsNone(normalize_phone(None))

    def test_try_normalize_returns_none_instead_of_raising(self):
        self.assertIsNone(try_normalize_phone("not-a-phone"))
        self.assertIsNone(try_normalize_phone(None))
        self.assertEqual(try_normalize_phone("08031234567"), "+2348031234567")


class CanonicalUsernameTests(TestCase):
    def test_a_bare_number_is_normalized(self):
        self.assertEqual(canonical_username("0803 123 4567"), "+2348031234567")

    def test_a_school_issued_handle_with_digits_is_left_alone(self):
        """The strict gate: one letter disqualifies the whole string."""
        self.assertEqual(canonical_username("STM-08031234567"), "STM-08031234567")

    def test_a_uri_form_is_left_alone(self):
        self.assertEqual(canonical_username("tel:08031234567"), "tel:08031234567")

    def test_a_slash_separated_handle_is_left_alone(self):
        self.assertEqual(canonical_username("STM/2026/0042"), "STM/2026/0042")

    def test_blank_is_unchanged(self):
        self.assertEqual(canonical_username(""), "")


class NormalizeEmailTests(TestCase):
    def test_lowercased(self):
        self.assertEqual(normalize_email("Ada@Stmarys.NG"), "ada@stmarys.ng")

    def test_blank_is_none(self):
        self.assertIsNone(normalize_email(""))
        self.assertIsNone(normalize_email(None))


class PhoneAndUsernameCollisionTests(TestCase):
    """The Option C check: an identifier cannot resolve to two people."""

    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")

    def test_phone_is_stored_in_e164(self):
        user = make_user("ada", "Ada Obi", phone="08031234567")
        self.assertEqual(user.phone, "+2348031234567")

    def test_two_spellings_of_one_phone_collide(self):
        make_user("ada", "Ada Obi", phone="08031234567")
        with self.assertRaises(ValidationError):
            make_user("bisi", "Bisi Ade", phone="+2348031234567")

    def test_a_phone_shaped_username_is_normalized_to_e164(self):
        user = make_user("0803 123 4567", "Bisi Ade")
        self.assertEqual(user.username, "+2348031234567")

    def test_a_username_equal_to_its_own_phone_is_not_a_self_collision(self):
        user = make_user("08031234567", "Bisi Ade", phone="08031234567")
        self.assertEqual(user.username, "+2348031234567")
        self.assertEqual(user.phone, "+2348031234567")

    def test_a_username_cannot_equal_another_users_phone(self):
        """Cross-column ambiguity: same value, different account, different column."""
        make_user("ada", "Ada Obi", phone="08031234567")
        with self.assertRaises(ValidationError):
            make_user("+2348031234567", "Bisi Ade")

    def test_any_countrys_valid_number_is_accepted_not_just_nigerian(self):
        user = make_user("ada", "Ada Obi", phone="+14155552671")
        self.assertEqual(user.phone, "+14155552671")

    def test_an_invalid_phone_is_rejected(self):
        with self.assertRaises(ValidationError):
            make_user("ada", "Ada Obi", phone="not-a-phone")


class MatchingIdentifierTests(TestCase):
    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")

    def test_resolves_across_username_email_and_phone(self):
        teacher = make_user("ada@stmarys.ng", "Ada Obi", email="ada@stmarys.ng")
        parent = make_user("bisi", "Bisi Ade", phone="08031234567")

        self.assertEqual(list(User.objects.matching_identifier("ADA@STMARYS.NG")), [teacher])
        self.assertEqual(list(User.objects.matching_identifier("+2348031234567")), [parent])
        self.assertEqual(list(User.objects.matching_identifier("08031234567")), [parent])

    def test_unknown_identifier_matches_nobody(self):
        self.assertEqual(list(User.objects.matching_identifier("nobody@nowhere.ng")), [])

    def test_blank_identifier_matches_nobody(self):
        self.assertEqual(list(User.objects.matching_identifier("")), [])
        self.assertEqual(list(User.objects.matching_identifier(None)), [])


class AmbiguousLoginRefusesToAuthenticateTests(TestCase):
    """order_by("pk").first() is gone: an ambiguous match must refuse, not guess."""

    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")

    def test_an_ambiguous_identifier_refuses_both_accounts(self):
        first = make_user("ada", "Ada Obi", phone="08031234567")
        second = make_user("bisi", "Bisi Ade")
        # assert_identifiers_unambiguous() stops this at save() time; forcing
        # it with a bare update() simulates the race the check is racy
        # against, so the backend's own guard can be exercised directly.
        User.objects.filter(pk=second.pk).update(username="+2348031234567")

        # "+2348031234567" now matches first via phone and second via
        # username exactly — genuinely ambiguous, so both logins refuse.
        self.assertIsNone(authenticate(username="+2348031234567", password=PASSWORD))
        # Each account's own, unambiguous identifier still works.
        self.assertEqual(authenticate(username="ada", password=PASSWORD), first)
        self.assertEqual(authenticate(username="08031234567", password=PASSWORD), first)

    def test_an_unambiguous_match_keeps_working(self):
        user = make_user("ada", "Ada Obi", phone="08031234567")
        self.assertEqual(authenticate(username="08031234567", password=PASSWORD), user)


class UpdateLastLoginSkipsTheCollisionCheckTests(TestCase):
    """update_last_login fires on every sign-in; it must not pay for Option C."""

    def setUp(self):
        self.school = make_school("St Mary's", "st-marys", "st_marys")

    def _select_queries(self, ctx):
        return [q for q in ctx.captured_queries if q["sql"].strip().upper().startswith("SELECT")]

    def test_updating_last_login_costs_zero_additional_selects(self):
        user = make_user("ada", "Ada Obi", phone="08031234567")
        with CaptureQueriesContext(connection) as ctx:
            user.save(update_fields=["last_login"])
        self.assertEqual(self._select_queries(ctx), [])

    def test_updating_a_non_identifier_field_also_skips_the_check(self):
        user = make_user("ada", "Ada Obi")
        user.short_name = "Ada"
        with CaptureQueriesContext(connection) as ctx:
            user.save(update_fields=["short_name"])
        self.assertEqual(self._select_queries(ctx), [])

    def test_updating_an_identifier_field_still_runs_the_check(self):
        make_user("bisi", "Bisi Ade", email="bisi@stmarys.ng")
        user = make_user("ada", "Ada Obi")
        user.email = "ada@stmarys.ng"
        with CaptureQueriesContext(connection) as ctx:
            user.save(update_fields=["email"])
        # One matching_identifier() query per distinct identifier value set on
        # the user (username and the new email both exist here) — the point
        # is that it's nonzero, unlike the last_login case above.
        self.assertGreater(len(self._select_queries(ctx)), 0)
