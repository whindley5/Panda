"""The offline HTML report view: built from the JSON reports only, loads nothing."""

import json
import re

import pytest

from panda import cli
from panda.report_html import REPORT_PAGE, ReportError, find_report_dir, render

RUN = {
    "completed_utc": "2026-09-29T13:05:15+00:00",
    "files": 1,
    "outcomes": {"file-0001": "sanitised", "file-0002": "skipped/unsupported"},
    "batch_status": "partial",
    "replacements": 2,
    "recognisers": ["patterns.au-cn.v2"],
    "verification": {"status": "clean_for_configured_recognisers", "files_scanned": 1,
                     "recognised_dummies": 2},
}
FILE = {
    "source": "file-0001",
    "counts": {"name": 1, "email": 1},
    "total": 2,
    "recognisers": ["patterns.au-cn.v2"],
    "detections": [
        {"kind": "name", "start": 0, "end": 5, "length": 5, "detector": "spacy.person",
         "confidence": "NER", "score": 0.85, "locator": "html/text/1",
         "surrogate": "<script>alert(1)</script>"},
        {"kind": "email", "start": 9, "end": 30, "length": 21, "detector": "pattern.email",
         "confidence": "VALIDATED", "score": None, "locator": "html/text/1",
         "surrogate": "person@example.invalid"},
    ],
}


def _job(tmp_path):
    report = tmp_path / "batch-SANITISED" / "batch-report"
    report.mkdir(parents=True)
    (report / "run.json").write_text(json.dumps(RUN))
    (report / "file-0001.json").write_text(json.dumps(FILE))
    return report


def test_page_is_self_contained_and_escaped():
    page = render(RUN, [FILE], batch="batch")
    # Nothing may be fetched: no external scripts, stylesheets, fonts or images.
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", page)
    assert "@import" not in page and "url(" not in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Verified, but some files were left out" in page
    assert "Model guess (0.85)" in page
    assert "1 replacement came from the language model" in page


def test_unverified_batch_is_flagged():
    page = render({**RUN, "batch_status": "unverified_debug"}, [FILE])
    assert "UNVERIFIED" in page and "Do not share" in page


def test_report_command_accepts_results_or_report_folder(tmp_path, capsys):
    report = _job(tmp_path)
    for target in (report.parent, report):
        assert cli.main(["report", str(target)]) == cli.EXIT_OK
        assert (report / REPORT_PAGE).is_file()
    assert "batch – sanitisation report" in (report / REPORT_PAGE).read_text("utf-8")
    assert not list(report.glob(".report-*"))


def test_report_command_refuses_non_reports(tmp_path, capsys):
    assert cli.main(["report", str(tmp_path / "missing")]) == cli.EXIT_ERROR
    assert cli.main(["report", str(tmp_path)]) == cli.EXIT_ERROR
    (tmp_path / "run.json").write_text("{not json")
    assert cli.main(["report", str(tmp_path)]) == cli.EXIT_ERROR
    assert "could not be read" in capsys.readouterr().err


def test_ambiguous_results_folder_is_refused(tmp_path):
    for name in ("a-report", "b-report"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "run.json").write_text(json.dumps(RUN))
    with pytest.raises(ReportError):
        find_report_dir(tmp_path)
