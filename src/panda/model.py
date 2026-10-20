"""Core data types shared by every detector and handler.

The one rule that shapes all of this: `Detection.text` holds raw client data, so it
must never reach a report, a log line or an exception message. Everything that
leaves the process carries spans and kinds instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from panda.limits import CapacityError


def safe_exception_category(error: Exception) -> str:
    """Use fixed labels: even a third-party exception class name is untrusted."""
    for category in (
        CapacityError, UnicodeDecodeError, PermissionError, FileNotFoundError, OSError,
        ValueError, TypeError, RuntimeError,
    ):
        if isinstance(error, category):
            return category.__name__
    return "ExtractionError"


class PIIKind(str, Enum):
    """A category of personally identifying information.

    Kind drives both which pool a replacement is drawn from and how a value is
    normalised, so two spellings of one phone number get the same dummy value.
    """

    NAME = "name"
    DOB = "dob"
    ADDRESS = "address"
    PHONE = "phone"
    EMAIL = "email"
    ACCOUNT = "account"
    ID_NUMBER = "id_number"


class Confidence(int, Enum):
    """How much a detection can be trusted, which decides overlap precedence.

    VALIDATED means a pattern matched and a checksum or parse confirmed it.
    PATTERN means a contextual pattern matched without an intrinsic validator. NER
    means a statistical model guessed a name or address, so it loses overlaps to
    deterministic recognisers.
    """

    NER = 0
    PATTERN = 1
    VALIDATED = 2


@dataclass(frozen=True)
class Detection:
    """One span of the source text identified as PII.

    Spans are half-open character offsets into the text that was scanned, so
    `text[start:end]` is the matched value.
    """

    kind: PIIKind
    start: int
    end: int
    text: str
    detector: str
    confidence: Confidence = Confidence.PATTERN
    # Provider-native score where available. Deterministic recognisers leave this
    # unset because their Confidence tier carries the stronger semantic guarantee.
    score: float | None = None
    # Safe, format-owned structural location (for example ``pdf/page/2`` or
    # ``eml/part/1/header/1``). It contains no source value or filename.
    locator: str | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"invalid span {self.start}:{self.end}")
        if self.score is not None and not 0 <= self.score <= 1:
            raise ValueError(f"invalid detector score {self.score}")

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: Detection) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Replacement:
    """A detection paired with the dummy value that will stand in for it."""

    detection: Detection
    surrogate: str


@dataclass
class SanitiseResult:
    """The outcome of sanitising one piece of text."""

    text: str
    replacements: list[Replacement]

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for replacement in self.replacements:
            key = replacement.detection.kind.value
            counts[key] = counts.get(key, 0) + 1
        return counts
