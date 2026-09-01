#!/usr/bin/env python3
"""Build the Stackroom demo collection from scratch.

Everything in this demo is invented. There is no Free City of Meridian Hollow,
no Bureau of Sunlight, no Halcyon Mirrorworks and no Carrow Ridge; every name,
number, date and signature below was written into this file by hand so that a
screenshot of the tool could show real machinery reading a document that hurts
nobody. The documents are a *worked example*, not a leaked record.

    python demo/build-demo.py            # regenerate the PDFs and both sites
    python demo/build-demo.py --pdfs     # PDFs only, no site build
    python demo/build-demo.py --leak DIR # a poisoned copy, for `stackroom check`

Why a script and not committed PDFs: the scans are several megabytes, every
clone would pay for them, and a demo whose contents nobody can change is a
demo nobody can improve. Regenerating takes about two minutes on a laptop.

What the demo is built to exercise, because a screenshot of a feature that is
not in the picture is a lie:

  * born-digital pages with a real text layer, and scans with none;
  * redactions from six words to a whole page, and a page withheld end to end;
  * exemption codes from two jurisdictions - US FOIA and the Privacy Act in
    the main release, UK FOIA 2000 in the second one;
  * a control-number sequence with a real gap, where three pages were withheld
    in full and never produced;
  * a page the recogniser genuinely cannot read, and a blank page;
  * a page in Cyrillic, whose text layer is read without OCR;
  * a dark band left by a scanner, which is counted as a redaction because it
    is genuinely indistinguishable from one - and warned about;
  * an earlier release of one document with one more black box on it, so
    `stackroom compare` has something true to say.

Requires reportlab and Pillow, both dev dependencies of the project.
"""

# ruff: noqa: RUF001, SIM905
#   RUF001 flags Cyrillic characters that look like Latin ones. The letter from
#   Zerkalsk really is in Russian, and a warning about Cyrillic in a Cyrillic
#   document is noise. SIM905 would have the scanned pages written as lists of
#   quoted strings; they are written as they will be typed, one line to a line,
#   because that is the only way to see what the page will look like.

from __future__ import annotations

import argparse
import io
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
LETTER = (612.0, 792.0)
MARGIN_X = 72.0
BODY_SIZE = 10.4
LEADING = 15.0
CODE_X = 26.0          # left margin: the one column body text never reaches
SCAN_W, SCAN_H = 1275, 1650   # US Letter at 150 dpi

MONO_TTF = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
SANS_TTF = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SANS_BOLD_TTF = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Exemption codes, keyed by the short tag used in the document text below.
CODES = {
    "b4": "(b)(4)",
    "b5": "(b)(5)",
    "b6": "(b)(6)",
    "b7c": "(b)(7)(C)",
    "b7e": "(b)(7)(E)",
    "k2": "(k)(2)",
    "s40": "s.40(2)",
    "s43": "s.43(2)",
    "s36": "s.36(2)",
}

MARKER = re.compile(r"\[\[([a-z0-9]+)(@\d{4})?\|(.+?)\]\]", re.S)


# --------------------------------------------------------------------------
# text layout
# --------------------------------------------------------------------------


@dataclass
class Span:
    """One run of words on one line, and whether it is under a box."""

    text: str
    x: float
    width: float
    code: str | None = None
    hidden: bool = False   # the leak variant: draw it, then cover it


def _wrap(parts: list[tuple[str, str | None]], width: float, size: float, font: str):
    """Wrap ``(word, code)`` pairs to *width*, returning lines of Spans.

    Contiguous words with the same code become one span, because reportlab
    lays a single drawString out with real spaces and a per-word loop does
    not: pdfplumber reads the second as one run-together token, which makes
    Stackroom distrust the text layer and re-OCR a born-digital page.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    lines: list[list[Span]] = []
    cur: list[tuple[str, str | None]] = []
    cur_w = 0.0
    space = stringWidth(" ", font, size)

    def flush() -> None:
        if not cur:
            return
        spans: list[Span] = []
        x = MARGIN_X
        run: list[str] = []
        run_code = cur[0][1]
        run_x = x
        for word, code in cur:
            if code != run_code and run:
                text = " ".join(run)
                w = stringWidth(text, font, size)
                spans.append(Span(text, run_x, w, run_code))
                run_x += w + space
                run = []
                run_code = code
            run.append(word)
        if run:
            text = " ".join(run)
            spans.append(Span(text, run_x, stringWidth(text, font, size), run_code))
        lines.append(spans)

    for word, code in parts:
        w = stringWidth(word, font, size)
        if cur and cur_w + space + w > width:
            flush()
            cur, cur_w = [], 0.0
        cur.append((word, code))
        cur_w += (space + w) if cur_w else w
    flush()
    return lines


def _tokenise(text: str) -> list[tuple[str, str | None]]:
    """Split marked-up prose into ``(word, code)`` pairs.

    ``[[b5|the words the agency took out]]`` marks a redaction; the words are
    never drawn. ``[[b4@2019|...]]`` marks one that only the 2019 release
    made - the whole point of the comparison the demo ships.
    """
    out: list[tuple[str, str | None]] = []
    pos = 0
    for m in MARKER.finditer(text):
        for word in text[pos : m.start()].split():
            out.append((word, None))
        tag, year, inner = m.group(1), m.group(2), m.group(3)
        code = tag + (year or "")
        for word in inner.split():
            out.append((word, code))
        pos = m.end()
    for word in text[pos:].split():
        out.append((word, None))
    return out


# --------------------------------------------------------------------------
# born-digital pages
# --------------------------------------------------------------------------


@dataclass
class Page:
    """One born-digital page: some blocks, and what to stamp on it."""

    blocks: list[tuple[str, str]] = field(default_factory=list)
    """(kind, text). kind is 'h1', 'h2', 'p', 'pre', 'rule' or 'gap'."""

    full_page_box: str | None = None
    """An exemption code: this page is withheld end to end."""

    legend: str = ""
    """A footer line listing the codes cited, as real responses print it."""

    font: str = "Helvetica"


def _fixed(line: str, size: float, font: str) -> list[Span]:
    """One verbatim line - a table row, an address block - as Spans."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    spans: list[Span] = []
    x = MARGIN_X
    pos = 0
    for m in MARKER.finditer(line):
        head = line[pos : m.start()]
        if head:
            w = stringWidth(head, font, size)
            spans.append(Span(head, x, w))
            x += w
        inner = m.group(3)
        w = stringWidth(inner, font, size)
        spans.append(Span(inner, x, w, m.group(1) + (m.group(2) or "")))
        x += w
        pos = m.end()
    tail = line[pos:]
    if tail:
        spans.append(Span(tail, x, stringWidth(tail, font, size)))
    return spans


def _bold(font: str) -> str:
    return "Helvetica-Bold" if font == "Helvetica" else font + "-Bold"


STAMP_LAYOUTS = ("margin", "in-box", "auto")
"""The placements ``--stamp`` chooses between. See :func:`_stamp`."""

MIN_STAMP_SIZE = 3.5
"""Smallest type ``in-box`` will shrink a code to before giving up on it."""


def _stamp(c, code: str, x: float, y: float, width: float, size: float,
           *, layout: str = "auto") -> None:
    """Print the exemption code for a box that has just been drawn.

    Both real layouts are here, and ``--stamp`` picks between them:

    ``in-box``
        Reversed out of the rectangle in white, right-aligned, shrunk as far
        as :data:`MIN_STAMP_SIZE` to fit inside a narrow box. A box too narrow
        for the code even at that size falls back to the margin: white type
        overhanging a black rectangle onto white paper is not a stamp, it is
        nothing at all.
    ``margin``
        In black out in the left margin at :data:`CODE_X` - the one column
        body text never reaches - level with the passage it covers. Several
        hundred points across the page from a redaction in the middle of a
        line.
    ``auto``
        In the box where it fits, in the margin where it does not. The default,
        because a real release is a mixture of the two.

    The flag exists because the difference between them is measurable rather
    than cosmetic. Stackroom attributes a code to the black box on its *line*
    at any distance across the page, and to a box within about 40pt otherwise;
    a margin stamp is reachable only by the first of those rules, while a code
    printed inside the rectangle is within the near field by construction. So a
    release stamped in the margin is the case that tells the two apart. Build
    the demo each way and count the boxes that come back carrying a code.
    Measured on this collection: 41 of 43 boxes carry a code under any of the
    three layouts with the line rule in place; take the line rule out and
    in-box stamps still reach 39 of 43 while margin stamps fall to 23 - which
    is what that rule is worth, and why measuring it needs a release stamped
    the way this flag's own default is not.

    Every code on the page obeys the flag, the one on the page withheld in full
    included - see :func:`_draw_page`, which used to have to make an exception
    of it.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if layout != "margin":
        pt = 6.0
        w = stringWidth(code, "Helvetica", pt)
        # `in-box` means in the box: shrink rather than let the code overhang
        # onto white paper, where white type would be invisible.
        while layout == "in-box" and w + 7 > width and pt > MIN_STAMP_SIZE:
            pt -= 0.25
            w = stringWidth(code, "Helvetica", pt)
        if w + 7 <= width:
            c.setFont("Helvetica", pt)
            c.setFillColorRGB(1, 1, 1)
            c.drawString(x + width - w - 2.5, y + 1.4, code)
            c.setFillColorRGB(0, 0, 0)
            return

    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 7.5)
    c.drawString(CODE_X, y, code)


def _draw_page(c, page: Page, *, release: str, leak: set[str] | None,
               stamp: str = "auto") -> None:
    width, height = LETTER
    body_w = width - 2 * MARGIN_X

    if page.full_page_box:
        # The box first, then the heading reversed out of it in white. That is
        # the layout an agency uses for a page withheld end to end, and it was
        # drawn *above* the box here for a long time to dodge a false positive:
        # the check read the rectangle's overall flatness and called white
        # lettering inside it recoverable text. It now asks each glyph about its
        # own footprint, where a white stem beside a black counter is the most
        # textured thing on the page, so the honest layout is quiet again -
        # `stackroom check` reports this page clear.
        c.setFillColorRGB(0, 0, 0)
        box_y, box_h = 86.0, height - 214
        box_x, box_w = MARGIN_X - 22, width - 2 * (MARGIN_X - 22)
        c.rect(box_x, box_y, box_w, box_h, stroke=0, fill=1)
        baseline = box_y + box_h - 26
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(MARGIN_X, baseline, "PAGE WITHHELD IN FULL")
        c.setFillColorRGB(0, 0, 0)
        # The code obeys `--stamp` like every other code on the page. It did
        # not used to: it was forced into the margin here, because the only
        # characters this rectangle covers are the *spaces* between the
        # heading's plainly visible words, `redaction.py` counted them as text
        # the box had obliterated, and `pipeline._drop_hidden` then withheld
        # every token the box touched - the whole transcription of the page,
        # and a code printed inside it along with it. That is fixed, so the one
        # page in this collection that shows what a full-page stamp looks like
        # can be stamped the way an agency stamps one.
        _stamp(c, page.full_page_box, box_x, baseline, box_w, 8.0, layout=stamp)
        return

    y = height - 72.0
    for kind, text in page.blocks:
        if kind == "gap":
            y -= float(text)
            continue
        if kind == "rule":
            c.setFillColorRGB(0, 0, 0)
            c.rect(MARGIN_X, y + 6, body_w, 0.7, stroke=0, fill=1)
            y -= 12
            continue
        if kind == "h1":
            c.setFont(_bold(page.font), 13)
            c.setFillColorRGB(0, 0, 0)
            c.drawString(MARGIN_X, y, text)
            y -= 22
            continue
        if kind == "h2":
            c.setFont(_bold(page.font), 10.4)
            c.setFillColorRGB(0, 0, 0)
            c.drawString(MARGIN_X, y, text)
            y -= LEADING
            continue

        font = "Courier" if kind == "pre" else page.font
        size = 9.4 if kind == "pre" else BODY_SIZE
        if kind == "pre":
            # Verbatim: a table that gets re-wrapped is not a table.
            lines = [_fixed(line, size, font) for line in text.split("\n")]
        else:
            lines = _wrap(_tokenise(text), body_w, size, font)
        for spans in lines:
            for span in spans:
                tag = span.code.split("@")[0] if span.code else None
                shown = span.code is None or (
                    "@" in span.code and span.code.split("@")[1] != release
                )
                if shown:
                    c.setFont(font, size)
                    c.setFillColorRGB(0, 0, 0)
                    c.drawString(span.x, y, span.text)
                    continue
                # A redaction. The words are simply not in the file - which is
                # what a correct redaction is - unless we were asked to plant
                # a leak, in which case they are drawn and then covered.
                if leak and tag in leak:
                    c.setFont(font, size)
                    c.setFillColorRGB(0, 0, 0)
                    c.drawString(span.x, y, span.text)
                c.setFillColorRGB(0, 0, 0)
                c.rect(span.x - 1.2, y - 2.6, span.width + 2.4, size + 2.4, stroke=0, fill=1)
                _stamp(c, CODES[tag], span.x, y, span.width, size, layout=stamp)
            y -= LEADING if kind != "pre" else 12.6
            if y < 96:
                break
        y -= 6

    if page.legend:
        c.setFont("Helvetica", 7.5)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(MARGIN_X, 58, page.legend)


def born_digital(
    path: Path,
    pages: list[Page],
    *,
    title: str,
    bates_prefix: str | None = None,
    bates_start: int = 1,
    bates_skip: set[int] | None = None,
    release: str = "2024",
    leak: set[str] | None = None,
    stamp: str = "auto",
) -> Path:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(TTFont("Demo-Cyrillic", SANS_TTF))
    pdfmetrics.registerFont(TTFont("Demo-Cyrillic-Bold", SANS_BOLD_TTF))
    width, _height = LETTER
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    c.setTitle(title)
    c.setAuthor("Bureau of Sunlight, Free City of Meridian Hollow")
    c.setSubject("Response to request BOS-2018-0117 (synthetic; see about.md)")

    counter = bates_start
    skip = bates_skip or set()
    for page in pages:
        _draw_page(c, page, release=release, leak=leak, stamp=stamp)
        if bates_prefix:
            while counter in skip:
                counter += 1
            c.setFillColorRGB(0, 0, 0)
            c.setFont("Helvetica", 8)
            c.drawRightString(width - 54, 40, f"{bates_prefix}{counter:06d}")
            counter += 1
        c.showPage()
    c.save()
    return path


# --------------------------------------------------------------------------
# scans
# --------------------------------------------------------------------------


def _scan_font(size: int):
    try:
        return ImageFont.truetype(MONO_TTF, size)
    except OSError:  # pragma: no cover - depends on the host
        return ImageFont.load_default()


def typed_scan(
    lines: list[str],
    *,
    grain: float = 0.0,
    band: tuple[int, int, int, int] | None = None,
    size: int = 21,
    top: int = 150,
    left: int = 150,
    leading: int = 41,
    seed: int = 5,
) -> Image.Image:
    """A page that looks like a photocopy of a typed sheet.

    Redactions are written in the text the same way as in the born-digital
    documents - ``[[b4|the words the reviewer took out]]`` - and the words are
    never drawn. The box goes exactly where they would have been and the code
    is stamped in the left margin beside it, which is where a reviewer's stamp
    goes and the one column body text never reaches.
    """
    im = Image.new("L", (SCAN_W, SCAN_H), 247)
    d = ImageDraw.Draw(im)
    font = _scan_font(size)
    boxes: list[tuple[tuple[int, int, int, int], str]] = []

    y = top
    for line in lines:
        if line.startswith("~"):
            d.text((left, y), line[1:], fill=16, font=_scan_font(size + 6))
            y += leading + 8
            continue
        x = float(left)
        pos = 0
        for m in MARKER.finditer(line):
            head = line[pos : m.start()]
            if head:
                d.text((x, y), head, fill=26, font=font)
                x += d.textlength(head, font=font)
            inner = m.group(3)
            w = d.textlength(inner, font=font)
            boxes.append((
                (int(x) - 4, y - 4, int(x + w) + 4, y + size + 8), CODES[m.group(1)]
            ))
            x += w
            pos = m.end()
        tail = line[pos:]
        if tail:
            d.text((x, y), tail, fill=26, font=font)
        y += leading
        if y > SCAN_H - 120:
            break

    for box, code in boxes:
        d.rectangle(list(box), fill=4)
        # Beside the box, on the same line, which is where a great many real
        # reviewers put it - and, unlike a stamp out in the left margin, it
        # does not make the recogniser read the page as two columns and
        # scramble every table on it.
        d.text((box[2] + 26, box[1] + 4), code, fill=20, font=_scan_font(size - 2))

    if band:
        d.rectangle(list(band), fill=6)

    if grain > 0:
        noise = Image.effect_noise((SCAN_W, SCAN_H), int(grain * 130))
        im = Image.blend(im, noise.convert("L"), min(0.55, grain))
        im = im.filter(ImageFilter.MedianFilter(3))
    return im


def unreadable_scan() -> Image.Image:
    """A third-generation photocopy of a fax. There is ink; there are no words.

    Stackroom is meant to say so rather than publish an empty page and let a
    reader believe the archive has been searched.
    """
    im = Image.effect_noise((SCAN_W, SCAN_H), 96).convert("L")
    d = ImageDraw.Draw(im)
    font = _scan_font(20)
    y = 200
    rnd = random.Random(3)
    while y < SCAN_H - 200:
        d.text((160, y), "".join(rnd.choice("mnwvuo ") for _ in range(58)), fill=70, font=font)
        y += 44
    return im.filter(ImageFilter.GaussianBlur(1.4))


def blank_scan() -> Image.Image:
    """A separator sheet. Nothing on it, and nothing wrong with it."""
    return Image.new("L", (SCAN_W, SCAN_H), 251)


def image_pdf(path: Path, images: list[Image.Image], *, title: str) -> Path:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    width, height = LETTER
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    c.setTitle(title)
    c.setAuthor("Bureau of Sunlight, Free City of Meridian Hollow")
    for im in images:
        buf = io.BytesIO()
        # JPEG, because that is what a scanner produces and because a grainy
        # PNG of a whole page is four megabytes. Quality 82 leaves the solid
        # blocks flat enough for the redaction detector two pixels in, which
        # is where it looks.
        im.convert("L").save(buf, format="JPEG", quality=82, optimize=True)
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, width=width, height=height)
        c.showPage()
    c.save()
    return path


# --------------------------------------------------------------------------
# the documents
#
# Everything below is invented. The markup ``[[b5|words]]`` means "the agency
# took these words out and stamped (b)(5) in the margin": the words are never
# written into the PDF. ``[[b4@2019|words]]`` means the 2019 release took them
# out and the 2024 release did not, which is what `stackroom compare` finds.
# --------------------------------------------------------------------------

AWARD = [
    Page(blocks=[
        ("h1", "BUREAU OF SUNLIGHT — FREE CITY OF MERIDIAN HOLLOW"),
        ("rule", ""),
        ("h2", "MEMORANDUM"),
        ("pre", "To:      Ottoline Frame, Director\n"
                "From:    Anselm Krige, Deputy Director\n"
                "Date:    3 September 2016\n"
                "Subject: Award of the Carrow Ridge heliostat maintenance contract"),
        ("rule", ""),
        ("p", "1. The Bureau maintains fourteen heliostats on Carrow Ridge. From "
              "1 November to 12 February they are the only direct sunlight that reaches "
              "Founders' Square, and the Charter obliges the Bureau to keep them tracking."),
        ("p", "2. Three firms were invited to tender and two returned bids. The evaluation "
              "panel recommends award to Halcyon Mirrorworks Ltd at a first-year price of "
              "412,000 crowns, on the terms at Annex A."),
        ("p", "3. The second-ranked bidder withdrew four days after the site visit, giving "
              "as its reason [[b4|that the Bureau's own survey of the ridge understated the "
              "number of units needing a new drive by at least six, and that no bidder could "
              "price the work honestly from it]], which the panel did not test."),
        ("p", "4. The panel notes that Halcyon has maintained the Founders' Square clock "
              "since 2009 and that its ridge crew is the only one in the city certified for "
              "work above the tree line."),
        ("p", "5. Approval is sought for the award and for the delegation at paragraph 11."),
    ]),
    Page(blocks=[
        ("h1", "ANNEX A — SCOPE OF WORK"),
        ("rule", ""),
        ("p", "A1. The contractor shall maintain fourteen heliostats, units 1 to 14, on the "
              "north face of Carrow Ridge, together with their drives, encoders, mounting "
              "frames and the access stair from the Tallow Lane gate."),
        ("p", "A2. Each heliostat carries a mirror of 4.2 square metres. Units 1 to 6 were "
              "installed in 1997 and re-silvered in 2011. Units 7 to 14 were installed in "
              "2009 and have not been re-silvered."),
        ("p", "A3. The contractor shall attend the ridge monthly from October to March and "
              "quarterly otherwise, shall clean every mirror on each visit, and shall re-aim "
              "any heliostat found more than 0.4 degrees off its commanded position."),
        ("p", "A4. The schedule of rates prices a single alignment visit at [[b4|18,400]] "
              "crowns and a full re-silvering at [[b4|61,900]] crowns per unit."),
        ("p", "A5. The winter alignment window runs from 20 October to 30 October. A "
              "heliostat not aligned within the window cannot be aligned again until the "
              "following year, because the sun does not rise high enough over the ridge to "
              "reach the calibration target on the clock tower."),
        ("p", "A6. Failure to keep a unit tracking for more than thirty consecutive days in "
              "the season is a default under clause 22."),
    ]),
    Page(blocks=[
        ("h1", "EVALUATION PANEL — RECORD OF DECISION"),
        ("rule", ""),
        ("p", "E1. The panel met on 29 August 2016. Present: [[b6|Perpetua Vane]], Chief "
              "Engineer; Anselm Krige, Deputy Director; and the City Procurement Officer."),
        ("p", "E2. Two bids were received. Halcyon Mirrorworks scored 82 of 100 on the "
              "published criteria. The second bid scored 74 and was withdrawn before the "
              "panel reported."),
        ("p", "E3. The panel's own note records that the Bureau had, by 29 August, "
              "[[b5|already told Halcyon that its ridge crew would be wanted from the second "
              "week of October, and had settled the dates with them by telephone before the "
              "tender closed]], and that a competitive process would have been difficult to "
              "defend had the second bidder stayed in."),
        ("p", "E4. The panel recommends award. One member asked that the file record that "
              "the recommendation rests on a single compliant bid. [[b5@2024|The member who "
              "asked for that entry was the City Procurement Officer, who then declined to "
              "sign the panel's report.]]"),
    ]),
    Page(full_page_box="(b)(5)"),
    Page(blocks=[
        ("h1", "APPROVAL"),
        ("rule", ""),
        ("p", "Award approved on the terms recommended. The delegation at paragraph 11 is "
              "granted to the Deputy Director for the life of the contract."),
        ("gap", "26"),
        ("pre", "Ottoline Frame\nDirector, Bureau of Sunlight\n9 September 2016"),
        ("gap", "22"),
        ("p", "First-year price: 412,000 crowns. Term: five years, with two one-year "
              "extensions at the Bureau's option."),
        ("p", "Filed: Bureau register 2016/318. Copy to City Procurement, to Halcyon "
              "Mirrorworks Ltd and to the Ridgeway Commons Trust."),
    ]),
]

CORRESPONDENCE = [
    Page(blocks=[
        ("h1", "BUREAU OF SUNLIGHT — INTERNAL"),
        ("pre", "From: Perpetua Vane, Chief Engineer\n"
                "To:   Anselm Krige, Deputy Director\n"
                "Sent: 22 November 2017, 09:12\n"
                "Subject: Unit 9 after Saturday's storm"),
        ("rule", ""),
        ("p", "Unit 9 is not tracking. The drive turned itself off at 03:40 on Sunday and "
              "has not come back. The mirror is parked face down, which is the safe "
              "position, and it will stay there until somebody climbs the ridge with a new "
              "encoder."),
        ("p", "I have told Halcyon. They say the encoder is a twelve-week item and that the "
              "ridge crew cannot go up until the stair is cleared."),
        ("p", "[[b4@2019|Halcyon has invoiced us for cleaning unit 9 in every month since "
              "the storm, at the full monthly rate, and every one of those invoices has been "
              "passed for payment by this office.]]"),
        ("p", "The practical effect is that Founders' Square loses about a fourteenth of its "
              "winter light. Nobody outside this building has noticed yet. The market traders "
              "will notice in January, when the sun is lowest and the beam from unit 9 is the "
              "one that reaches the north arcade."),
        ("p", "I would like to put unit 9 on the monthly return so that there is a record of "
              "how long it has been dark. [[b5|Tell me if you would rather I did not]]."),
    ]),
    Page(blocks=[
        ("h1", "FILE NOTE"),
        ("pre", "Note of a conversation, made the same day\n"
                "By:   Perpetua Vane, Chief Engineer\n"
                "Date: 9 January 2018"),
        ("rule", ""),
        ("p", "I saw the Deputy Director in the yard this morning and asked him about unit 9 "
              "and about the ridge survey."),
        ("p", "His view, which he asked me not to minute anywhere else, was that the Bureau "
              "should [[b5|hold the ridge survey back until the contract extension has been "
              "signed in April, and keep the unit 9 outage out of the winter report]], and "
              "that Halcyon should not be told which units the survey would cover."),
        ("p", "I said I would put a note of it on the file. He said that was a matter for me."),
        ("p", "Unit 9 has now been dark for 48 days. Units 3 and 11 are both more than 0.4 "
              "degrees off the calibration target and are outside the contract tolerance."),
    ]),
    Page(blocks=[
        ("h1", "COMPLAINT RECEIVED — TALLOW LANE"),
        ("pre", "Received: 8 March 2018, at the Tallow Lane gate\n"
                "Taken by: duty warden\n"
                "Referred: City Inspectorate, 12 March 2018"),
        ("rule", ""),
        ("p", "The complainant, [[b6|Wren Achterberg of 14 Tallow Lane]], states that at "
              "about 11:20 on 6 March a beam from the ridge entered the glasshouse at the "
              "foot of her garden and stayed on it for something over an hour."),
        ("p", "She reports that [[b6|the glasshouse reached a temperature she could not stand "
              "in, that a row of seedlings and two mature vines were destroyed, and that the "
              "polycarbonate on the south face has buckled and will have to be cut out and "
              "replaced before the autumn]]."),
        ("p", "She gives a telephone number, [[b6|4-771-208]], and asks to be told which unit "
              "was responsible."),
        ("p", "The warden's own note records that unit 11 was being re-aimed on the morning "
              "of 6 March, and that [[b7c|the crew on the ridge that day was working without "
              "the beam-safety observer the method statement requires]]."),
        ("p", "The Bureau has not written to the complainant. [[b5|Advice was taken from the "
              "City Solicitor on 14 March and is withheld]]."),
    ]),
    Page(blocks=[
        ("h1", "CITY INSPECTORATE — REFERRAL"),
        ("pre", "Our reference: INSP-2018-0441\n"
                "Subject: Beam incident, Tallow Lane, 6 March 2018"),
        ("rule", ""),
        ("p", "The Inspectorate has opened a file. The Bureau is asked not to interview the "
              "ridge crew before the Inspectorate has done so."),
        ("p", "The method by which the Inspectorate establishes where a beam was pointed at a "
              "given hour is [[b7e|a reconstruction from the drive logs against the shadow "
              "line in the crew's own photographs, and it is not published because a "
              "contractor who knew it could edit the logs to match]]."),
        ("p", "The names of the crew members present on 6 March are [[b7c|Devrim Oyelaran and "
              "Cosmo Dettwiler]] and are withheld from this copy."),
        ("p", "Material compiled for the Inspectorate's file is [[k2|held on the "
              "Inspectorate's investigative system, and none of it is disclosable while the "
              "file remains open]]."),
        ("p", "The Bureau's duty warden is asked to preserve the gate log for 6 March 2018."),
    ]),
    Page(blocks=[
        ("h1", "CONTRACT REGISTER — AMENDMENTS"),
        ("pre", "Contract 2016/318 — Halcyon Mirrorworks Ltd"),
        ("rule", ""),
        ("pre", "Amendment   Date          Value        Reason\n"
                "----------  ------------  -----------  ------------------------------\n"
                "Original    09 Sep 2016       412,000  Award\n"
                "No. 1       14 Mar 2017       168,000  Stair repair, Tallow Lane gate\n"
                "No. 2       02 Feb 2018       331,000  Encoder replacement, units 3-11\n"
                "No. 3       27 Jun 2018       269,000  [[b4|Settlement of the disputed]]\n"
                "----------  ------------  -----------  ------------------------------\n"
                "Total                       1,180,000  crowns"),
        ("gap", "10"),
        ("p", "The Bureau's standing instruction requires a fresh competition where "
              "cumulative amendments exceed half the original value of a contract. The file "
              "does not record that this was done."),
        ("p", "Amendment 3 was signed by the Deputy Director under the delegation at "
              "paragraph 11 of the award memorandum."),
    ]),
    Page(blocks=[
        ("h1", "HALCYON MIRRORWORKS LTD"),
        ("pre", "Ridge Yard, Carrow Lane, Meridian Hollow\n14 August 2018"),
        ("rule", ""),
        ("p", "Dear Deputy Director,"),
        ("p", "You ask why unit 9 appears on our monthly cleaning sheets for the period from "
              "November 2017. The sheets record the visit, not the unit. Our crew attends the "
              "ridge and cleans what can be reached, and unit 9, parked face down, cannot be "
              "reached at all without the stair."),
        ("p", "We do not accept that the sums invoiced were not earned. Our position on the "
              "2017-18 season is in our letter of 3 July, which [[b4|values the disputed "
              "cleaning at 41,600 crowns and offers to set it against the encoder work]]."),
        ("p", "The encoder for unit 9 was delivered on 2 August and will be fitted in the "
              "first week of the window."),
        ("p", "Yours faithfully,"),
        ("gap", "12"),
        ("pre", "[[b6|C. Dettwiler]]\nAccount manager"),
    ]),
    Page(blocks=[
        ("h1", "RESPONSE TO REQUEST BOS-2018-0117"),
        ("pre", "21 February 2024"),
        ("rule", ""),
        ("p", "This is the Bureau's response following the decision of the Review Panel of "
              "4 December 2023, which required the Bureau to reconsider the material withheld "
              "from its release of 14 March 2019."),
        ("p", "Twenty-one pages are released. Three pages are withheld in full and are marked "
              "in the control-number sequence by their absence, at BOS-000011 to BOS-000013."),
        ("p", "Passages are withheld under the codes stamped beside them in the left margin. "
              "One passage withheld in 2019 is released in this response."),
        ("p", "The Bureau maintains that the deliberative material on this file is withheld. "
              "[[b5|The Review Panel's contrary view on the file note of 9 January 2018 is at "
              "paragraph 31 of its decision]]."),
    ], legend="Codes cited in this response: (b)(4) (b)(5) (b)(6) (b)(7)(C) (b)(7)(E) (k)(2)"),
    Page(blocks=[
        ("h1", "RESPONSE — CONTINUED"),
        ("rule", ""),
        ("p", "You may ask the Review Panel to consider this response again within 40 working "
              "days of its date."),
        ("p", "The Bureau holds no further correspondence between the Deputy Director and "
              "Halcyon Mirrorworks for the period of your request beyond what is released or "
              "withheld above."),
        ("p", "Unit 9 was returned to service on 26 October 2018, eleven months and twelve "
              "days after it stopped tracking. It was cleaned monthly throughout."),
        ("p", "Signed for the Director,"),
        ("gap", "14"),
        ("pre", "A. Krige\nDeputy Director\n21 February 2024"),
    ]),
]

ZERKALSK = [
    Page(blocks=[
        ("h1", "ANNEX E — CORRESPONDENCE WITH ZERKALSK"),
        ("rule", ""),
        ("p", "The Bureau corresponds with the daylight office of Zerkalsk, a twinned city "
              "that installed heliostats of the same pattern on its own ridge in 2013. The "
              "letter that follows is reproduced as it was received, in Russian. The Bureau "
              "holds no translation of it and has made none for this response."),
        ("p", "One passage is withheld from the letter on privacy grounds. The remainder is "
              "released in full."),
        ("p", "Stackroom reads this page's text layer directly and does not send it to the "
              "recogniser, so the Cyrillic is searchable whether or not a Russian language "
              "pack is installed."),
    ]),
    Page(font="Demo-Cyrillic", blocks=[
        ("h1", "ГОРОДСКОЕ БЮРО "
               "СВЕТА — ЗЕРКАЛЬСК"),
        ("rule", ""),
        ("p", "Уважаемые коллеги!"),
        ("p", "Мы получили ваше "
              "письмо от 3 сентября "
              "и благодарим за "
              "подробный ответ о "
              "работе гелиостатов "
              "на хребте Карроу."),
        ("p", "У нас та же беда. "
              "Два зеркала из "
              "шестнадцати не "
              "следят за солнцем "
              "с зимы, и подрядчик "
              "говорит, что датчик "
              "придётся ждать три "
              "месяца."),
        ("p", "Наш инженер, "
              "[[b6|Марта Сельга]], "
              "готова приехать "
              "в Меридиан-Холлоу "
              "в октябре и "
              "посмотреть на "
              "ваши приводы."),
        ("p", "С уважением, "
              "городской инженер "
              "Зеркальска."),
    ]),
]

RIDGEWAY = [
    Page(blocks=[
        ("h1", "RIDGEWAY COMMONS TRUST"),
        ("pre", "Response to request RCT-2024-006\n3 May 2024"),
        ("rule", ""),
        ("p", "The Trust holds Carrow Ridge for the commoners of Meridian Hollow and licenses "
              "the Bureau of Sunlight to keep fourteen heliostats on the north face. You asked "
              "for the licence, the access agreement, and the correspondence about the beam "
              "incident of 6 March 2018."),
        ("p", "The licence and the access agreement follow. Some information is withheld."),
        ("p", "The name and address of the commoner who wrote to the Trust on 9 March 2018 "
              "are withheld under section 40(2): they are personal data about an identifiable "
              "person, and she has not agreed to be named."),
        ("p", "The licence fee and the schedule of rates behind it are withheld under section "
              "43(2). The Trust is in negotiation with the Bureau over the 2025 renewal, and "
              "publishing the figure now would prejudice that negotiation."),
        ("p", "The Trust's own assessment of the Bureau's conduct on the ridge is withheld "
              "under section 36(2), on the ground that its officers would not record such "
              "assessments candidly if they were published."),
    ]),
    Page(blocks=[
        ("h1", "LICENCE — CARROW RIDGE, NORTH FACE"),
        ("rule", ""),
        ("p", "1. The Trust grants the Bureau a licence to keep, operate and maintain "
              "fourteen heliostats on the north face of Carrow Ridge, and to use the access "
              "stair from the Tallow Lane gate for that purpose."),
        ("p", "2. The annual licence fee is [[s43|41,000 crowns, index-linked to the city "
              "rate, reviewable every third year]]."),
        ("p", "3. The Bureau shall not aim any heliostat so that its beam falls on land "
              "outside the ridge boundary. The Trust's remedy for a breach of this clause is "
              "suspension of the licence on seven days' notice."),
        ("p", "4. The Bureau shall give the Trust a copy of every inspection log within "
              "fourteen days of the inspection."),
    ]),
    Page(blocks=[
        ("h1", "CORRESPONDENCE — 9 MARCH 2018"),
        ("rule", ""),
        ("p", "A commoner wrote to the Trust on 9 March 2018 about a beam that entered a "
              "glasshouse on Tallow Lane. Her letter is released with her name and address "
              "removed."),
        ("p", "“I am writing because [[s40|Wren Achterberg, 14 Tallow Lane]] has had no "
              "answer from the Bureau. On Tuesday morning the light came off the ridge and sat "
              "in my glasshouse for an hour and I could not go into it. I am told the Trust "
              "owns the ridge. Somebody owns the mirrors.”"),
        ("p", "The Trust acknowledged the letter on 12 March 2018 and forwarded it to the "
              "Bureau the same day."),
    ]),
    Page(blocks=[
        ("h1", "TRUST — NOTE FOR THE COMMITTEE"),
        ("rule", ""),
        ("p", "The committee is asked to note the Bureau's response and to decide whether to "
              "raise the beam incident at the renewal."),
        ("p", "The clerk's own assessment is that [[s36|the Bureau has not once given the "
              "Trust an inspection log within the fourteen days clause 4 requires, and that "
              "the committee should say so in writing before the renewal rather than after "
              "it]]."),
        ("p", "The Trust has no power to inspect the heliostats itself and relies on the "
              "Bureau's logs."),
    ]),
]


# --------------------------------------------------------------------------
# the scanned annexes
# --------------------------------------------------------------------------

LOG_ONE = """~ANNEX C - HELIOSTAT INSPECTION LOG
Carrow Ridge, north face. Season 2017-18.
Inspected by the Chief Engineer, 4 December 2017.

UNIT INSTALLED SILVERED TRACKING OFFSET NOTE
 1   1997      2011     yes      10     clean
 2   1997      2011     yes      24     clean
 3   1997      2011     yes      62     outside tolerance
 4   1997      2011     yes      11     clean
 5   1997      2011     yes      14     clean
 6   1997      2011     yes      33     stiff in azimuth
 7   2009      --       yes      26     clean
 8   2009      --       yes      12     clean

Every heliostat on this ridge is aimed at one calibration
target on the face of the Founders Square clock tower. A
heliostat more than 0.4 degrees off that target puts its
beam into Tallow Lane, and the gardens on the east side of
the lane are the first thing the beam reaches.

The offset column is the reading taken at the ridge at
noon, in hundredths of a degree, uncorrected for the
season. The tolerance in the contract is 40, which is
four tenths of one degree, and unit 3 is over it.""".split("\n")

LOG_TWO = """~ANNEX C - INSPECTION LOG, CONTINUED
UNIT INSTALLED SILVERED TRACKING OFFSET NOTE
 9   2009      --       STOPPED  --     drive off since 19 Nov
10   2009      --       yes      23     clean
11   2009      --       yes      68     outside tolerance
12   2009      --       yes      15     clean
13   2009      --       yes      20     clean
14   2009      --       yes      31     clean

Unit 9 has not tracked since the storm of 19 November. The
contractor was asked on the ridge that day for a date to
fit a new encoder. The answer was twelve weeks from order,
and the order had not been placed.

The contract rate for an alignment visit is [[b4|18,400]]
crowns, and the rate for a re-silvering is [[b4|61,900]],
both of which the contractor treats as confidential.

The engineer who signed this log is [[b6|Perpetua Vane]].

Unit 11 was re-aimed on 6 March 2018. See the incident file.
Two units on this sheet are outside the contract tolerance
and one of the fourteen has not moved for a fortnight.""".split("\n")

GATE_LOG = """~ANNEX D - TALLOW LANE GATE LOG
6 March 2018. Kept by the duty warden.

TIME  VEHICLE     CREW DESTINATION      OUT
07:40 ridge truck 3    north face       11:55
09:05 --          1    stair, lower run 09:40
11:20 --          --   --               --
13:10 ridge truck 3    north face       16:20

Note by the warden: at 11:20 the light off the ridge came
down into the gardens on the east side of the lane. I have
not seen it do that before. I telephoned the yard and the
crew said they were re-aiming unit 11 and would stop. It
stopped at about half past twelve.

A resident came to the gate at 13:40 and asked which unit
had done it. I said that I did not know, and took her name.

The foot of this page is dark because the scanner lid was
open when the annexe was copied. It is a photograph of the
room, not a black box, and Stackroom cannot tell the two
apart: it counts the band as a redaction and says so.""".split("\n")

RECEIPT = """~ANNEX D - RECEIPT FOR THE GATE LOG
Received from the Bureau of Sunlight, one gate log for
6 March 2018, four pages, photocopied.

City Inspectorate, 14 March 2018.

Signed  [[b6|D. Oyelaran]]
Rank    Inspector, second grade

This copy was made on the Inspectorate's machine and it is
the copy released. The Bureau's own copy is not on this
file and the Bureau has not said where it is.

The log covers the gate only. It does not record who was on
the ridge, and the ridge has a second way up from the
Halcyon yard which is not gated and is not logged.""".split("\n")


def scanned_annexes() -> list[Image.Image]:
    return [
        typed_scan(LOG_ONE, grain=0.05, size=22, seed=2),
        typed_scan(LOG_TWO, grain=0.05, size=22, seed=4),
        blank_scan(),
        unreadable_scan(),
        # A horizontal band at the foot: the scanner lid, left open. It is
        # flat, black, solid and rectangular because it is, so the ledger
        # counts it. That is the honest failure, and the demo shows it.
        typed_scan(GATE_LOG, grain=0.07, size=22, band=(0, 1490, SCAN_W, SCAN_H), seed=6),
        typed_scan(RECEIPT, grain=0.05, size=22, seed=8),
    ]


# --------------------------------------------------------------------------
# putting it together
# --------------------------------------------------------------------------

BATES = "BOS-"
WITHHELD_IN_FULL = {11, 12, 13}   # produced to nobody; the gap in the sequence


def write_release(folder: Path, release: str, *, leak: set[str] | None = None,
                  stamp: str = "auto") -> None:
    """Write one release's PDFs into *folder*.

    The 2019 release is the same file tree seen a review decision earlier: one
    passage of the correspondence still under a black box, one passage of the
    evaluation record not yet under one, and no annexes at all.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*.pdf"):
        stale.unlink()

    born_digital(
        folder / "01-award-memorandum.pdf",
        AWARD,
        title="Heliostat maintenance: award memorandum",
        bates_prefix=BATES,
        bates_start=1,
        release=release,
        stamp=stamp,
    )
    born_digital(
        folder / "02-correspondence.pdf",
        CORRESPONDENCE if release == "2024" else CORRESPONDENCE[:6],
        title="Correspondence: unit 9 and the Tallow Lane complaint",
        bates_prefix=BATES,
        bates_start=6,
        bates_skip=WITHHELD_IN_FULL,
        release=release,
        leak=leak,
        stamp=stamp,
    )
    if release != "2024":
        return
    image_pdf(
        folder / "03-annexes-scanned.pdf",
        scanned_annexes(),
        title="Annexes C and D: inspection log and gate log",
    )
    born_digital(
        folder / "04-annex-e-zerkalsk.pdf",
        ZERKALSK,
        title="Annex E: correspondence with Zerkalsk",
        release=release,
        stamp=stamp,
    )


def write_ridgeway(folder: Path, *, stamp: str = "auto") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*.pdf"):
        stale.unlink()
    born_digital(
        folder / "01-trust-response.pdf",
        RIDGEWAY,
        title="Ridgeway Commons Trust: response to RCT-2024-006",
        stamp=stamp,
    )


def _run(cmd: list[str]) -> None:
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True)


def _facts(site: Path) -> dict:
    import json

    return json.loads((site / "manifest.json").read_text())


def verify(site: Path, uk_site: Path) -> int:
    """Say, out loud, whether the demo still contains what it claims to.

    A demo that quietly loses a feature takes the screenshot of that feature
    down with it, and the screenshot is the part people believe.
    """
    m = _facts(site)
    stats = m["stats"]
    docs = {d["id"]: d for d in m["documents"]}
    uk = _facts(uk_site)["stats"]
    compare_page = (site / "compare" / "index.html")
    compare_html = compare_page.read_text() if compare_page.exists() else ""

    checks: list[tuple[str, bool, str]] = [
        ("born-digital and scanned pages",
         stats["ocr_pages"] >= 4 and stats["pages"] - stats["ocr_pages"] >= 10,
         f"{stats['ocr_pages']} read by OCR of {stats['pages']} pages"),
        ("a page withheld end to end",
         any((site / "d" / "01-award-memorandum" / "p" / "4" / "index.html").exists() for _ in [0]),
         "01-award-memorandum p4"),
        ("a control-number gap",
         bool(docs["02-correspondence"]["bates_gaps"]),
         str(docs["02-correspondence"]["bates_gaps"])),
        ("a page the recogniser could not read",
         stats["unreadable_pages"] >= 1, f"{stats['unreadable_pages']}"),
        ("a blank page", stats["blank_pages"] >= 1, f"{stats['blank_pages']}"),
        ("a page in a non-Latin script",
         "ru" in stats["languages"], ", ".join(stats["languages"])),
        ("US FOIA and Privacy Act codes",
         {"b(4)", "b(5)", "b(6)", "b(7)(C)", "b(7)(E)", "k(2)"} <= set(stats["exemption_counts"]),
         ", ".join(f"{k}={v}" for k, v in sorted(stats["exemption_counts"].items()))),
        ("UK FOIA 2000 codes in the second collection",
         len(uk["exemption_counts"]) >= 2,
         ", ".join(f"{k}={v}" for k, v in sorted(uk["exemption_counts"].items()))),
        ("a comparison with findings",
         "compare" in compare_html and len(compare_html) > 2000,
         f"compare/index.html, {len(compare_html):,} bytes"),
    ]

    width = max(len(name) for name, _, _ in checks)
    ok = True
    print("\nWhat the built demo actually contains")
    print("-" * (width + 34))
    for name, passed, detail in checks:
        print(f"  {'ok ' if passed else 'MISSING'} {name.ljust(width)}  {detail}")
        ok &= passed
    print("-" * (width + 34))
    print(f"  {stats['documents']} documents, {stats['pages']} pages, "
          f"{stats['words']:,} words, {stats['redaction_boxes']} redactions on "
          f"{stats['pages_with_redactions']} pages")
    print(f"  withheld: {stats['redaction_ratio'] * 100:.1f}% of the content on the "
          f"redacted pages, {stats['redaction_ratio_collection'] * 100:.1f}% of the collection")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdfs", action="store_true", help="write the PDFs and stop")
    ap.add_argument("--leak", metavar="DIR", help="also write a poisoned copy of the 2024 "
                    "release into DIR, for demonstrating `stackroom check`. Never publish it.")
    ap.add_argument("--leak-codes", default="b7c", metavar="TAGS",
                    help="which redactions the poisoned copy should fail to remove, as a "
                    "comma-separated list of the tags used in this file (default: b7c)")
    ap.add_argument("--stamp", choices=STAMP_LAYOUTS, default="auto",
                    help="where the exemption codes are printed: reversed out of the box "
                         "(in-box), out in the left margin level with the passage (margin), "
                         "or in the box where it fits and the margin where it does not "
                         "(auto, the default, and what a real release looks like). Both are "
                         "real layouts and Stackroom reads them by different rules, so this "
                         "is how you measure the difference: build each way and count the "
                         "boxes that carry a code.")
    ap.add_argument("--no-search", action="store_true", help="skip the search index (faster)")
    ap.add_argument("-j", "--workers", type=int, default=0, help="parallel workers")
    args = ap.parse_args()

    write_release(HERE / "release-2024", "2024", stamp=args.stamp)
    write_release(HERE / "release-2019", "2019", stamp=args.stamp)
    write_ridgeway(HERE / "ridgeway-2024", stamp=args.stamp)
    print(f"wrote PDFs into {HERE}/release-2024, release-2019 and ridgeway-2024")

    if args.leak:
        leak_dir = Path(args.leak)
        leak_dir.mkdir(parents=True, exist_ok=True)
        for name in ("about.md", "stackroom.toml"):
            shutil.copy(HERE / "release-2024" / name, leak_dir / name)
        write_release(leak_dir, "2024", leak=set(args.leak_codes.split(",")),
                      stamp=args.stamp)
        print(f"wrote a POISONED copy into {leak_dir}: the {args.leak_codes} "
              "redactions cover their text without removing it.")
        print("Do not publish it. `stackroom check` on it is the point.")

    if args.pdfs:
        return 0

    extra = ["--no-search"] if args.no_search else []
    if args.workers:
        extra += ["-j", str(args.workers)]
    _run(["stackroom", "compare", str(HERE / "release-2019"), str(HERE / "release-2024"),
          "-o", str(HERE / "site"), "--old-label", "Released 14 March 2019",
          "--new-label", "Released 21 February 2024", "--force", *extra])
    _run(["stackroom", "build", str(HERE / "ridgeway-2024"),
          "-o", str(HERE / "site-ridgeway"), "--force", *extra])
    return verify(HERE / "site", HERE / "site-ridgeway")


if __name__ == "__main__":
    sys.exit(main())
