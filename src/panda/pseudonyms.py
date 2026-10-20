"""Replacement values.

Every kind except names is replaced by one obvious, fixed template. Names come from a
small dummy pool, one dummy per person for the job, so a sanitised conversation stays
readable: if the client is named four times across three files, all four become the
same fake person and the reviewer can still follow who said what.

Assignments live in memory for one job only. There is no stored mapping table, which
is deliberate: a mapping table would be a re-identification key, and the requirements
rule that out.
"""

from __future__ import annotations

import re
from typing import Protocol

from panda.model import PIIKind
from panda.normalise import normalise_value

# Reserved by RFC 2606 and guaranteed never to be a real domain, so a generated
# address can never reach a real inbox.
SAFE_EMAIL_DOMAIN = "example.com"


class SurrogateSource(Protocol):
    """Anything that can turn a real value into its dummy replacement.

    The pipeline depends only on this one method, so a run can swap `DummyNames`
    for `FixedPseudonymiser` without any other code changing.
    """

    def surrogate(self, kind: PIIKind, value: str) -> str: ...


def _has_han(value: str) -> bool:
    """True if the value contains CJK ideographs.

    Script decides which dummy a name gets. Replacing 张伟 with a Latin name would
    make a Chinese transcript jarring to read.
    """
    return any("一" <= character <= "鿿" for character in value)


# One obvious, constant dummy per kind. `panda run` uses these for everything except
# names (see DummyNames below): a template is enough for a phone, email or address,
# and makes it plain at a glance that sanitising happened.
#
# The values still look like their field so the output stays parseable, but each is
# transparently fake. The leak check imports this set to tell an intended dummy apart
# from PII that genuinely survived; see leakcheck.KNOWN_DUMMIES.
FIXED_SURROGATES: dict[PIIKind, str] = {
    PIIKind.NAME: "John Doe",
    PIIKind.DOB: "01/01/1990",
    PIIKind.ADDRESS: "16 Sample Street, Sampleton NSW 2000",
    PIIKind.PHONE: "0400000000",
    PIIKind.EMAIL: f"john.doe@{SAFE_EMAIL_DOMAIN}",
    PIIKind.ACCOUNT: "00000000",
    PIIKind.ID_NUMBER: "000000000000000000",
}


# 张三 is the conventional Chinese placeholder name, the equivalent of John Doe. Using
# it in Chinese prose keeps the sentence readable where a Latin name would jar.
FIXED_HAN_NAME = "张三"

# Dummy people for `DummyNames`. Bland first names with uncommon surnames, so a pool
# name is unlikely to be a real client's; the Chinese parts give the conventional
# placeholder series (张三, 李四, 王五 ...) first. Always used whole: the rescan accepts
# these exact strings as intended output, and accepting a bare "Peter" would let a
# real Peter through.
_FIRSTS = (
    "Alan", "Susan", "Trevor", "Fiona", "Douglas", "Helen", "Roger", "Bridget", "Clive",
    "Naomi", "Stuart", "Paula", "Neil", "Vera", "Gordon", "Rita", "Malcolm", "Cheryl",
    "Barry", "Yvonne", "Dennis", "Joyce", "Keith", "Lorna",
)
_LASTS = (
    "Hollis", "Bramley", "Cardew", "Nettleton", "Vickers", "Ashdown", "Renshaw",
    "Copeland", "Thurlow", "Ellery", "Bagshaw", "Pilbeam", "Warlow", "Stanbury", "Hensley",
    "Lockyer", "Padgett", "Trescott", "Fairhurst", "Ockenden", "Wexford", "Brightwell",
    "Halloran", "Kembrey",
)
_HAN_SURNAMES = "张李王赵孙周吴郑"
_HAN_GIVEN = "三四五六七八九十"


def _pairs(firsts, lasts) -> list[tuple[str, str]]:
    """Every first/last pair exactly once, cycling both so neighbours differ in each."""
    return [
        (firsts[i % len(firsts)], lasts[(i + i // len(firsts)) % len(lasts)])
        for i in range(len(firsts) * len(lasts))
    ]


DUMMY_NAMES = tuple(f"{first} {last}" for first, last in _pairs(_FIRSTS, _LASTS))
DUMMY_HAN_NAMES = tuple(
    surname + given for surname, given in _pairs(_HAN_SURNAMES, _HAN_GIVEN)
)
ALL_FIXED_SURROGATES = frozenset(
    {*FIXED_SURROGATES.values(), FIXED_HAN_NAME, *DUMMY_NAMES, *DUMMY_HAN_NAMES}
)


class FixedPseudonymiser:
    """Replaces every value of a kind with the same obvious dummy.

    Deliberately ignores the real value, so two different clients both become
    "John Doe". That is the point: the output is meant to read as unmistakably
    sanitised rather than realistic. No secret is needed because nothing is derived
    from the input.
    """

    def surrogate(self, kind: PIIKind, value: str) -> str:
        if kind is PIIKind.NAME and _has_han(value):
            return FIXED_HAN_NAME
        return FIXED_SURROGATES[kind]


_TITLES = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof"})
_HANDLE_PART = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


class DummyNames(FixedPseudonymiser):
    """Gives each person their own dummy name; every other kind stays a fixed template.

    Names are handed out from the pool in order of first appearance and reused in a
    cycle once it runs out (`exhausted` is then set so the CLI can warn), so a
    transcript between a client and their spouse stays readable. The same person
    keeps the same dummy for the whole job, and a fragment ("Erica", "Mr Lee") gets
    the dummy of the full name it belongs to when that name was seen first. The
    assignments live in memory for one job only and are never written anywhere.
    """

    def __init__(self) -> None:
        self._assigned: dict[str, str] = {}
        self._by_word: dict[str, str] = {}
        self._counts = {False: 0, True: 0}
        self.exhausted = False

    def surrogate(self, kind: PIIKind, value: str) -> str:
        if kind is not PIIKind.NAME:
            return super().surrogate(kind, value)
        key = normalise_value(kind, value)
        if key not in self._assigned:
            han = _has_han(value)
            words = [word for word in key.split() if word.rstrip(".") not in _TITLES]
            # Handle parts link too: "NadiaK" and "nadia_k" are the same Nadia.
            parts = [
                piece.casefold() for piece in _HANDLE_PART.findall(value)
                if len(piece) >= 2 and piece.casefold() not in _TITLES
            ]
            linked = None
            if len(words) == 1:
                linked = next(
                    (self._by_word[w] for w in (words[0], *parts) if w in self._by_word), None
                )
            if linked is None:
                pool = DUMMY_HAN_NAMES if han else DUMMY_NAMES
                # Never hand out a dummy that shares a word with the real name
                # ("Fiona Grech" must not become "Fiona Nettleton").
                real = set(words)
                for _ in range(len(pool)):
                    linked = pool[self._counts[han] % len(pool)]
                    self._counts[han] += 1
                    shared = real & set(linked.casefold().split()) or linked in value
                    if han:
                        shared = shared or linked[0] in value
                    if not shared:
                        break
                self.exhausted |= self._counts[han] > len(pool)
            self._assigned[key] = linked
            for word in (*words, *parts) if len(words) > 1 or parts != words else ():
                self._by_word.setdefault(word, linked)
        return self._assigned[key]
