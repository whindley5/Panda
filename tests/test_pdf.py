import pytest

from panda.detectors import default_detector_engine
from panda.handlers import pdf as pdf_handler
from panda.handlers.pdf import sanitise_pdf_file
from panda.leakcheck import scan_output
from panda.model import PIIKind
from panda.pseudonyms import FixedPseudonymiser


def _build_pdf(pages: list[list[str]]) -> bytes:
    """Build a minimal multi-page PDF, no dependencies.

    Each page draws its lines with the base-14 Helvetica font, which pypdf extracts
    without a ToUnicode map. This only covers Latin text (the standard encoding has no
    CJK), so the Chinese path is exercised by monkeypatching extraction instead.
    """
    objects: dict[int, bytes] = {}
    # 1 Catalog, 2 Pages, 3 shared Font; page objects and content streams follow.
    objects[3] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    kids: list[int] = []
    next_obj = 4
    for lines in pages:
        page_obj = next_obj
        content_obj = next_obj + 1
        next_obj += 2
        kids.append(page_obj)
        parts = ["BT", "/F1 12 Tf", "14 TL", "1 0 0 1 60 760 Tm"]
        for i, line in enumerate(lines):
            esc = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            parts.append(f"({esc}) Tj" if i == 0 else f"T* ({esc}) Tj")
        parts.append("ET")
        content = "\n".join(parts).encode("latin-1")
        objects[page_obj] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_obj
        )
        objects[content_obj] = (
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )
    kids_ref = b" ".join(b"%d 0 R" % k for k in kids)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids_ref, len(kids))

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for n in sorted(objects):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objects[n] + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for n in sorted(objects):
        out += b"%010d 00000 n \n" % offsets[n]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF" % xref_pos
    return bytes(out)


def _run(tmp_path, raw: bytes):
    source = tmp_path / "in.pdf"
    destination = tmp_path / "out" / "in.pdf"
    source.write_bytes(raw)
    engine = default_detector_engine()
    result = sanitise_pdf_file(source, destination, FixedPseudonymiser(), engine)
    # The transcript keeps the original name and gains .txt.
    out_path = tmp_path / "out" / "in.pdf.txt"
    return out_path, result


def test_transcribes_and_sanitises_english(tmp_path):
    raw = _build_pdf([["Alice: call me on 0412 345 678", "Bob: email real.person@mail.org"]])
    out_path, result = _run(tmp_path, raw)
    text = out_path.read_text(encoding="utf-8")
    assert "0412 345 678" not in text and "real.person@mail.org" not in text
    assert "0400000000" in text and "john.doe@example.com" in text
    kinds = {r.detection.kind for r in result.replacements}
    assert PIIKind.PHONE in kinds and PIIKind.EMAIL in kinds
    assert all(r.detection.locator == "pdf/page/1" for r in result.replacements)


def test_output_is_named_after_the_pdf(tmp_path):
    """`foo.pdf` transcribes to `foo.pdf.txt`, and no `.pdf` is left in output."""
    raw = _build_pdf([["nothing to redact here"]])
    out_path, _ = _run(tmp_path, raw)
    assert out_path.exists()
    assert not (tmp_path / "out" / "in.pdf").exists()


def test_pages_are_separated_by_a_form_feed(tmp_path):
    raw = _build_pdf([["page one text"], ["page two text"]])
    out_path, _ = _run(tmp_path, raw)
    text = out_path.read_text(encoding="utf-8")
    assert "\f" in text
    assert "page one text" in text and "page two text" in text


def test_empty_pdf_fails_closed(tmp_path):
    """A PDF with no text layer (encrypted or scan) must not emit a blank clean file."""
    raw = _build_pdf([[]])
    source = tmp_path / "in.pdf"
    destination = tmp_path / "out" / "in.pdf"
    source.write_bytes(raw)
    with pytest.raises(ValueError, match="no extractable text"):
        sanitise_pdf_file(source, destination, FixedPseudonymiser(), default_detector_engine())
    assert not (tmp_path / "out" / "in.pdf.txt").exists()


def test_chinese_is_sanitised(tmp_path, monkeypatch):
    """The Chinese path, via monkeypatched extraction (base-14 fonts have no CJK).

    A national ID (pattern + checksum) and a Han name/address (NER) must all be removed.
    """
    page = "客户张伟，身份证 110101199003072316，住址北京市海淀区中关村大街27号，电话13812345678"
    monkeypatch.setattr(pdf_handler, "_page_texts", lambda path: [page])
    out_path, result = _run(tmp_path, b"%PDF-1.4\n")  # bytes unused; extraction is patched
    text = out_path.read_text(encoding="utf-8")
    assert "110101199003072316" not in text
    assert "13812345678" not in text
    assert "张伟" not in text
    assert result.replacements  # something was detected and replaced


def test_output_passes_the_leak_rescan(tmp_path):
    """The acceptance test: nothing the transcript emits survives a rescan."""
    raw = _build_pdf(
        [["Client Margaret Whitfield", "Account 5540912", "Call 0412 345 678"]]
    )
    engine = default_detector_engine()
    out_path, result = _run(tmp_path, raw)
    report = scan_output(out_path.parent, engine, generated_dummies={
        out_path: frozenset(item.surrogate.casefold() for item in result.replacements),
    })
    assert report.clean, [(leak.kind, leak.detector) for leak in report.leaks]
    assert not report.unscannable
