"""Small synthetic capacities exercise real refusal paths without huge inputs."""

import hashlib
import subprocess
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from panda import limits
from panda.cli import main
from panda.detectors import DetectorEngine
from panda.formats import walk_files
from panda.handlers import register_defaults
from panda.handlers.docx import extract_docx_units, sanitise_docx_file
from panda.handlers.eml import _parse_bounded, sanitise_eml_file
from panda.handlers.html import sanitise_html_file
from panda.handlers.pdf import _page_texts, sanitise_pdf_file
from panda.leakcheck import scan_output
from panda.limits import CapacityError, Limits, capacity_scope, check_zip, read_bytes
from panda.pseudonyms import FixedPseudonymiser


def small(**kwargs):
    return capacity_scope(replace(Limits(), **kwargs))


def test_policy_rejects_disabled_limits():
    with pytest.raises(ValueError):
        Limits(source_bytes=0)


def test_source_boundary_and_no_truncation(tmp_path):
    path = tmp_path / "synthetic"
    path.write_bytes(b"1234")
    with small(source_bytes=4):
        assert read_bytes(path) == b"1234"
    with small(source_bytes=3), pytest.raises(CapacityError):
        read_bytes(path)


@pytest.mark.parametrize("constraint", [dict(total_files=1), dict(total_bytes=3)])
def test_batch_counts_are_cumulative(tmp_path, constraint):
    (tmp_path / "a").write_bytes(b"12")
    (tmp_path / "b").write_bytes(b"34")
    with small(total_files=2, total_bytes=4):
        assert len(walk_files(tmp_path)) == 2
    with small(**constraint), pytest.raises(CapacityError):
        walk_files(tmp_path)


@pytest.mark.parametrize("constraint", [dict(directory_depth=1), dict(directories=2)])
def test_directory_work_is_bounded_even_when_empty(tmp_path, constraint):
    (tmp_path / "a" / "b").mkdir(parents=True)
    with small(**constraint), pytest.raises(CapacityError):
        walk_files(tmp_path)


def test_detector_checks_before_provider_and_cumulative_text():
    class Provider:
        def detect(self, text):
            pytest.fail("provider must not see oversized text")
    with small(text_chars=3), pytest.raises(CapacityError):
        DetectorEngine(providers=(Provider(),)).detect("abcd")
    with small(total_text_chars=6):
        engine = DetectorEngine()
        engine.detect("abc")
        engine.detect("def")
        with pytest.raises(CapacityError):
            engine.detect("g")


@pytest.mark.parametrize("constraint", [dict(zip_entries=1), dict(expanded_bytes=7)])
def test_zip_declared_and_cumulative_limits(tmp_path, constraint):
    path = tmp_path / "synthetic.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("a", b"1234")
        archive.writestr("b", b"5678")
    with small(zip_entries=2, expanded_bytes=8):
        check_zip(path)
    with small(**constraint), pytest.raises(CapacityError):
        check_zip(path)
    with small(expanded_bytes=15):
        check_zip(path)
        with pytest.raises(CapacityError):
            check_zip(path)


def test_zip_bomb_refused_by_handler_and_independent_checker(tmp_path):
    source = tmp_path / "synthetic.docx"
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        archive.writestr("synthetic", b"x" * 10000)
    original = hashlib.sha256(source.read_bytes()).digest()
    for operation in (
        lambda: sanitise_docx_file(source, tmp_path / "out.docx",
                                   FixedPseudonymiser(), DetectorEngine()),
        lambda: extract_docx_units(source),
    ):
        with small(zip_ratio=2), pytest.raises(CapacityError):
            operation()
    assert not (tmp_path / "out.docx").exists()
    assert hashlib.sha256(source.read_bytes()).digest() == original


def message_with_parts(count):
    message = EmailMessage()
    message.make_mixed()
    for _ in range(count):
        child = EmailMessage()
        child.set_content("safe")
        message.attach(child)
    return message.as_bytes(policy=policy.default)


def test_mime_part_boundary():
    data = message_with_parts(2)
    with small(mime_parts=3):
        assert len(list(_parse_bounded(data).walk())) == 3
    with small(mime_parts=2), pytest.raises(CapacityError):
        _parse_bounded(data)


def test_mime_part_limit_is_per_message_not_per_job():
    data = message_with_parts(2)
    with small(mime_parts=3):
        for _ in range(10):
            _parse_bounded(data)


def test_mime_depth_checked_during_parsing():
    message = EmailMessage()
    message.set_content("safe")
    for _ in range(4):
        outer = EmailMessage()
        outer.make_mixed()
        outer.attach(message)
        message = outer
    data = message.as_bytes()
    with small(mime_depth=4):
        _parse_bounded(data)
    with small(mime_depth=3), pytest.raises(CapacityError):
        _parse_bounded(data)


def test_attachment_budget(tmp_path):
    message = EmailMessage()
    message.set_content("safe")
    for _ in range(2):
        message.add_attachment(b"data", maintype="application", subtype="octet-stream",
                               filename="synthetic.bin")
    source = tmp_path / "in.eml"
    source.write_bytes(message.as_bytes())
    with small(attachments=1), pytest.raises(CapacityError):
        sanitise_eml_file(source, tmp_path / "out.eml", FixedPseudonymiser(), DetectorEngine())
    assert not (tmp_path / "out.eml").exists()


def test_nested_attachment_uses_shared_zip_budget(tmp_path):
    import io
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("synthetic", b"x" * 10000)
    message = EmailMessage()
    message.set_content("safe")
    message.add_attachment(stream.getvalue(), maintype="application", subtype="octet-stream",
                           filename="synthetic.docx")
    source = tmp_path / "in.eml"
    source.write_bytes(message.as_bytes())
    register_defaults()
    with small(zip_ratio=2), pytest.raises(CapacityError):
        sanitise_eml_file(source, tmp_path / "out.eml", FixedPseudonymiser(), DetectorEngine())
    assert not (tmp_path / "out.eml").exists()


def test_html_node_limit_precedes_parser(tmp_path):
    source = tmp_path / "in.html"
    source.write_text("<p>safe</p>" * 3)
    with small(markup_nodes=5), pytest.raises(CapacityError):
        sanitise_html_file(source, tmp_path / "out.html", FixedPseudonymiser(), DetectorEngine())
    assert not (tmp_path / "out.html").exists()


def test_rescan_refuses_oversized_text(tmp_path):
    (tmp_path / "file-0001.txt").write_text("abcd")
    with small(text_chars=3):
        report = scan_output(tmp_path)
    assert not report.clean
    assert list(report.failure_reasons.values()) == ["CapacityError"]


def test_cli_capacity_failure_cleans_staging_and_hides_names(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    source.mkdir()
    (source / "synthetic-private-name.html").write_text("<p>abcdef</p>")
    output, reports = tmp_path / "output", tmp_path / "reports"
    monkeypatch.setattr("panda.cli.default_detector_engine", DetectorEngine)
    monkeypatch.setattr(limits, "DEFAULT_LIMITS", replace(Limits(), text_chars=3))
    assert main(["run", str(source), "--output", str(output), "--report", str(reports)]) == 2
    captured = capsys.readouterr()
    assert "CapacityError" in captured.err
    assert "synthetic-private-name" not in captured.out + captured.err
    assert not output.exists() and not reports.exists()
    assert not list(tmp_path.glob("*.staging-*"))


def test_pdf_page_limit_real_worker(tmp_path):
    from test_pdf import _build_pdf
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(_build_pdf([["safe"], ["safe"]]))
    with small(pdf_pages=2):
        assert len(_page_texts(source)) == 2
    with small(pdf_pages=1), pytest.raises(CapacityError):
        sanitise_pdf_file(source, tmp_path / "out.pdf", FixedPseudonymiser(), DetectorEngine())
    assert not (tmp_path / "out.pdf.txt").exists()


def test_pdf_timeout_refuses(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"synthetic")
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 1
        raise subprocess.TimeoutExpired("synthetic", 1)
    monkeypatch.setattr("panda.handlers.pdf.subprocess.run", timeout)
    with small(pdf_seconds=1), pytest.raises(CapacityError):
        _page_texts(source)


def test_pdf_compressed_expansion_refused_in_real_worker(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b" " * 10000)
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    source = tmp_path / "synthetic.pdf"
    writer.write(source)
    with small(expanded_bytes=100), pytest.raises(CapacityError):
        _page_texts(source)


def test_real_pdf_worker_timeout_kills_process(tmp_path, monkeypatch):
    import sys
    import time
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"synthetic")
    run = subprocess.run
    def sleeping_worker(*args, **kwargs):
        return run([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
    monkeypatch.setattr("panda.handlers.pdf.subprocess.run", sleeping_worker)
    started = time.monotonic()
    with small(pdf_seconds=1), pytest.raises(CapacityError):
        _page_texts(source)
    assert time.monotonic() - started < 5


def test_malformed_zip_refused(tmp_path):
    path = tmp_path / "synthetic.docx"
    path.write_bytes(b"PK\x03\x04broken")
    with pytest.raises(ValueError):
        check_zip(path)


def test_many_zip_entries_refused_before_zipfile_allocation(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.docx"
    with ZipFile(path, "w") as archive:
        for index in range(3):
            archive.writestr(str(index), b"")
    def must_not_open(*args):
        pytest.fail("ZipFile must not materialise an oversized directory")
    monkeypatch.setattr(limits, "ZipFile", must_not_open)
    with small(zip_entries=2), pytest.raises(CapacityError):
        check_zip(path)


def test_html_direct_extraction_has_cumulative_budget():
    from panda.handlers.html import extract_html_markup_units
    with small(total_text_chars=5), pytest.raises(CapacityError):
        extract_html_markup_units("<html><body><p>abc</p><p>def</p></body></html>")


def test_pdf_form_decode_failure_cannot_publish_partial_text(tmp_path):
    import io

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        EncodedStreamObject,
        NameObject,
        NumberObject,
    )

    from test_pdf import _build_pdf

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_build_pdf([["safe visible text"]]))))
    page = writer.pages[0]
    form = EncodedStreamObject()
    form._data = b"synthetic"
    form.update({NameObject("/Type"): NameObject("/XObject"),
                 NameObject("/Subtype"): NameObject("/Form"),
                 NameObject("/Filter"): NameObject("/LZWDecode"),
                 NameObject("/BBox"): ArrayObject([NumberObject(n) for n in (0, 0, 10, 10)]),
                 NameObject("/Resources"): DictionaryObject()})
    page["/Resources"][NameObject("/XObject")] = DictionaryObject(
        {NameObject("/Fm0"): writer._add_object(form)}
    )
    contents = DecodedStreamObject()
    contents.set_data(page.get_contents().get_data() + b"\n/Fm0 Do\n")
    page[NameObject("/Contents")] = writer._add_object(contents)
    source = tmp_path / "synthetic.pdf"
    writer.write(source)
    with pytest.raises(ValueError, match="PDF extraction refused"):
        _page_texts(source)
