"""The PDF handler.

A PDF is transcribed, not redacted in place. The output is a sanitised `.txt`, not a
PDF. The reasoning is in AGPL terms: the one library that removes glyphs cleanly while
preserving layout, PyMuPDF, is AGPL and rejected (`docs/DEPENDENCIES.md`), and rebuilding
a faithful PDF would need embedded CJK fonts, a licensing and bundling problem. A text
transcript is enough for the whole-PDF chat histories that are in scope, so this
handler extracts each page's text with pypdf, sanitises it through the shared core, and
writes the result as text.

Three things fall out of the transcript approach, all of them wanted:

- Images never enter the pipeline. Only the text layer is read, so a face, a signature or
  a screenshot of PII inside the PDF is dropped for free, which is the project's rule for
  images anyway.
- The output is UTF-8 text, so the leak check rescans it directly with no format-specific
  extractor. A `.docx` needs `extract_docx_text`; a PDF transcript needs nothing.
- A wholly image-only PDF yields no extractable text and is refused. Parser/decryption
  errors also refuse the job rather than producing trusted output. Mixed PDFs retain
  extractable text and omit images; OCR of scanned PDFs is out of scope.

Pages are sanitised one at a time and joined by a form feed, matching how a reader sees
them: a detector must not match a value across a page break the reader never crosses, the
same reason the `.docx` extractor joins paragraphs by newline.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

from panda.core import sanitise_text
from panda.detectors import DetectorEngine
from panda.errors import EncryptedError, ImageOnlyError, UnsupportedError
from panda.limits import CapacityError, bounded, budget, check
from panda.model import Replacement, SanitiseResult
from panda.pseudonyms import SurrogateSource

# U+000C FORM FEED: the conventional page separator in a plain-text rendering of a paged
# document. Keeping pages apart stops a detector from spanning a page boundary.
_PAGE_BREAK = "\f"


def _page_texts(path: Path) -> list[str]:
    """Return each page's extracted text, in reading order."""
    active = budget()
    remaining_pages = active.limits.pdf_pages - active.counts.get("pdf_pages", 0)
    remaining_expansion = active.limits.expanded_bytes - active.counts.get("expanded_bytes", 0)
    if remaining_pages < 1 or remaining_expansion < 1:
        raise CapacityError()
    limits = replace(active.limits, pdf_pages=remaining_pages, expanded_bytes=remaining_expansion)
    check(path.stat().st_size, limits.source_bytes)
    try:
        process = subprocess.run(
            [sys.executable, "-m", "panda.pdf_worker", str(path), json.dumps(asdict(limits))],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=limits.pdf_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CapacityError() from None
    if process.returncode == 3:
        raise CapacityError()
    if process.returncode == 4:
        raise EncryptedError()
    if process.returncode == 5:
        raise UnsupportedError("PDF extraction refused: unsupported stream encoding")
    if process.returncode == 6:
        raise PermissionError()
    if process.returncode == 7:
        raise OSError()
    if process.returncode != 0:
        raise ValueError("PDF extraction refused")
    result = json.loads(process.stdout)
    active.add("pdf_pages", len(result["pages"]))
    active.add("expanded_bytes", result["expanded_bytes"])
    return result["pages"]


@bounded
def sanitise_pdf_file(
    source: Path,
    destination: Path,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
) -> SanitiseResult:
    """Transcribe one PDF to a sanitised `.txt`, page by page.

    Append `.txt` to the caller-supplied destination name. The CLI supplies an opaque
    ordinal such as `file-0001.pdf`, yielding `file-0001.pdf.txt`; the source filename
    is not preserved. Mixed PDFs retain text and empty page separators, omitting images.
    """
    pages = _page_texts(source)
    replacements: list[Replacement] = []
    sanitised_pages: list[str] = []
    previous = ""
    for page_number, page in enumerate(pages, start=1):
        # A question at the foot of one page can be answered at the top of the next.
        result = sanitise_text(
            page, pseudonymiser, detector_engine, locator=f"pdf/page/{page_number}",
            context=previous + "\n" if previous else "",
        )
        previous = page
        replacements.extend(result.replacements)
        sanitised_pages.append(result.text)

    text = _PAGE_BREAK.join(sanitised_pages)

    if not text.strip():
        # Empty transcripts cannot establish that any content was verified.
        raise ImageOnlyError("no extractable text")

    out_path = destination.parent / (destination.name + ".txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return SanitiseResult(text=text, replacements=replacements)
