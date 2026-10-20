"""Audit reports.

A report exists so a reviewer can judge whether a job did enough work, without
being handed the data the job was meant to remove. So it carries what was found,
where, and what replaced it, and never the original value.

That last constraint is load bearing. A report holding raw values would be a second
copy of the client's PII sitting in a directory nobody thinks of as sensitive.
`tests/test_core.py` asserts it, and the assertion should not be relaxed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from panda.model import SanitiseResult


def build_report(
    source: str,
    result: SanitiseResult,
    recognisers: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Summarise one file using a job-owned ordinal, never a source path."""
    if re.fullmatch(r"file-[0-9]{4,}", source) is None:
        raise ValueError("report source must be an opaque file ordinal")
    return {
        "source": source,
        "counts": result.counts,
        "total": len(result.replacements),
        "recognisers": list(recognisers),
        "detections": [
            {
                "kind": replacement.detection.kind.value,
                "start": replacement.detection.start,
                "end": replacement.detection.end,
                "length": replacement.detection.length,
                "detector": replacement.detection.detector,
                "confidence": replacement.detection.confidence.name,
                "score": replacement.detection.score,
                "locator": replacement.detection.locator,
                # The dummy value is safe to include and makes a report reviewable
                # on its own. The value it replaced is not, and is never recorded.
                "surrogate": replacement.surrogate,
            }
            for replacement in result.replacements
        ],
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Write a report as UTF-8 JSON, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
