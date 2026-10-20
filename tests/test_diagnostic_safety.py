"""Synthetic client-controlled names must not become audit metadata."""

import hashlib
import json
from email.message import EmailMessage

import pytest

from panda import cli, formats
from panda.detectors import DetectorEngine
from panda.handlers import register_defaults
from panda.handlers.eml import extract_eml_units, sanitise_eml_file
from panda.model import SanitiseResult
from panda.pseudonyms import FixedPseudonymiser
from panda.report import build_report

NAME = "synthetic.person@example.org"
VALUE = "different.person@example.net"
MARKUP = f'<{NAME}>discard</{NAME}><p {NAME}="{VALUE}">email {VALUE}</p>'


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    handlers = dict(formats.HANDLERS)
    extractors = dict(formats.TEXT_EXTRACTORS)
    monkeypatch.setattr(cli, "default_detector_engine", DetectorEngine)
    register_defaults()
    yield
    formats.HANDLERS.clear()
    formats.HANDLERS.update(handlers)
    formats.TEXT_EXTRACTORS.clear()
    formats.TEXT_EXTRACTORS.update(extractors)


def mail():
    child = EmailMessage()
    child.set_content("safe body")
    # Structural headers precede this header, exposing checker ordinal drift.
    child[NAME] = VALUE
    child.add_attachment(MARKUP.encode(), maintype="text", subtype="html", filename=NAME + ".html")
    child.add_attachment(b"discard", maintype="image", subtype="png", filename=NAME + ".png")
    outer = EmailMessage()
    outer.set_content("safe cover")
    outer.add_attachment(child)
    return outer.as_bytes()


def test_nested_mime_locators_match_checker_and_preserve_evidence(tmp_path):
    source = tmp_path / (NAME + ".eml")
    source.write_bytes(mail())
    before = hashlib.sha256(source.read_bytes()).digest()
    destination = tmp_path / "safe.eml"
    result = sanitise_eml_file(source, destination, FixedPseudonymiser(), DetectorEngine())
    units = {unit.locator: unit.text for unit in extract_eml_units(destination)}
    report = build_report("file-0001", result, DetectorEngine().identity)
    assert report["total"] == sum(report["counts"].values()) > 0
    checked = 0
    for replacement, entry in zip(result.replacements, report["detections"], strict=True):
        detection = replacement.detection
        assert entry["detector"] == detection.detector
        assert entry["confidence"] == detection.confidence.name
        assert entry["score"] == detection.score
        assert entry["surrogate"] == replacement.surrogate
        if "/header/" in detection.locator or "/html/text/" in detection.locator:
            assert replacement.surrogate in units[detection.locator]
            checked += 1
    assert checked >= 2
    assert NAME not in json.dumps(report)
    assert VALUE not in json.dumps(report)
    assert hashlib.sha256(source.read_bytes()).digest() == before


@pytest.mark.parametrize("suffix,payload", [(".html", MARKUP.encode()), (".eml", mail())])
def test_success_reports_and_console_exclude_client_names(tmp_path, capsys, suffix, payload):
    source = tmp_path / NAME
    source.mkdir()
    path = source / (NAME + suffix)
    path.write_bytes(payload)
    output, reports = tmp_path / "output", tmp_path / "reports"
    assert cli.main(["run", str(source), "--output", str(output), "--report", str(reports)]) == 0
    assert cli.main(["check", "--output", str(output)]) == 0
    captured = capsys.readouterr()
    emitted = captured.out + captured.err
    for report in reports.glob("*.json"):
        emitted += report.read_text()
    assert NAME not in emitted and VALUE not in emitted
    assert path.read_bytes() == payload
    summary = json.loads((reports / "run.json").read_text())
    assert summary["replacements"] > 0
    assert summary["verification"]["recognised_dummies"] > 0


@pytest.mark.parametrize("stage", ["handler", "extractor", "discovery", "publication"])
def test_hostile_exception_names_and_messages_are_not_diagnostics(
    tmp_path, capsys, monkeypatch, stage,
):
    source = tmp_path / NAME
    source.mkdir()
    (source / (NAME + ".html")).write_text("<p>safe</p>")
    error = type(NAME, (OSError if stage == "publication" else ValueError,), {})

    def fail(*args, **kwargs):
        raise error(VALUE)

    if stage == "handler":
        formats.HANDLERS[".html"] = fail
    elif stage == "extractor":
        formats.TEXT_EXTRACTORS[".html"] = fail
    elif stage == "discovery":
        monkeypatch.setattr(cli, "discover", fail)
    else:
        monkeypatch.setattr(cli, "_promote_staged", fail)
    debug = stage == "extractor"
    assert cli.main(["run", str(source), "--opaque-names"]) == (cli.EXIT_DEBUG if debug else 2)
    captured = capsys.readouterr()
    assert NAME not in captured.out + captured.err
    assert VALUE not in captured.out + captured.err
    if debug:
        output, = tmp_path.glob("*-SANITISED/*-files")
        reports, = tmp_path.glob("*-SANITISED/*-report")
        assert (output / "UNVERIFIED" / "file-0001.html").exists()
        assert (output / "UNVERIFIED.txt").exists()
        assert json.loads((reports / "run.json").read_text())["verification"]["status"] == "failed"
        for report in reports.iterdir():
            assert NAME not in report.read_text()
            assert VALUE not in report.read_text()
    else:
        assert not list(tmp_path.glob("*-SANITISED/*-files"))
        assert not list(tmp_path.glob("*-SANITISED/*-report"))
        assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("suffix,payload", [(".html", MARKUP.encode()), (".eml", mail())])
def test_checker_refusal_does_not_echo_unsafe_markup_or_names(tmp_path, capsys, suffix, payload):
    (tmp_path / (NAME + suffix)).write_bytes(payload)
    assert cli.main(["check", "--output", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert NAME not in captured.out + captured.err
    assert VALUE not in captured.out + captured.err


def test_report_refuses_client_filename_as_source():
    with pytest.raises(ValueError, match="^report source must be an opaque file ordinal$"):
        build_report(NAME, SanitiseResult("", []))
