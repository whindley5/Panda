"""File format registry and input discovery.

Handlers register when the CLI loads supported formats. Discovery fails closed:
a file the tool does not understand must never reach the output directory, because
an untouched file can be mistaken for a sanitised one once published.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from panda.detectors import DetectorEngine
from panda.limits import budget, check, check_text
from panda.model import SanitiseResult
from panda.pseudonyms import SurrogateSource

# A handler reads one file, routes its text through panda.core, writes the
# sanitised file to the destination, and returns what it replaced so the caller can
# build a report. Handlers own only the format: getting text out of the container
# and back in without breaking it. They never detect or replace anything themselves.
Handler = Callable[[Path, Path, SurrogateSource, DetectorEngine], SanitiseResult]

# Extractors independently validate DOCX, HTML and MIME output structure before
# returning located text for rescanning. PDF transcripts are rescanned as plain UTF-8.
@dataclass(frozen=True)
class ExtractedText:
    """One independently scannable, structurally located container region."""

    text: str
    locator: str

    def __post_init__(self) -> None:
        check_text(self.text)


TextExtractor = Callable[[Path], list[ExtractedText]]

# Extensions the finished tool is expected to cover. Listing them separately from
# HANDLERS lets the CLI tell "not built yet" apart from "not something we handle",
# which are very different messages for the person running a job.
PLANNED_EXTENSIONS = frozenset({".eml", ".html", ".htm", ".docx", ".pdf", ".mp3"})

# Populated when format handlers are loaded via register().
HANDLERS: dict[str, Handler] = {}

# Keyed by output extension for formats requiring structural verification.
TEXT_EXTRACTORS: dict[str, TextExtractor] = {}


def register(extension: str, handler: Handler) -> None:
    """Register the handler for one file extension."""
    normalised = extension.lower()
    if normalised in HANDLERS:
        raise ValueError(f"handler already registered for {normalised}")
    HANDLERS[normalised] = handler


def handler_for(path: Path) -> Handler | None:
    """Return the handler for a path, or None if nothing can sanitise it."""
    return HANDLERS.get(path.suffix.lower())


def extractor_for(path: Path) -> TextExtractor | None:
    """Return the leak-check text extractor for a path, or None if it needs none."""
    return TEXT_EXTRACTORS.get(path.suffix.lower())


@dataclass
class Discovery:
    """What a scan of the input directory found.

    `unsupported` covers both planned-but-unbuilt formats and formats that are out
    of scope entirely. Both are refused, because the risk is identical.
    """

    supported: list[Path] = field(default_factory=list)
    unsupported: list[Path] = field(default_factory=list)

    identities: dict[Path, tuple[int, int, int, int, int]] = field(default_factory=dict)
    directories: dict[Path, tuple[int, int]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.supported) + len(self.unsupported)


def walk_files(directory: Path) -> list[Path]:
    """Enumerate completely or raise; pathlib glob may suppress traversal errors."""
    files = []
    limits = budget().limits
    total_bytes = 0
    directories = 0

    def visit(folder: Path, depth: int = 0) -> None:
        nonlocal total_bytes, directories
        check(depth, limits.directory_depth)
        directories += 1
        check(directories, limits.directories)
        with os.scandir(folder) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                # Links can escape the selected tree or hide files from traversal.
                # Require ordinary local entries.
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("linked filesystem entries are unsupported")
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    visit(path, depth + 1)
                elif stat.S_ISREG(info.st_mode):
                    check(info.st_size, limits.source_bytes)
                    total_bytes += info.st_size
                    check(total_bytes, limits.total_bytes)
                    check(len(files) + 1, limits.total_files)
                    files.append(path)
                else:
                    raise ValueError("non-regular filesystem entry")

    assert_regular_path(directory, directory=True)
    visit(directory)
    return sorted(files)


def discover(input_dir: Path) -> Discovery:
    """Include hidden files and refuse incomplete or linked directory trees."""
    result = Discovery()
    info = assert_regular_path(input_dir, directory=True)
    result.directories[input_dir] = (info.st_dev, info.st_ino)
    for path in walk_files(input_dir):
        for parent in path.parents:
            if parent in result.directories:
                break
            info = assert_regular_path(parent, directory=True)
            result.directories[parent] = (info.st_dev, info.st_ino)
        if handler_for(path) is not None:
            result.identities[path] = file_identity(path)
            result.supported.append(path)
        else:
            result.unsupported.append(path)
    return result


def assert_regular_path(path: Path, *, directory: bool = False) -> os.stat_result:
    """Refuse links in every component, including the selected root."""
    absolute = path.absolute()
    for component in (*reversed(absolute.parents), absolute):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("linked filesystem entries are unsupported")
        expected = stat.S_ISDIR if component != absolute or directory else stat.S_ISREG
        if not expected(info.st_mode):
            raise ValueError("non-regular filesystem entry")
    return info


def file_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = assert_regular_path(path)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def verify_discovery(found: Discovery) -> None:
    """Refuse source replacement or mutation before any result can be trusted."""
    for path, identity in found.directories.items():
        info = assert_regular_path(path, directory=True)
        if (info.st_dev, info.st_ino) != identity:
            raise ValueError("source directory changed after discovery")
    for path, identity in found.identities.items():
        if file_identity(path) != identity:
            raise ValueError("source changed after discovery")
