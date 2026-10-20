# EML (MIME) policy

How `panda.handlers.eml` reads a `.eml` file, what it writes, and what the leak check
accepts back. The output is a review copy of the message, not a faithful re-send. Its
structure is regenerated so that no source syntax, identifiers or parameters reach it
unscanned.

## Input: refused before anything is discarded

The message is parsed with the stdlib `email` package under job limits: source bytes,
MIME depth, part count, attachment count and expanded bytes. A message is refused
(`E_MALFORMED`, or `E_CAPACITY` for limits) when:

- the parser reports any defect on a part or a header
- a structural header (`Content-Type`, `Content-Transfer-Encoding`,
  `Content-Disposition`, `MIME-Version`, `Content-ID`) appears more than once on a part
- the transfer encoding is not `7bit`, `8bit`, `binary`, `base64` or `quoted-printable`
- a multipart container is itself base64 or quoted-printable encoded
- base64 is not strictly valid, quoted-printable has a bare `=`, or `7bit` holds
  bytes above 127
- a payload cannot be decoded, or a text part does not decode strictly in its
  declared charset

## What is kept

| Source part | Output |
|---|---|
| Headers (`From`, `Subject`, ...) | Decoded value, sanitised, under an inert ordinal name `X-Panda-Header-N`. Original header names are dropped. |
| `multipart/*` | `multipart/mixed` with a fresh `panda-<32 hex>` boundary. Dropped if no child survives. |
| `message/rfc822` | Sanitised end to end as a whole message: headers, body and attachments. Any other `message/*` is dropped. |
| `text/plain`, `text/*` | `text/plain`, sanitised, UTF-8, `8bit`. |
| `text/html` | Rebuilt in the closed HTML dialect (see [HTML_POLICY.md](HTML_POLICY.md)), UTF-8, `8bit`. |
| `.html` / `.htm` / `.docx` attachment | Sanitised by its own handler and re-embedded as `application/octet-stream`. |
| `.pdf` attachment | Content dropped; replaced with a text placeholder that names the attachment. A PDF's handler emits a `.txt` transcript, so it cannot go back in as a PDF. |
| Images and any other binary | Dropped entirely, name and all. A message that was only one such binary keeps its headers and gets an empty body. |

`8bit` text keeps the sanitised words literally in the file, so the rescan reads them
directly instead of through base64.

Attachment filenames are scanned for detections (and reported), but are never emitted.
A retained attachment is named `attachment<ext>` (for example `attachment.docx`), or
`attachment` when it has no extension. Preambles, epilogues, `Content-ID`s and every
source parameter except the regenerated boundary and `charset=utf-8` are discarded.

## Output validation

Before the file is written, the output is parsed again under the same limits and input
rules, then checked against this closed shape. Anything else refuses the file:

- headers are only the structural ones above, plus `X-Panda-Header-N`
- content types are only `multipart/mixed`, `message/rfc822`, `text/plain`,
  `text/html` and `application/octet-stream`
- parameters are exactly `boundary` (matching `panda-[0-9a-f]{32}`) on multipart and
  exactly `charset=utf-8` on text; there are none elsewhere
- `MIME-Version` is `1.0`; no preamble, epilogue or `Content-ID`
- a disposition is `attachment` with only a `filename` parameter, and the filename is
  one of `attachment`, `attachment.html`, `attachment.htm`, `attachment.docx`,
  `attachment.pdf` or `attachment.txt`
- a `message/rfc822` part wraps exactly one message
- every retained binary is HTML or DOCX, so it has a safe extractor; any other binary
  fails verification rather than being scanned as encoded bytes

The leak check runs this same validation and extraction (`extract_eml_units`). It
scans every header value, filename, text body, HTML paragraph and the paragraphs of
each embedded DOCX, each with its own locator, for example `eml/part/2/header/1`.
