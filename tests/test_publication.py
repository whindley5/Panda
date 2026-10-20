"""Synthetic publication failures, including abrupt exits without Python cleanup."""

import errno
import multiprocessing
import os
from pathlib import Path

import pytest

from panda import publication


def _trees(root):
    root.mkdir(exist_ok=True)
    output = root / '.output.staging-test'
    report = root / '.report.staging-test'
    output.mkdir()
    report.mkdir()
    (output / 'file-0001.html').write_text('<html><body><p>safe</p></body></html>')
    (report / 'run.json').write_text('{"files": 1}')
    return output, root / 'output', report, root / 'report'


def _crash_worker(paths, point):
    original = publication._rename_new
    calls = 0

    def rename(source, destination):
        nonlocal calls
        calls += 1
        if point == 'before' and calls == 1:
            os._exit(71)
        original(source, destination)
        if (point == 'between' and calls == 1) or (point == 'after' and calls == 2):
            os._exit(71)

    publication._rename_new = rename
    publication.promote(*map(Path, paths))


@pytest.mark.parametrize('point', ['before', 'between', 'after'])
def test_abrupt_termination_commit_point(tmp_path, point):
    paths = _trees(tmp_path)
    process = multiprocessing.get_context('spawn').Process(
        target=_crash_worker, args=([str(path) for path in paths], point))
    process.start()
    process.join(20)
    assert not process.is_alive()
    assert process.exitcode == 71
    assert paths[1].exists() == (point == 'after')
    assert paths[3].exists() == (point != 'before')
    if paths[1].exists():
        assert (paths[1] / 'file-0001.html').read_text().endswith('</html>')
        assert (paths[3] / 'run.json').read_text() == '{"files": 1}'
    if point == 'before':
        # Nothing was published, and nothing blocks a retry into the same paths.
        publication.promote(*paths)
        assert paths[1].exists() and paths[3].exists()
    else:
        # A published tree is never replaced.
        with pytest.raises(FileExistsError):
            publication.promote(*paths)
    assert not list(tmp_path.glob('.panda-reserved-*'))


def _race_worker(paths, start, results):
    start.wait(20)
    try:
        publication.promote(*map(Path, paths))
        results.put('ok')
    except OSError:
        results.put('refused')


def test_two_processes_same_destinations_never_mix_or_overwrite(tmp_path):
    first = _trees(tmp_path / 'first')
    second = _trees(tmp_path / 'second')
    second = (second[0], first[1], second[2], first[3])
    (first[0] / 'owner').write_text('first')
    (first[2] / 'owner').write_text('first')
    (second[0] / 'owner').write_text('second')
    (second[2] / 'owner').write_text('second')
    context = multiprocessing.get_context('spawn')
    start, results = context.Event(), context.Queue()
    processes = [context.Process(target=_race_worker,
                 args=([str(path) for path in paths], start, results))
                 for paths in (first, second)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)
        assert not process.is_alive()
        assert process.exitcode == 0
    assert sorted(results.get(timeout=2) for _ in processes) == ['ok', 'refused']
    assert (first[1] / 'owner').read_text() == (first[3] / 'owner').read_text()


@pytest.mark.parametrize('destination_index', [1, 3])
@pytest.mark.parametrize('with_contents', [True, False])
def test_no_replace_refuses_foreign_directory(tmp_path, destination_index, with_contents):
    paths = _trees(tmp_path)
    foreign = paths[destination_index]
    foreign.mkdir()
    if with_contents:
        (foreign / 'foreign').write_text('other job')
    identity = foreign.stat().st_ino
    with pytest.raises(OSError):
        publication.promote(*paths)
    assert foreign.stat().st_ino == identity
    assert sorted(path.name for path in foreign.iterdir()) == (['foreign'] if with_contents else [])
    if with_contents:
        assert (foreign / 'foreign').read_text() == 'other job'


@pytest.mark.parametrize('code', [errno.EACCES, errno.ENOSPC])
@pytest.mark.parametrize('phase', ['sync', 'report', 'output'])
def test_filesystem_failure_does_not_publish_incomplete_output(tmp_path, monkeypatch, code, phase):
    paths = _trees(tmp_path)
    rename = publication._rename_new

    def fail(*args):
        raise OSError(code, 'synthetic-private-value')

    def rename_or_fail(source, destination):
        if destination == paths[3 if phase == 'report' else 1]:
            fail()
        rename(source, destination)

    monkeypatch.setattr(publication, '_sync_tree' if phase == 'sync' else '_rename_new',
                        fail if phase == 'sync' else rename_or_fail)
    with pytest.raises(OSError):
        publication.promote(*paths)
    assert not paths[1].exists()
    assert paths[3].exists() == (phase == 'output')


def test_link_destination_never_replaced_or_followed(tmp_path):
    paths = _trees(tmp_path)
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'keep').write_text('safe')
    paths[1].symlink_to(foreign, target_is_directory=True)
    with pytest.raises(OSError):
        publication.promote(*paths)
    assert paths[1].is_symlink()
    assert (foreign / 'keep').read_text() == 'safe'


def test_report_flush_precedes_output_visibility(tmp_path, monkeypatch):
    paths = _trees(tmp_path)
    calls = []
    original = publication._sync_directory

    def sync(path):
        calls.append((path, paths[3].exists(), paths[1].exists()))
        original(path)

    monkeypatch.setattr(publication, '_sync_directory', sync)
    publication.promote(*paths)
    assert (tmp_path, True, False) in calls
    assert calls[-1] == (tmp_path, True, True)


def test_parent_replaced_by_link_during_publication_refuses(tmp_path, monkeypatch):
    root = tmp_path / 'job'
    paths = _trees(root)
    moved = tmp_path / 'moved'
    original = publication._sync_tree
    calls = 0

    def sync_then_substitute(path):
        nonlocal calls
        original(path)
        calls += 1
        if calls == 2:
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)

    monkeypatch.setattr(publication, '_sync_tree', sync_then_substitute)
    with pytest.raises(ValueError, match='linked'):
        publication.promote(*paths)
    assert not paths[1].exists()
    assert not paths[3].exists()
    assert (moved / paths[0].name / 'file-0001.html').exists()


@pytest.mark.parametrize('with_contents', [True, False])
def test_fallback_rename_never_replaces_a_directory(tmp_path, monkeypatch, with_contents):
    """macOS has no renameat2; the exclusive-mkdir fallback must still refuse."""
    monkeypatch.setattr(publication.sys, 'platform', 'darwin')
    source, destination = tmp_path / 'source', tmp_path / 'destination'
    source.mkdir()
    (source / 'file-0001.html').write_text('safe')
    destination.mkdir()
    if with_contents:
        (destination / 'foreign').write_text('other job')
    with pytest.raises(FileExistsError):
        publication._rename_new(source, destination)
    assert (source / 'file-0001.html').read_text() == 'safe'
    assert sorted(p.name for p in destination.iterdir()) == (['foreign'] if with_contents else [])
    if not with_contents:
        destination.rmdir()
        publication._rename_new(source, destination)
        assert (destination / 'file-0001.html').read_text() == 'safe'
        assert not source.exists()
