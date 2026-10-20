"""Synthetic regression coverage for scoped verification and batch isolation."""

import json
import re

import pytest

from panda import cli, formats
from panda.core import sanitise_text
from panda.errors import UnsupportedError
from panda.leakcheck import scan_output
from panda.model import Detection, PIIKind


@pytest.fixture(autouse=True)
def isolated_registry():
    handlers = dict(formats.HANDLERS)
    extractors = dict(formats.TEXT_EXTRACTORS)
    yield
    formats.HANDLERS.clear()
    formats.HANDLERS.update(handlers)
    formats.TEXT_EXTRACTORS.clear()
    formats.TEXT_EXTRACTORS.update(extractors)


class NameEngine:
    identity = ("synthetic-names",)

    def prepare(self):
        pass

    def with_provider(self, provider):
        # Propagation is covered in test_propagation; keep these cases single-engine.
        return self

    def detect(self, text):
        return [
            Detection(PIIKind.NAME, match.start(), match.end(), match.group(), "test")
            for match in re.finditer(r"[A-Z][a-z]+ [A-Z][a-z]+", text)
        ]


@pytest.mark.parametrize(
    "failure", ["unsupported", "residual", "parser", "checker", "handler_unsupported"],
)
@pytest.mark.parametrize("automatic", [False, True])
def test_batch_attempts_later_files_and_summarises(
    tmp_path, monkeypatch, capsys, failure, automatic,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a-private@example.org.case").write_text("Alice Smith")
    (source / "z-private@example.org.case").write_text("Carol Jones")
    if failure == "unsupported":
        (source / "private@example.org.xyz").write_text("synthetic")
    attempted = []

    def handler(path, destination, pseudonymiser, engine):
        if ".panda-prepass-" not in str(destination):
            attempted.append(path.name[0])
        if path.name.startswith("a") and failure == "handler_unsupported":
            raise UnsupportedError("private@example.org")
        if path.name.startswith("a") and failure == "parser":
            destination.write_text("Private Person")
            raise ValueError("private@example.org")
        result = sanitise_text(path.read_text(), pseudonymiser, engine)
        destination.write_text(
            "Private Person" if path.name.startswith("a") and failure == "residual"
            else result.text
        )
        return result

    def extractor(path):
        if path.name.startswith("file-0001") and failure == "checker":
            raise ValueError("private@example.org")
        return [formats.ExtractedText(path.read_text(), "text")]

    monkeypatch.setattr(cli, "register_defaults", lambda: None)
    monkeypatch.setitem(formats.HANDLERS, ".case", handler)
    monkeypatch.setitem(formats.TEXT_EXTRACTORS, ".case", extractor)
    monkeypatch.setattr(cli, "default_detector_engine", NameEngine)
    output = tmp_path / "output"
    reports = tmp_path / "reports"
    args = ["run", str(source), "--opaque-names"]
    if not automatic:
        args.extend(["--output", str(output), "--report", str(reports)])
    debug = failure in {"residual", "checker"}
    assert cli.main(args) == (cli.EXIT_DEBUG if debug else cli.EXIT_PARTIAL)
    if automatic:
        output, = tmp_path.glob("*-SANITISED/*-files")
        reports, = tmp_path.glob("*-SANITISED/*-report")
    assert attempted == ["a", "z"]
    captured = capsys.readouterr()
    if automatic:
        assert output.parent == reports.parent
        assert "Saved    <source folder name>-SANITISED, beside" in captured.out
    summary = next(
        line.strip() for line in captured.out.splitlines() if line.strip().startswith("Files ")
    )
    expected = {
        "unsupported": "2 sanitised · 1 skipped",
        "handler_unsupported": "1 sanitised · 1 skipped",
        "residual": "1 sanitised · 1 still had PII",
        "parser": "1 sanitised · 1 failed",
        "checker": "1 sanitised · 1 failed",
    }[failure]
    assert summary.split(None, 1)[1] == expected
    assert "not sanitised: file-000" in captured.out
    for private in ("private@example.org", "Alice Smith", "Carol Jones", "Private Person"):
        assert private not in captured.out + captured.err
    expected_ids = {"file-0002"}
    if failure == "unsupported" or debug:
        expected_ids.add("file-0001")
    published = output / "UNVERIFIED" if debug else output
    assert {
        path.stem for path in published.iterdir() if path.name != "UNVERIFIED.txt"
    } == expected_ids
    if debug:
        assert (output / "UNVERIFIED.txt").exists()
        assert not scan_output(output, NameEngine()).clean
        assert not scan_output(published, NameEngine()).clean
        assert "verified: configured recognisers" not in captured.out
    assert {path.stem for path in reports.iterdir()} == expected_ids | {"run"}
    run = json.loads((reports / "run.json").read_text())
    assert run["batch_status"] == ("unverified_debug" if debug else "partial")
    assert run["verification"]["status"] == (
        "failed" if debug else "clean_for_configured_recognisers"
    )
    assert run["files"] == len(expected_ids)
    assert run["verification"]["files_scanned"] == len(expected_ids) - (failure == "checker")
    assert run["replacements"] == len(expected_ids)
    contents = "".join(path.read_text() for path in published.iterdir())
    assert ("Private Person" in contents) == (failure == "residual")
    for path in reports.iterdir():
        assert "Private Person" not in path.read_text()
    assert not list(tmp_path.glob(".*.staging-*"))


def test_detector_failure_does_not_stop_verification(tmp_path):
    class FailingEngine(NameEngine):
        def detect(self, text):
            if text == "broken":
                raise RuntimeError("private@example.org")
            return super().detect(text)

    (tmp_path / "a.txt").write_text("broken")
    (tmp_path / "z.txt").write_text("Carol Jones")
    report = scan_output(tmp_path, FailingEngine())
    assert len(report.unscannable) == 1
    assert len(report.leaks) == 1
    assert "private@example.org" not in repr(report)


@pytest.mark.parametrize("failure", ["residual", "unscannable", "empty", "exception"])
def test_final_scan_failure_retains_debug_output(tmp_path, monkeypatch, capsys, failure):
    from panda.leakcheck import LeakReport

    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.html").write_text("<p>Alice Smith</p>")
    monkeypatch.setattr(cli, "default_detector_engine", NameEngine)
    original = cli.scan_output
    calls = 0

    def final_failure(directory, engine, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(directory, engine, **kwargs)
        artifact = directory / "file-0001.html"
        if failure == "exception":
            raise RuntimeError("Private Person")
        if failure == "empty":
            return LeakReport()
        if failure == "unscannable":
            return LeakReport(unscannable=[artifact])
        artifact.write_text("<html><body><p>Private Person</p></body></html>")
        return original(directory, engine, **kwargs)

    monkeypatch.setattr(cli, "scan_output", final_failure)
    output, reports = tmp_path / "output", tmp_path / "reports"
    assert cli.main([
        "run", str(source), "--opaque-names", "--output", str(output), "--report", str(reports),
    ]) == cli.EXIT_DEBUG
    assert calls == 2
    assert (output / "UNVERIFIED" / "file-0001.html").exists()
    report = json.loads((reports / "run.json").read_text())
    assert report["verification"]["status"] == "failed"
    assert report["batch_status"] == "unverified_debug"
    monkeypatch.setattr(cli, "scan_output", original)
    assert cli.main(["check", "--output", str(output)]) == cli.EXIT_UNSANITISED
    captured = capsys.readouterr()
    assert "Private Person" not in captured.out + captured.err
    assert "verified: configured recognisers" not in captured.out
    assert not list(tmp_path.glob(".*.staging-*"))
