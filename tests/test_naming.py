"""Readable output names: source folders and file names, with PII replaced."""

import json

import pytest

from panda import cli
from panda.detectors import default_detector_engine
from panda.naming import NamePlanner
from panda.pseudonyms import DUMMY_NAMES, FixedPseudonymiser


@pytest.fixture(scope="module")
def planner():
    engine = default_detector_engine()
    engine.prepare()
    return NamePlanner(FixedPseudonymiser(), engine)


@pytest.mark.parametrize(("name", "expected"), [
    ("synthetic.person@example.org", "john.doe@example.com"),
    ("call 0412.345.678", "call 0400000000"),
    ("Quarterly summary", "Quarterly summary"),
    ("report.final", "report.final"),
])
def test_components(planner, name, expected):
    assert planner.component(name) == expected


def test_dummy_with_path_separator_is_made_valid(planner):
    assert "/" not in (planner.component("DOB 14/03/1984") or "")
    assert planner.component("born 14.03.1984") == "born 01-01-1990"


@pytest.mark.parametrize("name", ["CON", "nul.txt", "..", ""])
def test_unusable_names_fall_back(planner, name):
    assert planner.component(name) is None


def _batch(tmp_path):
    source = tmp_path / "Batch"
    (source / "Rajesh Venkataraman").mkdir(parents=True)
    (source / "Rajesh Venkataraman" / "letter.html").write_text(
        "<p>Mr Rajesh Venkataraman wrote in.</p>")
    (source / "Erica Lee complaint.html").write_text("<p>Customer Erica Lee called.</p>")
    (source / "Tom Baker.html").write_text("<p>Dear Tom Baker, thanks.</p>")
    (source / "Summary.html").write_text("<p>Nothing personal.</p>")
    return source


def test_run_keeps_folders_and_replaces_names(tmp_path, capsys):
    source = _batch(tmp_path)
    assert cli.main(["run", str(source)]) == cli.EXIT_OK
    files = tmp_path / "Batch-SANITISED" / "Batch-files"
    published = sorted(p.relative_to(files).as_posix() for p in files.rglob("*") if p.is_file())
    # Each person gets their own dummy, consistent between content and names.
    assert "Summary.html" in published
    assert len([p for p in published if p.endswith("/letter.html")]) == 1
    assert any(p.endswith(" complaint.html") for p in published)
    assert all(
        p == "Summary.html" or any(dummy in p for dummy in DUMMY_NAMES) for p in published
    )
    letter, = (p for p in published if p.endswith("/letter.html"))
    assert letter.split("/")[0] in (files / letter).read_text()
    for raw in ("Rajesh", "Venkataraman", "Erica", "Tom Baker"):
        assert all(raw not in path for path in published)
        assert raw not in capsys.readouterr().out
    reports = tmp_path / "Batch-SANITISED" / "Batch-report"
    outputs = [
        json.loads(path.read_text())["outputs"][0] for path in sorted(reports.glob("file-*.json"))
    ]
    assert sorted(outputs) == published
    assert json.loads((reports / "run.json").read_text())["output_names"] == "source"


def test_opaque_names_flag(tmp_path):
    source = _batch(tmp_path)
    assert cli.main(["run", str(source), "--opaque-names"]) == cli.EXIT_OK
    files = tmp_path / "Batch-SANITISED" / "Batch-files"
    assert sorted(p.name for p in files.iterdir()) == [
        f"file-000{n}.html" for n in range(1, 5)
    ]
    run = json.loads((tmp_path / "Batch-SANITISED" / "Batch-report" / "run.json").read_text())
    assert run["output_names"] == "opaque"
