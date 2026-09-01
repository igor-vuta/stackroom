"""Synthetic documents, built on demand.

Every test in this project runs against a document we generated ourselves, so
the suite needs no fixtures checked into git, no network, and no third party's
copyrighted scan. The generators here deliberately produce the *ugly* cases:
the failed redaction, the rotated scan, the page that is only a photograph, the
production with a page missing from the middle.

Requires ``reportlab`` (a dev dependency).
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

LOREM = ["The", "Commission", "requested", "all", "correspondence", "between", "the", "office", "of", "the", "director", "and", "the", "contracting", "authority", "for", "the", "period", "beginning", "in", "March", "and", "ending", "on", "the", "last", "day", "of", "September.", "The", "response,", "when", "it", "arrived", "eleven", "months", "later,", "consisted", "of", "four", "hundred", "pages", "of", "which", "a", "substantial", "portion", "had", "been", "withheld."]


# --------------------------------------------------------------------------
# PDFs
# --------------------------------------------------------------------------


@dataclass
class RedactionSpec:
    """Where to put a black box, and what to hide underneath it."""

    x: float
    y: float
    w: float
    h: float
    hidden_text: str | None = None
    """When set, this text is drawn *first* and then covered - a failed
    redaction, recoverable by anyone with the file."""
    code: str | None = None
    """Exemption code printed beside the box, e.g. ``(b)(6)``."""
    draw_text_after: bool = False
    """Draw the text on top of the box instead. Not a failed redaction: the
    text is visible, so this must NOT be flagged."""


def _draw_line_around(c, text: str, x: float, y: float, size: float, boxes) -> None:
    """Draw a line of text, omitting whatever falls inside one of *boxes*.

    Word by word, because that is how a real redaction works: the withheld
    words are gone from the file and the words either side of them are not.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    # Contiguous visible words are drawn as one string. Drawing them one at a
    # time looks identical on paper but puts the glyphs a hair further apart
    # than a real space, and pdfplumber then reads the whole line as a single
    # run-together token. (Stackroom notices and re-OCRs the page, which is the
    # right behaviour and exactly why this fixture must not trigger it.)
    cursor = x
    run: list[str] = []
    run_x = x
    for word in text.split(" "):
        w = stringWidth(word + " ", "Helvetica", size)
        hidden = any(
            cursor + w > bx0 and cursor < bx1 and y + size > by0 and y < by1
            for bx0, by0, bx1, by1 in boxes
        )
        if hidden:
            if run:
                c.drawString(run_x, y, " ".join(run))
                run = []
        else:
            if not run:
                run_x = cursor
            run.append(word)
        cursor += w
    if run:
        c.drawString(run_x, y, " ".join(run))


def born_digital_pdf(
    path: Path,
    pages: int = 3,
    redactions: dict[int, list[RedactionSpec]] | None = None,
    bates_prefix: str | None = None,
    bates_start: int = 1,
    bates_skip: set[int] | None = None,
    title: str = "Synthetic release",
) -> Path:
    """A PDF with a real text layer, optional black boxes and control numbers."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    redactions = redactions or {}
    bates_skip = bates_skip or set()
    width, height = LETTER
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle(title)
    c.setAuthor("Office of Synthetic Records")

    counter = bates_start
    rnd = random.Random(7)
    for pno in range(1, pages + 1):
        c.setFont("Helvetica-Bold", 13)
        c.drawString(72, height - 72, f"MEMORANDUM {pno}")

        specs = redactions.get(pno, [])

        # A *properly* redacted page has no text under the box at all - the
        # words were removed before the box was drawn. Any spec that does not
        # explicitly ask for `hidden_text` produces that, by not drawing the
        # part of the line the box will cover. Otherwise every fixture in this
        # project would contain a failed redaction, and the tests that check we
        # can tell the two apart would have nothing to compare against.
        covered = [
            (s.x, s.y, s.x + s.w, s.y + s.h) for s in specs if not s.hidden_text
        ]

        c.setFont("Helvetica", 10.5)
        y = height - 104
        for _ in range(30):
            line = " ".join(rnd.choice(LOREM) for _ in range(11))
            _draw_line_around(c, line, 72, y, 10.5, covered)
            y -= 15.2
            if y < 110:
                break

        for spec in specs:
            if spec.hidden_text and not spec.draw_text_after:
                c.setFillColorRGB(0, 0, 0)
                c.setFont("Helvetica", 10.5)
                c.drawString(spec.x + 2, spec.y + 3, spec.hidden_text)
            c.setFillColorRGB(0, 0, 0)
            c.rect(spec.x, spec.y, spec.w, spec.h, stroke=0, fill=1)
            if spec.draw_text_after and spec.hidden_text:
                c.setFillColorRGB(1, 1, 1)
                c.setFont("Helvetica", 10.5)
                c.drawString(spec.x + 2, spec.y + 3, spec.hidden_text)
            if spec.code:
                # The left margin, which is the one place on a full page that
                # body text never reaches. A stamp printed *over* a line of
                # prose makes pdfplumber interleave the two runs into a single
                # nonsense token - a real behaviour, but not the one under
                # test, and it would silently break every exemption fixture.
                c.setFillColorRGB(0, 0, 0)
                c.setFont("Helvetica", 7)
                c.drawString(30, spec.y + 2, spec.code)
            c.setFillColorRGB(0, 0, 0)

        # a table rule: a wide, very short filled rect that must not be
        # mistaken for a redaction
        c.rect(72, 96, width - 144, 0.9, stroke=0, fill=1)

        if bates_prefix:
            while counter in bates_skip:
                counter += 1
            c.setFont("Helvetica", 8)
            c.drawRightString(width - 54, 40, f"{bates_prefix}{counter:06d}")
            counter += 1

        c.showPage()
    c.save()
    return path


def withheld_in_full_pdf(
    path: Path,
    code: str = "(b)(5)",
    pages: int = 2,
    withheld_page: int = 2,
) -> Path:
    """A production with one page withheld end to end, stamped the way agencies
    stamp one.

    The withheld sheet is a page-sized black rectangle with
    ``<code> PAGE WITHHELD IN FULL`` reversed out of it in white, drawn *after*
    the rectangle. Nothing is hidden underneath: the words are what a reader
    sees, the pixels have them, and the code they carry is the only statement
    of law on the page. It is the commonest single page in a FOIA release, and
    the one layout in which every character a black box covers is a character
    printed on top of it.

    The other pages are ordinary prose, so a collection built from this has
    something to be a page of.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    width, height = LETTER
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle("Synthetic release")
    rnd = random.Random(11)
    for pno in range(1, pages + 1):
        if pno == withheld_page:
            c.setFillColorRGB(0, 0, 0)
            box_h = height - 214
            c.rect(50, 86, width - 100, box_h, stroke=0, fill=1)
            c.setFillColorRGB(1, 1, 1)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(72, 86 + box_h - 26, f"{code} PAGE WITHHELD IN FULL")
            c.setFillColorRGB(0, 0, 0)
            c.showPage()
            continue
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(72, height - 72, f"MEMORANDUM {pno}")
        c.setFont("Helvetica", 10.5)
        y = height - 104
        for _ in range(30):
            c.drawString(72, y, " ".join(rnd.choice(LOREM) for _ in range(11)))
            y -= 15.2
            if y < 110:
                break
        c.showPage()
    c.save()
    return path


def image_only_pdf(path: Path, images: list[Image.Image]) -> Path:
    """A PDF with no text layer at all - the scan case."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    width, height = LETTER
    c = canvas.Canvas(str(path), pagesize=LETTER)
    for im in images:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="PNG")
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, width=width, height=height)
        c.showPage()
    c.save()
    return path


# --------------------------------------------------------------------------
# page images
# --------------------------------------------------------------------------


def typed_page(
    width: int = 1275,
    height: int = 1650,
    lines: int = 34,
    redactions: list[tuple[int, int, int, int]] | None = None,
    grain: float = 0.0,
    rotate: float = 0.0,
    invert: bool = False,
    photo_block: bool = False,
    scan_border: bool = False,
) -> Image.Image:
    """A grayscale page that looks like a photocopied typed document."""
    from PIL import ImageFont

    im = Image.new("L", (width, height), 246)
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 22
        )
    except OSError:  # pragma: no cover - depends on the host
        font = ImageFont.load_default()

    rnd = random.Random(11)
    y = 150
    for _ in range(lines):
        text = " ".join(rnd.choice(LOREM) for _ in range(8))
        d.text((140, y), text, fill=28, font=font)
        y += 40
        if y > height - 200:
            break

    for box in redactions or []:
        d.rectangle(box, fill=6)

    if photo_block:
        px = Image.effect_noise((360, 260), 42).filter(ImageFilter.GaussianBlur(2))
        im.paste(px.point(lambda v: int(v * 0.55)), (760, 1180))

    if scan_border:
        d.rectangle([0, 0, width - 1, height - 1], outline=4, width=18)

    if grain > 0:
        noise = Image.effect_noise((width, height), int(grain * 100))
        im = Image.blend(im.convert("L"), noise.convert("L"), min(0.6, grain))

    if rotate:
        im = im.rotate(rotate, fillcolor=246, expand=False)

    if invert:
        im = Image.eval(im, lambda v: 255 - v)

    return im


def blank_page(width: int = 1275, height: int = 1650) -> Image.Image:
    return Image.new("L", (width, height), 250)


def noise_page(width: int = 1275, height: int = 1650) -> Image.Image:
    """Ink everywhere, nothing readable: the classic unreadable scan."""
    return Image.effect_noise((width, height), 90).convert("L")
