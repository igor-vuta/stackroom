"""Hostile and malformed documents, generated rather than checked in.

Two layers, because they catch different things.

**Hazards** are hand-written structural shapes: a page tree whose ``/Count``
lies, a ``/CropBox`` that disagrees with the ``/MediaBox``, a page 200 inches
across, an incremental update that keeps the previous revision, a filename that
is not valid UTF-8. Each one is a thing a real producer, or a real attacker,
actually emits, and none of them would be reached by flipping bits.

**Mutations** are the bit-flipping layer, applied to a small seed corpus of
well-formed files. They catch the parser's edges: a ``/Length`` that overruns,
a truncated stream, an xref pointing into the middle of an object.

Every generator takes a seeded ``random.Random`` and returns
``(filename, bytes)``, so a failure is reproducible from its name and seed
alone.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from pathlib import Path

import rawpdf

Generator = Callable[[random.Random], tuple[str, bytes]]

HAZARDS: dict[str, Generator] = {}

SLOW: set[str] = set()
"""Hazards whose *cost* is the point: a big page, a lot of pages, a lot of
shapes. They get a longer budget and are left out of the CI smoke run, because
"a 400-page document takes four minutes" is throughput, not a hang, and a
harness that cries hang about throughput is a harness nobody reruns."""


def hazard(name: str, *, slow: bool = False) -> Callable[[Generator], Generator]:
    def register(fn: Generator) -> Generator:
        HAZARDS[name] = fn
        if slow:
            SLOW.add(name)
        return fn

    return register


LEAK = (
    b"BT /F1 14 Tf 72 700 Td (SOURCE NAME ALPHA) Tj ET\n"
    b"0 0 0 rg 68 694 190 24 re f\n"
)
BODY = LEAK + rawpdf.prose_lines(3)


# --------------------------------------------------------------------------
# structural hazards
# --------------------------------------------------------------------------


@hazard("ordinary")
def _ordinary(rnd: random.Random) -> tuple[str, bytes]:
    """The control. If this one fails, nothing else in the run means anything."""
    return "ordinary.pdf", rawpdf.page_pdf(BODY)


@hazard("count-lies")
def _count_lies(rnd: random.Random) -> tuple[str, bytes]:
    """``/Count 1`` over three ``/Kids``: poppler and pdfminer disagree about
    how many pages exist, so pages 2 and 3 are queued and cannot be rendered."""
    return "count-lies.pdf", rawpdf.multi_page_pdf(
        [rawpdf.prose_lines(3), BODY, BODY], declared_count=1
    )


@hazard("count-inflated")
def _count_inflated(rnd: random.Random) -> tuple[str, bytes]:
    """The other direction: a ``/Count`` far larger than the number of kids."""
    return "count-inflated.pdf", rawpdf.page_pdf(BODY, declared_count=5000)


@hazard("cropbox-mismatch")
def _cropbox(rnd: random.Random) -> tuple[str, bytes]:
    """``/CropBox`` a quarter of the ``/MediaBox``: pdfinfo measures one and
    pdftoppm draws the other, so every crop lands somewhere else on the page."""
    return "cropbox-mismatch.pdf", rawpdf.page_pdf(
        BODY, mediabox="0 0 1224 1584", cropbox="0 0 612 792"
    )


@hazard("cropbox-inverted")
def _cropbox_inverted(rnd: random.Random) -> tuple[str, bytes]:
    """A ``/CropBox`` written back to front, and larger than the media box."""
    return "cropbox-inverted.pdf", rawpdf.page_pdf(
        BODY, mediabox="0 0 612 792", cropbox="900 900 -100 -100"
    )


@hazard("poster-page", slow=True)
def _poster(rnd: random.Random) -> tuple[str, bytes]:
    """200 inches square: 900 megapixels at the default 150 dpi."""
    return "poster.pdf", rawpdf.page_pdf(BODY, mediabox="0 0 14400 14400")


@hazard("absurd-mediabox", slow=True)
def _absurd(rnd: random.Random) -> tuple[str, bytes]:
    """A page far larger than the PDF specification allows anyone to ask for."""
    return "absurd.pdf", rawpdf.page_pdf(BODY, mediabox="0 0 2000000 2000000")


@hazard("zero-mediabox")
def _zero_box(rnd: random.Random) -> tuple[str, bytes]:
    return "zero-mediabox.pdf", rawpdf.page_pdf(BODY, mediabox="0 0 0 0")


@hazard("negative-mediabox")
def _negative_box(rnd: random.Random) -> tuple[str, bytes]:
    return "negative-mediabox.pdf", rawpdf.page_pdf(BODY, mediabox="500 500 -500 -500")


@hazard("many-rects", slow=True)
def _many_rects(rnd: random.Random) -> tuple[str, bytes]:
    """Twenty thousand filled rectangles: every one is a redaction candidate,
    and every candidate is rendered, measured and written into the page HTML."""
    ops = [b"BT /F1 8 Tf 72 700 Td (the of and to a in that is) Tj ET\n"]
    for i in range(20_000):
        x = 10 + (i % 50) * 11.0
        y = 10 + ((i // 50) % 60) * 12.0
        ops.append(f"0 0 0 rg {x:.1f} {y:.1f} 10 10 re f\n".encode())
    return "many-rects.pdf", rawpdf.page_pdf(b"".join(ops))


@hazard("many-pages", slow=True)
def _many_pages(rnd: random.Random) -> tuple[str, bytes]:
    """Four hundred pages from a twenty-kilobyte file: the ratio is the point,
    and 400 is as far as a test suite people actually run can go."""
    return "many-pages.pdf", rawpdf.page_pdf(rawpdf.prose_lines(1), pages=400)


@hazard("deep-nesting", slow=True)
def _deep(rnd: random.Random) -> tuple[str, bytes]:
    """Eight thousand unbalanced ``q`` operators."""
    return "deep-nesting.pdf", rawpdf.page_pdf(b"q " * 8000 + BODY)


@hazard("recursive-xobject")
def _recursive(rnd: random.Random) -> tuple[str, bytes]:
    """A form XObject that draws itself."""
    form = rawpdf.stream_obj(
        b"q /Im0 Do Q\n",
        "/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
        "/Resources << /XObject << /Im0 6 0 R >> >> ",
    )
    return "recursive-xobject.pdf", rawpdf.page_pdf(
        BODY + b"q /Im0 Do Q\n",
        extra_objects=(form,),
        xobjects="/XObject << /Im0 6 0 R >> ",
    )


@hazard("image-over-text")
def _image_over_text(rnd: random.Random) -> tuple[str, bytes]:
    """A redaction applied as an image: no rectangle for the check to anchor on."""
    return "image-over-text.pdf", rawpdf.page_pdf(
        b"BT /F1 14 Tf 72 700 Td (SOURCE NAME ALPHA) Tj ET\n"
        b"q 200 0 0 24 68 694 cm /Im0 Do Q\n" + rawpdf.prose_lines(2),
        extra_objects=(rawpdf.noise_image(60, 10, seed=2),),
        xobjects="/XObject << /Im0 6 0 R >> ",
    )


@hazard("invisible-text")
def _invisible(rnd: random.Random) -> tuple[str, bytes]:
    """Render mode 3: in the file, in the search index, on nobody's screen."""
    return "invisible-text.pdf", rawpdf.page_pdf(
        b"BT 3 Tr /F1 14 Tf 72 700 Td (SOURCE NAME ALPHA) Tj ET\n"
        b"BT /F1 14 Tf 72 700 Td ([REDACTED]) Tj ET\n" + rawpdf.prose_lines(2)
    )


@hazard("incremental-update")
def _incremental(rnd: random.Random) -> tuple[str, bytes]:
    """A corrected release whose first revision is still inside the file."""
    first = rawpdf.page_pdf(
        b"BT /F1 14 Tf 72 700 Td (WITHHELD NAME: Jonathan Smith) Tj ET\n"
        + rawpdf.prose_lines(2)
    )
    return "incremental.pdf", rawpdf.incremental_update(
        first,
        obj_number=5,
        body=rawpdf.stream_obj(
            b"BT /F1 14 Tf 72 700 Td (WITHHELD NAME: [REDACTED]) Tj ET\n"
            + rawpdf.prose_lines(2)
        ),
    )


@hazard("no-pages")
def _no_pages(rnd: random.Random) -> tuple[str, bytes]:
    return "no-pages.pdf", rawpdf.build_pdf(
        [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"]
    )


@hazard("no-root")
def _no_root(rnd: random.Random) -> tuple[str, bytes]:
    return "no-root.pdf", rawpdf.build_pdf([b"<< /Nothing /Here >>"], root=99)


@hazard("bad-xref")
def _bad_xref(rnd: random.Random) -> tuple[str, bytes]:
    """A ``startxref`` pointing at the wrong byte, which is what a truncated
    upload and a badly repaired file both look like."""
    data = rawpdf.page_pdf(BODY)
    return "bad-xref.pdf", data.replace(b"startxref\n", b"startxref\n9999999\n%", 1)


@hazard("truncated")
def _truncated(rnd: random.Random) -> tuple[str, bytes]:
    data = rawpdf.page_pdf(BODY)
    cut = rnd.randrange(len(data) // 4, len(data) - 1)
    return "truncated.pdf", data[:cut]


@hazard("bad-length")
def _bad_length(rnd: random.Random) -> tuple[str, bytes]:
    """A ``/Length`` that claims two gigabytes for a forty-byte stream."""
    data = rawpdf.page_pdf(BODY)
    return "bad-length.pdf", data.replace(b"/Length ", b"/Length 2147483647 % ", 1)


@hazard("html-polyglot")
def _polyglot(rnd: random.Random) -> tuple[str, bytes]:
    """A file that is a valid HTML document and a valid PDF, named ``.html``."""
    return "annual-report.html", (
        b"<!doctype html><html><body><script>1</script></body></html>\n"
        + rawpdf.page_pdf(BODY)
    )


@hazard("undecodable-filename")
def _bad_name(rnd: random.Random) -> tuple[str, bytes]:
    """A name holding bytes that are not UTF-8, as a zip made on Windows does."""
    import os

    return os.fsdecode(b"report-\xff\xfe.pdf"), rawpdf.page_pdf(BODY)


@hazard("device-name")
def _device_name(rnd: random.Random) -> tuple[str, bytes]:
    return "CON.pdf", rawpdf.page_pdf(BODY)


@hazard("dot-name")
def _dot_name(rnd: random.Random) -> tuple[str, bytes]:
    return "   ...   .pdf", rawpdf.page_pdf(BODY)


@hazard("long-name")
def _long_name(rnd: random.Random) -> tuple[str, bytes]:
    return ("l" * 200) + ".pdf", rawpdf.page_pdf(BODY)


@hazard("markup-metadata")
def _markup_metadata(rnd: random.Random) -> tuple[str, bytes]:
    """Every metadata field the site renders, filled with markup."""
    payload = r"</title></script><img src=x onerror=alert\(1\)>"
    return "markup-metadata.pdf", rawpdf.page_pdf(
        BODY,
        info={"Title": payload, "Author": payload, "Subject": payload,
              "Producer": payload, "Creator": payload},
    )


@hazard("surrogate-tounicode")
def _surrogate(rnd: random.Random) -> tuple[str, bytes]:
    """A ``ToUnicode`` map that decodes glyphs to unpaired surrogates, which
    ``json.dumps`` will accept and ``str.encode('utf-8')`` will not."""
    cmap = rawpdf.stream_obj(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"/CMapName /Custom def /CMapType 2 def\n"
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        b"2 beginbfchar <41> <D800> <42> <DC00> endbfchar\n"
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>"
    body = rawpdf.build_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            font,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
            rawpdf.stream_obj(
                b"BT /F1 20 Tf 72 700 Td (ABAB) Tj ET\n" + rawpdf.prose_lines(2)
            ),
            cmap,
        ]
    )
    return "surrogate.pdf", body


@hazard("encrypted")
def _encrypted(rnd: random.Random) -> tuple[str, bytes]:
    """An ``/Encrypt`` dictionary with nothing behind it."""
    data = rawpdf.page_pdf(BODY)
    return "encrypted.pdf", data.replace(
        b"/Root 1 0 R ", b"/Root 1 0 R /Encrypt << /Filter /Standard /V 1 /R 2 "
        b"/O (0000000000000000) /U (0000000000000000) /P -1 >> ", 1
    )


@hazard("not-a-pdf")
def _not_a_pdf(rnd: random.Random) -> tuple[str, bytes]:
    return "claims.pdf", b"%PDF-1.7\n" + bytes(rnd.randrange(256) for _ in range(2048))


@hazard("empty")
def _empty(rnd: random.Random) -> tuple[str, bytes]:
    return "empty.pdf", b""


@hazard("tiny-png")
def _tiny_png(rnd: random.Random) -> tuple[str, bytes]:
    """A page image rather than a PDF: a different branch of the pipeline."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("L", (64, 64), 255).save(buffer, format="PNG")
    return "page.png", buffer.getvalue()


@hazard("truncated-png")
def _truncated_png(rnd: random.Random) -> tuple[str, bytes]:
    data = _tiny_png(rnd)[1]
    return "broken.png", data[: len(data) // 2]


# --------------------------------------------------------------------------
# byte mutation
# --------------------------------------------------------------------------

SEEDS: tuple[Callable[[], bytes], ...] = (
    lambda: rawpdf.page_pdf(BODY),
    lambda: rawpdf.page_pdf(BODY, pages=3),
    lambda: rawpdf.page_pdf(BODY, info={"Title": "A memo", "Author": "An agency"}),
    lambda: rawpdf.page_pdf(BODY, mediabox="0 0 842 1191", cropbox="20 20 800 1150"),
)

_TOKENS = (
    b"/Count 2147483647", b"/Length -1", b"/MediaBox [0 0 1e400 1e400]",
    b"/Filter /JBIG2Decode", b"\x00", b"endobj", b"trailer", b"%%EOF",
    b"/Prev 0", b"/Root 0 0 R", b"stream", b"9" * 64,
)


def mutate(data: bytes, rnd: random.Random) -> bytes:
    """One of a handful of dumb, reproducible corruptions."""
    if not data:
        return data
    out = bytearray(data)
    for _ in range(rnd.randrange(1, 6)):
        choice = rnd.randrange(6)
        at = rnd.randrange(len(out))
        if choice == 0:  # flip a bit
            out[at] ^= 1 << rnd.randrange(8)
        elif choice == 1:  # truncate
            del out[at:]
        elif choice == 2:  # splice a hostile token in
            token = rnd.choice(_TOKENS)
            out[at:at] = token
        elif choice == 3:  # duplicate a chunk
            size = min(len(out) - at, rnd.randrange(1, 512))
            out[at:at] = out[at : at + size]
        elif choice == 4:  # replace a run of digits with something enormous
            out[at : at + 4] = b"9" * rnd.randrange(1, 40)
        else:  # zero a run
            out[at : at + rnd.randrange(1, 64)] = b"\x00" * rnd.randrange(1, 64)
        if not out:
            break
    return bytes(out)


# --------------------------------------------------------------------------
# the corpus
# --------------------------------------------------------------------------


def hazard_cases(seed: int = 0) -> Iterator[tuple[str, str, bytes]]:
    """``(case name, filename, bytes)`` for every registered hazard."""
    for name, generator in HAZARDS.items():
        filename, data = generator(random.Random(seed ^ hash(name) & 0xFFFFFFFF))
        yield name, filename, data


def mutation_cases(count: int, seed: int) -> Iterator[tuple[str, str, bytes]]:
    """*count* mutated seed documents, reproducible from *seed*."""
    rnd = random.Random(seed)
    for i in range(count):
        base = rnd.choice(SEEDS)()
        yield f"mutation-{seed}-{i}", f"mutant-{i}.pdf", mutate(base, rnd)


def hostile_config() -> str:
    """A ``stackroom.toml`` that pushes every field it is allowed to push."""
    return (
        'title = "</title><script>alert(1)</script>"\n'
        'description = "\\" onload=alert(1) x=\\""\n'
        'language = "en"\n'
        'jurisdiction = "us"\n'
        'base_url = "https://example.org/../../"\n'
        "[ocr]\n"
        'mode = "never"\n'
        "[render]\n"
        "dpi = 72\n"
        "widths = [400]\n"
        'formats = ["webp"]\n'
        "[safety]\n"
        'hidden_text = "warn"\n'
        "[search]\n"
        "enabled = false\n"
    )


def write_case(folder: Path, filename: str, data: bytes, *, config: bool = True) -> Path:
    """Lay one case out as a document folder, the way an operator receives it."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(data)
    if config:
        (folder / "stackroom.toml").write_text(hostile_config(), encoding="utf-8")
        (folder / "about.md").write_text(
            "# About\n\n<!-- a comment -->\n\n**Released** under [FOIA](https://x.example/).\n",
            encoding="utf-8",
        )
    return path
