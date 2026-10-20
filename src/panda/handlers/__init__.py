"""Built-in format handlers.

Registration is a function, not an import side effect, on purpose: `run` calls
`register_defaults` when it needs the handlers, and it is idempotent, so a process
that clears the registry (the tests do this between cases) can always rebuild it.
Importing this package therefore registers nothing on its own.
"""

from __future__ import annotations

from panda.formats import HANDLERS, TEXT_EXTRACTORS
from panda.handlers.docx import extract_docx_units, sanitise_docx_file
from panda.handlers.eml import extract_eml_units, sanitise_eml_file
from panda.handlers.html import extract_html_units, sanitise_html_file
from panda.handlers.pdf import sanitise_pdf_file


def register_defaults() -> None:
    """Install the built-in handlers, skipping any already present."""
    for extension in (".html", ".htm"):
        HANDLERS.setdefault(extension, sanitise_html_file)
    HANDLERS.setdefault(".eml", sanitise_eml_file)
    HANDLERS.setdefault(".docx", sanitise_docx_file)
    HANDLERS.setdefault(".pdf", sanitise_pdf_file)
    # A .docx is a binary zip, so the leak check needs a way to read its text back.
    TEXT_EXTRACTORS.setdefault(".docx", extract_docx_units)
    TEXT_EXTRACTORS.setdefault(".eml", extract_eml_units)
    for extension in (".html", ".htm"):
        TEXT_EXTRACTORS.setdefault(extension, extract_html_units)
    # A PDF needs no extractor: its sanitised output is already a `.txt` the check reads
    # directly. A stray `.pdf` in output has no extractor and so fails closed as
    # unscannable, which is correct - the tool never emits a `.pdf`.
