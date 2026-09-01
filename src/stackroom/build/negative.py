"""The negative: every redaction in the release, drawn, and nothing else.

A page in an archive normally shows what survived. This one shows what did
not. Every redaction has a rectangle in page-relative coordinates on a page of
known aspect, so all of them can be drawn together at true relative size and
position - and what comes back is a picture of the shape of the withholding.
Some releases turn out to be a scatter of names; some are eleven pages of solid
ink with a date left showing.

Three claims are made on that page, and this module has to keep all three or
the picture is a lie:

1. **Area is proportional.** Every page in the field is drawn at the same area
   on screen, so a rectangle's area is exactly its share of the page it came
   from. Nothing is normalised to make the grid tidy. A withheld name is a
   sliver two pixels tall next to a withheld chapter, because that is what they
   are.

2. **Position is true.** In the default arrangement a redaction sits where it
   sat on the page. That is the arrangement that shows an agency blacking out
   every signature block, or the same paragraph on forty consecutive pages.

3. **Every rectangle leads back.** Each one is inside a link to the page it was
   cut out of, so the picture is a finding aid and not an illustration.

The field is built here, in Python, as inline SVG, because a static picture
that is already correct beats one a script has to assemble - it survives
JavaScript being off, a reader saving the page, and the archive being read out
of a zip in ten years. ``assets/js/negative.js`` adds a tooltip, keyboard
exploration and a per-code filter on top of a page that is already complete.

Byte cost is the constraint that shapes everything else, and it is measured
rather than guessed - ``tests/test_negative.py`` holds the ceilings to it. On a
release whose boxes are all in different places, so that nothing compresses
away:

===============  =========  =========  ===================================
Redactions       Markup     Gzipped    What is drawn
===============  =========  =========  ===================================
10                  4.7 KB     1.1 KB  all three arrangements
100                  34 KB     4.9 KB  all three arrangements
1,000                95 KB      17 KB  page order only, one rect each
4,000               382 KB      61 KB  page order only, one rect each
10,000              347 KB      96 KB  merged paths, 93% of the withheld area
40,000              366 KB     104 KB  merged paths, 56% of the withheld area
===============  =========  =========  ===================================

One ``<rect>`` per redaction is about 55 bytes, which is nothing at 200 and
severe at 40,000, so the renderer changes shape twice on the way up - see
:data:`DETAIL_LIMIT` and :data:`PACKED_LIMIT` - and stops at
:data:`CELL_LIMIT`, saying in plain words on the page how much it left out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from markupsafe import Markup, escape

from ..i18n import Translator, translator_for
from ..ingest import exemptions as exemptions_mod
from ..model import Collection, PageVerdict

__all__ = [
    "CELL_LIMIT",
    "DETAIL_LIMIT",
    "PACKED_LIMIT",
    "RECT_LIMIT",
    "Field",
    "build",
    "page_context",
]


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------

FIELD_WIDTH = 1000.0
"""Width of every field's viewBox. The SVG scales to its container, so these
are not pixels - but at a comfortable reading width one unit is about one
pixel, which is the right way to think about the numbers below."""

MAX_CELL_W = 200.0
"""No page cell is wider than a fifth of the field. A release with one redacted
page should not draw that page a metre high."""

MIN_CELL_W = 26.0
"""And none is narrower than this. Below it a redacted line is a third of a
pixel tall and the picture stops being one."""

CELL_GAP = 6.0
BAND_GAP = 32.0
BAND_LABEL = 24.0
PACK_GAP = 1.6
"""A hairline between packed rectangles. Without it two neighbours read as one
larger redaction, which is exactly the fact this page exists to get right."""

DETAIL_LIMIT = 4_000
"""Above this many rectangles, each page's redactions are merged into one path
per exemption code instead of one ``<rect>`` each: half the bytes, the same
picture, no per-rectangle tooltip. The links survive, because they are per
page."""

PACKED_LIMIT = 500
"""Above this, the regrouped arrangements are not drawn. Each one is another
copy of every rectangle in the release, and three copies of a large one is a
page that costs more to fetch than the second and third copies are worth. The
table under the field carries the same finding in numbers at any size."""

RECT_LIMIT = 8_000
CELL_LIMIT = 1_200
"""Hard ceilings, set by what the picture costs rather than by what looks
round. A page cell is a link and a mount, about 83 bytes, and a rectangle is
about 30 more, so a field at both ceilings is roughly 370 KB of markup and 100
KB over the wire - the same order as one of the page scans this archive serves
without apology, and no more than that.

Beyond them the field draws the pages with the most withheld on them, and the
page says how many it left out and what share of the withheld area is on
screen. A picture that quietly omits half its subject is worse than one that
admits it."""

LIST_LIMIT = 500
"""Rows in the index below the field. It is the keyboard and screen-reader
route into the field, so it is complete for any release under this size."""

DOC_LABEL_LIMIT = 40
"""Past this many documents the field stops captioning each one: forty rows of
heading in a picture is a table of contents, not a picture."""

LIMITS = {
    "detail": DETAIL_LIMIT,
    "packed": PACKED_LIMIT,
    "rects": RECT_LIMIT,
    "cells": CELL_LIMIT,
    "list": LIST_LIMIT,
}

NO_CODE = ""
"""The grouping key for a redaction with no exemption printed beside it. It is
usually the largest group in the release, which is itself the finding."""


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Mark:
    """One redaction, ready to draw.

    ``x``/``y``/``w``/``h`` are the page-relative box, clamped to the page.
    ``code`` is the first exemption cited for it, or ``NO_CODE``.
    """

    x: float
    y: float
    w: float
    h: float
    code: str
    inherited: bool = False
    """True when the code came from the page rather than from beside the box.
    Counted separately and said out loud: it is a weaker claim than a code
    stamped next to the rectangle, and a reader should know which they have."""

    @property
    def share(self) -> float:
        """Share of the page's area this box covers."""
        return self.w * self.h


@dataclass(slots=True)
class Cell:
    """One page that has something withheld on it."""

    doc_id: str
    doc_title: str
    number: int
    url: str
    aspect: float
    marks: list[Mark]
    ratio: float
    """The page's own redaction ratio: withheld share of its *inked* area, which
    is the measure the rest of the archive uses."""

    codes: list[str] = dc_field(default_factory=list)
    bates: str | None = None

    # filled in by the layout
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0

    @property
    def share(self) -> float:
        return sum(m.share for m in self.marks)

    @property
    def label(self) -> str:
        return f"{self.doc_title}, page {self.number}"


@dataclass(slots=True)
class Field:
    """One arrangement of the whole field, as ready-to-drop-in SVG."""

    id: str
    name: str
    """What the reader picks in the control: "In page order"."""
    caption: str
    """What this arrangement is for, in one sentence."""
    svg: Markup
    height: float
    rects: int
    label: str
    """The reading of the picture, for a screen reader and for the caption."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _num(value: float) -> str:
    """A coordinate, as short as it can be written without lying.

    Two decimals of a thousand-unit field is a hundredth of a pixel at any
    width a person reads at, and trimming the zeros off saves a character on
    most of the numbers in a file that is mostly numbers.
    """
    if value <= 0 and value > -0.005:
        return "0"
    text = f"{value:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _clamped(box: Any) -> tuple[float, float, float, float] | None:
    """A box trimmed to the page, or nothing if there is no box left.

    Detection can push a rectangle a hair past the edge of the page, and a
    rectangle drawn outside its cell would land on the neighbouring page and
    read as a redaction that is not there.
    """
    x1 = min(max(box.x, 0.0), 1.0)
    y1 = min(max(box.y, 0.0), 1.0)
    x2 = min(max(box.x + box.w, 0.0), 1.0)
    y2 = min(max(box.y + box.h, 0.0), 1.0)
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2 - x1, y2 - y1)


def _median(values: list[float], default: float) -> float:
    if not values:
        return default
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _percent(share: float, t: Translator) -> str:
    """A share of a page, said the way a person would say it.

    Four bands, four messages, and the reason there are four rather than one
    sentence with a number in it is that "0.0% of the page" reads as a
    measurement that failed rather than as a very small rectangle. The same
    four are used by the tooltip in ``assets/js/negative.js``, which recovers a
    rectangle's share from the drawing rather than being shipped every box's
    measurements a second time - so the two say it in the same words because
    they read the same keys.
    """
    if share <= 0:
        return str(t("negative.share_none"))
    if share < 0.001:
        return str(t("negative.share_tiny"))
    if share < 0.01:
        return str(t("negative.share_small", percent=t.pct(share, digits=1, of_one=True)))
    return str(t("negative.share_large", percent=t.pct(share, digits=0, of_one=True)))


def _share_text(share: float, t: Translator) -> str:
    """A share of one page, as a column of a table wants it.

    A number rather than a sentence, so there is nothing here to translate but
    the decimal mark and where the percent sign goes, both of which the
    catalogue already knows. "<" is a comparison, not a word.
    """
    if share <= 0:
        return t.pct(0)
    if share < 0.001:
        return "<" + t.pct(0.1, digits=1)
    if share < 0.1:
        return t.pct(share, digits=1, of_one=True)
    return t.pct(share, digits=0, of_one=True)


def _extent(total: float, t: Translator) -> str:
    """A run of redactions added up, said as an amount of paper.

    "Twelve pages' worth" is a quantity a reader can picture and quote. Below
    about a page it stops being one, so it goes back to being a share.
    """
    if total >= 1.95:
        return str(t("negative.extent_pages", pages=t.n(total, digits=1)))
    if total >= 0.95:
        return str(t("negative.extent_page"))
    if total >= 0.01:
        return str(t("negative.extent_share", percent=t.pct(total, digits=0, of_one=True)))
    if total >= 0.001:
        return str(t("negative.extent_share", percent=t.pct(total, digits=1, of_one=True)))
    return str(t("negative.extent_tiny"))


def _truncate(text: str, limit: int = 56) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _code_slug(code: str) -> str:
    """A class-safe form of a statutory code: ``b(7)(C)`` becomes ``b7c``."""
    return "".join(ch for ch in code.lower() if ch.isalnum()) or "none"


# --------------------------------------------------------------------------
# gathering
# --------------------------------------------------------------------------


def _cells(collection: Collection, root: str) -> list[Cell]:
    """Every page with a drawable redaction on it, in reading order."""
    out: list[Cell] = []
    for doc in collection.documents:
        for page in doc.pages:
            # A code printed beside a box belongs to that box. A single code
            # printed for the whole page belongs to everything withheld on it,
            # which is a weaker claim but a true one, so it is used and marked
            # as inherited. Several page-level codes and nothing beside the
            # boxes is where it stops: picking whichever is nearest would
            # invent a fact, which is the thing ingest/exemptions.py refuses to
            # do and this page has no business undoing.
            page_code = page.exemptions[0] if len(page.exemptions) == 1 else NO_CODE
            marks: list[Mark] = []
            for redaction in page.redactions:
                clamped = _clamped(redaction.box)
                if clamped is None:
                    continue
                if redaction.codes:
                    marks.append(Mark(*clamped, code=redaction.codes[0]))
                else:
                    marks.append(Mark(*clamped, code=page_code, inherited=bool(page_code)))
            if not marks:
                continue
            aspect = page.aspect if 0.2 <= page.aspect <= 5.0 else 1.294
            out.append(
                Cell(
                    doc_id=doc.id,
                    doc_title=doc.title,
                    number=page.number,
                    url=f"{root}d/{doc.id}/p/{page.number}/index.html",
                    aspect=aspect,
                    marks=marks,
                    ratio=page.redaction_ratio,
                    codes=list(page.exemptions),
                    bates=page.bates,
                )
            )
    return out


@dataclass(slots=True)
class Selection:
    """What was drawn, and what could not be."""

    cells: list[Cell]
    total_cells: int
    total_marks: int
    drawn_marks: int
    drawn_area: float
    total_area: float

    @property
    def dropped_cells(self) -> int:
        return self.total_cells - len(self.cells)

    @property
    def dropped_marks(self) -> int:
        return self.total_marks - self.drawn_marks

    @property
    def complete(self) -> bool:
        return not self.dropped_cells and not self.dropped_marks

    @property
    def area_share(self) -> float:
        return self.drawn_area / self.total_area if self.total_area else 1.0


def _select(cells: list[Cell]) -> Selection:
    """Fit the field inside its ceilings, keeping the most that was withheld.

    When a release is too big to draw whole, the pages that stay are the ones
    with the most taken out of them, and within them the largest rectangles.
    That is a real bias and it is stated on the page: what a reader is looking
    at is the substance of the withholding, not a sample of it.
    """
    total_cells = len(cells)
    total_marks = sum(len(c.marks) for c in cells)
    total_area = sum(m.share for c in cells for m in c.marks)

    kept = cells
    if total_cells > CELL_LIMIT:
        ranked = sorted(
            range(total_cells),
            key=lambda i: (-cells[i].share, cells[i].doc_id, cells[i].number),
        )[:CELL_LIMIT]
        keep = set(ranked)
        kept = [c for i, c in enumerate(cells) if i in keep]

    marks_kept = sum(len(c.marks) for c in kept)
    if marks_kept > RECT_LIMIT:
        # Rank every surviving box by area and keep the largest. Ties break on
        # position so two identical boxes always resolve the same way.
        ranked = sorted(
            ((c_i, m_i) for c_i, c in enumerate(kept) for m_i in range(len(c.marks))),
            key=lambda pair: (
                -kept[pair[0]].marks[pair[1]].share,
                kept[pair[0]].doc_id,
                kept[pair[0]].number,
                kept[pair[0]].marks[pair[1]].y,
                kept[pair[0]].marks[pair[1]].x,
            ),
        )[:RECT_LIMIT]
        by_cell: dict[int, set[int]] = {}
        for c_i, m_i in ranked:
            by_cell.setdefault(c_i, set()).add(m_i)
        trimmed: list[Cell] = []
        for c_i, cell in enumerate(kept):
            wanted = by_cell.get(c_i)
            if not wanted:
                continue
            cell.marks = [m for m_i, m in enumerate(cell.marks) if m_i in wanted]
            trimmed.append(cell)
        kept = trimmed

    drawn_marks = sum(len(c.marks) for c in kept)
    drawn_area = sum(m.share for c in kept for m in c.marks)
    return Selection(
        cells=kept,
        total_cells=total_cells,
        total_marks=total_marks,
        drawn_marks=drawn_marks,
        drawn_area=drawn_area,
        total_area=total_area,
    )


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Scale:
    """How big a page is on screen, and how many fit across.

    Every cell has the same *area*, not the same width: that is what makes a
    rectangle's area on screen its share of the page it came from, whether the
    page was Letter, A4 or a fold-out. Pages of an unusual shape keep their
    shape and their area, and the grid slot grows to hold them.
    """

    cols: int
    slot_w: float
    slot_h: float
    unit_w: float
    unit_h: float
    """The dominant page's cell, which is also the ruler the packed
    arrangements draw with, so a rectangle is the same size in all of them."""

    cell_area: float
    odd_pages: int = 0


def _scale(cells: list[Cell]) -> Scale:
    count = max(1, len(cells))
    # The gutter is part of what a column costs, so both bounds are written
    # against (cell + gap); without that the widest field ends up with cells a
    # fifth narrower than the floor this file claims to keep.
    cols = max(1, math.ceil(math.sqrt(count * 1.6)))
    cols = max(cols, math.ceil((FIELD_WIDTH + CELL_GAP) / (MAX_CELL_W + CELL_GAP)))
    cols = min(cols, max(1, math.floor((FIELD_WIDTH + CELL_GAP) / (MIN_CELL_W + CELL_GAP))))
    cols = max(1, cols)

    slot_w = (FIELD_WIDTH - (cols - 1) * CELL_GAP) / cols
    dominant = _median([c.aspect for c in cells], 1.294)

    # Every cell has the same area; the question is which page gets to be
    # exactly one column wide. It has to be the widest one, or that page
    # overruns its column and the grid walks off the edge of the field. But not
    # at any price: one fold-out map among a thousand letter pages would set
    # the scale for all of them, so the base stops a third short of the
    # ordinary page and anything wider than that is scaled to fit and counted.
    # The cost of that floor is a slightly smaller picture, applied to every
    # page equally, which distorts no comparison in it.
    widest = min((c.aspect for c in cells), default=dominant)
    base = max(widest, dominant / 1.35)
    cell_area = slot_w * slot_w * base
    unit_w = slot_w
    unit_h = slot_w * dominant

    widths, heights = [], []
    for cell in cells:
        cell.w = math.sqrt(cell_area / cell.aspect)
        cell.h = math.sqrt(cell_area * cell.aspect)
        widths.append(cell.w)
        heights.append(cell.h)

    # The row is as tall as its tallest page, so a legal-size page among letter
    # ones simply makes the row taller - vertical space costs nothing. Width is
    # not free: the columns were divided out of a fixed field, so the slot can
    # never grow sideways or the grid runs off the edge of the picture. A page
    # wider than a column, and any page taller than about two rows' worth, is
    # scaled down to fit and counted, because that page's rectangles are then
    # true to each other but no longer to the rest of the field.
    slot_h = min(max(heights, default=unit_h), unit_h * 2.2)
    odd = 0
    for cell in cells:
        fit = min(1.0, slot_w / cell.w, slot_h / cell.h)
        if fit < 0.999:
            cell.w *= fit
            cell.h *= fit
            odd += 1
    return Scale(
        cols=cols,
        slot_w=slot_w,
        slot_h=slot_h,
        unit_w=unit_w,
        unit_h=unit_h,
        cell_area=cell_area,
        odd_pages=odd,
    )


def _lay_out_pages(
    cells: list[Cell], scale: Scale, *, bands: bool
) -> tuple[list[tuple[str, float]], float]:
    """Place every cell in the grid, banded by document. Returns the captions."""
    captions: list[tuple[str, float]] = []
    y = 0.0
    index = 0
    while index < len(cells):
        doc_id = cells[index].doc_id
        if bands:
            stop = index
            while stop < len(cells) and cells[stop].doc_id == doc_id:
                stop += 1
            run = cells[index:stop]
        else:
            run = cells[index:]
        if bands:
            captions.append((cells[index].doc_title, y))
            y += BAND_LABEL
        for offset, cell in enumerate(run):
            col = offset % scale.cols
            row = offset // scale.cols
            # Cells sit at the bottom of their slot, so the foot of every page
            # lands on one line and a run of pages reads as a shelf.
            cell.x = col * (scale.slot_w + CELL_GAP) + (scale.slot_w - cell.w) / 2
            cell.y = y + row * (scale.slot_h + CELL_GAP) + (scale.slot_h - cell.h)
        rows = math.ceil(len(run) / scale.cols)
        y += rows * scale.slot_h + max(0, rows - 1) * CELL_GAP
        y += BAND_GAP if bands else CELL_GAP
        index += len(run)
    return captions, max(0.0, y - (BAND_GAP if bands else CELL_GAP))


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------


def _class_for(code: str, order: dict[str, int]) -> str:
    return f"c{order.get(code, len(order))}"


def _page_field(
    cells: list[Cell],
    scale: Scale,
    order: dict[str, int],
    *,
    t: Translator,
    label: str,
    detail: bool,
    bands: bool,
) -> Field:
    captions, height = _lay_out_pages(cells, scale, bands=bands)

    # Every page's paper is one path rather than one rect each: it is the same
    # shape a thousand times over and there is no reason to pay for it twice.
    mounts: list[str] = []
    for cell in cells:
        mounts.append(
            f"M{_num(cell.x)} {_num(cell.y)}h{_num(cell.w)}v{_num(cell.h)}h-{_num(cell.w)}z"
        )

    parts: list[str] = [f'<path class="negative__paper" d="{"".join(mounts)}"/>']
    rects = 0
    for cell in cells:
        parts.append(f'<a href="{escape(cell.url)}" tabindex="-1">')
        if detail:
            for mark in cell.marks:
                parts.append(
                    f'<rect class="{_class_for(mark.code, order)}"'
                    f' x="{_num(cell.x + mark.x * cell.w)}"'
                    f' y="{_num(cell.y + mark.y * cell.h)}"'
                    f' width="{_num(mark.w * cell.w)}"'
                    f' height="{_num(mark.h * cell.h)}"/>'
                )
                rects += 1
        else:
            # One path per code on this page. Half the bytes of a rect each,
            # and the class is what a filter needs, so nothing is lost but the
            # per-rectangle tooltip - which the list below carries anyway.
            grouped: dict[str, list[str]] = {}
            for mark in cell.marks:
                grouped.setdefault(mark.code, []).append(
                    f"M{_num(cell.x + mark.x * cell.w)} {_num(cell.y + mark.y * cell.h)}"
                    f"h{_num(mark.w * cell.w)}v{_num(mark.h * cell.h)}"
                    f"h-{_num(mark.w * cell.w)}z"
                )
                rects += 1
            for code in sorted(grouped, key=lambda c: order.get(c, len(order))):
                parts.append(
                    f'<path class="{_class_for(code, order)}" d="{"".join(grouped[code])}"/>'
                )
        parts.append("</a>")

    for title, y in captions:
        parts.append(
            f'<text class="negative__band" x="0" y="{_num(y + BAND_LABEL - 11)}">'
            f"{escape(_truncate(title))}</text>"
        )

    return Field(
        id="page",
        name=str(t("negative.arrange_page")),
        caption=str(t("negative.arrange_page_caption")),
        svg=_svg("page", parts, height, label, scale.cell_area),
        height=height,
        rects=rects,
        label=label,
    )


def _packed_field(
    groups: list[tuple[str, str, list[tuple[Cell, Mark]]]],
    scale: Scale,
    order: dict[str, int],
    *,
    field_id: str,
    name: str,
    caption: str,
    label: str,
) -> Field:
    """Bands of rectangles at true size, packed into rows.

    Position is gone here - a rectangle has left its page - and size is all
    that is left, which is the point. Set beside each other at the same scale
    as the page field, the difference between an exemption used to remove a
    name and one used to remove a chapter is a difference you can see without
    reading a number.
    """
    parts: list[str] = []
    y = 0.0
    rects = 0
    for heading, _key, items in groups:
        if not items:
            continue
        parts.append(
            f'<text class="negative__band" x="0" y="{_num(y + BAND_LABEL - 11)}">'
            f"{escape(heading)}</text>"
        )
        y += BAND_LABEL
        x = 0.0
        row_h = 0.0
        open_url: str | None = None
        for cell, mark in items:
            w = mark.w * scale.unit_w
            h = mark.h * scale.unit_h
            if x > 0 and x + w > FIELD_WIDTH:
                y += row_h + PACK_GAP
                x, row_h = 0.0, 0.0
            # A run of rectangles cut out of the same page shares one link.
            # Two boxes the same size on the same page sort next to each other,
            # so this is not a rare case, and a link is longer than the
            # rectangle it wraps.
            if cell.url != open_url:
                if open_url is not None:
                    parts.append("</a>")
                parts.append(f'<a href="{escape(cell.url)}" tabindex="-1">')
                open_url = cell.url
            parts.append(
                f'<rect class="{_class_for(mark.code, order)}"'
                f' x="{_num(x)}" y="{_num(y)}"'
                f' width="{_num(w)}" height="{_num(h)}"/>'
            )
            rects += 1
            x += w + PACK_GAP
            row_h = max(row_h, h)
        if open_url is not None:
            parts.append("</a>")
            open_url = None
        y += row_h + BAND_GAP
    height = max(0.0, y - BAND_GAP)
    return Field(
        id=field_id,
        name=name,
        caption=caption,
        svg=_svg(field_id, parts, height, label, scale.cell_area),
        height=height,
        rects=rects,
        label=label,
    )


def _svg(field_id: str, parts: list[str], height: float, label: str, area: float) -> Markup:
    """Assemble one field.

    ``role="img"`` and a label: the picture is one object with one reading, and
    its four thousand children are not four thousand things to announce. The
    links inside it are ``tabindex="-1"`` for the same reason - see the index
    list under the field, which is the keyboard and screen-reader route in, and
    the note in ``negative.js`` about what the script adds on top.
    """
    # data-area is one page's area in the field's own units. Every page is
    # drawn at that area whatever shape it is, so a rectangle's area divided by
    # it is the rectangle's share of its page - which is what the tooltip says,
    # recovered from the drawing rather than shipped again beside it.
    attrs = Markup(
        'class="negative__field" viewBox="0 0 %s %s" role="img" aria-label="%s" '
        'data-negative="%s" data-area="%s" preserveAspectRatio="xMinYMin meet"'
    ) % (_num(FIELD_WIDTH), _num(max(height, 1.0)), label, field_id, _num(area))
    return Markup("<svg %s>%s</svg>") % (attrs, Markup("".join(parts)))


# --------------------------------------------------------------------------
# what the picture cannot show
# --------------------------------------------------------------------------


def _blind_spots(
    collection: Collection,
    cells: list[Cell],
    odd_pages: int = 0,
    *,
    t: Translator,
) -> list[dict[str, str]]:
    """The absences that leave no rectangle.

    A page about what is missing that quietly omits the parts of the missing it
    cannot draw would be the same failure it is documenting.
    """
    spots: list[dict[str, str]] = []

    gaps = [(doc, a, b) for doc in collection.documents for a, b in doc.bates_gaps]
    if gaps:
        pages = 0
        certain = True
        for _doc, a, b in gaps:
            first = "".join(ch for ch in a if ch.isdigit())
            last = "".join(ch for ch in b if ch.isdigit())
            # A gap is the inclusive run of numbers nobody delivered, so both
            # ends of it are pages that are not here.
            if first and last and int(last) >= int(first):
                pages += int(last) - int(first) + 1
            else:
                certain = False
        # The number of pages behind the gaps is a clause rather than a
        # number dropped into a slot: in Russian a numeral governs the case of
        # what follows it, so "47 pages were withheld whole" has to be able to
        # inflect as a whole. Where the stamps are not arithmetic enough to
        # count, the clause loses its number instead of guessing one.
        withheld_whole = (
            t("negative.blind_gaps_pages", count=pages)
            if pages and certain
            else t("negative.blind_gaps_pages_unknown")
        )
        spots.append(
            {
                "heading": str(t("negative.blind_gaps_heading")),
                "body": str(
                    t("negative.blind_gaps_body", count=len(gaps), pages=withheld_whole)
                ),
            }
        )

    full = sum(1 for cell in cells if cell.ratio >= 0.9)
    if full:
        spots.append(
            {
                "heading": str(t("negative.blind_solid_heading")),
                "body": str(
                    t(
                        "negative.blind_solid_body",
                        count=full,
                        pages=t("count.pages", count=len(cells)),
                    )
                ),
            }
        )

    uncoded = sum(1 for cell in cells for m in cell.marks if not m.code)
    if uncoded:
        spots.append(
            {
                "heading": str(t("negative.blind_uncoded_heading")),
                "body": str(t("negative.blind_uncoded_body", count=uncoded)),
            }
        )

    dark = sum(
        1
        for doc in collection.documents
        for page in doc.pages
        if page.quality.verdict.is_failure
    )
    if dark:
        spots.append(
            {
                "heading": str(t("negative.blind_dark_heading")),
                "body": str(t("negative.blind_dark_body", count=dark)),
            }
        )

    unrendered = sum(
        1
        for doc in collection.documents
        for page in doc.pages
        if not page.images and page.quality.verdict is not PageVerdict.BLANK
    )
    if unrendered:
        spots.append(
            {
                "heading": str(t("negative.blind_unrendered_heading")),
                "body": str(t("negative.blind_unrendered_body", count=unrendered)),
            }
        )

    if odd_pages:
        spots.append(
            {
                "heading": str(t("negative.blind_odd_heading")),
                "body": str(t("negative.blind_odd_body", count=odd_pages)),
            }
        )

    spots.append(
        {
            "heading": str(t("negative.blind_invisible_heading")),
            "body": str(t("negative.blind_invisible_body")),
        }
    )
    return spots


# --------------------------------------------------------------------------
# the ledger under the picture
# --------------------------------------------------------------------------


def _code_rows(
    cells: list[Cell], labels: dict[str, str], order: dict[str, int], t: Translator
) -> list[dict[str, Any]]:
    """Per exemption: how many boxes, how big they are, how much they came to.

    This table is the finding in numbers, and it is here so that the finding
    does not depend on the picture, on colour, or on a script. "Exemption 5 is
    used 60 times and takes a third of a page each time; exemption 6 is used
    400 times and takes a line" is a sentence a reader can quote.
    """
    buckets: dict[str, list[Mark]] = {}
    for cell in cells:
        for mark in cell.marks:
            buckets.setdefault(mark.code, []).append(mark)

    rows = []
    for code, marks in buckets.items():
        shares = sorted(m.share for m in marks)
        total = sum(shares)
        rows.append(
            {
                "code": code,
                "inherited": sum(1 for m in marks if m.inherited),
                "slug": _code_slug(code),
                "klass": _class_for(code, order),
                "label": labels.get(
                    code, str(t("negative.no_code_label")) if not code else ""
                ),
                "count": len(marks),
                "total": total,
                "median": _median(shares, 0.0),
                "largest": shares[-1],
                "median_text": _share_text(_median(shares, 0.0), t),
                "total_text": _extent(total, t),
            }
        )
    rows.sort(key=lambda r: (-r["total"], order.get(r["code"], len(order))))
    return rows


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


def page_context(
    collection: Collection,
    *,
    root: str = "../../",
    t: Translator | None = None,
) -> dict[str, Any]:
    """Everything ``negative.html.jinja`` needs, and nothing it does not.

    *t* is the build's translator. It is keyword-only and defaults to English
    so that a caller that predates the catalogue - a test, a script, anything
    that only wanted the picture - keeps working and keeps producing exactly
    the sentences it produced before.
    """
    t = t or translator_for(None)
    cells = _cells(collection, root)
    selection = _select(cells)
    drawn = selection.cells

    codes_present = sorted({m.code for c in drawn for m in c.marks if m.code})
    legend = exemptions_mod.legend(codes_present, jurisdiction=collection.jurisdiction)
    labels = dict(legend)
    order = {code: i for i, (code, _label) in enumerate(legend)}
    order[NO_CODE] = len(order)

    fields: list[Field] = []
    scale: Scale | None = None
    if drawn:
        scale = _scale(drawn)
        detail = selection.drawn_marks <= DETAIL_LIMIT
        bands = len({c.doc_id for c in drawn}) <= DOC_LABEL_LIMIT
        fields.append(
            _page_field(
                drawn,
                scale,
                order,
                t=t,
                label=_field_label(selection, drawn, t),
                detail=detail,
                bands=bands,
            )
        )

        if selection.drawn_marks <= PACKED_LIMIT:
            fields.append(_by_code(drawn, scale, order, labels, t))
            fields.append(_by_size(drawn, scale, order, t))

    total_pages = collection.stats.pages or sum(d.page_count for d in collection.documents)
    return {
        "nav": "withheld",
        "wrap": "",
        "page_title": str(t("negative.heading")),
        "page_description": str(
            t(
                "negative.description",
                count=selection.total_marks,
                collection=collection.title,
            )
        ),
        "fields": fields,
        "selection": selection,
        "cells": drawn[:LIST_LIMIT],
        "cells_listed": min(len(drawn), LIST_LIMIT),
        "code_rows": _code_rows(drawn, labels, order, t),
        "blind_spots": _blind_spots(
            collection, drawn, scale.odd_pages if scale else 0, t=t
        ),
        "total_pages": total_pages,
        "counted_boxes": collection.stats.redaction_boxes,
        "withheld_url": f"{root}withheld/index.html",
        "limits": LIMITS,
        "detail": bool(drawn) and selection.drawn_marks <= DETAIL_LIMIT,
        "packed": bool(drawn) and selection.drawn_marks <= PACKED_LIMIT,
    }


def _field_label(selection: Selection, drawn: list[Cell], t: Translator) -> str:
    """The whole picture read as one sentence, for somebody who cannot see it.

    Three sentences joined by a space rather than one sentence assembled out of
    fragments: the count of pages and the count of documents arrive already
    pluralised and already written with this locale's separators, and the frame
    around them is one message a translator can reorder entirely.
    """
    docs = len({c.doc_id for c in drawn})
    parts = [
        str(
            t(
                "negative.label_page",
                count=selection.drawn_marks,
                pages=t("count.pages", count=len(drawn)),
                documents=t("count.documents", count=docs),
            )
        )
    ]
    if not selection.complete:
        parts.append(
            str(t("negative.label_page_dropped", count=selection.dropped_cells))
        )
    parts.append(str(t("negative.label_page_tail")))
    return " ".join(parts)


def _by_code(
    cells: list[Cell],
    scale: Scale,
    order: dict[str, int],
    labels: dict[str, str],
    t: Translator,
) -> Field:
    buckets: dict[str, list[tuple[Cell, Mark]]] = {}
    for cell in cells:
        for mark in cell.marks:
            buckets.setdefault(mark.code, []).append((cell, mark))

    uncited = str(t("withheld.no_code_printed"))
    groups = []
    for code, items in buckets.items():
        total = sum(m.share for _c, m in items)
        heading = str(
            t(
                "negative.band_code",
                code=code or uncited,
                count=len(items),
                extent=_extent(total, t),
            )
        )
        groups.append((total, order.get(code, len(order)), heading, code, items))
    groups.sort(key=lambda g: (-g[0], g[1]))

    return _packed_field(
        [(heading, code, items) for _t, _o, heading, code, items in groups],
        scale,
        order,
        field_id="code",
        name=str(t("negative.arrange_code")),
        caption=str(t("negative.arrange_code_caption")),
        label=str(
            t(
                "negative.label_code",
                groups=str(t("negative.label_code_join")).join(
                    str(
                        t(
                            "negative.label_code_group",
                            code=code or uncited,
                            count=len(items),
                        )
                    )
                    for _t, _o, _h, code, items in groups
                ),
            )
        ),
    )


def _by_size(
    cells: list[Cell], scale: Scale, order: dict[str, int], t: Translator
) -> Field:
    """Every rectangle, largest first, in one run.

    The shape of this one is the distribution: a few slabs at the top, then a
    long grey drift of names and dates. A release where that drift is missing
    is a release where somebody was withholding whole paragraphs.
    """
    items = sorted(
        ((cell, mark) for cell in cells for mark in cell.marks),
        key=lambda pair: (-pair[1].share, pair[0].doc_id, pair[0].number, pair[1].y),
    )
    biggest = items[0][1].share if items else 0.0
    smallest = items[-1][1].share if items else 0.0
    largest = _percent(biggest, t)
    smallest_text = _percent(smallest, t)
    return _packed_field(
        [(str(t("negative.band_size")), "size", items)],
        scale,
        order,
        field_id="size",
        name=str(t("negative.arrange_size")),
        caption=str(
            t("negative.arrange_size_caption", largest=largest, smallest=smallest_text)
        ),
        label=str(
            t(
                "negative.label_size",
                count=len(items),
                largest=largest,
                smallest=smallest_text,
            )
        ),
    )


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def build(builder: Any, *, t: Translator | None = None) -> None:
    """Render the page. Called once from :meth:`SiteBuilder.run`.

    It takes the builder rather than a collection because the builder owns the
    output path, the template environment and the shared context - and because
    a second module that knows how to write files is a second module that can
    disagree with the first about where the site root is.

    The translator comes the same way, from the builder, with an explicit *t*
    for a caller that has one and no builder. Defaulting to the builder's own
    means the picture and the page around it can never end up in two different
    languages.
    """
    builder.render(
        "negative.html.jinja",
        "withheld/negative/index.html",
        **page_context(
            builder.collection,
            root="../../",
            t=t if t is not None else getattr(builder, "t", None),
        ),
    )
