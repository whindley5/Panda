"""Sanitise DOCX story text into a closed, independently verified review dialect.

Paragraphs are detected as a whole even when PII spans runs. Historical text,
field commands and images are removed before projection. Only the explicit
structure and formatting in docx_policy survive; auxiliary parts are omitted.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from lxml import etree

from panda.core import sanitise_text
from panda.detectors import DetectorEngine
from panda.formats import ExtractedText
from panda.handlers.docx_policy import project, verify_package, write_package
from panda.limits import bounded, check_zip
from panda.model import Replacement, SanitiseResult
from panda.pseudonyms import SurrogateSource

# Content-type fragments of the parts that hold reader-visible wordprocessing text.
# Matching on the content type rather than the filename catches header2.xml, footer3.xml
# and the like without hard-coding numbers, and skips styles/settings/fontTable.
# Auxiliary parts are excluded by the closed output package policy.
_STORY_TYPES = (
    "wordprocessingml.document.main+xml",
    "wordprocessingml.header+xml",
    "wordprocessingml.footer+xml",
    "wordprocessingml.comments+xml",
    "wordprocessingml.footnotes+xml",
    "wordprocessingml.endnotes+xml",
)

# Elements that embed a picture or an OLE object. Removed whole: a text box caught here is
# rare and losing it is safe, where leaving an image is not.
_IMAGE_ELEMENTS = (qn("w:drawing"), qn("w:pict"), qn("w:object"))

_W_T = qn("w:t")
# Historical text and executable instructions are unnecessary for review. Remove
# them regardless of detector confidence, including instructions split across runs.
_REMOVED_STORY_TAGS = tuple(qn(f"w:{name}") for name in (
    "del", "moveFrom", "delText", "instrText", "delInstrText", "fldChar",
))
_PRIVATE_STORY_ATTRIBUTES = tuple(qn(f"w:{name}") for name in (
    "author", "initials", "date",
))
_W_P = qn("w:p")
_W_TC = qn("w:tc")
_W_TR = qn("w:tr")
_XML_SPACE = qn("xml:space")


def _is_story(content_type: str) -> bool:
    return content_type in {
        f"application/vnd.openxmlformats-officedocument.{fragment}"
        for fragment in _STORY_TYPES
    }


def _story_parts(document: Document):
    """Yield every package part that holds reader-visible wordprocessing text."""
    for part in document.part.package.iter_parts():
        if _is_story(part.content_type):
            yield part


def _root_of(part):
    """Return a part's XML root, whether python-docx models it (`.element`) or not."""
    if hasattr(part, "element"):
        return part.element
    return parse_xml(part.blob)


def _preserve_space(element) -> None:
    """Keep leading, trailing and repeated spaces from being collapsed by a reader."""
    element.set(_XML_SPACE, "preserve")


def _visible_text(element) -> str:
    return "".join(node.text or "" for node in element.findall(f".//{_W_T}"))


def _column_header(paragraph) -> str:
    """Return the first-row cell text above a table cell's paragraph, if any.

    KYC forms and file notes put labels in a header row ("BSB", "Date of birth") and
    values below, so the label is several paragraphs away in document order.
    """
    cell = next((a for a in paragraph.iterancestors(_W_TC)), None)
    if cell is None:
        return ""
    row = cell.getparent()
    table = row.getparent()
    rows = table.findall(_W_TR)
    if not rows or rows[0] is row:
        return ""
    index = row.findall(_W_TC).index(cell)
    header_cells = rows[0].findall(_W_TC)
    return _visible_text(header_cells[index]) if index < len(header_cells) else ""


def _sanitise_paragraph(
    paragraph,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
    collected: list[Replacement],
    locator: str,
    previous: str = "",
) -> None:
    """Sanitise one `<w:p>`: its retained visible text as a whole."""
    texts = paragraph.findall(f".//{_W_T}")
    if texts:
        joined = "".join(node.text or "" for node in texts)
        header = _column_header(paragraph)
        # Label/value forms put the label in the left cell (the previous paragraph);
        # header-row tables put it above. Try both rather than guess the layout.
        context = (f"{previous}\n", f"{header}: ") if header else f"{previous}\n"
        result = sanitise_text(
            joined, pseudonymiser, detector_engine, locator=locator, context=context
        )
        if result.replacements:
            collected.extend(result.replacements)
            # Consolidate into the first run so a value split across runs is replaced as
            # one; empty the rest rather than delete them, leaving run structure intact.
            texts[0].text = result.text
            _preserve_space(texts[0])
            for node in texts[1:]:
                node.text = None


def _strip_images(root) -> None:
    """Remove every picture/object element from a story root."""
    for tag in _IMAGE_ELEMENTS:
        for element in root.findall(f".//{tag}"):
            element.getparent().remove(element)


_REMOVED_RELATIONSHIPS = frozenset({
    RT.CUSTOM_XML, RT.CUSTOM_XML_PROPS, RT.OLE_OBJECT, RT.PACKAGE, RT.A_F_CHUNK,
    RT.CONTROL,
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/subDocument",
    "http://schemas.microsoft.com/office/2006/relationships/vbaProject",
    "http://schemas.microsoft.com/office/2006/relationships/activeXControlBinary",
})
_REMOVED_CONTENT_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.customXmlProperties+xml",
    "application/vnd.openxmlformats-officedocument.oleObject",
    "application/vnd.ms-office.vbaProject",
    "application/vnd.ms-office.activeX+xml",
    "application/vnd.ms-office.activeX",
})
_REMOVED_PART_PREFIXES = ("customxml/", "word/embeddings/", "word/activex/")


def _removed_part(name: str, content_type: str) -> bool:
    return (
        unquote(name).lstrip("/").lower().startswith(_REMOVED_PART_PREFIXES)
        or content_type in _REMOVED_CONTENT_TYPES
    )


def _strip_unsupported_payloads(package) -> None:
    # Remove all incoming edges, including unexpected owners and unreferenced
    # relationships. Removing only a visible object leaves its binary payload.
    owners = [package, *list(package.iter_parts())]
    removed_targets = {
        relationship.target_part
        for owner in owners for relationship in owner.rels.values()
        if not relationship.is_external and (
            relationship.reltype in _REMOVED_RELATIONSHIPS
            or _removed_part(
                str(relationship.target_part.partname), relationship.target_part.content_type
            )
        )
    }
    for owner in owners:
        for rid, relationship in list(owner.rels.items()):
            if (
                relationship.reltype in _REMOVED_RELATIONSHIPS
                or relationship.is_external
                or relationship.target_part in removed_targets
            ):
                del owner.rels[rid]


def _unwrap_hyperlinks(root) -> None:
    # A destination is unnecessary for review; keep its visible runs in place.
    for link in list(root.iter(qn("w:hyperlink"))):
        parent = link.getparent()
        index = parent.index(link)
        for child in list(link):
            parent.insert(index, child)
            index += 1
        parent.remove(link)


def _strip_history_and_fields(root) -> None:
    for node in list(root.iter()):
        if node.tag in _REMOVED_STORY_TAGS:
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
        else:
            for attribute in _PRIVATE_STORY_ATTRIBUTES:
                node.attrib.pop(attribute, None)
    # Keep the cached visible result, but remove the command and its attributes.
    for field in list(root.iter(qn("w:fldSimple"))):
        parent = field.getparent()
        index = parent.index(field)
        for child in list(field):
            parent.insert(index, child)
            index += 1
        parent.remove(field)


@bounded
def sanitise_docx_file(
    source: Path,
    destination: Path,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
) -> SanitiseResult:
    """Sanitise DOCX stories into the closed review dialect, omitting auxiliary parts."""
    document = Document(BytesIO(check_zip(source)))
    replacements: list[Replacement] = []
    _strip_unsupported_payloads(document.part.package)

    parts = list(_story_parts(document))
    parts.remove(document.part)
    parts.insert(0, document.part)
    indices = {part: index for index, part in enumerate(parts)}
    roots = []
    for story_index, part in enumerate(parts, start=1):
        root = _root_of(part)
        _strip_history_and_fields(root)
        _strip_images(root)
        _unwrap_hyperlinks(root)
        references = {
            rid: (f"rId{indices[rel.target_part]}",
                  etree.QName(_root_of(rel.target_part)).localname)
            for rid, rel in part.rels.items()
            if not rel.is_external and rel.target_part in indices and indices[rel.target_part]
        }
        root = project(root, references)
        roots.append(root)
        previous = ""
        for paragraph_index, paragraph in enumerate(root.findall(f".//{_W_P}"), start=1):
            original = _visible_text(paragraph)
            _sanitise_paragraph(
                paragraph,
                pseudonymiser,
                detector_engine,
                replacements,
                f"docx/story/{story_index}/paragraph/{paragraph_index}",
                previous,
            )
            if original.strip():
                previous = original
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_package(destination, roots)
    # A .docx is a binary zip, so there is no text form to return; the leak check reads it
    # back through extract_docx_text instead. Replacements still carry the audit trail.
    return SanitiseResult(text="", replacements=replacements)


def extract_docx_text(path: Path) -> str:
    """Extract a `.docx`'s reader-visible text for the leak-check rescan.

    Covers the same story parts and the same paragraph grouping the handler sanitises, so
    a value the handler missed - including one split across runs - is caught here, and the
    dummies it wrote are recognised rather than reported. Paragraphs are joined by newlines
    so a detector cannot match across a paragraph boundary that a reader never sees.
    """
    return "\n".join(unit.text for unit in extract_docx_units(path))


@bounded
def extract_docx_units(path: Path) -> list[ExtractedText]:
    """Extract DOCX paragraphs and fields with stable story/paragraph locations."""
    roots = verify_package(path)
    units: list[ExtractedText] = []
    for story_index, root in enumerate(roots, start=1):
        for paragraph_index, paragraph in enumerate(root.findall(f".//{_W_P}"), start=1):
            locator = f"docx/story/{story_index}/paragraph/{paragraph_index}"
            texts = paragraph.findall(f".//{_W_T}")
            if texts:
                units.append(ExtractedText("".join(node.text or "" for node in texts), locator))
    return units or [ExtractedText("", "docx/document")]
