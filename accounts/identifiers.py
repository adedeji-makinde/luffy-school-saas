"""Turning what someone types into the one form we store.

A phone number can be typed a dozen ways — 08031234567, +2348031234567,
0803 123 4567 — and they are all the same person's number. If we store
whatever was typed, two spellings of one number become two rows, and a
family that shows up with "0803..." on the enrolment form and "+234803..."
on the parent portal ends up split across two accounts, each holding half
of their children. Normalizing to a single canonical form (E.164) before
anything is saved or compared is what keeps that from happening.

Usernames are a harder case, because not every username is a phone number.
A school-issued handle like STM-08031234567 *contains* digits that look
like a phone number, but rewriting it would silently turn one student's
handle into another's, or into an actual phone number that collides with
a real parent. So username normalization only fires when the ENTIRE string
is phone-shaped — digits and phone punctuation only, nothing else — and
otherwise leaves the value untouched. See canonical_username() below.
"""

import re

import phonenumbers
from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.core.exceptions import ValidationError

# Only digits and the punctuation a person might type in a phone number.
# Anything else (letters, colons, slashes, dashes-as-separators-in-a-code)
# disqualifies the whole string from being treated as a phone number at all.
_PHONE_SHAPED = re.compile(r"^[+()\d\s.\-]+$")


def normalize_phone(value):
    """Parse `value` as a phone number and return it in E.164 form.

    Raises ValidationError if `value` is not a valid phone number anywhere
    in the world. A number with no country code is parsed as
    settings.PHONE_DEFAULT_REGION (a parsing default, not a restriction) —
    numbers from any other country, including landlines and toll-free
    numbers, are accepted as-is.
    """
    if not value:
        return None
    try:
        parsed = phonenumbers.parse(value, settings.PHONE_DEFAULT_REGION)
    except phonenumbers.NumberParseException as exc:
        raise ValidationError(f"{value!r} is not a valid phone number.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValidationError(f"{value!r} is not a valid phone number.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def try_normalize_phone(value):
    """Like normalize_phone(), but returns None instead of raising.

    For sign-in, where "this isn't a phone number" is an ordinary, expected
    answer (someone typing a username or email), not an error.
    """
    try:
        return normalize_phone(value)
    except ValidationError:
        return None


def canonical_username(value):
    """Normalize `value` to E.164 if, and only if, it is entirely phone-shaped.

    The gate is strict on purpose: only a string made up solely of digits
    and phone punctuation (^[+()\\d\\s.\\-]+$) is treated as a phone number.
    A single letter, colon or slash disqualifies it outright, so a
    school-issued handle like STM-08031234567 or tel:08031234567 is
    returned unchanged. A loose normalizer would rewrite handles like that
    into real phone numbers and could collide two different students' handles
    that differ only in punctuation — this is what stops that.
    """
    if not value:
        return value
    if not _PHONE_SHAPED.match(value):
        return value
    normalized = try_normalize_phone(value)
    return normalized if normalized is not None else value


def normalize_email(value):
    """Lowercase and normalize an email address, or return None if blank."""
    if not value:
        return None
    return BaseUserManager().normalize_email(value).lower() or None
