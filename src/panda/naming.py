"""Readable output names: the source's folders and file names, with PII replaced.

A job runs entirely on opaque ordinals (`file-0001.pdf.txt`), so handlers, the
rescan and failure attribution never see a source name. Only after verification
are outputs moved to their source-relative paths. Every folder and file name goes
through the same detectors as document text (including values propagated from the
rest of the batch) and is then checked again. A name that still shows anything
the detectors recognise, or that cannot be made a valid file name, falls back to
its opaque ordinal rather than being published.

Names are short and carry little context, so a name that appears only in a file
name can be missed; `--opaque-names` keeps every output opaque.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from panda.core import detect_in_context
from panda.detectors import DetectorEngine, resolve_overlaps
from panda.pseudonyms import SurrogateSource

# Separators common in file names hide word boundaries from the recognisers. Each is
# swapped for a space of the same length, so detected offsets apply to the original.
_SEPARATORS = re.compile(r"[_+.~]")
# Dummy values can contain characters no file system accepts ("01/01/1990"). The set
# also covers characters Windows refuses, because outputs may be shared with Windows
# users even though Panda itself runs only on Linux and macOS.
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Device names Windows refuses as file names; kept for the same sharing reason.
_WINDOWS_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)
_RESERVED_TOP_LEVEL = frozenset({"unverified", "unverified.txt"})


class NamePlanner:
    def __init__(self, pseudonymiser: SurrogateSource, engine: DetectorEngine) -> None:
        self._pseudonymiser = pseudonymiser
        self._engine = engine
        self._cache: dict[str, str | None] = {}

    def component(self, name: str) -> str | None:
        """Return `name` with detected values replaced, or None if it is not safe."""
        if name not in self._cache:
            self._cache[name] = self._sanitise(name)
        return self._cache[name]

    def _detect(self, name: str):
        # Both spellings: separators hide word boundaries from name recognisers
        # ("Erica_Lee"), but dots belong to emails and numbers. Same-length
        # substitution keeps both sets of offsets on the original string.
        found = detect_in_context(name, self._engine)
        found += detect_in_context(_SEPARATORS.sub(" ", name), self._engine)
        return resolve_overlaps(found)

    def _sanitise(self, name: str) -> str | None:
        pieces, inserted, cursor = [], [], 0
        length = 0
        for detection in self._detect(name):
            if detection.start < cursor:
                continue
            surrogate = self._pseudonymiser.surrogate(detection.kind, detection.text)
            pieces.append(name[cursor : detection.start])
            length += detection.start - cursor
            inserted.append((length, length + len(surrogate)))
            pieces.append(surrogate)
            length += len(surrogate)
            cursor = detection.end
        pieces.append(name[cursor:])
        # One-for-one substitution keeps the inserted spans' offsets valid.
        result = _INVALID.sub("-", "".join(pieces))
        # Fail closed: anything recognised outside an inserted dummy survived.
        for detection in self._detect(result):
            if not any(start < detection.end and detection.start < end
                       for start, end in inserted):
                return None
        # Windows drops trailing dots and spaces, which could merge distinct names once
        # the output is copied to a Windows user.
        result = result.rstrip(" .")
        if not result or result in (".", "..") or _WINDOWS_RESERVED.match(result):
            return None
        return result

    def stem_path(self, source: Path, input_dir: Path) -> PurePosixPath | None:
        """Sanitised folders plus the file name without its source extension."""
        relative = source.relative_to(input_dir)
        parts = [*relative.parent.parts, relative.name[: len(relative.name) - len(source.suffix)]]
        cleaned = [self.component(part) for part in parts]
        if any(part is None for part in cleaned):
            return None
        return PurePosixPath(*cleaned)


def apply_output_names(
    directory: Path,
    sources: dict[str, Path],
    input_dir: Path,
    pseudonymiser: SurrogateSource,
    engine: DetectorEngine,
) -> dict[str, list[str]]:
    """Move each job's `<id>.*` outputs in `directory` to readable relative paths.

    Returns the published relative paths per ordinal; ordinals whose names could
    not be made safe keep their opaque outputs and are left out.
    """
    planner = NamePlanner(pseudonymiser, engine)
    # Case-folded, because macOS (and Windows, where outputs may be shared) treat names
    # differing only in case as one.
    used_files = set(_RESERVED_TOP_LEVEL)
    used_files.update(artifact.name.casefold() for artifact in directory.iterdir())
    used_dirs: set[str] = set()
    published: dict[str, list[str]] = {}
    for safe_id, source in sorted(sources.items()):
        artifacts = sorted(
            item for item in directory.iterdir() if item.name.startswith(safe_id + ".")
        )
        if not artifacts:
            continue
        stem_path = planner.stem_path(source, input_dir)
        if stem_path is None:
            continue
        for number in range(1, 10_000):
            stem = stem_path.name if number == 1 else f"{stem_path.name} ({number})"
            targets = [
                stem_path.parent / (stem + item.name[len(safe_id):]) for item in artifacts
            ]
            if not any(
                str(target).casefold() in used_files | used_dirs
                or any(str(parent).casefold() in used_files for parent in target.parents)
                for target in targets
            ):
                break
        else:
            continue
        for item, target in zip(artifacts, targets, strict=True):
            used_files.add(str(target).casefold())
            used_dirs.update(str(parent).casefold() for parent in target.parents)
            destination = directory / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError("output name collision")
            item.rename(destination)
        published[safe_id] = [target.as_posix() for target in targets]
    return published
