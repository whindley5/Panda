"""Offline English name and street-address recognition through direct spaCy."""

from __future__ import annotations

import importlib.util
import re
from importlib.metadata import version
from typing import Any

from panda.detectors import is_organisation
from panda.model import Confidence, Detection, PIIKind
from panda.normalise import to_search_text

_DEFAULT_MODEL = "en_core_web_md"
_STREET_LEVEL = re.compile(
    r"\b(?:street|st|road|rd|avenue|ave|parade|pde|crescent|cres|lane|ln|"
    r"drive|dr|boulevard|blvd|highway|hwy|place|pl|court|ct|terrace|tce)\b",
    re.IGNORECASE,
)
_NAME_PREFIX = re.compile(
    r"^(?:(?:Mr|Mrs|Ms|Miss|Dr|Prof|Customer|Client)\.?\s+)+", re.IGNORECASE
)
_NAME_POSSESSIVE = re.compile(r"(?:['’]s)$", re.IGNORECASE)
_CACHE_CHARS = 64_000_000


_WORD_BEFORE = re.compile(r"(?:^|(?<=[\s(,]))([A-Z][a-z]+(?:['’][A-Z][a-z]+)?)[ \t]+$")
_WORD_AFTER = re.compile(r"^[ \t]+([A-Z][a-z]+(?:['’-][A-Z][a-z]+)?)\b")
_SENTENCE_END = re.compile(r"(?:^|[.!?:;\n]\s*)$")


# Han is masked before parsing, so a person span can run across Chinese prose into
# the next field ("Emily的邮箱是 emily@example.com"); Han runs separate fields too.
_FIELD_SEPARATOR = re.compile(r"[\r\n\[\]<>]|\s[-–—|/]\s|[一-鿿]+")


def _name_segment(raw: str) -> tuple[int, int] | None:
    """Return the span of the first field-delimited segment that holds a name word.

    Brackets and spaced dashes separate fields in chat headers and footers
    ("Customer]Erica Lee", "Amelia Foster - Confidential - Acme internal").
    """
    from panda.detectors import _NOT_NAME_WORDS, _ORGANISATION, _is_name_word

    segments = []
    cursor = 0
    for boundary in [*_FIELD_SEPARATOR.finditer(raw), None]:
        stop = boundary.start() if boundary else len(raw)
        piece = raw[cursor:stop]
        found = [
            m for m in re.finditer(r"[^\W\d_][\w'’.-]*", piece) if _is_name_word(m.group())
        ]
        if found:
            words = [m.group() for m in found]
            # Start at the first capitalised word that can be part of a name, dropping
            # lower-case lead-ins such as "met"; "May Green" keeps its capitalised "May".
            capitalised = [
                m for m in re.finditer(r"[^\W\d_][\w'’.-]*", piece)
                if m.group()[:1].isupper() and m.group().casefold() not in _NOT_NAME_WORDS
                and m.group().casefold() not in _ORGANISATION.get().brands
            ]
            first = capitalised[0] if capitalised else found[0]
            segments.append((words, (cursor + first.start(), cursor + len(piece.rstrip()))))
        cursor = boundary.end() if boundary else stop
    # Prefer a capitalised name; fall back to lower-case ("i spoke to alice mercer").
    for words, span in segments:
        if any(word[:1].isupper() for word in words):
            return span
    return segments[0][1] if segments else None


def _extend_to_adjacent_name(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a one-word person to a neighbouring capitalised name word.

    spaCy often tags only the surname of an unfamiliar full name ("Our referrer
    Sanjay Patel" -> "Patel"), leaving the given name behind. A capitalised,
    non-stop-word neighbour is taken too, except at the start of a sentence where
    capitalisation says nothing.
    """
    from panda.detectors import _is_name_word

    if " " in text[start:end].strip():
        return start, end
    window_start = max(0, start - 40)
    before = _WORD_BEFORE.search(text[window_start:start])
    if before and _is_name_word(before.group(1)):
        word_start = window_start + before.start(1)
        if not _SENTENCE_END.search(text[max(0, word_start - 3) : word_start]):
            return word_start, end
    after = _WORD_AFTER.match(text[end : end + 40])
    if after and _is_name_word(after.group(1)):
        return start, end + after.end(1)
    return start, end


class ModelSetupError(RuntimeError):
    """The pinned offline NLP installation is absent or cannot be loaded."""


class SpacyRecognizer:
    """Lazily load one pinned, local spaCy pipeline per job."""

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._nlp: Any | None = None
        # The CLI detects every text twice (collect, then sanitise with propagated
        # names). Parsing is by far the slowest step, so remember results per text.
        self._cache: dict[str, list[Detection]] = {}
        self._cached_chars = 0

    @property
    def identity(self) -> str:
        try:
            return (
                f"spacy-{version('spacy')}/{self.model_name}-{version(self.model_name)}/"
                "panda-ner-v2"
            )
        except Exception:
            raise ModelSetupError("local NLP package metadata is unavailable") from None

    def prepare(self) -> None:
        """Validate and load before the CLI reads document content or stages output."""
        self._load()

    def _load(self) -> Any:
        if self._nlp is not None:
            return self._nlp
        if importlib.util.find_spec(self.model_name) is None:
            raise ModelSetupError("required local NLP model is not installed")

        # Loading a package never downloads models. Keep the full pipeline to
        # preserve the entity boundaries evaluated during the provider comparison.
        try:
            import spacy

            if version("spacy") != "3.8.16" or version(self.model_name) != "3.8.0":
                raise ModelSetupError("local NLP package versions do not match the release")
            nlp = spacy.load(self.model_name)
        except ModelSetupError:
            raise
        except Exception:
            # Import/loader exceptions can contain arbitrary local paths. Expose a
            # stable diagnosis without passing their messages into logs or reports.
            raise ModelSetupError("local NLP model could not be loaded") from None
        if "ner" not in nlp.pipe_names:
            raise ModelSetupError("local NLP model has no entity recogniser")
        self._nlp = nlp
        return self._nlp

    def detect(self, text: str) -> list[Detection]:
        cached = self._cache.get(text)
        if cached is not None:
            return list(cached)
        detections = self._detect(text)
        if self._cached_chars + len(text) > _CACHE_CHARS:
            self._cache.clear()
            self._cached_chars = 0
        self._cache[text] = detections
        self._cached_chars += len(text)
        return list(detections)

    def _detect(self, text: str) -> list[Detection]:
        if not any(character.isascii() and character.isalpha() for character in text):
            return []
        # The English model invents PERSON spans in Han prose and can truncate
        # adjacent English names. Mask Han at identical offsets; jieba and shared
        # contextual patterns own Chinese recognition. Fold export punctuation too.
        search = to_search_text(text).translate(str.maketrans({'。': ';', '、': ','}))
        search = "".join(" " if "一" <= char <= "鿿" else char for char in search)
        document = self._load()(search)
        detections: list[Detection] = []
        for result in document.ents:
            start, end = result.start_char, result.end_char
            raw = text[start:end]
            if result.label_ == "PERSON":
                # Staff codes such as "AU-0120" get tagged as people; names hold no digits.
                if any(character.isdigit() for character in raw):
                    continue
                kind = PIIKind.NAME
                # spaCy occasionally joins the next transcript field to a person
                # (for example ``Margaret Whitfield\nAccount``). A person locator
                # must never consume a structural line boundary: doing so deletes
                # the label needed by the deterministic account recogniser.
                # Brackets and spaced dashes separate fields in chat headers and
                # document footers ("Amelia Foster - Confidential - Acme internal").
                if is_organisation(raw):
                    continue
                segment = _name_segment(raw)
                if segment is None:
                    continue
                start, end = start + segment[0], start + segment[1]
                raw = text[start:end]
                prefix = _NAME_PREFIX.match(raw)
                if prefix:
                    start += prefix.end()
                # spaCy may include a field delimiter in a person's span. Keep
                # that delimiter intact and make surrogate rescans comparable.
                # Trim masked trailing Han in search text, retaining source prose.
                end = start + len(search[start:end].rstrip(" \t:;,~!?.-"))
                possessive = _NAME_POSSESSIVE.search(text[start:end])
                if possessive:
                    end = start + possessive.start()
                start, end = _extend_to_adjacent_name(text, start, end)
                raw = text[start:end]
            elif result.label_ in {"GPE", "LOC"} and _STREET_LEVEL.search(raw):
                kind = PIIKind.ADDRESS
            else:
                continue
            if start >= end or not raw.strip():
                continue
            detections.append(
                Detection(
                    kind=kind,
                    start=start,
                    end=end,
                    text=raw,
                    detector=f"spacy.{result.label_.lower()}",
                    confidence=Confidence.NER,
                    # spaCy Span has no calibrated per-entity probability.
                    score=None,
                )
            )
        return detections
