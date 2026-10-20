"""Console output for `panda run`: short step lines, a live progress bar, a summary.

Plain `print` underneath, so output still reads well when piped into a log. Colour
and the redrawn progress bar are used only on an interactive terminal, and the tick
symbols fall back to ASCII where the console encoding cannot show them (a C or
POSIX locale). Nothing here may print a source name or value.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def _can_encode(stream: TextIO, text: str) -> bool:
    try:
        text.encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Console:
    def __init__(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        fancy = _can_encode(self.out, "✓✗─├└█░·") and _can_encode(self.err, "✓✗")
        self.ok, self.bad, self.warn = ("✓", "✗", "!") if fancy else ("+", "x", "!")
        self.rule, self.branch, self.last = ("─", "├─", "└─") if fancy else ("-", "|-", "`-")
        self.full, self.empty = ("█", "░") if fancy else ("#", "-")
        self.dot = "·" if fancy else "-"
        # NO_COLOR is the usual opt-out.
        self.colour = self.tty and "NO_COLOR" not in os.environ
        self._progress_open = False

    def _paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour else text

    def green(self, text: str) -> str:
        return self._paint("32", text)

    def red(self, text: str) -> str:
        return self._paint("31", text)

    def yellow(self, text: str) -> str:
        return self._paint("33", text)

    def bold(self, text: str) -> str:
        return self._paint("1", text)

    def dim(self, text: str) -> str:
        return self._paint("2", text)

    def _end_progress(self) -> None:
        if self._progress_open:
            print("\r\033[K" if self.tty else "", end="", file=self.out, flush=True)
            self._progress_open = False

    def line(self, text: str = "") -> None:
        self._end_progress()
        print(text, file=self.out, flush=True)

    def title(self, text: str) -> None:
        self.line(self.bold(text))

    def step(self, text: str) -> None:
        self.line(f"  {self.green(self.ok)} {text}")

    def warning(self, text: str) -> None:
        self.line(f"  {self.yellow(self.warn)} {text}")

    def problem(self, text: str) -> None:
        """A failure detail; stderr, so a log still separates it from progress."""
        self._end_progress()
        print(f"  {self.red(self.bad)} {text}", file=self.err, flush=True)

    def notice(self, text: str) -> None:
        """A skipped file or similar; stderr, like problems, but not an error."""
        self._end_progress()
        print(f"  {self.yellow(self.warn)} {text}", file=self.err, flush=True)

    def detail(self, text: str) -> None:
        self._end_progress()
        print(f"      {text}", file=self.err, flush=True)

    def progress(self, done: int, total: int, label: str) -> None:
        """Redraw a bar on a terminal; stay silent in logs, where the step line suffices."""
        if not self.tty or total <= 0:
            return
        width = 24
        filled = round(width * done / total)
        bar = self.full * filled + self.empty * (width - filled)
        print(
            f"\r\033[K  {self.dim(label)} {bar} {done}/{total}",
            end="", file=self.out, flush=True,
        )
        self._progress_open = True
