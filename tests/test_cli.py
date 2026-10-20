import io
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from docx import Document

from panda import cli, formats
from panda.cli import EXIT_ERROR, EXIT_OK, EXIT_PARTIAL, EXIT_UNSANITISED, main
from panda.handlers import register_defaults
from panda.handlers.docx import extract_docx_text
from panda.pseudonyms import DUMMY_NAMES

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_registry():
    handlers = dict(formats.HANDLERS)
    extractors = dict(formats.TEXT_EXTRACTORS)
    yield
    formats.HANDLERS.clear()
    formats.HANDLERS.update(handlers)
    formats.TEXT_EXTRACTORS.clear()
    formats.TEXT_EXTRACTORS.update(extractors)


def _args(tmp_path, input_dir):
    return [
        "run",
        "--opaque-names",
        "--input",
        str(input_dir),
        "--output",
        str(tmp_path / "output"),
        "--report",
        str(tmp_path / "reports"),
    ]


def test_version_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert "panda" in capsys.readouterr().out


def test_bare_command_shows_help(capsys):
    assert main([]) == EXIT_OK
    out = capsys.readouterr().out
    for flag in ("--html", "--opaque-names", "--output", "--report", "panda report", "Exit codes"):
        assert flag in out


def test_help_flag_shows_full_help(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert "--opaque-names" in capsys.readouterr().out


def test_missing_input_directory(tmp_path, capsys):
    assert main(_args(tmp_path, tmp_path / "nope")) == EXIT_ERROR
    assert "input directory not found" in capsys.readouterr().err


def test_empty_input_directory(tmp_path, capsys):
    empty = tmp_path / "input"
    empty.mkdir()

    assert main(_args(tmp_path, empty)) == EXIT_ERROR
    assert "no files found" in capsys.readouterr().err


def test_unsupported_file_refuses_the_whole_run(tmp_path, capsys):
    """An unsupported file prevents publication of an incomplete batch."""
    source = tmp_path / "input"
    source.mkdir()
    (source / "notes.xlsx").write_text("x")

    code = main(_args(tmp_path, source))

    assert code == EXIT_UNSANITISED
    assert "not a supported format" in capsys.readouterr().err


def test_unsupported_file_leaves_no_output_directory(tmp_path):
    """A refused run must not leave a directory that looks like a result."""
    source = tmp_path / "input"
    source.mkdir()
    (source / "notes.xlsx").write_text("x")

    main(_args(tmp_path, source))

    assert not (tmp_path / "output").exists()


def test_planned_but_unbuilt_format_says_so(tmp_path, capsys):
    """'Not built yet' and 'out of scope' are different problems for the operator."""
    source = tmp_path / "input"
    source.mkdir()
    # .mp3 is planned (in PLANNED_EXTENSIONS) but has no handler yet; .pdf is now built.
    (source / "call.mp3").write_bytes(b"ID3 not really an mp3")

    code = main(_args(tmp_path, source))

    assert code == EXIT_UNSANITISED
    assert "no handler built yet" in capsys.readouterr().err


def _run_html(tmp_path, body):
    source = tmp_path / "input"
    source.mkdir()
    (source / "page.html").write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    args = _args(tmp_path, source)
    code = main(args)
    output = tmp_path / "output" / "file-0001.html"
    return code, output


def test_run_sanitises_an_html_file(tmp_path, capsys):
    code, output = _run_html(tmp_path, "<p>reach me at real.person@mail.org</p>")
    assert code == EXIT_OK
    text = output.read_text(encoding="utf-8")
    assert "real.person@mail.org" not in text
    assert "john.doe@example.com" in text  # the fixed email dummy
    assert "configured recognisers found no PII" in capsys.readouterr().out


def test_run_removes_a_mailto_attribute(tmp_path):
    _, output = _run_html(tmp_path, '<a href="mailto:real.person@mail.org">write</a>')
    text = output.read_text(encoding="utf-8")
    assert "real.person@mail.org" not in text
    assert "href" not in text
    assert "write" in text


def test_run_preserves_markup(tmp_path):
    _, output = _run_html(tmp_path, "<p>call 0298761234</p><p>ok</p>")
    text = output.read_text(encoding="utf-8")
    assert text.count("<p>") == 2
    assert "0298761234" not in text


def test_run_automatically_replaces_an_english_name(tmp_path):
    _, output = _run_html(tmp_path, "<p>spoke to Margaret Whitfield</p>")
    text = output.read_text(encoding="utf-8")
    assert "Margaret Whitfield" not in text
    assert DUMMY_NAMES[0] in text  # the first dummy name handed out


def test_run_writes_a_report_without_raw_values(tmp_path):
    _run_html(tmp_path, "<p>reach me at real.person@mail.org</p>")
    report = (tmp_path / "reports" / "file-0001.json").read_text(encoding="utf-8")
    assert '"email"' in report
    assert "real.person@mail.org" not in report


def test_run_publishes_verified_subset_when_a_file_is_unsupported(tmp_path, capsys):
    """Unsupported files are excluded while verified supported files are published."""
    source = tmp_path / "input"
    source.mkdir()
    (source / "page.html").write_text("<p>hi</p>", encoding="utf-8")
    (source / "notes.xlsx").write_text("x")

    assert main(_args(tmp_path, source)) == EXIT_PARTIAL
    assert [path.name for path in (tmp_path / "output").iterdir()] == ["file-0001.html"]


@pytest.mark.parametrize("destination", ["output", "reports"])
def test_run_refuses_an_existing_destination(tmp_path, capsys, destination):
    source = tmp_path / "input"
    source.mkdir()
    (source / "page.html").write_text("<p>safe</p>", encoding="utf-8")
    (tmp_path / destination).mkdir()

    assert main(_args(tmp_path, source)) == EXIT_ERROR
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("output", "report"),
    [
        ("input/output", "reports"),
        ("output", "input/reports"),
        ("results", "results/reports"),
    ],
)
def test_run_refuses_overlapping_job_paths(tmp_path, capsys, output, report):
    source = tmp_path / "input"
    source.mkdir()
    (source / "page.html").write_text("<p>safe</p>", encoding="utf-8")
    args = [
        "run",
        "--opaque-names",
        "--input",
        str(source),
        "--output",
        str(tmp_path / output),
        "--report",
        str(tmp_path / report),
    ]

    assert main(args) == EXIT_ERROR
    assert "paths overlap" in capsys.readouterr().err


def test_handler_failure_leaves_no_partial_output_or_report(tmp_path, capsys):
    source = tmp_path / "input"
    source.mkdir()
    (source / "page.html").write_text("<p>safe</p>", encoding="utf-8")

    def fail_after_write(source, destination, pseudonymiser, detector_engine):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("plausible partial output", encoding="utf-8")
        raise ValueError("message may contain source data")

    formats.HANDLERS[".html"] = fail_after_write

    assert main(_args(tmp_path, source)) == EXIT_UNSANITISED
    captured = capsys.readouterr()
    assert "file-0001 failed (ValueError)" in captured.err
    assert "message may contain source data" not in captured.err
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "reports").exists()
    assert not list(tmp_path.glob(".*.staging-*"))


def _check_args(output_dir):
    return ["check", "--output", str(output_dir)]


def test_check_missing_output_directory(tmp_path, capsys):
    assert main(_check_args(tmp_path / "nope")) == EXIT_ERROR
    assert "output directory not found" in capsys.readouterr().err


def test_check_empty_output_directory(tmp_path, capsys):
    output = tmp_path / "output"
    output.mkdir()
    assert main(_check_args(output)) == EXIT_ERROR
    assert "no files found" in capsys.readouterr().err


def test_check_passes_on_clean_output(tmp_path, capsys):
    output = tmp_path / "output"
    output.mkdir()
    (output / "note.txt").write_text("nothing sensitive here", encoding="utf-8")

    assert main(_check_args(output)) == EXIT_OK
    assert "configured recognisers found no PII" in capsys.readouterr().out


def test_check_fails_on_surviving_pattern_pii(tmp_path, capsys):
    output = tmp_path / "output"
    output.mkdir()
    real_email = "victim.person@realmail.co"
    (output / "leaky.txt").write_text(f"reach me at {real_email}", encoding="utf-8")

    assert main(_check_args(output)) == EXIT_UNSANITISED
    captured = capsys.readouterr()
    assert "email" in captured.err
    # The whole point: the audit output must not re-expose the value it found.
    assert real_email not in captured.err
    assert real_email not in captured.out


def test_check_accepts_fixed_name_replacements(tmp_path, capsys):
    """Standalone checks accept the configured fixed placeholders, including names."""
    output = tmp_path / "output"
    output.mkdir()
    (output / "done.txt").write_text(
        "John Doe, 0400000000, john.doe@example.com, born 01/01/1990",
        encoding="utf-8",
    )

    assert main(_check_args(output)) == EXIT_OK
    captured = capsys.readouterr()
    assert "configured recognisers found no PII" in captured.out
    assert "expected dummy replacement" in captured.out


def test_check_finds_surviving_english_name(tmp_path, capsys):
    output = tmp_path / "output"
    output.mkdir()
    (output / "doc.txt").write_text("call from Margaret Whitfield", encoding="utf-8")
    assert main(_check_args(output)) == EXIT_UNSANITISED
    captured = capsys.readouterr()
    assert "name" in captured.err
    # A locator, never the name itself.
    assert "Margaret" not in captured.err


def test_check_fails_closed_on_unscannable_file(tmp_path, capsys):
    """A file we cannot read as text is unverifiable, so it must not pass."""
    output = tmp_path / "output"
    output.mkdir()
    (output / "scan.bin").write_bytes(b"\xff\xfe\x00\x01not utf-8")

    assert main(_check_args(output)) == EXIT_UNSANITISED
    assert "UnicodeDecodeError" in capsys.readouterr().err


def _build_pdf(pages: list[list[str]]) -> bytes:
    """Minimal multi-page PDF in base-14 Helvetica, as in test_pdf.py.

    Only Latin text; the base-14 standard encoding has no CJK glyphs.
    """
    objects: dict[int, bytes] = {}
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


def _write_e2e_batch(tmp_path: Path) -> dict[str, str]:
    """Write one synthetic file per supported format."""
    source = tmp_path / "input"
    source.mkdir()
    name, spouse = "Margaret Whitfield", "Susan Whitfield"
    dob, mobile = "14/07/1968", "0412 908 771"
    email = "m.whitfield@example.com"
    address = "42 Kembla Street, Wollongong NSW 2500"
    account, landline = "ACC-5540912", "02 4225 1180"

    (source / "letter.html").write_text(
        f"<html><body><p>{name}, born {dob}, {mobile}, {email}, {account}.</p>"
        f"<p>{address}</p></body></html>",
        encoding="utf-8",
    )

    doc = Document()
    doc.sections[0].header.paragraphs[0].text = f"header for {email}"
    paragraph = doc.add_paragraph()
    paragraph.add_run(f"Call {mobile} ")
    paragraph.add_run(f"for {name} ")
    paragraph.add_run(f"account {account}")
    doc.add_paragraph(f"DOB {dob}, address {address}.")
    buffer = io.BytesIO()
    doc.save(buffer)
    (source / "notes.docx").write_bytes(buffer.getvalue())

    message = EmailMessage()
    message["From"] = f'"{name}" <{email}>'
    message["To"] = "advisor@firm.com"
    message["Subject"] = f"account {account}"
    message.set_content(
        f"Hello, this is {name}. DOB {dob}. Call {mobile} or {landline}. {address}."
    )
    message.add_attachment(
        b"<html>notes for 0298765432</html>",
        maintype="application",
        subtype="octet-stream",
        filename="notes.html",
    )
    (source / "chat.eml").write_bytes(message.as_bytes(policy=policy.default))

    (source / "transcript.pdf").write_bytes(
        _build_pdf([[f"{name} ({spouse}), DOB {dob}, {mobile}, {email}, {account}"]])
    )

    return {"name": name, "spouse": spouse, "dob": dob, "mobile": mobile,
            "email": email, "address": address, "account": account, "landline": landline}


def _e2e_args(tmp_path: Path) -> list[str]:
    return [
        "run",
        "--opaque-names",
        "--input",
        str(tmp_path / "input"),
        "--output",
        str(tmp_path / "output"),
        "--report",
        str(tmp_path / "reports"),
    ]


def _read_back(path: Path) -> str:
    """Decode an output file for assertion, using the registered extractor if binary."""
    register_defaults()
    extractor = formats.extractor_for(path)
    if extractor is not None:
        return "\n".join(unit.text for unit in extractor(path))
    return path.read_bytes().decode("utf-8")


def test_full_batch_across_every_format_is_sanitised(tmp_path, capsys):
    """The end to end proof: all four formats through the real CLI, then a clean rescan."""
    values = _write_e2e_batch(tmp_path)
    real = list(values.values())

    assert main(_e2e_args(tmp_path)) == EXIT_OK
    assert "configured recognisers found no PII" in capsys.readouterr().out

    output = tmp_path / "output"
    names = sorted(path.name for path in output.iterdir())
    assert names == [
        "file-0001.eml",
        "file-0002.html",
        "file-0003.docx",
        "file-0004.pdf.txt",
    ]

    for path in output.iterdir():
        text = _read_back(path)
        for value in real:
            assert value not in text, f"{value} survived in {path.name}"

    assert extract_docx_text(output / "file-0003.docx")  # still a readable document
    assert Document(str(output / "file-0003.docx")).paragraphs  # valid .docx

    check_args = ["check", "--output", str(output)]
    assert main(check_args) == EXIT_OK


@pytest.mark.parametrize("fixture", ["en/chat_transcript.txt", "zh/chat_transcript.txt"])
def test_fixtures_are_present_and_readable(fixture):
    """The core in phase 1 is developed against these, so a missing one is a blocker."""
    content = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert content.strip()


def test_chinese_fixture_carries_the_awkward_cases():
    content = (FIXTURES / "zh" / "chat_transcript.txt").read_text(encoding="utf-8")
    assert "１００" in content, "full-width digits must be exercised"
    assert "310101196807141237" in content, "national ID must be exercised"


@pytest.mark.parametrize("failure", [PermissionError, KeyboardInterrupt])
def test_second_staging_failure_cleans_first_directory(tmp_path, monkeypatch, capsys, failure):
    from panda import cli

    source = tmp_path / "input"
    source.mkdir()
    (source / "sample.html").write_text("<p>safe</p>")
    original = cli._new_staging_dir
    calls = 0

    def staging(destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure("synthetic-private-value")
        return original(destination)

    monkeypatch.setattr(cli, "_new_staging_dir", staging)
    code = main(_args(tmp_path, source))
    assert code == (130 if failure is KeyboardInterrupt else EXIT_UNSANITISED)
    assert not list(tmp_path.glob(".*.staging-*"))
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "reports").exists()
    assert "synthetic-private-value" not in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "check"])
def test_traversal_failure_refuses_without_exposing_paths(tmp_path, monkeypatch, capsys, command):
    source = tmp_path / "input"
    source.mkdir()
    (source / "sample.html").write_text("<p>safe</p>")

    def fail_scandir(path):
        raise PermissionError("synthetic-private-value")

    monkeypatch.setattr(formats.os, "scandir", fail_scandir)
    args = _args(tmp_path, source) if command == "run" else _check_args(source)
    assert main(args) == EXIT_UNSANITISED
    captured = capsys.readouterr()
    assert "PermissionError" in captured.err
    assert "synthetic-private-value" not in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "output").exists()


def test_unknown_arguments_use_setup_exit_code_and_do_not_echo_values(capsys):
    with pytest.raises(SystemExit) as error:
        main(["synthetic-private-value"])
    assert error.value.code == EXIT_ERROR
    assert "synthetic-private-value" not in capsys.readouterr().err


def test_unknown_extension_is_not_printed(tmp_path, capsys):
    source = tmp_path / "input"
    source.mkdir()
    (source / "file.synthetic-private-value").write_text("safe")
    assert main(_args(tmp_path, source)) == EXIT_UNSANITISED
    assert "synthetic-private-value" not in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "check"])
def test_linked_subdirectory_is_refused(tmp_path, capsys, command):
    source = tmp_path / "input"
    source.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "sample.html").write_text("<p>safe</p>")
    try:
        (source / "linked").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    args = _args(tmp_path, source) if command == "run" else _check_args(source)
    assert main(args) == EXIT_UNSANITISED
    assert "configured recognisers found no PII" not in capsys.readouterr().out


def test_folder_command_verifies_nested_files_and_supports_repeat_runs(tmp_path, capsys):
    source = tmp_path / "private-client-name"
    nested = source / "nested"
    nested.mkdir(parents=True)
    original = "<p>Contact sample.person@example.org</p>"
    (nested / "private-name.html").write_text(original)
    for _ in range(2):
        assert main(["run", str(source), "--opaque-names"]) == EXIT_OK
    # Named after the source folder; a rerun gets the next free suffix.
    roots = sorted(path.name for path in tmp_path.glob("*-SANITISED*"))
    assert roots == ["private-client-name-SANITISED", "private-client-name-SANITISED-2"]
    for name in roots:
        output = tmp_path / name / "private-client-name-files"
        report = tmp_path / name / "private-client-name-report"
        assert sorted(path.name for path in (tmp_path / name).iterdir()) == [
            output.name, report.name,
        ]
        assert "sample.person@example.org" not in (output / "file-0001.html").read_text()
        assert (report / "run.json").exists()
        assert not (report / "report.html").exists()
    assert (nested / "private-name.html").read_text() == original
    console = capsys.readouterr().out
    # The folder name is used on disk only; the console never echoes source names.
    assert "private-client-name" not in console
    assert "private-name.html" not in console
    assert "sample.person@example.org" not in console
    assert "<source folder name>-SANITISED," in console
    assert "<source folder name>-SANITISED-2," in console


def test_folder_command_failure_publishes_nothing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "unsupported.xlsx").write_bytes(b"synthetic")
    assert main(["run", str(source)]) == EXIT_UNSANITISED
    assert list(tmp_path.iterdir()) == [source]


def test_folder_command_custom_output_and_automatic_reports(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.html").write_text("<p>sample.person@example.org</p>")
    output = tmp_path / "chosen"
    assert main(["run", str(source), "--output", str(output), "--opaque-names"]) == EXIT_OK
    assert (output / "file-0001.html").exists()
    assert len(list(tmp_path.glob("*-SANITISED/*-report/run.json"))) == 1


def test_folder_command_html_report(tmp_path, capsys):
    source = tmp_path / "batch"
    source.mkdir()
    (source / "sample.html").write_text("<p>Contact sample.person@example.org</p>")
    assert main(["run", str(source), "--html"]) == EXIT_OK
    page = tmp_path / "batch-SANITISED" / "batch-report" / "report.html"
    content = page.read_text(encoding="utf-8")
    assert "batch \u2013 sanitisation report" in content
    assert "file-0001" in content and "Email addresses" in content
    assert "sample.person@example.org" not in content
    assert "audit reports + report.html" in capsys.readouterr().out
    assert not list(page.parent.glob(".report-*"))


def test_folder_and_input_option_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["run", str(tmp_path), "--input", str(tmp_path)])
    assert error.value.code == EXIT_ERROR


@pytest.mark.parametrize('command', ['run', 'check'])
def test_missing_model_precedes_processing(tmp_path, monkeypatch, capsys, command):
    import panda.cli as cli
    from panda.spacy_recognizer import ModelSetupError, SpacyRecognizer

    source = tmp_path / 'source'
    source.mkdir()
    (source / 'sample.html').write_text('<p>普通文本</p>', encoding='utf-8')

    def missing(self):
        raise ModelSetupError('synthetic.private@example.org')

    monkeypatch.setattr(SpacyRecognizer, 'prepare', missing)
    monkeypatch.setattr(cli, 'handler_for', lambda path: pytest.fail('must not process a document'))
    args = ['run', str(source)] if command == 'run' else ['check', '--output', str(source)]
    assert cli.main(args) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert 'local NLP installation' in captured.err
    assert 'reinstall Panda' in captured.err
    assert 'synthetic.' not in captured.err
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize('replacement', ['file', 'link', 'root_link'])
def test_source_substitution_after_discovery_refuses(tmp_path, monkeypatch, replacement):
    from panda import cli

    source = tmp_path / 'source'
    source.mkdir()
    item = source / 'sample.html'
    item.write_text('<p>safe original</p>')
    original = cli.discover

    def discover_then_replace(path):
        result = original(path)
        if replacement == 'root_link':
            moved = tmp_path / 'moved'
            source.rename(moved)
            source.symlink_to(moved, target_is_directory=True)
        else:
            item.unlink()
            if replacement == 'link':
                target = tmp_path / 'foreign.html'
                target.write_text('<p>foreign</p>')
                item.symlink_to(target)
            else:
                item.write_text('<p>changed file</p>')
        return result

    monkeypatch.setattr(cli, 'discover', discover_then_replace)
    assert main(_args(tmp_path, source)) == EXIT_UNSANITISED
    assert not (tmp_path / 'output').exists()
    assert not (tmp_path / 'reports').exists()


def test_check_refuses_abandoned_staging(tmp_path, capsys):
    stage = tmp_path / '.output.staging-abandoned'
    stage.mkdir()
    (stage / 'sample.html').write_text('<html><body><p>safe</p></body></html>')
    assert main(_check_args(stage)) == EXIT_UNSANITISED
    assert 'staging is not completed output' in capsys.readouterr().err


def test_cleanup_does_not_delete_replaced_staging_directory(tmp_path, monkeypatch):
    from panda import cli

    source = tmp_path / 'source'
    source.mkdir()
    (source / 'sample.html').write_text('<p>safe</p>')
    original = cli._new_staging_dir
    created = []

    def staging(destination):
        if created:
            created[0].rename(tmp_path / 'original-stage')
            created[0].mkdir()
            (created[0] / 'foreign').write_text('other job')
            raise PermissionError('synthetic-private-value')
        stage = original(destination)
        created.append(stage)
        return stage

    monkeypatch.setattr(cli, '_new_staging_dir', staging)
    assert main(_args(tmp_path, source)) == EXIT_UNSANITISED
    assert (created[0] / 'foreign').read_text() == 'other job'


def test_keep_terms_merge_flags_and_file(tmp_path):
    keep_file = tmp_path / "keep.txt"
    keep_file.write_text("# our names\nCrane Brokers\n\n白鹤  # Chinese brand\n", encoding="utf-8")
    parser = cli.build_parser()
    args = parser.parse_args(
        ["run", str(tmp_path), "--keep", "CraneAI", "--keep-file", str(keep_file)]
    )
    assert cli._keep_terms(parser, args) == ["CraneAI", "Crane Brokers", "白鹤"]
