# Changelog

## Unreleased (2026-10-20)

Fixes from an external review of the repository.

### Added
- CI on Ubuntu and macOS with Python 3.11 and 3.13, running `ruff` and `pytest`.
- A test gate that holds the blind corpus at 1/440 personal values surviving and 1/251
  business strings lost, so accuracy regressions fail CI.
- `docs/MIME_POLICY.md`, `docs/HTML_POLICY.md` and `docs/DEPENDENCIES.md`, which set out
  what each handler may output and why each dependency was chosen (or rejected).

### Changed
- Dummy names are now built from first-name and surname lists (576 Latin, 64 Chinese)
  instead of rotating through 24 and 8 fixed names, so different people no longer share a
  dummy. A run warns if it runs out of names. The first dummies are unchanged.
- `command_run` (about 270 lines) is now split into its stages: stage, sanitise, verify or
  quarantine, name outputs, write summary, check the source is unchanged, publish.

### Fixed
- A symbolic link above the chosen folder (e.g. `/tmp` on macOS) no longer refuses the
  whole job. Links inside the tree are still refused, now with their own `E_LINKED` error
  instead of a misleading `E_MALFORMED`.
- Docstrings no longer cite documents that didn't exist, and leftover internal notes were
  removed, along with the unused `.mp3` placeholder format.

### Performance
- The leak check builds its coverage map once per text unit instead of once per
  detection.
- The second full rescan of the output only runs when failed files' partial output had to
  be removed; otherwise the first scan's result is reused.

## 0.1.0 (2026-10-09)

First public version: `.docx`, `.eml`, HTML and PDF input, English and simplified-Chinese
detection, batch-wide name propagation, output rescanning with an `UNVERIFIED/`
quarantine, and JSON and HTML reports.

## Before 0.1.0

- **Text only.** Pattern detection for phones, emails, dates, addresses and IDs, plus
  spaCy names, on plain text.
- **More formats.** Handlers for `.docx`, `.eml`, HTML and PDF, each limited to
  extracting and rebuilding text.
- **Accuracy rounds.** Tuned on a realistic corpus, checked on a holdout (32/326
  surviving before tuning, 0 after), then measured on a blind set (37/440 on its first
  run). Each category found was fixed in general form: derived nicknames, initial and
  surname aliases, joined hyphenated names, Hong Kong and mainland addresses written in
  English, and oddly spaced phone numbers.
