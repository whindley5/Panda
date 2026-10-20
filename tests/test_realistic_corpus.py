"""End-to-end gate: no labelled PII survives in the synthetic realistic corpus.

Both corpora (tests/fixtures/{realistic,holdout}/generate.py) were written by separate
authors without reading the detectors, to mirror real chat exports, emails, letters
and forms. The holdout was used for tuning after its baseline was recorded
(32/326 survivors), so neither is a clean accuracy measure any more. A failure prints
the surviving synthetic values, which are invented and safe to show.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("fixture", ["realistic", "holdout"])
def test_corpus_has_no_surviving_pii(fixture):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "realistic_eval.py"), "--fixture", fixture],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "TOTAL: 0/" in result.stdout
