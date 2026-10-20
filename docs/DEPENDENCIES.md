# Dependencies

Panda runs offline on client data, so every runtime dependency is pinned exactly, in
`pyproject.toml` and in the full lock `requirements.txt`. Each one must have a
permissive licence that is compatible with Panda's MIT licence. No dependency may
download anything at runtime.

## Direct runtime dependencies

| Package | Version | Licence | Used for |
|---|---|---|---|
| spacy | 3.8.16 | MIT | English name and place recognition |
| en-core-web-md | 3.8.0 | MIT | spaCy's English model; a hash-pinned GitHub wheel, because it is not on PyPI |
| jieba | 0.42.1 | MIT | Chinese word segmentation |
| beautifulsoup4 | 4.12.3 | MIT | Reading HTML (`html.parser`) |
| lxml | 6.1.1 | BSD-3-Clause | XML parsing for DOCX |
| python-docx | 1.1.2 | MIT | Reading and writing DOCX |
| pypdf | 5.1.0 | BSD-3-Clause | PDF text extraction, in a time-limited subprocess (`panda.pdf_worker`) |

`spacy_recognizer` checks the exact spaCy and model versions at load time. A mismatch
fails as a setup error (exit 1), not as a silent change in detection.

## Rejected

- **PyMuPDF (AGPL-3.0).** It is the one library that can remove glyphs from a PDF
  cleanly while keeping the layout. The AGPL is incompatible with distributing Panda
  under MIT, so PDFs are transcribed to sanitised UTF-8 text instead of being redacted
  in place (see `handlers/pdf.py`). Rebuilding a faithful PDF would also need embedded
  CJK fonts, which raises its own licensing and bundling problem.
- **OCR engines.** Scanned PDFs are refused (`E_IMAGE_ONLY`) rather than recognised.
  An OCR stack is heavy, and its errors would be unverifiable by the rescan.

## Adding or upgrading a dependency

1. Pin it exactly in `pyproject.toml` (direct deps) and regenerate `requirements.txt`.
2. Check its licence, and the licences of anything new it pulls in, against MIT.
3. Run `python scripts/generate_sbom.py` in a fresh environment from `requirements.txt`.
   It refuses a version mismatch and records each declared licence.
4. Run the full test suite, including the realistic, holdout and blind corpus gates.
