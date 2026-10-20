from pathlib import Path

from panda.core import sanitise_text
from panda.detectors import default_detector_engine
from panda.leakcheck import Leak, scan_output
from panda.model import PIIKind
from panda.pseudonyms import FixedPseudonymiser


def _write(dir_path: Path, name: str, text: str) -> None:
    (dir_path / name).write_text(text, encoding="utf-8")


def test_clean_output_has_no_leaks(tmp_path):
    _write(tmp_path, "a.txt", "nothing to see")
    report = scan_output(tmp_path)
    assert report.clean
    assert report.leaks == []
    assert report.scanned == [tmp_path / "a.txt"]


def test_surviving_email_is_a_leak(tmp_path):
    _write(tmp_path, "a.txt", "write to real.person@somewhere.org please")
    report = scan_output(tmp_path)
    assert not report.clean
    assert [leak.kind for leak in report.leaks] == [PIIKind.EMAIL]
    assert report.counts == {"email": 1}


def test_a_leak_carries_a_locator_not_the_value(tmp_path):
    _write(tmp_path, "a.txt", "call 0298765432 today")
    (leak,) = scan_output(tmp_path).leaks
    # The span points at the value without reproducing it: a Leak has no text field.
    assert isinstance(leak, Leak)
    assert not hasattr(leak, "text")
    assert leak.end > leak.start
    assert leak.locator == "text"


def test_fixed_dummies_are_recognised_not_leaked(tmp_path):
    _write(tmp_path, "a.txt", "John Doe on 0400000000, john.doe@example.com, DOB 01/01/1990")
    report = scan_output(tmp_path)
    assert report.clean
    assert report.leaks == []
    # The dummy phone, email and DOB all match patterns but are the intended output.
    assert report.recognised_dummies == 3


def test_english_name_surviving_is_a_leak(tmp_path):
    _write(tmp_path, "a.txt", "spoke to Susan Whitfield")
    report = scan_output(tmp_path, default_detector_engine())
    assert not report.clean
    assert report.counts == {"name": 1}


def test_surviving_chinese_name_is_a_leak(tmp_path):
    _write(tmp_path, "a.txt", "请问是张伟先生吗")
    report = scan_output(tmp_path)
    assert any(leak.kind is PIIKind.NAME for leak in report.leaks)


def test_unscannable_file_fails_closed(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    report = scan_output(tmp_path)
    assert report.unscannable == [tmp_path / "blob.bin"]
    assert not report.clean  # unverifiable counts against a clean result


def test_fixed_pseudonymiser_output_passes_its_own_leak_check(tmp_path):
    """End to end: sanitise real PII with the fixed dummies, then prove none survived."""
    source = "Email real.person@mail.example.org or ring 0298761234. Client: Susan Whitfield."
    engine = default_detector_engine()

    result = sanitise_text(source, FixedPseudonymiser(), engine)
    _write(tmp_path, "out.txt", result.text)

    report = scan_output(tmp_path, engine, generated_dummies={
        tmp_path / "out.txt": frozenset(
            item.surrogate.casefold() for item in result.replacements
        ),
    })
    assert report.clean, [(leak.kind, leak.detector) for leak in report.leaks]
    # The real values are gone and the obvious dummies are in their place.
    assert "real.person@mail.example.org" not in result.text
    assert "Susan Whitfield" not in result.text
    assert "John Doe" in result.text


def test_docx_allowed_story_text_is_still_scanned(tmp_path, monkeypatch):
    import zipfile

    from docx import Document

    from panda import formats
    from panda.detectors import DetectorEngine
    from panda.handlers.docx import extract_docx_units, sanitise_docx_file
    from panda.pseudonyms import FixedPseudonymiser

    monkeypatch.setitem(formats.TEXT_EXTRACTORS, ".docx", extract_docx_units)
    source = tmp_path / 'source.docx'
    doc = Document()
    doc.add_paragraph('Visible review text')
    doc.save(source)
    destination = tmp_path / 'out' / 'safe.docx'
    sanitise_docx_file(source, destination, FixedPseudonymiser(), DetectorEngine())
    with zipfile.ZipFile(destination) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries['word/document.xml'] = entries['word/document.xml'].replace(
        b'Visible review text', b'synthetic.survivor@example.org')
    with zipfile.ZipFile(destination, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    report = scan_output(destination.parent, DetectorEngine())
    assert not report.clean
    assert not report.unscannable
    assert any(leak.locator == 'docx/story/1/paragraph/1' for leak in report.leaks)
    assert 'synthetic.survivor' not in repr(report)


def test_report_only_publication_is_not_completed_output(tmp_path):
    (tmp_path / 'run.json').write_text('{"files": 1}')
    report = scan_output(tmp_path)
    assert not report.clean
    assert report.failure_reasons[tmp_path] == 'NotCompletedOutput'


def test_staging_is_untrusted_except_for_internal_prepublication_scan(tmp_path):
    stage = tmp_path / '.output.staging-abandoned'
    stage.mkdir()
    (stage / 'sample.txt').write_text('safe')
    assert not scan_output(stage).clean
    assert scan_output(stage, staged=True).clean
