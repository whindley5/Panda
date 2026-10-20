"""The format-agnostic sanitising pipeline: detect, resolve, replace.

Every format handler funnels its text through here, so a fix to detection benefits
`.eml`, `.html` and `.docx` at once. Handlers are responsible only for pulling text
out of their format and putting it back without breaking the container.
"""

from __future__ import annotations

from dataclasses import replace

from panda.detectors import DetectorEngine, resolve_overlaps
from panda.model import Detection, Replacement, SanitiseResult
from panda.pseudonyms import SurrogateSource

# Enough preceding text for a label or question to inform detection, bounded so a
# long previous paragraph does not multiply NLP cost.
_CONTEXT_CHARS = 300


def detect_in_context(
    text: str, detector_engine: DetectorEngine, context: str | tuple[str, ...] = ""
) -> list[Detection]:
    """Detect PII in `text`, letting preceding `context` inform the recognisers.

    Handlers process one paragraph, table cell or block at a time, which separates a
    value from its label ("BSB" | "062-000") or a question from its answer. Context
    is scanned together with the text but never replaced: detections are clipped to
    `text` and anything wholly inside the context is dropped. Several alternative
    contexts may be given (a table cell's left neighbour and its column header); the
    union of what each reveals is kept.
    """
    engine = detector_engine or DetectorEngine()
    contexts = (context,) if isinstance(context, str) else context
    if not any(contexts):
        return engine.detect(text)
    found = []
    for option in contexts:
        prefix = option[-_CONTEXT_CHARS:]
        offset = len(prefix)
        for detection in engine.detect(prefix + text):
            if detection.end <= offset:
                continue
            start, end = max(detection.start, offset) - offset, detection.end - offset
            if text[start:end].strip():
                found.append(replace(detection, start=start, end=end, text=text[start:end]))
    return resolve_overlaps(found) if len(contexts) > 1 else found


def sanitise_text(
    text: str,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine | None = None,
    *,
    locator: str | None = None,
    context: str | tuple[str, ...] = "",
) -> SanitiseResult:
    """Replace every detected value in `text` with its dummy equivalent."""
    kept = detect_in_context(text, detector_engine, context)

    pieces: list[str] = []
    replacements: list[Replacement] = []
    cursor = 0
    for detection in kept:
        if locator is not None:
            detection = replace(detection, locator=locator)
        surrogate = pseudonymiser.surrogate(detection.kind, detection.text)
        pieces.append(text[cursor : detection.start])
        pieces.append(surrogate)
        replacements.append(Replacement(detection=detection, surrogate=surrogate))
        cursor = detection.end
    pieces.append(text[cursor:])

    return SanitiseResult(text="".join(pieces), replacements=replacements)
