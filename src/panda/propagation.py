"""Batch-wide propagation of values that were detected somewhere.

Recognisers work on local context, and chat transcripts are short on context: the
customer's full name appears once after "your name please", then the agent writes
"Thank you Erica~" twenty lines later and no model sees a person there. Once a value
has been identified anywhere in the job, every later occurrence is the same person
and must be replaced too.

The CLI therefore runs detection in two passes. The first collects every value the
recognisers found across all files; the second sanitises with an extra recogniser
that matches those values, plus safe fragments of names, wherever they appear. The
same recogniser runs on the output during verification, so a known name that
survives is reported as a leak instead of being certified clean.

The collected terms live only in memory for the duration of one job. They are never
written anywhere, because a list of real names would be a re-identification key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from panda.detectors import ZH_HONORIFICS, ZH_SURNAMES, _is_name_word
from panda.model import Confidence, Detection, PIIKind
from panda.normalise import to_search_text

_HAN = re.compile(r"[一-鿿]")
# Splits CamelCase chat handles ("EricaL" -> "Erica", "L"; "JohnSmith" -> both parts).
_CAMEL_PART = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
_HONORIFIC_SUFFIX = re.compile(rf"(?:{'|'.join(ZH_HONORIFICS)})$")

# Everyday short forms that are not a prefix of the full name. Only names actually
# detected in the job are expanded, so this never finds a name on its own.
_NICKNAMES = {
    "alexander": ("Alex", "Xander"), "alexandra": ("Alex", "Sasha", "Lexi"),
    "andrew": ("Andy",), "anthony": ("Tony", "Ant"), "benjamin": ("Ben", "Benji"),
    "catherine": ("Cath", "Cathy", "Kate", "Katie"), "christopher": ("Chris",),
    "christine": ("Chris", "Chrissy"), "daniel": ("Dan", "Danny"), "david": ("Dave",),
    "deborah": ("Deb", "Debbie"), "edward": ("Ed", "Eddie", "Ted"),
    "elizabeth": ("Liz", "Lizzie", "Beth", "Eliza"), "gregory": ("Greg",),
    "james": ("Jim", "Jimmy", "Jamie"), "jennifer": ("Jen", "Jenny"),
    "jonathan": ("Jon", "Jonno"), "joseph": ("Joe", "Joey"),
    "katherine": ("Kath", "Kathy", "Kate", "Katie"), "kathryn": ("Kath", "Kate", "Katie"),
    "margaret": ("Maggie", "Meg", "Peggy"), "matthew": ("Matt",), "michael": ("Mike", "Mick"),
    "nicholas": ("Nick",), "patricia": ("Trish", "Patty"), "peter": ("Pete",),
    "rebecca": ("Bec", "Becky"), "richard": ("Rick", "Ricky"), "robert": ("Rob", "Bob", "Bobby"),
    "samantha": ("Sam",), "samuel": ("Sam", "Sammy"), "stephen": ("Steve",),
    "steven": ("Steve",), "thomas": ("Tom", "Tommy"), "timothy": ("Tim",),
    "victoria": ("Tori",), "william": ("Liam", "Billy"),
}
# A derived short form or name fragment that is also an ordinary word, month, weekday
# or state would erase that word from every document in the job wherever it starts a
# sentence.
_COMMON_SHORT_WORDS = frozenset("""
jan feb mar apr may jun jul aug sep sept oct nov dec mon tue tues wed thu thur thurs fri
sat sun nsw vic qld tas act wa sa nt aus usa dob ref tel fax mob acc bal dep fin gen max
min net per pro rep sec tax tot van war win col com con cor del des dir doc hon law lot
pet sim sus car can man den bus pin bat bet bid bin cap cat cop cot cut dam dim dot fan
fat fig fit fun gas gem gum gut hat hit hot hug jam jet job jog kit lab lad lap lid lip
log mad map mat mud nap nut pad pan pat pit pop pot pub rag ram rat rid rim rip rob rod
rot rub rug sad sap set sin sip sit sob sum tab tan tap tin tip top tub van vet wit
able also back bank bare base beat bell best bill bold bond cash case char chat clos
code cost date deal dear debt dele done fair fall farm fast file fine firm fund gain
gift give glad good hold home hope just keep kind land late lead less life line list
live loan long look loss love main make mark mess mind miss more most move must near
next note open over paid part pass past pay plan poli rate real rest rich risk role
safe sale same save seal sell send ship sign sole some sort step stop sure take talk
tell term test that time told tour tran tree true turn unit user very view vote wait
want ward warm wear week well west wife wind wise with word work year
""".split())
_VOWELS = "aeiouAEIOU"


def nicknames(given: str) -> set[str]:
    """Short forms of a Latin given name that people use in its place.

    Beyond the table, the first syllable is taken when it ends on a single consonant
    followed by a vowel (Rajesh -> Raj, Jonathan -> Jon, Daniel -> Dan). Where the
    split falls inside a consonant cluster (Benjamin, Katherine) there is no reliable
    short form, so none is guessed.
    """
    forms = set(_NICKNAMES.get(given.casefold(), ()))
    if len(given) >= 5 and given.isalpha() and given[:1].isupper() and given[1:].islower():
        match = re.match(r"[^aeiouy]*[aeiouy]+[^aeiouy]", given, re.I)
        if (
            match and 3 <= len(match.group()) <= len(given) - 2
            and given[len(match.group())] in _VOWELS
        ):
            forms.add(match.group())
    return {
        form for form in forms
        if form.casefold() not in _COMMON_SHORT_WORDS and _is_name_word(form)
    }


def name_terms(value: str) -> set[str]:
    """Return the value plus the fragments of it that can identify the person alone."""
    value = value.strip()
    terms = {value} if len(value) >= 2 else set()
    if _HAN.search(value):
        core = _HONORIFIC_SUFFIX.sub("", value)
        if not core or core[0] not in ZH_SURNAMES:
            return terms
        if 2 <= len(core) <= 4:
            terms.add(core)
        if len(core) == 3:
            # A three-character name is surname + two-character given name, and the
            # given name alone is how friends and agents often address the person.
            terms.add(core[1:])
        terms.update(core[0] + honorific for honorific in ZH_HONORIFICS)
        return terms
    letters = re.sub(r"[ .-]", "", value)
    if re.fullmatch(r"(?:[A-Z][ .-]){2,}[A-Z]\.?(?:[ ]{1,2}(?:[A-Z][ .-])+[A-Z])*", value.strip()):
        # "A-M-I-R" -> "Amir", "L-I-U Y-A-N-G" -> "Liu", "Yang".
        for word in re.split(r"[ ]{1,2}(?=[A-Z](?:[ .-]|$))", value.strip()):
            joined = re.sub(r"[ .-]", "", word)
            if len(joined) >= 2:
                terms.add(joined.title())
        if len(letters) >= 2:
            terms.add(letters.title())
        return terms
    parts = [part.strip("'’-") for part in re.split(r"[^A-Za-z'’-]+", value)]
    first = next((part for part in parts if part), "")
    titled = any(part[:1].isupper() and part[1:].islower() for part in parts if len(part) > 1)
    for part in parts:
        # In "Grace Okonkwo-Lee CRM.html" the lower-case and short upper-case tokens
        # are an extension and an acronym, not name fragments.
        if titled and part.islower():
            continue
        # Short capitals inside a longer value ("AU" of "AU-0120", "CRM") are codes or
        # acronyms; spreading them erased "AUD" and every staff code. A value that is
        # itself an alias ("JP") is kept whole above.
        if part.isupper() and len(part) <= 4 and part != value:
            continue
        if len(part) >= 2 and _is_name_word(part):
            terms.add(part)
            # Short forms come from the given name, which leads a Latin full name.
            if part == first:
                terms.update(nicknames(part))
        pieces = part.split("-")
        if len(pieces) == 2 and all(len(piece) >= 2 for piece in pieces) and (
            pieces[1][:1].islower() or max(len(piece) for piece in pieces) <= 4
        ):
            # A hyphenated romanised given name is also written joined: "Min-jun"
            # becomes "Minjun" in the same letter. Double-barrelled surnames
            # ("Lo-Brennan") have longer halves and keep their separate parts.
            terms.add(pieces[0].title() + pieces[1].lower())
        for piece in _CAMEL_PART.findall(part):
            # A lower-case piece after a hyphen ("jun" of "Min-jun") is only a
            # syllable, and "Jun"/"Min" alone would erase months and abbreviations.
            if (
                len(piece) >= 3 and piece[:1].isupper() and _is_name_word(piece)
                and (piece == part or piece.casefold() not in _COMMON_SHORT_WORDS)
            ):
                terms.add(piece)
    return terms


_FILENAME = re.compile(r"\.[A-Za-z0-9]{2,5}$")


def _worth_propagating(detection: Detection) -> bool:
    """Spread only values that look like names.

    A model guess is replaced where it was found either way; spreading it to every
    other document is reserved for plausible names. spaCy has tagged an attachment
    filename ("crm_export.html") as a person, which would otherwise erase "export"
    from every file in the job.
    """
    value = detection.text.strip()
    if _FILENAME.search(value):
        return False
    if detection.confidence is Confidence.NER and not _HAN.search(value):
        return any(character.isupper() for character in value)
    return True


def collect_terms(detections: Iterable[Detection]) -> dict[PIIKind, set[str]]:
    """Group detected values into the terms the second pass should search for."""
    terms: dict[PIIKind, set[str]] = {}
    for detection in detections:
        if detection.kind is PIIKind.NAME:
            if not _worth_propagating(detection):
                continue
            found = name_terms(detection.text)
        elif detection.kind in (PIIKind.ACCOUNT, PIIKind.ID_NUMBER, PIIKind.EMAIL):
            # Exact repeats only; shorter values are too likely to be coincidental.
            found = {detection.text.strip()} if len(detection.text.strip()) >= 5 else set()
        else:
            # Phones, dates and addresses have strong patterns of their own that find
            # every spelling, so propagating one spelling adds nothing.
            continue
        if found:
            terms.setdefault(detection.kind, set()).update(found)
    return terms


# A username-shaped token: letters, digits and . _ - with no spaces. Not part of an email
# (the address has its own recogniser) and not a dotted domain fragment.
_HANDLE = re.compile(
    r"(?<![A-Za-z0-9@._-])[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?![A-Za-z0-9@_-]|\.[A-Za-z0-9])"
)
# Hyphens are not a handle mark: "Young-adult" is an ordinary word, not a username.
_HANDLE_MARK = re.compile(r"[0-9._]")


def _handle_vocabulary(names: Iterable[str]) -> tuple[set[str], set[str]]:
    """Lower-case spellings a username built from a known name would contain.

    Returns the joined forms of each full name (elijahzimmerman, zimmermanelijah,
    ezimmerman, elijahz) and the single name words long enough to stand alone in a
    handle such as "yevgenia_ivanov12" or "Melissa2023".
    """
    joined: set[str] = set()
    singles: set[str] = set()
    for value in names:
        if _HAN.search(value):
            continue
        words = [word.casefold() for word in re.findall(r"[A-Za-z]+", value)]
        singles.update(
            word for word in words if len(word) >= 4 and word not in _COMMON_SHORT_WORDS
        )
        if len(words) >= 2 and len(words[0]) >= 2 and len(words[-1]) >= 2:
            given, surname = words[0], words[-1]
            joined.update({given + surname, surname + given})
            if len(surname) >= 4:
                joined.add(given[0] + surname)
            if len(given) >= 4:
                joined.add(given + surname[0])
    return joined, singles


def _latin_variants(term: str) -> set[str]:
    return {term, term.title(), term.upper(), term.lower()} if " " in term else {
        term, term[:1].upper() + term[1:], term.upper(),
    }


class KnownTermsRecognizer:
    """Find every occurrence of values already identified elsewhere in the job."""

    def __init__(self, terms: dict[PIIKind, set[str]], exempt: Iterable[str] = ()) -> None:
        self._patterns: list[tuple[PIIKind, re.Pattern[str]]] = []
        self._exempt = sorted({value for value in exempt if value}, key=len, reverse=True)
        self._names = terms.get(PIIKind.NAME, set())
        self._joined, self._singles = _handle_vocabulary(self._names)
        for kind, values in terms.items():
            alternatives: set[str] = set()
            for value in values:
                if _HAN.search(value):
                    alternatives.add(re.escape(value))
                elif kind is PIIKind.NAME:
                    # Fragments match in their written case forms only, so a client
                    # called Will or Grace does not erase every "will" and "grace".
                    alternatives.update(re.escape(v) for v in _latin_variants(value))
                else:
                    alternatives.add(re.escape(value))
            if not alternatives:
                continue
            # Longest first, so "EricaL" is taken whole before "Erica" can match inside.
            body = "|".join(sorted(alternatives, key=len, reverse=True))
            flags = re.IGNORECASE if kind is PIIKind.EMAIL else 0
            self._patterns.append(
                (kind, re.compile(rf"(?<![A-Za-z0-9])(?:{body})(?![a-z0-9])", flags))
            )

    @property
    def identity(self) -> str:
        return "propagation.v1"

    def prepare(self) -> None:
        return None

    def _is_handle(self, token: str) -> bool:
        """True for a username made from a known name: joined, or marked by . _ or digits.

        An unmarked token must be a whole joined name, so ordinary words that merely
        contain a name ("willingness") are left alone.
        """
        if token in self._names:
            return False
        folded = token.casefold()
        if re.sub(r"[^a-z]", "", folded) in self._joined:
            return True
        if not _HANDLE_MARK.search(token):
            return False
        return any(
            chunk in self._joined or chunk in self._singles
            for chunk in re.split(r"[^a-z]+", folded) if chunk
        )

    def detect(self, text: str) -> list[Detection]:
        if not self._patterns:
            return []
        search = to_search_text(text)
        # A known name inside a fixed dummy ("John" in "John Doe") is the replacement
        # doing its job, not a surviving occurrence.
        protected = [
            (match.start(), match.end())
            for value in self._exempt
            for match in re.finditer(re.escape(value), search, re.IGNORECASE)
        ]
        found = []
        for kind, pattern in self._patterns:
            for match in pattern.finditer(search):
                start, end = match.start(), match.end()
                if any(start < p_end and p_start < end for p_start, p_end in protected):
                    continue
                found.append(
                    Detection(
                        kind, start, end, text[start:end],
                        f"propagated.{kind.value}", Confidence.PATTERN,
                    )
                )
        if self._joined or self._singles:
            for match in _HANDLE.finditer(search):
                start, end = match.start(), match.end()
                if not self._is_handle(match.group()) or any(
                    start < p_end and p_start < end for p_start, p_end in protected
                ):
                    continue
                found.append(
                    Detection(
                        PIIKind.ACCOUNT, start, end, text[start:end],
                        "propagated.handle", Confidence.PATTERN,
                    )
                )
        return found
