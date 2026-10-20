"""Command line entry point.

Two subcommands. `run` sanitises every supported file in a directory, writes the
results to the output directory and an audit report per file, then rescans its own
output and marks failed verification results as unverified debug output. `check`
runs that same rescan on its own, for verifying output the tool did not just produce.

Every supported file is attempted. EXIT_PARTIAL indicates excluded input files;
EXIT_DEBUG indicates retained, unverified output for local debugging.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from panda import __version__
from panda.console import Console, plural
from panda.detectors import default_detector_engine, organisation
from panda.errors import UnsupportedError, refusal_reason
from panda.formats import (
    Discovery,
    assert_regular_path,
    discover,
    file_identity,
    handler_for,
    verify_discovery,
)
from panda.handlers import register_defaults
from panda.leakcheck import UNVERIFIED_MARKER, LeakReport, scan_output
from panda.limits import bounded, capacity_scope
from panda.model import safe_exception_category
from panda.naming import apply_output_names
from panda.propagation import KnownTermsRecognizer, collect_terms
from panda.pseudonyms import ALL_FIXED_SURROGATES, DummyNames
from panda.publication import promote
from panda.report import build_report, write_report
from panda.report_html import REPORT_PAGE, ReportError, batch_name, write_html_report
from panda.spacy_recognizer import ModelSetupError

# Only EXIT_OK and EXIT_PARTIAL claim verified output.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNSANITISED = 2
EXIT_PARTIAL = 3
EXIT_DEBUG = 4


class CLIParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's original message can echo arbitrary arguments or client paths.
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, "error: invalid or missing arguments; use --help for examples\n")


HELP = """\
Panda replaces personal information in client files with dummy values, entirely
offline. Point it at a folder; it writes sanitised copies beside it.

Usage:
  panda run FOLDER [options]     sanitise every supported file in FOLDER (and subfolders)
  panda report RESULTS           build report.html for a finished job
  panda check --output FOLDER    rescan sanitised files for anything left behind
  panda COMMAND --help           more detail on one command
  panda --version                show the version

Options for run:
  --html            also write report.html: a page showing what was replaced,
                    file by file, with search and filters (opens offline)
  --opaque-names    name outputs file-0001, file-0002, ... in one folder instead
                    of keeping the source folders and file names
  --output DIR      put sanitised files in DIR (must not exist) instead of the
                    default results folder; for scripts
  --report DIR      put the audit report in DIR (must not exist); for scripts
  --keep TERM       never replace TERM, e.g. your organisation or product name
                    (repeatable; "Acme" also keeps "Acme Support", "ACME TRADER")
  --keep-file FILE  read --keep terms from FILE, one per line
  --account-prefix P
                    replace codes such as P-12345678 as account numbers: your
                    organisation's own customer numbers (repeatable)

Where results go:
  FOLDER-SANITISED/              beside FOLDER; FOLDER-SANITISED-2 on the next run
    FOLDER-files/                sanitised copies, same folders and names, with any
                                 personal details in the names replaced
    FOLDER-report/               run.json, one JSON per file, report.html (--html)

What is replaced:
  names             dummy names (each person gets their own, consistently)
  phone numbers     0400000000
  emails            john.doe@example.com
  dates of birth    01/01/1990
  addresses         16 Sample Street, Sampleton NSW 2000
  accounts and IDs  00000000 / 000000000000000000

Formats: HTML, EML (with attachments), DOCX, and PDF with a text layer (saved as
UTF-8 text). Images are removed. Scanned PDFs, spreadsheets and audio are skipped.

Exit codes:
  0  every file sanitised, rescan clean     3  some files skipped or failed; the
  1  bad command or setup problem              rest are sanitised and verified
  2  refused; nothing published             4  rescan still found something: output
  130 cancelled                                is under UNVERIFIED/ - do not share

Examples:
  panda run ~/Clients/"Batch 12" --html
  panda run ~/Clients/"Batch 12" --opaque-names --keep "Acme Brokers"
  panda report ~/Clients/"Batch 12-SANITISED"

Automated detection can miss things. Read the output before handing it off.
"""


class HelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS,
                 help=None):
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        print(HELP, end="")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = CLIParser(prog="panda", add_help=False)
    parser.add_argument("-h", "--help", action=HelpAction, help="show help and exit")
    parser.add_argument("--version", action="version", version=f"panda {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser(
        "run", help="sanitise every file in a directory",
        description="Sanitise every supported file in a folder and its subfolders, rescan the\n"
                    "result, and save it in <folder>-SANITISED beside the source.",
        epilog='Examples:\n  panda run ~/Clients/"Batch 12"\n'
               '  panda run ~/Clients/"Batch 12" --html --opaque-names',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument(
        "folder", type=Path, nargs="?", help="folder to sanitise, including subfolders")
    run.add_argument("--input", type=Path, help="alternative to the positional folder")
    run.add_argument(
        "--html", action="store_true",
        help="also write report.html, a self-contained page for viewing the report offline")
    run.add_argument(
        "--opaque-names", action="store_true",
        help="name outputs file-0001, file-0002, ... in one folder instead of keeping the "
             "source folders and file names (with detected PII replaced)")
    run.add_argument(
        "--output", type=Path,
        help="advanced: new directory for sanitised files instead of <folder>-SANITISED "
             "(failed verification is retained under UNVERIFIED)")
    run.add_argument(
        "--report", type=Path,
        help="advanced: new directory for the audit report instead of <folder>-SANITISED")
    _add_keep_arguments(run)

    check = subcommands.add_parser(
        "check", help="rescan sanitised output with the configured recognisers",
        description="Rescan a folder of sanitised files and report anything the detectors "
                    "still recognise (kinds and positions only).",
        epilog='Example: panda check --output "Batch-SANITISED/Batch-files"',
    )
    check.add_argument("--output", type=Path, required=True, help="required directory to rescan")
    _add_keep_arguments(check)

    report = subcommands.add_parser(
        "report", help="build a viewable HTML page from a finished job's report",
        description="Write report.html into a job's report folder. Works offline; the page "
                    "needs no internet and contains only what the JSON reports contain.",
        epilog='Example: panda report "documents-SANITISED"',
    )
    report.add_argument(
        "folder", type=Path,
        help="a <folder>-SANITISED results folder, or the report folder inside it")

    return parser


def _results_root(source: Path) -> tuple[Path, str]:
    """Pick the first free `<folder>-SANITISED[-N]` beside the source folder."""
    source = source.resolve()
    base = source.name or "folder"
    for number in itertools.count(1):
        suffix = "" if number == 1 else f"-{number}"
        candidate = source.parent / f"{base}-SANITISED{suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate, base
    raise AssertionError("unreachable")


_UNSUPPORTED_REASON = (
    "E_UNSUPPORTED: file is not a supported format; supported: HTML, EML, DOCX, text-layer PDF"
)


def _real_parent(path: Path | None) -> Path | None:
    """Resolve links above the chosen folder; the folder itself stays unresolved.

    System folders such as macOS /tmp are links, so they are trusted once here, while a
    linked chosen folder, or any link inside it, is still refused.
    """
    if path is None:
        return None
    path = path.absolute()
    return path.parent.resolve() / path.name


def _paths_overlap(first: Path, second: Path) -> bool:
    """Return whether either resolved path contains the other."""
    first = first.resolve()
    second = second.resolve()
    return first == second or first in second.parents or second in first.parents


def _validate_run_paths(input_dir: Path, output_dir: Path, report_dir: Path) -> str | None:
    """Reject layouts that could rediscover or overwrite source/job data."""
    labelled = (("input", input_dir), ("output", output_dir), ("report", report_dir))
    for index, (first_name, first) in enumerate(labelled):
        for second_name, second in labelled[index + 1 :]:
            if _paths_overlap(first, second):
                return f"{first_name} and {second_name} paths overlap"
    if any(".staging-" in path.name for path in (output_dir, report_dir)):
        return "destination uses the reserved staging namespace"
    if output_dir.exists():
        return "output path already exists; choose a destination that does not exist"
    if report_dir.exists():
        return "report path already exists; choose a destination that does not exist"
    return None


def _new_staging_dir(destination: Path) -> Path:
    """Create a same-filesystem staging directory beside its destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert_regular_path(destination.parent, directory=True)
    return Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))


def _promote_staged(staged_output: Path, output: Path, staged_report: Path, report: Path) -> None:
    """Publish reports durably, then commit output without replacing any target."""
    promote(staged_output, output, staged_report, report)


def _scan_for_publication(directory, engine, *, generated_dummies) -> LeakReport:
    try:
        return scan_output(
            directory, engine, staged=True, generated_dummies=generated_dummies,
        )
    except Exception as error:
        # Scanner failures must preserve completed results for debugging without
        # exposing exception messages, which can contain source text or paths.
        paths = [path for path in generated_dummies if path.exists()]
        return LeakReport(
            unscannable=paths,
            failure_reasons={path: safe_exception_category(error) for path in paths},
            failure_details={path: refusal_reason(error) for path in paths},
        )


def _with_propagated_terms(paths, scratch_parent, pseudonymiser, engine, console=None):
    """First pass: detect across the whole batch, then match those values everywhere.

    A throwaway sanitisation of each file yields every detection its handler saw,
    including nested attachments, without a second extraction path per format.
    Failures are ignored here; the real pass reports them.
    """
    detections = []
    # Its own budget with the same limits: reading every file twice must not spend
    # the real pass's cumulative allowance.
    with capacity_scope(fresh=True), tempfile.TemporaryDirectory(
        prefix=".panda-prepass-", dir=scratch_parent
    ) as scratch:
        for index, path in enumerate(paths, start=1):
            handler = handler_for(path)
            destination = Path(scratch) / f"file-{index:04d}{path.suffix.lower()}"
            try:
                result = handler(path, destination, pseudonymiser, engine)
            except Exception:
                continue
            finally:
                if console is not None:
                    console.progress(index, len(paths), "Reading   ")
            detections.extend(item.detection for item in result.replacements)
    terms = collect_terms(detections)
    return engine.with_provider(KnownTermsRecognizer(terms, exempt=ALL_FIXED_SURROGATES))


@dataclass
class _Staging:
    """Private directories beside each destination, recorded so cleanup removes only them."""

    output: Path | None = None
    report: Path | None = None
    identities: dict[Path, os.stat_result] = field(default_factory=dict)


@bounded
def command_run(args: argparse.Namespace) -> int:
    register_defaults()

    if not args.input.is_dir():
        print("error: input directory not found", file=sys.stderr)
        return EXIT_ERROR

    path_error = _validate_run_paths(args.input, args.output, args.report)
    if path_error is not None:
        print(f"error: {path_error}", file=sys.stderr)
        return EXIT_ERROR

    found: Discovery = discover(args.input)
    if found.total == 0:
        print("error: no files found in input", file=sys.stderr)
        return EXIT_ERROR

    console = Console()
    skipped = len(found.unsupported)
    console.title(
        f"Panda {__version__} {console.dot} {plural(len(found.supported), 'file')} to sanitise"
        + (f" ({skipped} unsupported, skipped)" if skipped else "")
    )

    outcomes = {}
    for index in range(len(found.supported) + 1, found.total + 1):
        outcomes[f"file-{index:04d}"] = "skipped/unsupported"
        console.notice(f"file-{index:04d} skipped: {_UNSUPPORTED_REASON}")

    if not found.supported:
        _print_outcomes(console, outcomes)
        return EXIT_UNSANITISED

    pseudonymiser = DummyNames()
    detector_engine = default_detector_engine()
    detector_engine.prepare()
    console.step("Language model ready (offline)")
    staged = _Staging()
    try:
        _stage(staged, args)
        verify_discovery(found)
        detector_engine = _with_propagated_terms(
            found.supported, staged.output.parent, pseudonymiser, detector_engine, console,
        )
        console.step("Collected names and identifiers from across the batch")
        replacement_counts, generated_dummies = _sanitise_all(
            found, staged, pseudonymiser, detector_engine, console, outcomes,
        )
        if pseudonymiser.exhausted:
            console.warning(
                "More people than dummy names: some dummies stand for more than one person"
            )
        console.step(
            f"Sanitised {len(replacement_counts)} of {plural(len(found.supported), 'file')} "
            f"{console.dot} {plural(sum(replacement_counts.values()), 'replacement')}"
        )
        # A completed handler result is useful for debugging even if its scan fails.
        retained = set(replacement_counts)
        verified, debug, file_ids = _verify_or_quarantine(
            staged, outcomes, retained, detector_engine, generated_dummies,
        )
        if not retained:
            _print_outcomes(console, outcomes)
            console.line()
            console.line(console.red("Nothing published: no file could be sanitised."))
            return EXIT_UNSANITISED
        partial = len(retained) != len(outcomes)
        if debug:
            console.problem(console.red("Rescan found possible PII or could not check a file"))
            _print_leak_summary(console, verified, file_ids)
        else:
            console.step("Rescan: configured recognisers found no PII in the output")
        if not getattr(args, "opaque_names", False):
            _name_outputs(
                args, staged, found, retained, debug, pseudonymiser, detector_engine, console,
            )
        _write_run_summary(
            args, staged, outcomes, retained, replacement_counts, partial, debug, verified,
            detector_engine, console,
        )
        _assert_source_unchanged(args.input, found)

        try:
            _promote_staged(staged.output, args.output, staged.report, args.report)
        except OSError as error:
            console.problem(
                "refusing batch: could not publish output "
                f"({safe_exception_category(error)}): {refusal_reason(error)}"
            )
            return EXIT_UNSANITISED

        _print_result(console, args, outcomes, retained, partial, debug)
        return EXIT_DEBUG if debug else (EXIT_PARTIAL if partial else EXIT_OK)
    finally:
        _cleanup(staged)


def _stage(staged: _Staging, args: argparse.Namespace) -> None:
    staged.output = _new_staging_dir(args.output)
    staged.identities[staged.output] = staged.output.stat()
    staged.report = _new_staging_dir(args.report)
    staged.identities[staged.report] = staged.report.stat()


def _cleanup(staged: _Staging) -> None:
    """Never roll back published paths or delete a replacement staging entry."""
    for stage in (staged.output, staged.report):
        if stage is None:
            continue
        try:
            current = stage.lstat()
            original = staged.identities[stage]
            if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                shutil.rmtree(stage, ignore_errors=True)
        except OSError:
            pass


def _sanitise_all(found, staged, pseudonymiser, detector_engine, console, outcomes):
    """Run each handler into staging; returns replacement counts and per-output dummies."""
    replacement_counts: dict[str, int] = {}
    generated_dummies: dict[Path, frozenset[str]] = {}
    for index, path in enumerate(found.supported, start=1):
        # Source names and directory names may themselves contain PII, so the job
        # runs on opaque ordinals; readable names are applied after verification.
        safe_id = f"file-{index:04d}"
        safe_name = f"{safe_id}{path.suffix.lower()}"
        handler = handler_for(path)
        assert handler is not None  # discover only lists a path if a handler exists
        try:
            if file_identity(path) != found.identities[path]:
                raise ValueError("source changed after discovery")
            result = handler(path, staged.output / safe_name, pseudonymiser, detector_engine)
            if file_identity(path) != found.identities[path]:
                raise ValueError("source changed during extraction")
            # Scope exemptions to replacements returned by this file's handler,
            # including retained nested container parts; never persist raw values.
            output_paths = [
                item for item in staged.output.iterdir()
                if item.name.startswith(safe_id + ".")
            ]
            if not output_paths:
                raise ValueError("handler produced no output")
            for output_path in output_paths:
                if output_path.is_file():
                    generated_dummies[output_path] = frozenset(
                        item.surrogate.casefold() for item in result.replacements
                    )
            write_report(
                staged.report / f"{safe_id}.json",
                build_report(safe_id, result, detector_engine.identity),
            )
        except Exception as error:
            # Exception messages and filenames can contain raw source values. Only
            # expose the stable batch position and exception category.
            status = (
                "skipped/unsupported" if isinstance(error, UnsupportedError)
                else "processing error"
            )
            verb = "skipped" if status == "skipped/unsupported" else "failed"
            (console.notice if verb == "skipped" else console.problem)(
                f"{safe_id} {verb} ({safe_exception_category(error)}): "
                f"{refusal_reason(error)}"
            )
            outcomes[safe_id] = status
            console.progress(index, len(found.supported), "Sanitising")
            continue
        outcomes[safe_id] = "sanitised"
        replacement_counts[safe_id] = len(result.replacements)
        console.progress(index, len(found.supported), "Sanitising")
    return replacement_counts, generated_dummies


def _record_scan(outcomes: dict[str, str], report: LeakReport) -> None:
    for leak in report.leaks:
        safe_id = leak.path.name.split(".")[0]
        if outcomes.get(safe_id) == "sanitised":
            outcomes[safe_id] = "residual PII"
    for path in report.unscannable:
        outcomes[path.name.split(".")[0]] = "processing error"


def _verify_or_quarantine(staged, outcomes, retained, detector_engine, generated_dummies):
    """Rescan staged output, drop failed files' artifacts, and move unverified output aside.

    Returns the verification scan, whether the batch is debug-only, and file ids by path.
    """
    leaks = _scan_for_publication(
        staged.output, detector_engine, generated_dummies=generated_dummies,
    )
    _record_scan(outcomes, leaks)
    file_ids = {
        path: path.name.split(".")[0] for path in (*leaks.scanned, *leaks.unscannable)
    }
    # Remove incomplete handler artifacts, including partial writes.
    # Unknown artifacts cannot be attributed safely and must refuse publication.
    removed_output = False
    for stage in (staged.output, staged.report):
        for artifact in stage.iterdir():
            safe_id = artifact.name.split(".")[0]
            if safe_id not in outcomes:
                raise ValueError("unattributed staged artifact")
            if safe_id not in retained:
                removed_output |= stage == staged.output
                if artifact.is_dir() and not artifact.is_symlink():
                    shutil.rmtree(artifact)
                else:
                    artifact.unlink()
    if not retained:
        return leaks, True, file_ids
    # Recheck the exact retained subset only if removing artifacts changed the output.
    verified = leaks
    if removed_output:
        verified = _scan_for_publication(
            staged.output, detector_engine, generated_dummies=generated_dummies,
        )
    debug = (
        any(outcomes[key] != "sanitised" for key in retained)
        or not verified.clean or not verified.scanned
    )
    if debug:
        _record_scan(outcomes, verified)
        # A separate, conspicuous subtree prevents debug artifacts looking like
        # ordinary verified output. The marker also makes standalone check refuse.
        artifacts = list(staged.output.iterdir())
        debug_dir = staged.output / "UNVERIFIED"
        debug_dir.mkdir()
        for artifact in artifacts:
            artifact.rename(debug_dir / artifact.name)
        warning = "UNVERIFIED DEBUG OUTPUT: may contain PII. Do not share.\n"
        (staged.output / UNVERIFIED_MARKER).write_text(warning, encoding="utf-8")
        (debug_dir / UNVERIFIED_MARKER).write_text(warning, encoding="utf-8")
    return verified, debug, file_ids


def _name_outputs(args, staged, found, retained, debug, pseudonymiser, detector_engine, console):
    """Rename outputs after their sources; runs last, so everything before stays ordinal."""
    sources = {
        f"file-{index:04d}": path for index, path in enumerate(found.supported, start=1)
    }
    output_names = apply_output_names(
        staged.output / "UNVERIFIED" if debug else staged.output,
        {safe_id: path for safe_id, path in sources.items() if safe_id in retained},
        args.input, pseudonymiser, detector_engine,
    )
    for safe_id, names in output_names.items():
        report_path = staged.report / f"{safe_id}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        write_report(report_path, {**report, "outputs": names})
    opaque = len(retained) - len(output_names)
    if opaque:
        console.warning(
            f"Named outputs after their source files; {opaque} name(s) could not "
            "be made safe and are file-NNNN instead"
        )
    else:
        console.step("Named outputs after their source files (PII in names replaced)")


def _write_run_summary(
    args, staged, outcomes, retained, replacement_counts, partial, debug, verified,
    detector_engine, console,
):
    write_report(
        staged.report / "run.json",
        {
            "completed_utc": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
            "files": len(retained),
            "outcomes": outcomes,
            "batch_status": (
                "unverified_debug" if debug else ("partial" if partial else "complete")
            ),
            "replacements": sum(replacement_counts[key] for key in retained),
            "recognisers": list(detector_engine.identity),
            "output_names": "opaque" if getattr(args, "opaque_names", False) else "source",
            "verification": {
                "status": "failed" if debug else "clean_for_configured_recognisers",
                "files_scanned": len(verified.scanned),
                "recognised_dummies": verified.recognised_dummies,
            },
        },
    )
    if getattr(args, "html", False):
        try:
            write_html_report(staged.report, batch=batch_name(args.report))
        except Exception as error:
            # A convenience view; the JSON reports remain the record.
            console.warning(f"Visual report not written ({safe_exception_category(error)})")
            args.html = False


def _assert_source_unchanged(input_dir: Path, found: Discovery) -> None:
    verify_discovery(found)
    final_discovery = discover(input_dir)
    if (
        final_discovery.unsupported != found.unsupported
        or final_discovery.supported != found.supported
        or any(file_identity(path) != identity for path, identity in found.identities.items())
    ):
        raise ValueError("source changed during job")


def _print_result(console, args, outcomes, retained, partial, debug) -> None:
    _print_outcomes(console, outcomes)
    console.line()
    if debug:
        console.line(console.red(console.bold(
            "UNVERIFIED: output may still contain PII. Do not share it."
        )))
        console.line("  Files are under UNVERIFIED/ for checking only; fix and rerun.")
    elif partial:
        console.line(console.yellow(console.bold(
            f"Done, with gaps: {len(retained)} of {plural(len(outcomes), 'file')} "
            "sanitised and verified."
        )))
        console.line("  Skipped and failed files are not in the output (see above).")
    else:
        console.line(console.green(console.bold(
            f"Done: {'all ' if len(retained) > 1 else ''}{plural(len(retained), 'file')} "
            "sanitised and verified."
        )))
    _print_destinations(console, args)
    if not debug:
        console.line(console.dim("  Automated detection can miss things: read the output "
                                 "before handing it off."))


_OUTCOME_LABELS = (
    ("sanitised", "sanitised"),
    ("skipped/unsupported", "skipped"),
    ("residual PII", "still had PII"),
    ("processing error", "failed"),
)


def _print_outcomes(console: Console, outcomes: dict[str, str]) -> None:
    """One line of counts; files are listed only when something went wrong."""
    counts = {
        label: sum(value == status for value in outcomes.values())
        for status, label in _OUTCOME_LABELS
    }
    console.line()
    console.line("  Files    " + f" {console.dot} ".join(
        f"{count} {label}" for label, count in counts.items() if count or label == "sanitised"
    ))
    problems = sorted(
        safe_id for safe_id, status in outcomes.items() if status != "sanitised"
    )
    if problems:
        # Reasons were printed as they happened; this just gathers the ids.
        console.line(f"           not sanitised: {', '.join(problems)}")


def _print_destinations(console: Console, args: argparse.Namespace) -> None:
    root = getattr(args, "results_root", None)
    extra = f" + {REPORT_PAGE}" if getattr(args, "html", False) else ""
    if root is not None:
        # Described, not echoed: the folder name can itself be PII and console output
        # ends up in logs.
        base = getattr(args, "results_base", "")
        suffix = root.name[len(base):]
        console.line(
            f"  Saved    <source folder name>{suffix}, beside the source folder"
        )
        console.line(f"           {console.branch} -files    sanitised copies")
        console.line(f"           {console.last} -report   audit reports{extra}")
    else:
        console.line(f"  Files    {args.output}")
        console.line(f"  Report   {args.report}{extra}")


def _print_leak_summary(
    console: Console, report: LeakReport, file_ids: dict[Path, str] | None = None,
) -> None:
    """Print what the rescan found, spans and kinds only, never the value itself."""
    file_ids = file_ids or {}
    if report.leaks:
        by_kind = ", ".join(f"{kind}={count}" for kind, count in sorted(report.counts.items()))
        console.detail(f"leaked PII still present: {len(report.leaks)} span(s) [{by_kind}]")
        for index, leak in enumerate(report.leaks, start=1):
            console.detail(
                f"  {file_ids.get(leak.path, 'output')} detection {index}: "
                f"{leak.kind.value} at {leak.locator} "
                f"offset {leak.start}:{leak.end} "
                f"({leak.detector})"
            )
    for index, path in enumerate(report.unscannable, start=1):
        # Not a leak, but not verifiable either. Fail closed rather than assume clean.
        console.detail(
            f"unscannable file {file_ids.get(path, str(index))}: "
            f"{report.failure_reasons.get(path, 'ExtractionError')}; "
            f"{report.failure_details.get(path, 'E_UNREADABLE: it could not be checked')}"
        )


def command_report(args: argparse.Namespace) -> int:
    try:
        page = write_html_report(args.folder)
    except ReportError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(f"wrote {page}")
    print("open it in any web browser; it works offline")
    return EXIT_OK


def command_check(args: argparse.Namespace) -> int:
    # Needed for the text extractors, or a .docx in the output would be unscannable.
    register_defaults()

    if not args.output.is_dir():
        print("error: output directory not found", file=sys.stderr)
        return EXIT_ERROR

    if ".staging-" in args.output.name:
        print("refusing operation: staging is not completed output", file=sys.stderr)
        return EXIT_UNSANITISED

    detector_engine = default_detector_engine()
    detector_engine.prepare()
    report = scan_output(args.output, detector_engine)
    if not report.scanned and not report.unscannable:
        print("error: no files found in output", file=sys.stderr)
        return EXIT_ERROR

    print(f"checked {len(report.scanned)} file(s)", flush=True)
    if report.recognised_dummies:
        print(f"saw {report.recognised_dummies} expected dummy replacement(s)", flush=True)

    if report.clean:
        print("configured recognisers found no PII")
        return EXIT_OK

    _print_leak_summary(Console(), report)
    return EXIT_UNSANITISED


def _add_keep_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--keep", action="append", default=[], metavar="TERM",
        help="never replace TERM, such as your organisation, product or bot name; "
             "repeatable")
    parser.add_argument(
        "--keep-file", type=Path, metavar="FILE",
        help="read --keep terms from FILE, one per line (# starts a comment)")
    parser.add_argument(
        "--account-prefix", action="append", default=[], metavar="PREFIX",
        help="replace codes such as PREFIX-12345678 as account numbers wherever they "
             "appear, for your organisation's own customer numbers; repeatable")


def _keep_terms(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list[str]:
    terms = list(getattr(args, "keep", []))
    path = getattr(args, "keep_file", None)
    if path is not None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            parser.error(f"cannot read --keep-file {path}")
        terms.extend(line.split("#", 1)[0].strip() for line in lines)
    return [term for term in terms if term.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if not (sys.argv[1:] if argv is None else argv):
        print(HELP, end="")
        return EXIT_OK
    args = parser.parse_args(argv)
    results_root = None
    if args.command == "run":
        if (args.folder is None) == (args.input is None):
            parser.error("provide either folder or --input")
        args.input = _real_parent(args.folder if args.folder is not None else args.input)
        args.report = _real_parent(args.report)
    if args.command in {"run", "check"}:
        args.output = _real_parent(args.output)
    terms = _keep_terms(parser, args)
    try:
        if args.command == "run":
            if args.output is None or args.report is None:
                # Named after the source folder so a batch is easy to find and hand
                # off; the operator has already seen that name. File names inside
                # stay opaque ordinals. A sibling is never rediscovered as input.
                results_root, base = _results_root(args.input)
                args.results_root, args.results_base = results_root, base
                if args.output is None:
                    args.output = results_root / f"{base}-files"
                if args.report is None:
                    args.report = results_root / f"{base}-report"
            with organisation(terms, args.account_prefix):
                return command_run(args)
        if args.command == "report":
            return command_report(args)
        with organisation(terms, args.account_prefix):
            return command_check(args)
    except ModelSetupError:
        print(
            "error: local NLP installation is missing, incompatible or damaged; "
            "reinstall Panda with pip (see the README). "
            "No model is downloaded at runtime.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(
            "interrupted: operation did not complete; rerun with new destinations",
            file=sys.stderr,
        )
        return 130
    except Exception as error:
        print(
            f"refusing operation: could not complete ({safe_exception_category(error)}); "
            f"{refusal_reason(error)}",
            file=sys.stderr,
        )
        return EXIT_UNSANITISED
    finally:
        # A failed job publishes nothing; do not leave its empty results folder behind.
        if results_root is not None:
            with contextlib.suppress(OSError):
                results_root.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
