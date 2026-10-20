"""The leak check: rescan sanitised output and fail if any PII survived.

`run` is only as good as its detectors, and its most dangerous failure is silent:
a name that no recogniser understands can survive while the run still exits cleanly.
This module is a consistency pass, not independent proof of anonymisation. It
rescans finished output with the configured detectors and reports survivors.

Two rules shape the design, both inherited from the rest of the project:

- Fail closed. A file we cannot decode as text cannot be verified, so it is
  reported as unscannable and counts against a clean result rather than being
  waved through. Trusting an unreadable file is exactly the mistake the check
  exists to prevent.
- No raw values leave the process. A `Leak` carries the kind and the span, never
  `text`, for the same reason a `Report` does not: the output of an audit must not
  become a fresh copy of the data being audited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from panda.core import detect_in_context
from panda.detectors import DetectorEngine, _is_name_word
from panda.errors import refusal_reason
from panda.formats import ExtractedText, extractor_for, walk_files
from panda.limits import CapacityError, bounded, read_bytes
from panda.model import Confidence, PIIKind, safe_exception_category
from panda.pseudonyms import ALL_FIXED_SURROGATES

# Fixed placeholders, including names, are accepted in runs and standalone checks.
KNOWN_DUMMIES = frozenset(value.casefold() for value in ALL_FIXED_SURROGATES)
UNVERIFIED_MARKER = "UNVERIFIED.txt"


@dataclass(frozen=True)
class Leak:
    """One piece of PII found still present in a sanitised file.

    The span is a locator into that file's extracted text, deliberately without the
    matched value, so a leak report can be read and archived without re-exposing
    what leaked.
    """

    path: Path
    kind: PIIKind
    start: int
    end: int
    detector: str
    confidence: Confidence
    locator: str


@dataclass
class LeakReport:
    """The outcome of rescanning an output directory."""

    leaks: list[Leak] = field(default_factory=list)
    scanned: list[Path] = field(default_factory=list)
    # Files that could not be decoded as text, so nothing could be verified about
    # them. Kept apart from leaks because the operator's next step differs: a leak
    # means detection missed something, an unscannable file means the check itself
    # could not run and format-aware extraction is needed (arriving with the handlers).
    unscannable: list[Path] = field(default_factory=list)
    # Safe exception categories, never exception messages (which may contain PII).
    failure_reasons: dict[Path, str] = field(default_factory=dict)
    failure_details: dict[Path, str] = field(default_factory=dict)
    # Detections that matched a known dummy and so are the intended output, not a
    # leak. Counted, not stored, purely so the check can say out loud that it saw the
    # replacements it expected rather than silently dropping them.
    recognised_dummies: int = 0

    @property
    def clean(self) -> bool:
        """True only when every file was scanned and nothing survived."""
        return not self.leaks and not self.unscannable

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for leak in self.leaks:
            counts[leak.kind.value] = counts.get(leak.kind.value, 0) + 1
        return counts


def _read_text(path: Path) -> list[ExtractedText] | None:
    """Return the file's text for rescanning, or None if it cannot be read as text.

    A format whose sanitised output is a binary container (a `.docx` zip) registers a
    text extractor; that is used in preference to decoding the raw bytes, which would
    otherwise fail and wrongly mark every such file unscannable. Everything else is
    decoded strictly: `errors="replace"` would let a binary container through as
    mojibake and let the scan report it clean, the false reassurance this module
    exists to avoid. A container we can neither extract nor decode is surfaced as
    unscannable rather than trusted, including one whose extractor raised.
    """
    extractor = extractor_for(path)
    if extractor is not None:
        try:
            units = extractor(path)
            if not units:
                raise ValueError("extractor returned no scannable regions")
            return units
        except Exception as error:
            _READ_FAILURES[path] = safe_exception_category(error)
            _READ_DETAILS[path] = refusal_reason(error)
            return None
    try:
        return [ExtractedText(read_bytes(path).decode("utf-8"), "text")]
    except (UnicodeDecodeError, OSError, CapacityError) as error:
        _READ_FAILURES[path] = safe_exception_category(error)
        _READ_DETAILS[path] = refusal_reason(error)
        return None


def _only_dummies(
    text: str, start: int, end: int, allowed: frozenset[str], kind: PIIKind | None = None
) -> bool:
    """True when nothing identifying in the detected span lies outside a known dummy.

    A recogniser may take a dummy with punctuation glued to it ("John Doe~") or only
    part of it ("Doe" in "[Customer]John Doe 15:16:18"). Outside every dummy
    occurrence the span may hold only punctuation, spaces and lone Latin initials
    ("D. John Doe"). Any Han character counts ("张三明" leaves 明), and so does any digit,
    except that digits glued to a name dummy ("John Doe-2026-0417") are a reference,
    not part of the person.
    """
    folded = text.casefold()
    covered = [False] * len(text)
    for dummy in allowed:
        position = folded.find(dummy)
        while position != -1:
            covered[position : position + len(dummy)] = [True] * len(dummy)
            position = folded.find(dummy, position + 1)
    span = range(start, end)
    if not any(covered[i] for i in span):
        return False
    uncovered = "".join(text[i] if not covered[i] else " " for i in span)
    if any("一" <= character <= "鿿" for character in uncovered):
        return False
    if kind is not PIIKind.NAME and any(character.isdigit() for character in uncovered):
        return False
    # Leftover ordinary words ("Trust", "bank", "Centre") are not a person either.
    return not any(
        len(word) > 1 and _is_name_word(word) for word in re.findall(r"[A-Za-z]+", uncovered)
    )


_READ_FAILURES: dict[Path, str] = {}
_READ_DETAILS: dict[Path, str] = {}


@bounded
def scan_output(
    output_dir: Path,
    detector_engine: DetectorEngine | None = None,
    known_dummies: frozenset[str] = KNOWN_DUMMIES,
    *,
    staged: bool = False,
    generated_dummies: dict[Path, frozenset[str]] | None = None,
) -> LeakReport:
    """Rescan every file under `output_dir` for surviving PII.

    Fixed placeholders are accepted everywhere. Additional generated values are
    keyed by output path, so realistic replacements remain file-scoped.
    """
    report = LeakReport()
    _READ_FAILURES.clear()
    _READ_DETAILS.clear()
    if (output_dir / UNVERIFIED_MARKER).exists():
        report.unscannable.append(output_dir)
        report.failure_reasons[output_dir] = "UnverifiedDebugOutput"
        report.failure_details[output_dir] = "E_UNVERIFIED: debug output may contain PII"
        return report
    if (not staged and ".staging-" in output_dir.name) or (output_dir / "run.json").exists():
        report.unscannable.append(output_dir)
        report.failure_reasons[output_dir] = "NotCompletedOutput"
        report.failure_details[output_dir] = (
            "E_INCOMPLETE: incomplete publication;"
            " rerun into fresh destinations"
        )
        return report
    for path in walk_files(output_dir):
        units = _read_text(path)
        if units is None:
            report.unscannable.append(path)
            report.failure_reasons[path] = _READ_FAILURES.get(path, "ExtractionError")
            report.failure_details[path] = _READ_DETAILS[path]
            continue
        report.scanned.append(path)
        try:
            # The same preceding-unit context the handlers use, so a lone table
            # header such as "Item" is not read as a person on the rescan only.
            detections = [
                (unit, detection)
                for previous, unit in zip([None, *units], units, strict=False)
                for detection in detect_in_context(
                    unit.text, detector_engine or DetectorEngine(),
                    previous.text + "\n" if previous else "",
                )
            ]
        except Exception as error:
            report.scanned.remove(path)
            report.unscannable.append(path)
            report.failure_reasons[path] = safe_exception_category(error)
            report.failure_details[path] = refusal_reason(error)
            continue
        allowed = (
            known_dummies | generated_dummies.get(path, frozenset())
            if generated_dummies is not None else known_dummies
        )
        for unit, detection in detections:
            if _only_dummies(
                unit.text, detection.start, detection.end, allowed, detection.kind
            ):
                report.recognised_dummies += 1
                continue
            report.leaks.append(
                Leak(
                    path=path,
                    kind=detection.kind,
                    start=detection.start,
                    end=detection.end,
                    detector=detection.detector,
                    confidence=detection.confidence,
                    locator=unit.locator,
                )
            )
    return report
