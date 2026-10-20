"""Private, time-isolated PDF extraction worker. Never emit parser diagnostics."""

import contextlib
import json
import resource
import sys
import zlib
from pathlib import Path

from panda.errors import EncryptedError, UnsupportedError
from panda.limits import CapacityError, Limits, capacity_scope, check, read_bytes


def extract(path: Path, limits: Limits) -> dict:
    from io import BytesIO

    from pypdf import PdfReader, filters

    expanded = 0
    failures = []
    original_decode = filters.decode_stream_data

    def decompress(data):
        nonlocal expanded
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(data, limits.expanded_bytes - expanded + 1)
        check(expanded + len(decoded), limits.expanded_bytes)
        expanded += len(decoded)
        if not decoder.eof or decoder.unused_data:
            raise ValueError("unverifiable PDF stream")
        return decoded

    def decode_impl(stream):
        nonlocal expanded
        names = stream.get("/Filter", [])
        if isinstance(names, str):
            names = [names]
        # Other filters can expand without a bounded decoder in the pinned parser.
        if any(name not in {"/FlateDecode", "/Fl", "/ASCIIHexDecode", "/AHx",
                            "/ASCII85Decode", "/A85"} for name in names):
            raise UnsupportedError("unsupported PDF stream filter")
        check(len(names), 4)
        data = original_decode(stream)
        expanded += len(data)
        check(expanded, limits.expanded_bytes)
        return data

    def decode(stream):
        try:
            return decode_impl(stream)
        except Exception as error:
            # pypdf catches errors in form XObjects; never accept its partial text.
            failures.append(error)
            raise

    original_decompress = filters.decompress
    filters.decompress = decompress
    filters.decode_stream_data = decode
    try:
        with capacity_scope(limits):
            reader = PdfReader(BytesIO(read_bytes(path)), strict=True)
            if reader.is_encrypted:
                raise EncryptedError()
            pages = []
            total = 0
            for index, page in enumerate(reader.pages, 1):
                check(index, limits.pdf_pages)
                text = page.extract_text() or ""
                if failures:
                    raise failures[0]
                check(len(text), limits.text_chars)
                total += len(text)
                check(total, limits.total_text_chars)
                pages.append(text)
            return {"pages": pages, "expanded_bytes": expanded}
    finally:
        filters.decompress = original_decompress
        filters.decode_stream_data = original_decode


def main() -> int:
    try:
        # A hard address-space ceiling backs up the byte/expansion policy and the
        # worker timeout.
        limits = Limits(**json.loads(sys.argv[2]))
        maximum = limits.pdf_memory_bytes
        # macOS rejects RLIMIT_AS; the expansion limits and timeout still apply.
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))
        pages = extract(Path(sys.argv[1]), limits)
        sys.stdout.write(json.dumps(pages, ensure_ascii=True))
        return 0
    except (CapacityError, MemoryError):
        return 3
    except EncryptedError:
        return 4
    except UnsupportedError:
        return 5
    except PermissionError:
        return 6
    except OSError:
        return 7
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
