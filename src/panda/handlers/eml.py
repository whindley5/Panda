"""MIME review output: regenerated structure and recursively verified content.

See docs/MIME_POLICY.md for retained fields and strict input/output validation.
"""

from __future__ import annotations

import base64
import re
import tempfile
import uuid
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesFeedParser
from pathlib import Path

from panda.core import sanitise_text
from panda.detectors import DetectorEngine
from panda.formats import ExtractedText, handler_for
from panda.handlers.docx import extract_docx_units
from panda.handlers.html import extract_html_markup_units, sanitise_html
from panda.limits import bounded, budget, check, read_bytes
from panda.model import Replacement, SanitiseResult
from panda.pseudonyms import SurrogateSource

_STRUCTURAL_HEADERS = frozenset({
    "content-type", "content-transfer-encoding", "content-disposition",
    "mime-version", "content-id",
})


def _parse_bounded(data: bytes) -> EmailMessage:
    active = budget()
    check(len(data), active.limits.source_bytes)
    # Part and attachment counts bound the structure of one message, so they are counted
    # per parse. Charged to the job budget, they capped a batch at about 32 emails,
    # because each email is parsed several times (sanitise, verify, rescan). Cumulative
    # work stays bounded by expanded_bytes and total_text_chars.
    parts = attachments = 0

    class BoundedParser(BytesFeedParser):
        def _new_message(self):
            nonlocal parts
            # Check before the stdlib parser allocates or recurses into a part.
            check(len(self._msgstack), active.limits.mime_depth)
            parts += 1
            check(parts, active.limits.mime_parts)
            super()._new_message()

    parser = BoundedParser(policy=policy.default)
    for offset in range(0, len(data), 65536):
        parser.feed(data[offset:offset + 65536])
    message = parser.close()
    stack = [(message, 0)]
    while stack:
        part, depth = stack.pop()
        check(depth, active.limits.mime_depth)
        if part.get_content_disposition() == "attachment" or part.get_filename():
            attachments += 1
            check(attachments, active.limits.attachments)
        payload = part.get_payload()
        if isinstance(payload, list):
            stack.extend((child, depth + 1) for child in payload)
        else:
            # Decoding cannot expand base64/quoted-printable beyond encoded bytes.
            active.add("expanded_bytes", len(payload or ""))
    return message


def _validate_input(message: EmailMessage) -> None:
    """Force lazy parser/decoder errors before any source content is discarded."""
    for part in message.walk():
        headers = list(part.items())
        if part.defects or any(getattr(value, "defects", ()) for _, value in headers):
            raise ValueError("malformed MIME headers or structure")
        for name in _STRUCTURAL_HEADERS:
            if len(part.get_all(name, [])) > 1:
                raise ValueError("ambiguous MIME structure")
        cte = str(part.get("Content-Transfer-Encoding", "7bit")).lower()
        if cte not in {"7bit", "8bit", "binary", "base64", "quoted-printable"}:
            raise ValueError("unsupported MIME transfer encoding")
        if part.is_multipart():
            if cte not in {"7bit", "8bit", "binary"}:
                raise ValueError("encoded MIME container")
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            raise ValueError("undecodable MIME payload")
        raw = (part.get_payload().encode("ascii", "surrogateescape")
               if cte in {"base64", "quoted-printable"} else payload)
        if cte == "base64":
            try:
                base64.b64decode(re.sub(rb"[ \t\r\n]", b"", raw), validate=True)
            except ValueError:
                raise ValueError("malformed MIME base64") from None
        if cte == "quoted-printable" and re.search(rb"=(?![0-9A-Fa-f]{2}|\r?\n)", raw):
            raise ValueError("malformed MIME quoted printable")
        if cte == "7bit" and any(byte > 127 for byte in raw):
            raise ValueError("invalid MIME 7bit payload")
        if part.defects:
            raise ValueError("malformed MIME payload")
        if part.get_content_maintype() == "text":
            try:
                payload.decode(part.get_content_charset() or "ascii", errors="strict")
            except (LookupError, UnicodeError):
                raise ValueError("unverifiable MIME text encoding") from None


def _canonical_structure(part: EmailMessage) -> None:
    """Discard source parameters/identifiers even on retained containers."""
    content_type = part.get_content_type()
    filename = part.get_filename() if not part.is_multipart() else None
    charset = part.get_content_charset()
    cte = part.get("Content-Transfer-Encoding")
    for name in _STRUCTURAL_HEADERS:
        del part[name]
    part["MIME-Version"] = "1.0"
    part["Content-Type"] = content_type
    if part.get_content_maintype() == "multipart":
        part.set_boundary("panda-" + uuid.uuid4().hex)
    elif part.get_content_maintype() == "text":
        part.set_param("charset", charset or "utf-8")
    if cte:
        part["Content-Transfer-Encoding"] = str(cte)
    if filename:
        part.add_header("Content-Disposition", "attachment", filename=filename)
    part.preamble = None
    part.epilogue = None


# Attachment extensions we sanitise and re-embed as the same attachment. Only formats
# whose handler emits the same container type belong here: `.html` -> html, `.docx` ->
# docx. A `.pdf` handler emits a `.txt` transcript, which is not a PDF, so a PDF is not
# embeddable and is handled by drop-but-name instead (see below).
_EMBEDDABLE = frozenset({".html", ".htm", ".docx"})

# Attachment extensions dropped for content but kept as a named placeholder, so the audit
# trail records the file was present.
_DROP_BUT_NAME = frozenset({".pdf"})


def _sanitise_str(
    value: str,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
    collected: list[Replacement],
    locator: str,
) -> str:
    """Sanitise one string, recording what was replaced. Returns the new string."""
    result = sanitise_text(value, pseudonymiser, detector_engine, locator=locator)
    collected.extend(result.replacements)
    return result.text


def _sanitise_headers(
    message: EmailMessage,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
    collected: list[Replacement],
    locator: str,
) -> None:
    """Retain decoded header values under inert ordinal header names."""
    captured = [(name, str(value)) for name, value in message.items()
                if name.lower() not in _STRUCTURAL_HEADERS]
    for name in dict.fromkeys(name for name, _ in captured):
        del message[name]
    for index, (_, value) in enumerate(captured, 1):
        message[f"X-Panda-Header-{index}"] = _sanitise_str(
            value, pseudonymiser, detector_engine, collected,
            f"{locator}/header/{index}",
        )


def _sanitise_filename(
    part: EmailMessage,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
    collected: list[Replacement],
    locator: str,
) -> str | None:
    """Return a sanitised filename for a part, or None if it has none."""
    filename = part.get_filename()
    if filename is None:
        return None
    _sanitise_str(
        filename, pseudonymiser, detector_engine, collected, f"{locator}/filename"
    )
    # A detector cannot guarantee a filename is PII-free. Retained attachments use
    # a structural name; the original name is neither emitted nor reported.
    suffix = Path(filename).suffix.lower()
    return f"attachment{suffix}" if suffix else "attachment"


def _extend_at(
    collected: list[Replacement], replacements: list[Replacement], locator: str
) -> None:
    """Prefix nested handler locations with their containing MIME part."""
    for replacement in replacements:
        nested = replacement.detection.locator or "content"
        collected.append(
            replace(
                replacement,
                detection=replace(replacement.detection, locator=f"{locator}/{nested}"),
            )
        )


def _replace_with_placeholder(
    part: EmailMessage,
    safe_name: str,
) -> None:
    """Turn a dropped attachment into a text placeholder that keeps its filename.

    Used for a `.pdf`: the content is not carried, but the sanitised filename is, so a
    reviewer can see a file was attached and reference it. Written as 8bit text like the
    other text parts, so the leak-check rescan can read straight through it.
    """
    part.set_content(
        f"[Attachment omitted during sanitising. Original file: {safe_name}]\n",
        subtype="plain",
        cte="8bit",
        disposition="attachment",
        filename=safe_name,
    )


def _sanitise_attachment_via_handler(
    part: EmailMessage,
    filename: str | None,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
    collected: list[Replacement],
    locator: str,
) -> bool:
    """Sanitise an embeddable binary attachment through its registry handler.

    Returns True if the part was sanitised and should be kept, False if it could not be
    (no filename, or a decode failure). Only called for `_EMBEDDABLE` extensions, whose
    handler emits the same container type going back in.
    """
    if not filename:
        return False
    handler = handler_for(Path(filename))
    if handler is None:
        return False

    payload = part.get_payload(decode=True)
    if payload is None:
        return False

    suffix = Path(filename).suffix
    with tempfile.TemporaryDirectory() as workspace:
        source = Path(workspace) / f"attachment{suffix}"
        destination = Path(workspace) / f"sanitised{suffix}"
        source.write_bytes(payload)
        result = handler(source, destination, pseudonymiser, detector_engine)
        cleaned = read_bytes(destination)
    _extend_at(collected, result.replacements, locator)

    maintype, subtype = "application", "octet-stream"
    safe_name = _sanitise_str(
        filename,
        pseudonymiser,
        detector_engine,
        collected,
        f"{locator}/filename",
    )
    part.set_content(
        cleaned,
        maintype=maintype,
        subtype=subtype or "octet-stream",
        disposition="attachment",
        filename=safe_name,
    )
    return True


def _process_part(part, pseudonymiser, detector_engine, collected, locator) -> bool:
    kept = _process_part_content(part, pseudonymiser, detector_engine, collected, locator)
    if kept:
        _sanitise_headers(part, pseudonymiser, detector_engine, collected, locator)
        _canonical_structure(part)
    return kept


def _process_part_content(
    part: EmailMessage,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
    collected: list[Replacement],
    locator: str,
) -> bool:
    """Sanitise one MIME part in place. Returns True to keep it, False to drop it.

    A container (multipart, or a message/rfc822 wrapping a nested email) recurses
    into its children; a leaf is sanitised by type. Only binary attachments without a
    handler are dropped, and a container is dropped once it has no surviving children.
    """
    maintype = part.get_content_maintype()

    if maintype == "multipart":
        part.set_type("multipart/mixed")
        kept = []
        for index, child in enumerate(part.iter_parts(), start=1):
            if _process_part(
                child,
                pseudonymiser,
                detector_engine,
                collected,
                f"{locator}/part/{index}",
            ):
                kept.append(child)
        part.set_payload(kept)
        return bool(kept)

    if maintype == "message":
        if part.get_content_type() != "message/rfc822" or not part.is_multipart():
            return False
        # A message/rfc822 part is a whole email. Sanitise it end to end - headers and
        # body, with the same empty-body fallback - so its own From/Subject or a bare
        # image inside it cannot smuggle PII past us within the wrapper.
        for index, nested in enumerate(part.iter_parts(), start=1):
            _sanitise_message(
                nested,
                pseudonymiser,
                detector_engine,
                collected,
                f"{locator}/message/{index}",
            )
        return True

    disposition = part.get_content_disposition()
    filename = _sanitise_filename(
        part, pseudonymiser, detector_engine, collected, locator
    )

    if maintype == "text":
        text = part.get_content()
        subtype = "html" if part.get_content_subtype() == "html" else "plain"
        if subtype == "html":
            cleaned = sanitise_html(text, pseudonymiser, detector_engine)
            _extend_at(collected, cleaned.replacements, locator)
            body = cleaned.text
        else:
            body = _sanitise_str(
                text,
                pseudonymiser,
                detector_engine,
                collected,
                f"{locator}/text",
            )
        if filename is not None:
            filename = "attachment.html" if subtype == "html" else "attachment.txt"
        # cte=8bit keeps the sanitised text literally in the file, so the leak check
        # can rescan it rather than being blinded by base64.
        if filename is not None:
            part.set_content(
                body,
                subtype=subtype,
                cte="8bit",
                disposition=disposition or "attachment",
                filename=filename,
            )
        else:
            part.set_content(body, subtype=subtype, cte="8bit")
        return True

    # Anything else is a binary attachment, handled by extension policy.
    suffix = Path(filename).suffix.lower() if filename else ""
    if suffix in _EMBEDDABLE:
        return _sanitise_attachment_via_handler(
            part, filename, pseudonymiser, detector_engine, collected, locator
        )
    if filename and suffix in _DROP_BUT_NAME:
        # filename is already sanitised by _sanitise_filename above.
        _replace_with_placeholder(part, filename)
        return True
    # Images and unknown binaries: dropped entirely, name and all.
    return False


def _sanitise_message(
    message: EmailMessage,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
    collected: list[Replacement],
    locator: str,
) -> None:
    """Sanitise a whole message: its body first, then its headers.

    Body before headers on purpose: processing a leaf calls set_content, which
    rewrites that part's Content-Type and transfer encoding, and the header pass must
    preserve whatever those end up as. Used for both the top-level message and any
    nested message/rfc822, so a forwarded email gets identical, complete treatment.
    """
    if not _process_part(message, pseudonymiser, detector_engine, collected, locator):
        # The whole message was a single unhandleable binary (e.g. a bare image).
        # Replace its body with an empty note rather than emit a dropped shell.
        message.set_content("", subtype="plain", cte="8bit")
        _sanitise_headers(message, pseudonymiser, detector_engine, collected, locator)
        _canonical_structure(message)


@bounded
def sanitise_eml_file(
    source: Path,
    destination: Path,
    pseudonymiser: SurrogateSource,
    detector_engine: DetectorEngine,
) -> SanitiseResult:
    """Sanitise one `.eml` file, preserving its structure and MIME layout."""
    message = _parse_bounded(read_bytes(source))
    _validate_input(message)
    replacements: list[Replacement] = []

    _sanitise_message(message, pseudonymiser, detector_engine, replacements, "eml")

    data = message.as_bytes(policy=policy.default)
    verified = _parse_bounded(data)
    _validate_input(verified)
    _extract_message_units(verified, "eml")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    # Everything we emit is ASCII headers, 8bit UTF-8 text, or base64 attachments, so
    # the output decodes cleanly, the same way the leak check will read it back.
    return SanitiseResult(text=data.decode("utf-8"), replacements=replacements)


def _verify_structure(part: EmailMessage) -> None:
    """Independently refuse unscanned syntax, parameters and container text."""
    if part.preamble or part.epilogue or part.get("Content-ID"):
        raise ValueError("unverified MIME metadata")
    for name, _ in part.items():
        if name.lower() not in _STRUCTURAL_HEADERS and not re.fullmatch(
            r"X-Panda-Header-[1-9][0-9]*", name, re.I
        ):
            raise ValueError("unverified MIME header")
    ct = part.get_content_type()
    allowed = {"multipart/mixed", "message/rfc822", "text/plain", "text/html",
               "application/octet-stream"}
    if ct not in allowed:
        raise ValueError("unverified MIME type")
    params = dict(part.get_params()[1:]) if part.get_params() else {}
    expected = {"boundary"} if ct.startswith("multipart/") else (
        {"charset"} if ct.startswith("text/") else set())
    if set(params) != expected:
        raise ValueError("unverified MIME parameters")
    if "boundary" in params and not re.fullmatch(r"panda-[0-9a-f]{32}", params["boundary"]):
        raise ValueError("unverified MIME boundary")
    if "charset" in params and params["charset"] != "utf-8":
        raise ValueError("unverified MIME charset")
    if part.get("MIME-Version") != "1.0":
        raise ValueError("unverified MIME version")
    if part.get("Content-Disposition"):
        disposition = part.get_params(header="content-disposition")
        if (part.get_content_disposition() != "attachment"
                or len(disposition) != 2 or disposition[1][0] != "filename"
                or part.get_filename() not in {
                    "attachment.html", "attachment.htm", "attachment.docx",
                    "attachment.pdf", "attachment.txt", "attachment",
                }):
            raise ValueError("unverified MIME disposition")
    if ct == "message/rfc822" and len(part.get_payload()) != 1:
        raise ValueError("unverified nested message")


def _extract_message_units(message: EmailMessage, locator: str) -> list[ExtractedText]:
    """Recursively expose every retained, reader-relevant MIME component."""
    units: list[ExtractedText] = []
    _verify_structure(message)
    headers = [value for name, value in message.items()
               if name.lower() not in _STRUCTURAL_HEADERS]
    for index, value in enumerate(headers, 1):
        units.append(ExtractedText(str(value), f"{locator}/header/{index}"))

    filename = message.get_filename()
    if filename:
        units.append(ExtractedText(filename, f"{locator}/filename"))

    maintype = message.get_content_maintype()
    if maintype == "multipart":
        for index, child in enumerate(message.iter_parts(), start=1):
            units.extend(_extract_message_units(child, f"{locator}/part/{index}"))
        return units
    if maintype == "message":
        for index, nested in enumerate(message.iter_parts(), start=1):
            units.extend(_extract_message_units(nested, f"{locator}/message/{index}"))
        return units
    if maintype == "text":
        content = message.get_content()
        if message.get_content_subtype() == "html":
            units.extend(
                ExtractedText(unit.text, f"{locator}/{unit.locator}")
                for unit in extract_html_markup_units(content)
            )
        else:
            units.append(ExtractedText(content, f"{locator}/text"))
        return units

    # Sanitised EML may retain only DOCX binary attachments. Treat any other
    # binary as a verification failure instead of scanning encoded bytes.
    suffix = Path(filename).suffix.lower() if filename else ""
    payload = message.get_payload(decode=True)
    if suffix in {".html", ".htm"}:
        if payload is None:
            raise ValueError("HTML attachment has no decodable payload")
        units.extend(
            ExtractedText(unit.text, f"{locator}/{unit.locator}")
            for unit in extract_html_markup_units(payload)
        )
        return units
    if suffix == ".docx":
        if payload is None:
            raise ValueError("DOCX attachment has no decodable payload")
        with tempfile.TemporaryDirectory() as workspace:
            attachment = Path(workspace) / "attachment.docx"
            attachment.write_bytes(payload)
            units.extend(
                ExtractedText(unit.text, f"{locator}/{unit.locator}")
                for unit in extract_docx_units(attachment)
            )
        return units
    raise ValueError("retained MIME component has no registered safe extractor")


@bounded
def extract_eml_units(path: Path) -> list[ExtractedText]:
    """Parse and recursively extract an EML without scanning MIME/base64 syntax."""
    message = _parse_bounded(read_bytes(path))
    _validate_input(message)
    return _extract_message_units(message, "eml") or [ExtractedText("", "eml")]
