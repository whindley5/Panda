"""Shared provisional capacity policy; counters contain no client values."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from io import BytesIO
from pathlib import Path
from struct import unpack_from
from zipfile import ZipFile


class CapacityError(ValueError):
    def __init__(self):
        super().__init__("resource capacity exceeded")


@dataclass(frozen=True)
class Limits:
    source_bytes: int = 16 * 1024 * 1024
    total_files: int = 1000
    total_bytes: int = 256 * 1024 * 1024
    directories: int = 2000
    directory_depth: int = 64
    text_chars: int = 250_000
    total_text_chars: int = 2_000_000
    markup_nodes: int = 50_000
    zip_entries: int = 512
    expanded_bytes: int = 64 * 1024 * 1024
    zip_ratio: int = 200
    mime_depth: int = 16
    mime_parts: int = 128
    attachments: int = 64
    pdf_pages: int = 200
    pdf_seconds: int = 30
    pdf_memory_bytes: int = 768 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ValueError("capacity limits must be positive integers")


DEFAULT_LIMITS = Limits()


@dataclass
class Budget:
    limits: Limits
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, amount: int) -> None:
        value = self.counts.get(kind, 0) + amount
        check(value, getattr(self.limits, kind))
        self.counts[kind] = value


_ACTIVE: ContextVar[Budget | None] = ContextVar("panda_capacity", default=None)


def check(value: int, maximum: int) -> None:
    if value > maximum:
        raise CapacityError()


def budget() -> Budget:
    return _ACTIVE.get() or Budget(DEFAULT_LIMITS)


@contextmanager
def capacity_scope(limits: Limits | None = None, *, fresh: bool = False):
    current = _ACTIVE.get()
    if current is not None and limits is None and not fresh:
        yield current
        return
    token = _ACTIVE.set(Budget(limits or (current.limits if current else DEFAULT_LIMITS)))
    try:
        yield budget()
    finally:
        _ACTIVE.reset(token)


def bounded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with capacity_scope():
            return function(*args, **kwargs)
    return wrapped


def read_bytes(path: Path) -> bytes:
    maximum = budget().limits.source_bytes
    check(path.stat().st_size, maximum)
    with path.open("rb") as stream, BytesIO() as output:
        total = 0
        while chunk := stream.read(min(65536, maximum - total + 1)):
            total += len(chunk)
            check(total, maximum)
            output.write(chunk)
        return output.getvalue()


def check_markup(markup: str | bytes) -> None:
    limits = budget().limits
    check(len(markup), limits.source_bytes)
    check(markup.count(b"<" if isinstance(markup, bytes) else "<"), limits.markup_nodes)


def check_text(text: str) -> None:
    active = budget()
    check(len(text), active.limits.text_chars)
    active.add("total_text_chars", len(text))


def check_zip(path: Path) -> bytes:
    """Validate declared sizes before reads, then count actual streamed expansion."""
    active = budget()
    data = read_bytes(path)
    # Bound the central-directory object count before ZipFile materialises it.
    # ZIP64/multidisk archives are unnecessary at these small source limits.
    with BytesIO(data) as source:
        source.seek(max(0, len(data) - 65557))
        tail = source.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise ValueError("malformed ZIP envelope")
    _, disk, directory_disk, disk_entries, entries, size, start, comment = unpack_from(
        "<4s4H2LH", tail, offset
    )
    if disk or directory_disk or disk_entries != entries or entries == 65535:
        raise ValueError("unsupported ZIP envelope")
    if len(tail) - offset != 22 + comment:
        raise ValueError("malformed ZIP envelope")
    check(active.counts.get("zip_entries", 0) + entries, active.limits.zip_entries)
    # Scan fixed central headers without allocating filename/comment objects.
    with BytesIO(data) as source:
        source.seek(start)
        consumed = 0
        for _ in range(entries):
            header = source.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise ValueError("malformed ZIP directory")
            name, extra, note = unpack_from("<3H", header, 28)
            length = name + extra + note
            source.seek(length, 1)
            consumed += 46 + length
        if consumed != size:
            raise ValueError("malformed ZIP directory")
    with ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        active.add("zip_entries", len(entries))
        declared = sum(entry.file_size for entry in entries)
        check(active.counts.get("expanded_bytes", 0) + declared, active.limits.expanded_bytes)
        for entry in entries:
            check(entry.file_size, max(1, entry.compress_size) * active.limits.zip_ratio)
        for entry in entries:
            with archive.open(entry) as stream:
                while chunk := stream.read(min(65536, active.limits.expanded_bytes + 1)):
                    active.add("expanded_bytes", len(chunk))
    return data
