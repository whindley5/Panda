"""Text normalisation, used for two different jobs that must not be confused.

`to_search_text` produces a string that detectors scan. It is deliberately a
character-for-character mapping, never a length-changing one, so that a span found
in the search text points at exactly the same characters in the original. Anything
that changed the length here would silently corrupt every replacement.

`normalise_value` collapses the spellings of one real value to a single key, so
"0412 908 771" and "+61412908771" resolve to the same dummy phone number.
"""

from __future__ import annotations

import re
import unicodedata

from panda.model import PIIKind

# Full-width ASCII (U+FF01 to U+FF5E) maps onto ASCII by a fixed offset, one
# character to one character. Chinese exports use these constantly, and without
# this a full-width phone number reads as unmatchable punctuation.
_FULLWIDTH_OFFSET = 0xFEE0
_SEARCH_TABLE = {code: code - _FULLWIDTH_OFFSET for code in range(0xFF01, 0xFF5F)}
# Ideographic space, same width in characters as an ASCII space.
_SEARCH_TABLE[0x3000] = 0x20


def to_search_text(text: str) -> str:
    """Return a scannable form of `text` with identical character offsets."""
    converted = text.translate(_SEARCH_TABLE)
    if len(converted) != len(text):  # pragma: no cover - guards an impossible case
        raise AssertionError("search text changed length, spans would be wrong")
    return converted


_NON_ALNUM = re.compile(r"[^0-9a-z]", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _normalise_phone(value: str) -> str:
    """Reduce a phone number to a comparable national form.

    International prefixes are stripped so that the same subscriber written two
    ways produces one key. Australia writes +61 4xx as 04xx locally, and mainland
    China writes +86 13x as 13x, so both drop to the local spelling.
    """
    digits = _digits(value)
    if digits.startswith("61") and len(digits) == 11:
        return "0" + digits[2:]
    if digits.startswith("86") and len(digits) == 13:
        return digits[2:]
    return digits


def _normalise_dob(value: str) -> str:
    """Reduce a date to its digits.

    Ordering is left alone on purpose. Deciding whether 04/03 is March or April
    needs locale knowledge this layer does not have, and guessing would merge two
    genuinely different dates onto one dummy value.
    """
    return _digits(value)


def normalise_value(kind: PIIKind, value: str) -> str:
    """Return the key under which `value` is looked up when choosing a dummy value."""
    # Compatibility normalisation folds full-width and other presentation forms,
    # so the key does not depend on how the source happened to encode a character.
    value = unicodedata.normalize("NFKC", value).strip()

    if kind is PIIKind.PHONE:
        return _normalise_phone(value)
    if kind is PIIKind.DOB:
        return _normalise_dob(value)
    if kind is PIIKind.EMAIL:
        return value.casefold()
    if kind in (PIIKind.ACCOUNT, PIIKind.ID_NUMBER):
        return _NON_ALNUM.sub("", value).upper()
    # Names and addresses: fold case and collapse runs of whitespace. Chinese text
    # has no spaces between characters, so this is a no-op there and harmless.
    return _WHITESPACE.sub(" ", value.casefold())
