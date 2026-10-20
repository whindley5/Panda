"""T09 operator diagnostics using only synthetic documents and injected failures."""

import errno
import json

import pytest
from pypdf import PdfWriter

from panda import cli, formats
from panda.detectors import DetectorEngine
from panda.errors import UnsupportedError
from panda.limits import CapacityError


@pytest.fixture(autouse=True)
def isolated_engine_and_registry(monkeypatch):
    monkeypatch.setattr(cli, 'default_detector_engine', DetectorEngine)
    handlers = dict(formats.HANDLERS)
    extractors = dict(formats.TEXT_EXTRACTORS)
    yield
    formats.HANDLERS.clear()
    formats.HANDLERS.update(handlers)
    formats.TEXT_EXTRACTORS.clear()
    formats.TEXT_EXTRACTORS.update(extractors)


def test_check_requires_output(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(['check'])
    assert caught.value.code == 1
    assert 'usage: panda check' in capsys.readouterr().err


@pytest.mark.parametrize(('error', 'code'), [
    (PermissionError('private@example.org'), 'E_PERMISSION'),
    (OSError(errno.ENOSPC, 'private@example.org'), 'E_DISK'),
    (OSError(errno.EDQUOT, 'private@example.org'), 'E_DISK'),
    (FileNotFoundError('private@example.org'), 'E_UNREADABLE'),
    (ValueError('private@example.org'), 'E_MALFORMED'),
    (CapacityError(), 'E_CAPACITY'),
    (UnsupportedError('private@example.org'), 'E_UNSUPPORTED'),
])
@pytest.mark.parametrize('stage', ['handler', 'checker', 'publication'])
def test_safe_actionable_refusals(tmp_path, monkeypatch, capsys, error, code, stage):
    source = tmp_path / 'private@example.org'
    source.mkdir()
    (source / 'private@example.org.html').write_text('<p>ordinary text</p>')

    def fail(*args, **kwargs):
        raise error

    if stage == 'handler':
        formats.HANDLERS['.html'] = fail
    elif stage == 'checker':
        formats.TEXT_EXTRACTORS['.html'] = fail
    else:
        monkeypatch.setattr(cli, '_promote_staged', fail)
    debug = stage == 'checker'
    assert cli.main(['run', str(source), '--opaque-names']) == (cli.EXIT_DEBUG if debug else 2)
    captured = capsys.readouterr()
    assert code in captured.err
    assert 'private@example.org' not in captured.out + captured.err
    assert 'detected PII' not in captured.err
    if debug:
        output, = tmp_path.glob('*-SANITISED/*-files')
        reports, = tmp_path.glob('*-SANITISED/*-report')
        assert (output / 'UNVERIFIED' / 'file-0001.html').exists()
        assert (output / 'UNVERIFIED.txt').exists()
        run = json.loads((reports / 'run.json').read_text())
        assert run['verification']['status'] == 'failed'
        assert run['batch_status'] == 'unverified_debug'
        assert 'private@example.org' not in ''.join(p.read_text() for p in reports.iterdir())
    else:
        assert not list(tmp_path.glob('*-SANITISED*'))
    assert not list(tmp_path.glob('.*.staging-*'))


@pytest.mark.parametrize(('kind', 'code'), [
    ('encrypted', 'E_ENCRYPTED'), ('empty', 'E_IMAGE_ONLY'), ('broken', 'E_MALFORMED'),
])
def test_real_pdf_worker_refusal(tmp_path, capsys, kind, code):
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'private@example.org.pdf'
    if kind == 'broken':
        path.write_bytes(b'%PDF-1.4\ninvalid synthetic document')
    else:
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        if kind == 'encrypted':
            writer.encrypt('synthetic-password')
        writer.write(path)
    original = path.read_bytes()
    assert cli.main(['run', str(source)]) == 2
    captured = capsys.readouterr()
    assert code in captured.err
    assert 'private@example.org' not in captured.out + captured.err
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('*-SANITISED*'))


def test_check_distinguishes_survivors_from_unscannable(tmp_path, capsys):
    (tmp_path / 'file-0001.txt').write_text('private@example.org')
    assert cli.main(['check', '--output', str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert 'leaked PII still present' in captured.err
    assert 'unscannable' not in captured.err
    (tmp_path / 'file-0001.txt').write_bytes(b'\xff')
    assert cli.main(['check', '--output', str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert 'E_MALFORMED' in captured.err
    assert 'leaked PII' not in captured.err
