"""Report-first publication: the output directory rename is the commit point.

Staged trees are flushed, the report directory is renamed into place, then the
output directory. Neither rename may replace an existing path. On Linux the
atomic no-replace rename (renameat2) is used; elsewhere (macOS, or a Linux
filesystem without it) the destination is claimed with an exclusive mkdir and the
contents are moved in. Nothing is left behind beside the destinations, so a deleted
result folder can be reused.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path

from panda.formats import assert_regular_path, walk_files


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(path: Path) -> None:
    for item in walk_files(path):
        with item.open("rb+") as stream:
            os.fsync(stream.fileno())
    directories = [path, *(item for item in path.rglob("*") if item.is_dir())]
    for directory in reversed(directories):
        _sync_directory(directory)


def _rename_new(source: Path, destination: Path) -> None:
    """Rename without replacing an existing destination."""
    assert_regular_path(source, directory=True)
    assert_regular_path(destination.parent, directory=True)
    if sys.platform == "linux":
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is not None:
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                               ctypes.c_uint]
            rename.restype = ctypes.c_int
            # RENAME_NOREPLACE; EINVAL/ENOSYS mean the filesystem lacks it (e.g. some
            # network or WSL mounts), so fall back to check-then-rename below.
            if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
                return
            code = ctypes.get_errno()
            if code not in (errno.EINVAL, errno.ENOSYS):
                raise OSError(code, "exclusive publication failed")
    # rename(2) silently replaces an empty directory, so claim the destination with an
    # exclusive mkdir (which fails if anything exists there) and move the contents in.
    os.mkdir(destination)
    for child in source.iterdir():
        os.rename(child, destination / child.name)
    os.rmdir(source)


def promote(staged_output: Path, output: Path, staged_report: Path, report: Path) -> None:
    """Make reports durable before an output directory can become visible."""
    for destination in (output, report):
        assert_regular_path(destination.parent, directory=True)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(errno.EEXIST, "destination already exists")
    _sync_tree(staged_output)
    _sync_tree(staged_report)
    _rename_new(staged_report, report)
    _sync_directory(report.parent)
    _rename_new(staged_output, output)
    _sync_directory(output.parent)
