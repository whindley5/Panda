"""Measure synthetic capacity checks, without loading models or client material."""

import json
import platform
import resource
import sys
import tempfile
import time
import tracemalloc
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from panda.handlers.eml import _parse_bounded
from panda.handlers.html import extract_html_markup_units
from panda.handlers.pdf import _page_texts
from panda.limits import CapacityError, Limits, capacity_scope, check_zip


def measure(label, operation, *, refused):
    tracemalloc.start()
    started = time.perf_counter()
    did_refuse = False
    try:
        operation()
    except CapacityError:
        did_refuse = True
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if did_refuse != refused:
        raise RuntimeError("unexpected capacity result")
    print(json.dumps({"case": label, "refused": did_refuse,
                      "seconds": round(elapsed, 6), "python_peak_bytes": peak}))


def main():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    print(json.dumps({"os": platform.system(), "release": platform.release(),
                      "architecture": platform.machine(), "python": platform.python_version()}))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive = root / "synthetic.docx"
        with ZipFile(archive, "w", ZIP_DEFLATED) as output:
            output.writestr("synthetic", b"x" * (4 * 1024 * 1024))
        with capacity_scope(replace(Limits(), zip_ratio=2)):
            measure("zip_4MiB_expansion", lambda: check_zip(archive), refused=True)
        message = EmailMessage()
        message.set_content("safe")
        for _ in range(50):
            parent = EmailMessage()
            parent.make_mixed()
            parent.attach(message)
            message = parent
        data = message.as_bytes()
        with capacity_scope(replace(Limits(), mime_depth=8)):
            measure("mime_50_levels", lambda: _parse_bounded(data), refused=True)
        markup = "<html><body>" + "<p>safe review text</p>" * 10000 + "</body></html>"
        with capacity_scope():
            measure("html_10000_paragraphs", lambda: extract_html_markup_units(markup),
                    refused=False)
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        stream = DecodedStreamObject()
        stream.set_data(b" " * (4 * 1024 * 1024))
        page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
        pdf = root / "synthetic.pdf"
        writer.write(pdf)
        with capacity_scope(replace(Limits(), expanded_bytes=1024)):
            measure("pdf_4MiB_expansion", lambda: _page_texts(pdf), refused=True)
    print(json.dumps({"process_peak_rss_native_units":
                      resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      "rss_units": "bytes" if sys.platform == "darwin" else "KiB"}))


if __name__ == "__main__":
    main()
