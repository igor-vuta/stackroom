"""A PDF writer made of bytes, for the documents a generator will not make.

``tests/synth.py`` builds *plausible* documents - a memo, a scan, a production
with a page missing - and it is the right tool for almost every test in this
suite. The security tests need the opposite: files that are deliberately
malformed, that lie about themselves, or that exercise a PDF feature no
well-behaved producer emits. reportlab will not write those, and pikepdf is not
a declared dependency of this project, so the security tests assemble the bytes
themselves.

Everything here is a complete, uncompressed, classically cross-referenced PDF.
That is on purpose: an uncompressed content stream is readable in a hex dump, so
a test that fails five years from now can be understood without running it.
"""

from __future__ import annotations

import random

__all__ = [
    "HELVETICA",
    "PROSE",
    "build_pdf",
    "incremental_update",
    "multi_page_pdf",
    "noise_image",
    "page_pdf",
    "prose_lines",
    "stream_obj",
]

HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

# Enough function words that ingest/quality.py judges the page readable prose
# rather than garbage. Without it a one-line test document is scored UNREADABLE
# and half the pipeline takes a different branch.
PROSE = (
    "the of and to a in that is was he for it with as his on be at by i this "
    "had not are but from or have an they which one you were her all she there"
)


def stream_obj(data: bytes, extra: str = "") -> bytes:
    """One stream object body, with a correct ``/Length``."""
    return f"<< /Length {len(data)} {extra}>>\nstream\n".encode() + data + b"\nendstream"


def build_pdf(objects: list[bytes], root: int = 1, extra_trailer: str = "") -> bytes:
    """Assemble numbered object bodies into a complete PDF.

    ``objects[i]`` becomes object ``i + 1``, so cross-references written inside
    the bodies are one-based and stable.
    """
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {root} 0 R {extra_trailer}>>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def page_pdf(
    content: bytes,
    *,
    pages: int = 1,
    mediabox: str = "0 0 612 792",
    cropbox: str | None = None,
    declared_count: int | None = None,
    info: dict[str, str] | None = None,
    extra_objects: tuple[bytes, ...] = (),
    xobjects: str = "",
    page_extra: str = "",
) -> bytes:
    """A PDF of *pages* identical pages sharing one content stream.

    ``declared_count`` writes a ``/Count`` that disagrees with the number of
    ``/Kids``. Poppler believes ``/Count``; pdfminer walks the kids. That
    disagreement is a security property, not a curiosity - see
    ``test_security.py``.

    ``extra_objects`` are appended after the content stream and are therefore
    numbered from ``4 + pages + 1``; ``xobjects`` is spliced into the page's
    ``/Resources`` so a caller can point at them.
    """
    kids = " ".join(f"{4 + i} 0 R" for i in range(pages))
    count = pages if declared_count is None else declared_count
    content_obj = 4 + pages
    crop = f" /CropBox [{cropbox}]" if cropbox else ""
    resources = f"<< /Font << /F1 3 0 R >> {xobjects}>>"

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        HELVETICA,
    ]
    for _ in range(pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [{mediabox}]{crop} "
            f"/Resources {resources} /Contents {content_obj} 0 R {page_extra}>>".encode()
        )
    objects.append(stream_obj(content))
    objects.extend(extra_objects)

    trailer = ""
    if info:
        objects.append(
            ("<< " + " ".join(f"/{k} ({v})" for k, v in info.items()) + " >>").encode()
        )
        trailer = f"/Info {len(objects)} 0 R "
    return build_pdf(objects, root=1, extra_trailer=trailer)


def multi_page_pdf(
    contents: list[bytes],
    *,
    declared_count: int | None = None,
    mediabox: str = "0 0 612 792",
) -> bytes:
    """A PDF where every page has its own content stream.

    Needed wherever the interesting page is *not* the first one - a document
    that is clean on page one and leaking on pages two and three is the shape
    that gets past a check keyed on "did anything at all come back".
    """
    pages = len(contents)
    kids = " ".join(f"{4 + i} 0 R" for i in range(pages))
    count = pages if declared_count is None else declared_count
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        HELVETICA,
    ]
    for i in range(pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [{mediabox}] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {4 + pages + i} 0 R >>".encode()
        )
    for content in contents:
        objects.append(stream_obj(content))
    return build_pdf(objects, root=1)


def noise_image(width: int = 400, height: int = 120, seed: int = 11) -> bytes:
    """An uncompressed grey-noise image object: never flat, never dark.

    Used to put something under a mis-mapped crop that is neither paper nor a
    black box, so that a check looking at the wrong pixels reaches the wrong
    answer rather than accidentally the right one.
    """
    rnd = random.Random(seed)
    raw = bytes(rnd.randrange(70, 200) for _ in range(width * height))
    return stream_obj(
        raw,
        f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
        "/ColorSpace /DeviceGray /BitsPerComponent 8 ",
    )


def prose_lines(count: int = 2, x: int = 72, top: int = 640, size: int = 11) -> bytes:
    """A few lines of ordinary English, so quality.py calls the page readable."""
    return b"".join(
        f"BT /F1 {size} Tf {x} {top - i * 20} Td ({PROSE[:70]}) Tj ET\n".encode()
        for i in range(count)
    )


def incremental_update(base: bytes, obj_number: int, body: bytes) -> bytes:
    """Append a genuine incremental update, keeping the first revision intact.

    A PDF saved this way carries every earlier version of every object it
    replaced. That is how a "corrected" release ships with the uncorrected text
    still inside it, and it is why what a viewer shows is not what the file
    contains.
    """
    import re

    prev = int(re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", base, re.S).group(1))
    root = re.search(rb"/Root (\d+ \d+ R)", base).group(1).decode()
    size = int(re.search(rb"/Size (\d+)", base).group(1))

    out = bytearray(base)
    out += b"\n"
    obj_at = len(out)
    out += f"{obj_number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n{obj_number} 1\n{obj_at:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root {root} /Prev {prev} >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)
