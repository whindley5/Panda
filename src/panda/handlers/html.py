"""HTML review transcripts; see docs/HTML_POLICY.md for the closed output dialect."""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from panda.core import sanitise_text
from panda.detectors import DetectorEngine
from panda.formats import ExtractedText
from panda.limits import bounded, check_markup, read_bytes
from panda.model import SanitiseResult
from panda.pseudonyms import SurrogateSource

# Non-content and embedded-language elements lose their entire subtree. Every other
# element, including unknown ones such as <form>, keeps its text: dropping unknown
# containers silently deleted whole web-form exports. The output is plain paragraphs
# either way, so retained text cannot become markup or an executable surface.
_DROPPED = frozenset(
    "head title script style template noscript iframe frame frameset object embed applet "
    "svg math canvas video audio img picture source track map area meta link base param "
    "dialog slot".split()
)
_INLINE = frozenset(
    "a abbr b bdi bdo cite code del em i ins kbd mark q s samp small span strong "
    "sub sup time u var label font nobr wbr big tt data output".split()
)
# Form controls carry the submitted value in an attribute, which is document content.
_VALUE_ELEMENTS = frozenset({"input", "option", "button"})
_ENCODED = re.compile(r"%[0-9a-fA-F]{2}|\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4})")


def _reading_blocks(markup: str | bytes) -> list[str]:
    check_markup(markup)
    soup = BeautifulSoup(markup, "html.parser")
    if soup.contains_replacement_characters:
        raise ValueError("HTML decoding failed")
    blocks: list[str] = []
    fragments: list[str] = []

    def flush() -> None:
        value = "".join(fragments)
        fragments.clear()
        if value.strip():
            if _ENCODED.search(value) or "\ufffd" in value or "\x00" in value:
                raise ValueError("Unsupported HTML text encoding")
            blocks.append(value)

    # An iterative walk avoids introducing a second recursion limit for markup.
    stack = list(reversed(soup.contents))
    while stack:
        node = stack.pop()
        if node is None:
            flush()
        elif type(node) is NavigableString:
            fragments.append(str(node))
        elif isinstance(node, Tag):
            if node.name in _DROPPED:
                continue
            if node.name in _VALUE_ELEMENTS:
                value = node.get("value")
                kind = str(node.get("type", "")).lower()
                if isinstance(value, str) and kind not in {"hidden", "password"}:
                    fragments.append(" " + value + " ")
                stack.extend(reversed(node.contents))
            elif node.name in _INLINE:
                stack.extend(reversed(node.contents))
            elif node.name in {"br", "hr"}:
                flush()
            else:
                flush()
                stack.append(None)
                stack.extend(reversed(node.contents))
    flush()
    return blocks


@bounded
def sanitise_html(
    markup: str | bytes,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
) -> SanitiseResult:
    """Rebuild inert paragraphs, detecting across inline boundaries first."""
    paragraphs = []
    replacements = []
    previous = ""
    for index, text in enumerate(_reading_blocks(markup), 1):
        # The previous block carries labels and questions ("Date of birth:" in one
        # table cell, the value in the next); it informs detection only.
        result = sanitise_text(
            text, pseudonymiser, detector_engine, locator=f"html/text/{index}",
            context=previous + "\n",
        )
        previous = text
        paragraphs.append("<p>" + escape(result.text, quote=False) + "</p>")
        replacements.extend(result.replacements)
    return SanitiseResult(
        text="<html><body>" + "".join(paragraphs) + "</body></html>",
        replacements=replacements,
    )


def sanitise_html_file(
    source: Path,
    destination: Path,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
) -> SanitiseResult:
    """Write a UTF-8 HTML review transcript without changing the source."""
    result = sanitise_html(read_bytes(source), pseudonymiser, detector_engine)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(result.text, encoding="utf-8")
    return result


@bounded
def extract_html_markup_units(markup: str | bytes) -> list[ExtractedText]:
    """Independently validate every serialized byte before extracting text.

    Exact envelope and canonical escaping checks prevent tolerant HTML parsing
    from repairing, ignoring or interpreting an unsafe retained surface.
    """
    from html import unescape

    check_markup(markup)
    text = markup.decode("utf-8") if isinstance(markup, bytes) else markup
    # MIME text decoding may append one terminal line ending.
    text = text.removesuffix("\r\n") if text.endswith("\r\n") else text.removesuffix("\n")
    prefix, suffix = "<html><body>", "</body></html>"
    if not text.startswith(prefix) or not text.endswith(suffix):
        raise ValueError("Unsupported HTML envelope")
    content = text[len(prefix) : -len(suffix)]
    units = []
    position = 0
    paragraph = re.compile(r"<p>([^<]*)</p>")
    while position < len(content):
        match = paragraph.match(content, position)
        if match is None:
            raise ValueError("Unsupported HTML surface")
        encoded = match.group(1)
        value = unescape(encoded)
        if (
            escape(value, quote=False) != encoded
            or _ENCODED.search(value)
            or "\ufffd" in value
            or "\x00" in value
        ):
            raise ValueError("Unsupported HTML text encoding")
        units.append(ExtractedText(value, f"html/text/{len(units) + 1}"))
        position = match.end()
    return units or [ExtractedText("", "html/document")]


def extract_html_units(path: Path) -> list[ExtractedText]:
    """Validate and extract a standalone HTML review transcript."""
    return extract_html_markup_units(read_bytes(path))
