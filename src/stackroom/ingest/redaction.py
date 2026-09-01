"""Redactions: the ones that worked, and the ones that only look like they did.

Two jobs, and they are not equally important.

**Hidden text** is text a redaction failed to remove: a black rectangle painted
over characters that are still in the content stream, still selectable, still
copyable, still in every mirror of the file forever. Publishing a page like
that does not merely fail to protect a source, it *advertises* which words were
worth protecting and hands them over. People have been hurt by this. A missed
one is the worst bug this program can have, so when the evidence is ambiguous
this module flags and lets a human look. A false positive costs an operator ten
minutes; a false negative can cost somebody rather more.

**Visible redactions** are the boxes that did work. Finding them is how the
archive can say "37% of this page was withheld" and draw the ledger, so here
precision is what matters: a page furniture rule counted as a redaction makes
every number in the ledger a lie. Nothing bad happens if we miss one.

Method
------
The hidden-text algorithm is a re-implementation of Free Law Project's
``x-ray`` (BSD), which is the reference work on this problem. Their code is
built on PyMuPDF, which is AGPL and cannot be used here (see
``docs/ARCHITECTURE.md``), so this is the same reasoning rebuilt on the
pdfplumber/pdfminer objects that ``ingest/pdf.py`` hands us, plus one addition:
the box is *rendered* and the pixels are checked before anything is reported.

The steps, and why each one is there:

1. Consider only opaque filled rectangles taller and wider than 4pt. Table
   rules, underlines and cell borders are all thin in one dimension; requiring
   both dimensions kills them without a single special case.
2. Use the individual ``re`` sub-rectangles of a path, never the enclosing
   drawing's bounding box. A three-line redaction drawn as one path has an
   outer bbox that spans visible text between the lines, and that bbox
   generates false positives on text nobody hid. ``ingest/pdf.py`` owes us the
   sub-rectangles; this module cannot tell the difference and would be wrong.
3. A character is *covered* when at least 80% of its area is inside the
   rectangle, and *hidden* when it is covered and either the content stream
   paints it before the rectangle, or it is the same colour as the rectangle
   (black on black is invisible whatever the order).
4. Throw away the strings that are not text: runs of one repeated character,
   strings with no word characters at all, the words "confidential",
   "privileged" and "redacted" printed under their own box, and - if it is true
   of every box on the page - dates, which are boxes on a form rather than
   somebody's name.
5. Render the box and look - **at each character's own footprint**. A glyph a
   reader cannot see is a glyph whose own cell has nothing in it but box
   colour; a glyph reversed out of the box in white is the most textured thing
   on the page. Whether the *rectangle* is flat is a different question with a
   different answer, and the two only agree when the rectangle is small.

Step 5 is the one that turns a noisy heuristic into something you can put in
front of an operator, and it is also why step 3's draw-order test is not
load-bearing: if the pixels where a character is are flat and the file says a
character is there, the text is hidden no matter which order it was painted in.

Why per character and not per box
---------------------------------
Because ``std`` and a percentile span are *shares*, and a rectangle can be
arbitrarily larger than the lettering on it. ``(b)(5) PAGE WITHHELD IN FULL``
set in 10pt across a page-sized black box - the single most ordinary thing in a
FOIA production, printed on every page an agency has withheld end to end -
covers 0.2% of that box, and reads std 10.5 and spread 0 at 150 dpi. Both are
inside the cuts a photocopied black block needs, so the box was judged uniform
and every character the file reported inside it was called invisible: the
loudest thing this program can say, fired on a page that is exactly right. An
operator who is shown a leak on an obviously-fine page stops believing the
next one, which is worse than the false alarm itself.

So :meth:`_Crop.box_is_flat` no longer decides. It answers only for characters
whose own cell could not be measured, and only while no character contradicts
it - see :func:`_scan_hidden`. Nothing is lost by that: a character whose cell
is flat is still hidden, whatever the rest of the rectangle looks like, which
is how a leak under a *labelled* box was already being found.

Coordinates and units
---------------------
Everything this module returns is in ``model.Box`` page-relative units.
Internally, thresholds that came from the reference implementation are in
PostScript points (the 4pt minimum, the 43pt header band) and thresholds that
came from measuring rendered pages are in pixels at 150 dpi, scaled for other
resolutions. Both are labelled at the point of use.

This module runs no subprocesses and opens no files. Rendering is injected as
``crop_renderer``, a callable taking a page-relative :class:`~stackroom.model.Box`
and returning the pixels of exactly that region of the page - in practice
``functools.partial(raster.render_page_crop, path, page_number)``. That keeps
the decisions here testable against a handful of arrays.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import numpy as np

from ..imaging import binarise, connected_components, is_uniform, to_gray, uniformity
from ..model import Box, HiddenText, Redaction, RedactionKind

__all__ = [
    "NO_DRAW_ORDER",
    "CharLike",
    "PageLike",
    "RectLike",
    "RedactionFindings",
    "analyse_page",
    "content_area",
    "find_hidden_text",
    "find_visible_redactions",
    "redaction_ratio",
]


# --------------------------------------------------------------------------
# thresholds
#
# Every number below is either quoted from the reference implementation or
# measured on a synthetic page; none of them are guesses, and none of them
# should be changed without a page to measure the change on.
# --------------------------------------------------------------------------

COVER_FRACTION = 0.8
"""Share of a character's own area that must be inside a rectangle.

From x-ray. Below this you start catching the characters on the line above,
whose descenders clip the top of the box."""

MIN_RECT_PT = 4.0
"""A candidate rectangle must exceed this in *both* dimensions, in points.

From x-ray. This is the single most effective filter in the file: it removes
table rules, cell borders, underlines and hairlines - anything long and thin -
without knowing anything about them. The table rule our own test fixture draws
on every page is 468 x 0.9pt."""

HEADER_BAND_PT = 43.0
"""Rectangles lying entirely within this many points of the page top are
running headers, not redactions.

From x-ray, which drops any rectangle whose top edge is above this line. We
require the *whole* rectangle to be in the band instead, because "top edge
above 43pt" also discards a genuine 300pt-tall redaction that happens to start
high on the page, and discarding a genuine redaction is the failure mode this
module exists to prevent. The blind spot that remains - a redaction that lives
entirely in the top 43pt, e.g. a name blacked out of a letterhead - is
inherited from the reference implementation and is documented in the module
tests."""

OPAQUE = 0.99
"""Minimum fill opacity for a rectangle to hide anything.

A rectangle we cannot see through is the whole premise. Where the PDF does not
say (no ``opacity`` on the object at all) we assume it is opaque, because
assuming otherwise silently drops candidates."""

COLOUR_TOLERANCE = 0.05
"""Per-channel distance at which two fills count as the same ink.

Exact float equality is the wrong test: the same black reaches us as
``(0.0, 0.0, 0.0)`` from an RGB rect and ``(0,)`` from a DeviceGray char, and
producers round. 0.05 is about 13 levels out of 255 - well inside "you cannot
see the difference", well outside any deliberate contrast."""

MAX_UNIFORM_STD = 12.0
MAX_UNIFORM_SPREAD = 25.0
"""Flatness limits for a rendered patch, in 8-bit grey levels.

Measured at 150 dpi: a true redaction box reads std 0.0 / spread 0; a dark
photograph reads std 14.4 / spread 50. Toner grain on a photocopied black block
sits around std 8, which is why the cut is not tighter."""

RIM_PX = 1
"""Pixels of a rendered crop that belong to the page rather than to the box.

``render_page_crop`` rounds its crop outwards, so the outermost row and column
of the image can straddle the edge of the rectangle and hold the paper behind
it. One pixel is the whole of that overhang - the rounding is to the nearest
pixel boundary, not further - and it matters because the per-character test in
:meth:`_Crop.hides` is measured on cells that may sit flush with that edge.
Measured on a real 150 dpi rendering, shifting a 40pt box by two thirds of a
pixel moved a buried glyph's cell from std 0.0 to std 56.9, i.e. from "hidden"
to "legible", with nothing about the document changed."""

NEAR_BLACK = 60
"""Grey level at or below which a rendered pixel counts as part of a black box.

Not Otsu: Otsu asks "what is ink on this page", and grey toner is ink. A
redaction is not merely darker than the paper, it is black. 60 leaves room for
a photocopied black that has drifted up from 0."""

MIN_SOLIDITY = 0.92
"""Set pixels over bounding-box pixels for a raster redaction candidate.

Measured: a true box reads 1.00, the black border of a skewed scan reads 0.02.
Nothing else in the filter chain rejects that border, and every scanned
production has one."""

MIN_BOX_W_PX = 25
MIN_BOX_H_PX = 10
"""Minimum size of a raster redaction candidate, in pixels at 150 dpi.

10px at 150 dpi is 4.8pt, about the cap height of 7pt type: smaller than that
and there is nothing to hide. Scaled for other resolutions by the caller's
``dpi``."""

ASPECT_MIN = 0.8
ASPECT_MAX = 60.0
"""Width over height for a raster redaction candidate.

Redactions are drawn over lines of text, so they are wider than they are tall,
or nearly square when a paragraph goes. The upper bound admits a full-width
box on a letter page (about 45:1) and excludes a page rule."""

MAX_PAGE_FRACTION = 0.40
"""A candidate covering more of the page than this is the scan, not a box.

A page withheld in full arrives as a blank page with a Bates number, not as a
900 x 1100pt black rectangle. Inverted scans and dark photographs do arrive
that way."""

MAX_BOX_MEAN = 70
"""Mean grey level inside a raster candidate. Above this it is a grey panel."""

DARK_FILL = MAX_BOX_MEAN / 255.0
"""Luma at or below which a *vector* fill counts as dark enough to be a box.

Deliberately the same number as :data:`MAX_BOX_MEAN`, converted to the 0..1
scale colours arrive in. The same physical rectangle has to be classified the
same way whether we found it in the content stream or in the pixels of a
render; two different cuts would have ``analyse_page`` disagree with itself on
a hybrid page, counting a grey fill as a redaction and then not counting the
grey block it renders into."""

REFERENCE_DPI = 150
"""The resolution the pixel thresholds above were measured at."""

RATIO_GRID = 800
"""Cells across the page when measuring the redaction ratio by area.

One cell is 0.77pt on a letter page - a tenth of a character - and the whole
grid is under a megabyte. See :func:`redaction_ratio` for why this is done as
an area union and not with bounding boxes."""

NO_DRAW_ORDER = -1
"""The ``seq`` a caller sends when it could not recover the draw order.

This mirrors :data:`stackroom.ingest.pdf.NO_ZORDER`, which this module will not
import: nothing here may depend on pdfminer being installed, and the two files
would then be circular in spirit if not in fact. The *number* is not the
contract. A position in a content stream is an index into that stream and is
never negative, so **any negative ``seq`` is read here as "unknown"** - which
means a caller that invents its own sentinel is still understood, and a caller
that forgets to check cannot have -1 read as "painted first".

Getting this wrong is not a cosmetic bug. :func:`_stream_hides` asks whether the
character was painted *before* the rectangle; with every shape stamped -1 the
comparison ``char.seq < rect.seq`` is False for every pair, so the draw-order
test silently answers "no" for the whole page - and the warning that exists to
say "we could only use colour here" never fires, because it was looking for
``None``. See ``test_a_page_stamped_with_the_no_zorder_sentinel_is_judged_as
_having_no_draw_order``."""


# --------------------------------------------------------------------------
# what this module needs from ingest/pdf.py
#
# Deliberately structural. `ingest/pdf.py` owns the real RawPage/RawChar/
# RawRect; this module is not going to import it (nothing here should depend on
# pdfminer being importable) and is not going to grow an opinion about the
# names of its fields beyond the ones it actually reads.
# --------------------------------------------------------------------------


class RectLike(Protocol):
    """A filled shape from the content stream, in PDF points from the top-left."""

    x0: float
    top: float
    x1: float
    bottom: float
    seq: int
    """Position in the content stream. Higher means painted later, i.e. on top."""


class CharLike(Protocol):
    """One glyph from the content stream, in PDF points from the top-left."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    seq: int


class PageLike(Protocol):
    """What :func:`analyse_page` needs: a size, some glyphs, some shapes."""

    width: float
    height: float
    chars: Sequence[CharLike]
    rects: Sequence[RectLike]


# Field aliases. The left-hand name is what we prefer; the rest are what
# pdfplumber dicts, model.Page and a plausible RawRect happen to call the same
# thing. Reading through this table rather than a fixed attribute name is what
# lets the tests drive this module with three-line stand-ins.
#
# An alias table is a hazard as well as a convenience: it means a rename in
# `ingest/pdf.py` stops being a crash and becomes a shrug. Two things stop that
# here, and both are needed because they fail at different times:
#
#   * Geometry has no default, so a rect that carries none of `_X0` raises
#     immediately - a rename of `x0` cannot be quiet.
#   * Everything else does have a default, so the miss is silent by
#     construction. For the one that matters - the fill colour, which is half
#     of the hidden-text test - :func:`_has_field` records whether the *field
#     was there at all*, separately from whether it held a colour, and
#     :func:`_scan_hidden` reports the difference. "Unknown colour space" is an
#     honest None; "no colour field on the object" is a broken contract, and
#     they must not look alike.
#
# ``test_the_field_names_ingest_pdf_uses_are_the_ones_this_module_reads`` pins
# the table against the real ``RawRect``/``RawChar``, so a rename fails there
# too, before anybody builds anything.
_X0 = ("x0", "left")
_X1 = ("x1", "right")
_TOP = ("top", "y0_top")
_BOTTOM = ("bottom",)
_SEQ = ("seq", "index", "order", "z")
_COLOUR = ("fill_colour", "fill_color", "colour", "color", "non_stroking_color")
_OPACITY = ("opacity", "alpha", "fill_alpha")
_WIDTH = ("width_pt", "width", "page_width")
_HEIGHT = ("height_pt", "height", "page_height")
_ORDERED = ("has_zorder", "has_draw_order")

_MISSING = object()


def _field(obj: Any, names: Sequence[str], default: Any = _MISSING) -> Any:
    """First of *names* that *obj* actually carries, by key or by attribute.

    ``None`` counts as absent, because a dataclass that defaults a colour to
    ``None`` means "I do not know", not "no colour". Use :func:`_has_field`
    where the difference between the two matters.
    """
    for name in names:
        value = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if value is not None:
            return value
    if default is _MISSING:
        raise AttributeError(
            f"{type(obj).__name__} carries none of {names!r}; "
            "ingest/pdf.py must provide it"
        )
    return default


def _has_field(obj: Any, names: Sequence[str]) -> bool:
    """Does *obj* carry any of *names* at all, whatever its value?

    The question :func:`_field` cannot answer. An object that has
    ``fill_color = None`` is telling us it could not name the colour; an object
    with no colour field at all is telling us this module is reading a shape it
    does not understand, and only one of those is a reason to warn a person.
    """
    if isinstance(obj, Mapping):
        return any(name in obj for name in names)
    return any(hasattr(obj, name) for name in names)


# --------------------------------------------------------------------------
# normalised internal view
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Shape:
    """A rectangle or a glyph, in both unit systems, with its paint order."""

    box: Box
    """Page-relative, which is what we return."""

    top_pt: float
    width_pt: float
    height_pt: float
    """Points, which is what the 4pt and 43pt thresholds are in."""

    seq: int | None
    """Position in the content stream, or ``None`` for *we do not know*.

    ``None`` is the only spelling of "unknown" inside this module.
    :func:`_shape` maps every sentinel it is handed - a missing field, or the
    negative :data:`NO_DRAW_ORDER` that ``ingest/pdf.py`` stamps on a page whose
    draw order it could not recover - onto it, so that no test downstream has to
    remember there is a sentinel."""

    colour: tuple[float, float, float] | None
    text: str = ""
    fill: bool = True
    opacity: float = 1.0
    """Defaults say "opaque fill". Where the PDF does not tell us, assuming a
    shape hides nothing is the assumption that loses source material."""

    colour_given: bool = True
    """Was there a colour *field* on the object this came from?

    False means the shape carried none of :data:`_COLOUR` - not that its colour
    was unknown, which is ``colour is None`` and is ordinary. It is the shape a
    renamed field in ``ingest/pdf.py`` takes, and :func:`_scan_hidden` says so
    out loud rather than quietly running half the hidden-text test."""


def _normalise_colour(raw: Any) -> tuple[float, float, float] | None:
    """Any of PDF's colour spellings as an RGB triple in 0..1, or None.

    DeviceGray arrives as a scalar or a 1-tuple, DeviceRGB as 3, DeviceCMYK as
    4, and some producers hand out 0..255 integers. Anything else - a separation
    colour, a pattern, a name - we decline to interpret rather than guess, and
    the same-colour test simply does not fire.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        values = [float(raw)]
    elif isinstance(raw, (list, tuple)):
        try:
            values = [float(v) for v in raw]
        except (TypeError, ValueError):
            return None
    else:
        return None

    if values and max(values) > 1.0:  # 0..255 producers
        values = [v / 255.0 for v in values]

    if len(values) == 1:
        g = values[0]
        return (g, g, g)
    if len(values) == 3:
        return (values[0], values[1], values[2])
    if len(values) == 4:
        c, m, y, k = values
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def _same_colour(a: tuple[float, float, float] | None, b: tuple[float, float, float] | None) -> bool:
    if a is None or b is None:
        return False
    return all(abs(u - v) <= COLOUR_TOLERANCE for u, v in zip(a, b, strict=True))


def _shape(obj: Any, page_w: float, page_h: float, *, text: str = "") -> _Shape:
    """Normalise one char or rect. Geometry is required; the rest is not."""
    existing = _field(obj, ("box",), None)
    if isinstance(existing, Box):
        box = existing
        top_pt, w_pt, h_pt = box.y * page_h, box.w * page_w, box.h * page_h
    else:
        x0 = float(_field(obj, _X0))
        x1 = float(_field(obj, _X1))
        top = float(_field(obj, _TOP))
        bottom = float(_field(obj, _BOTTOM))
        box = Box.from_pdf_rect(x0, top, x1, bottom, page_w, page_h)
        top_pt, w_pt, h_pt = top, x1 - x0, bottom - top

    # One spelling of "unknown" from here on. A content-stream position is an
    # index into that stream, so a negative one is a sentinel and never a real
    # answer: `ingest/pdf.py` stamps NO_ZORDER (-1) on every shape of a page it
    # had to read without draw order. Folding it into None here is what stops
    # `_stream_hides` comparing two sentinels and quietly concluding "no", and
    # what lets the "judged without a draw order" warning fire on that page.
    raw_seq = _field(obj, _SEQ, None)
    seq = int(raw_seq) if raw_seq is not None else None
    if seq is not None and seq < 0:
        seq = None
    return _Shape(
        box=box,
        top_pt=top_pt,
        width_pt=w_pt,
        height_pt=h_pt,
        seq=seq,
        colour=_normalise_colour(_field(obj, _COLOUR, None)),
        text=text,
        fill=bool(_field(obj, ("fill",), True)),
        opacity=float(_field(obj, _OPACITY, 1.0)),
        colour_given=_has_field(obj, _COLOUR),
    )


def _page_parts(page: Any) -> tuple[list[_Shape], list[_Shape], float, float]:
    """``(rects, chars, width_pt, height_pt)`` from anything page-shaped.

    A page may also say, once and for the whole page, that its draw order is not
    to be trusted: ``RawPage.has_zorder`` is False when ``ingest/pdf.py`` had to
    fall back to stock pdfminer, whose ordering is an implementation detail. We
    honour that here by dropping every ``seq`` on the page, which is belt as
    well as braces - :func:`_shape` already folds the per-shape sentinel into
    ``None`` - because the two would have to be changed together for the
    draw-order test to start lying again.
    """
    for name in ("rects", "chars"):
        if _field(page, (name,), None) is None and not hasattr(page, name):
            raise TypeError(
                f"{type(page).__name__} has no {name!r}; find_hidden_text needs the "
                "raw page from ingest/pdf.py, not a model.Page"
            )
    page_w = float(_field(page, _WIDTH, 612.0))
    page_h = float(_field(page, _HEIGHT, 792.0))
    rects = [_shape(r, page_w, page_h) for r in _field(page, ("rects",), []) or []]
    chars = [
        _shape(c, page_w, page_h, text=str(_field(c, ("text",), "")))
        for c in _field(page, ("chars",), []) or []
    ]
    if not bool(_field(page, _ORDERED, True)):
        rects = [replace(shape, seq=None) for shape in rects]
        chars = [replace(shape, seq=None) for shape in chars]
    return rects, chars, page_w, page_h


# --------------------------------------------------------------------------
# candidate rectangles
# --------------------------------------------------------------------------


def _is_candidate(rect: _Shape) -> bool:
    """Could this rectangle hide something?"""
    if not rect.fill:
        return False  # an unfilled outline hides nothing
    if rect.opacity < OPAQUE:
        return False  # see-through, so not a redaction
    if rect.width_pt <= MIN_RECT_PT or rect.height_pt <= MIN_RECT_PT:
        return False  # a rule, a border, an underline
    return not rect.top_pt + rect.height_pt <= HEADER_BAND_PT


def _candidates(rects: Iterable[_Shape]) -> list[_Shape]:
    return [shape for shape in rects if _is_candidate(shape)]


def _covers(rect: Box, char: Box) -> bool:
    """Is at least 80% of *char* inside *rect*?

    Degenerate glyphs - zero width or zero height, which some producers emit
    for spacing - have no area to take a fraction of, so they are judged by
    their centre point instead of being silently dropped.
    """
    if char.area > 0:
        return char.overlap_ratio(rect) > COVER_FRACTION
    cx, cy = char.x + char.w / 2, char.y + char.h / 2
    return rect.x <= cx <= rect.x2 and rect.y <= cy <= rect.y2


def _is_dark(colour: tuple[float, float, float] | None) -> bool:
    """Is this fill dark enough to obliterate whatever is under it?

    Rec. 601 luma, matching :func:`stackroom.imaging.to_gray`, so that the
    answer is the same as it would be for the rendered pixels of the same fill.
    An unknown colour - a pattern, a separation ink, a colour space we decline
    to guess at - is *not* dark: it goes in the ledger only if it can be shown
    to cover something, which is the conservative answer for a number the whole
    archive is judged on.
    """
    if colour is None:
        return False
    red, green, blue = colour
    return 0.299 * red + 0.587 * green + 0.114 * blue <= DARK_FILL


def _stream_hides(char: _Shape, rect: _Shape) -> bool:
    """Does the *file* say this character is invisible under this rectangle?

    Two ways, either sufficient: the rectangle is painted after the character,
    or the character is painted in the rectangle's own colour. The second
    matters because black-on-black text painted *after* the box is a common
    shape for this bug - the box goes down first and the text is dropped on top
    by a tool that thinks it is annotating.

    When the draw order is unknown (``ingest/pdf.py`` did not give us ``seq``)
    only the colour test can speak, and the caller records that in a warning
    rather than pretending the answer is no.
    """
    if char.seq is not None and rect.seq is not None and char.seq < rect.seq:
        return True
    return _same_colour(char.colour, rect.colour)


class _CharIndex:
    """Characters indexed by their top edge, so a rectangle only reads its band.

    Comparing every rectangle against every character is fine for a memo and
    quadratic for a form with three hundred filled cells, which is a document
    type this archive exists to publish.
    """

    def __init__(self, chars: Sequence[_Shape]) -> None:
        self._chars = sorted(chars, key=lambda c: c.box.y)
        self._tops = [c.box.y for c in self._chars]
        self._tallest = max((c.box.h for c in self._chars), default=0.0)

    def covered(self, rect: _Shape) -> list[_Shape]:
        """Every character at least 80% inside *rect*, in reading order.

        A character 80% inside starts no lower than the rectangle's bottom edge
        and no higher than one character-height above its top edge, so the
        window cannot drop one that :func:`_covers` would have kept.
        """
        window = self._chars[
            bisect_left(self._tops, rect.box.y - self._tallest) : bisect_right(
                self._tops, rect.box.y2
            )
        ]
        return [c for c in window if _covers(rect.box, c.box)]


# --------------------------------------------------------------------------
# raster confirmation
# --------------------------------------------------------------------------

CropRenderer = Callable[[Box], Any]
"""``(box) -> PIL.Image.Image``, rendering exactly that region of the page."""


def _flat(patch: np.ndarray) -> bool:
    """Is this patch one solid colour, by this module's two cuts?

    One place, so that the whole-box question and the per-character question
    cannot drift apart: they have to be the same test asked of two different
    areas, or comparing their answers - which is what :func:`_scan_hidden` does
    - would be comparing two different notions of flat.
    """
    return is_uniform(patch, max_std=MAX_UNIFORM_STD, max_spread=MAX_UNIFORM_SPREAD)



class _Crop:
    """The rendered pixels of one candidate rectangle.

    The renderer is asked for exactly the rectangle, so the mapping from
    page-relative coordinates into the crop is a straight linear scale. A
    caller that returns a padded or differently-cropped image will make this
    module wrong, which is why the contract is stated in the module docstring
    and again here.
    """

    def __init__(self, box: Box, image: Any) -> None:
        self.box = box
        self.gray = to_gray(image)
        self.h, self.w = self.gray.shape

    @property
    def usable(self) -> bool:
        """Enough pixels to say anything at all."""
        return self.w >= 4 and self.h >= 4

    def _patch(self, box: Box, inset: int = 0, *, keep_off_the_rim: bool = False) -> np.ndarray:
        """Pixels of a page-relative sub-box, clipped to the crop.

        *inset* shrinks the requested box by that many pixels on every side.
        *keep_off_the_rim* does something different and only to the sub-boxes
        that need it: it clamps the slice to the interior of the crop, so a
        sub-box flush with the edge of the rectangle cannot reach the outermost
        row or column. That is where the renderer's rounding puts paper - see
        :data:`RIM_PX` - and a sub-box that does not touch the edge is not
        touched at all, which is the difference between this and an inset.
        """
        if self.box.w <= 0 or self.box.h <= 0:
            return self.gray[:0, :0]
        fx0 = (box.x - self.box.x) / self.box.w
        fx1 = (box.x2 - self.box.x) / self.box.w
        fy0 = (box.y - self.box.y) / self.box.h
        fy1 = (box.y2 - self.box.y) / self.box.h
        rim = RIM_PX if keep_off_the_rim and min(self.w, self.h) > 4 * RIM_PX else 0
        x0 = max(rim, int(np.floor(fx0 * self.w)) + inset)
        x1 = min(self.w - rim, int(np.ceil(fx1 * self.w)) - inset)
        y0 = max(rim, int(np.floor(fy0 * self.h)) + inset)
        y1 = min(self.h - rim, int(np.ceil(fy1 * self.h)) - inset)
        if x1 <= x0 or y1 <= y0:
            return self.gray[:0, :0]
        return self.gray[y0:y1, x0:x1]

    def box_is_flat(self) -> bool:
        """Is the whole rectangle one flat colour?

        Measured a pixel or two in from the edge: a renderer antialiases the
        boundary of the box against the paper behind it, and that rim is not
        part of the box.

        **This is a weak test and it is used as one.** ``std`` and the p95-p5
        spread are shares of the whole patch, so what they answer is "is the
        *typical* pixel in here the same as its neighbours", and lettering that
        covers a small enough share of a large enough rectangle does not move
        either of them: measured at 150 dpi, ``(b)(5) PAGE WITHHELD IN FULL``
        set in 10pt across a 512 x 600pt box reads std 10.5, spread 0 - flat,
        by these numbers, with the words plainly legible on the page. So this
        answer never decides a character that could be looked at directly; see
        :meth:`hides` and :func:`_scan_hidden`.
        """
        inset = max(1, round(0.02 * min(self.w, self.h)))
        patch = self._patch(self.box, inset=inset)
        if patch.size == 0:
            patch = self._patch(self.box)
        return _flat(patch)

    def hides(self, char: Box) -> bool | None:
        """Is this one character's own footprint flat? ``None`` if unmeasurable.

        This is the load-bearing question. Under a redaction a glyph's cell is
        solid box colour; under a highlight, or under lettering reversed out of
        a black box, the glyph is right there in the cell - a white stem beside
        a black counter is the most textured thing on the page - so asking each
        character about its own pixels separates the two whatever the box
        around them looks like.

        The cell is measured whole, with no inset: trimming a ring off it would
        also trim the two-pixel stem of a real glyph, and noticing real glyphs
        is the entire job. It is clamped off the crop's outermost pixels
        though, because those are not the box - ``render_page_crop`` rounds its
        crop outwards, so a cell flush with the edge of the rectangle otherwise
        catches a sliver of paper and a genuinely buried glyph reads as
        textured. Measured on a real 150 dpi rendering, moving the same box by
        two thirds of a pixel took one such cell from std 0.0 to std 56.9; with
        the rim excluded it stays at 0.0 wherever the box lands.
        """
        patch = self._patch(char, keep_off_the_rim=True)
        if patch.size < 4:
            return None
        return _flat(patch)


PAGE = Box(0.0, 0.0, 1.0, 1.0)
"""The whole page, for clamping crops."""


def _render(box: Box, renderer: CropRenderer | None, notes: list[str]) -> _Crop | None:
    """Render one box, or explain in *notes* why we could not.

    The box is clipped to the page before it is asked for. Every renderer in
    this project clamps its crop to the page - it has to, there are no pixels
    outside it - while ``ingest/pdf.py`` deliberately does *not* clamp
    rectangles, because content that bleeds past the media box is real and
    moving a redaction box away from what it covers would be worse. Those two
    correct decisions meet here: ask for a box that hangs off the page and the
    image that comes back covers less than what was asked for, and every
    coordinate this class maps into it is then wrong by the overhang. Asking
    only for the part that exists keeps the crop and the box the same rectangle.
    """
    if renderer is None:
        return None
    visible = box.intersection(PAGE)
    if visible is None:
        return None  # entirely off-page: there is nothing to look at
    box = visible
    try:
        image = renderer(box)
        if image is None:
            return None
        crop = _Crop(box, image)
    except Exception as exc:  # a broken renderer must never stop ingest
        notes.append(f"crop renderer failed on box {_where(box)}: {exc!r}")
        return None
    if not crop.usable:
        notes.append(f"crop for box {_where(box)} came back {crop.w}x{crop.h}px, too small to judge")
        return None
    return crop


def _where(box: Box) -> str:
    """A box as a human-quotable position, for warnings."""
    return f"({box.x * 100:.0f}%, {box.y * 100:.0f}%)"


# --------------------------------------------------------------------------
# text reconstruction and the junk filters
# --------------------------------------------------------------------------

_HAS_WORD_CHAR = re.compile(r"[\w\d]")
_JUNK_WORD = re.compile(r"^(?:name\s+redacted|confidential|privileged|redacted)$", re.I)
_DATE = re.compile(r"^[0-3]?\d[/-][0-3]?\d[/-]\d{2,4}$")


def _text_bearing(chars: Sequence[_Shape]) -> list[_Shape]:
    """The characters in a run that actually carry a glyph.

    The gaps between words are characters in the file and nothing in the eye,
    and under a heading reversed out of a black rectangle they are also the
    *only* characters whose own cells are solid box colour - the letters stand
    out of theirs. A count taken over the whole covered run therefore says
    "this box wiped out text" about the one part of the box that was plainly
    legible, and the whole page is then withheld on the strength of its own
    word spacing. So every question in :func:`_scan_hidden` that means *was
    there text here* is asked of this list.

    The runs themselves keep their spaces: :func:`_assemble` needs them to put
    the words back, and dropping them would run "Gregory Aldana" together.
    """
    return [c for c in chars if c.text.strip()]


def _assemble(chars: Sequence[_Shape]) -> str:
    """Put the covered glyphs back into reading order.

    Grouped into lines by vertical position, ordered left to right within a
    line, with a space inserted where the gap between two glyphs is wide enough
    to have been one. Producers that space words with a positioning operator
    instead of a space glyph are common, and "GregoryAldana" is a worse thing
    to show an operator than "Gregory Aldana".
    """
    if not chars:
        return ""
    heights = sorted(c.box.h for c in chars)
    line_tol = max(heights[len(heights) // 2] * 0.5, 1e-6)
    widths = sorted(c.box.w for c in chars if c.box.w > 0)
    # No usable widths means no way to tell a word gap from a kern, and
    # inventing word breaks in a leak report is worse than running the words
    # together, so in that case no spaces are added at all.
    gap_tol = (widths[len(widths) // 2] * 0.25) if widths else float("inf")

    lines: list[list[_Shape]] = []
    for char in sorted(chars, key=lambda c: (c.box.y, c.box.x)):
        if lines and abs(char.box.y - lines[-1][0].box.y) <= line_tol:
            lines[-1].append(char)
        else:
            lines.append([char])

    out: list[str] = []
    for line in lines:
        parts: list[str] = []
        previous: _Shape | None = None
        for char in sorted(line, key=lambda c: c.box.x):
            if (
                previous is not None
                and char.box.x - previous.box.x2 > gap_tol
                and not char.text.isspace()
                and not (parts and parts[-1].endswith(" "))
            ):
                parts.append(" ")
            parts.append(char.text)
            previous = char
        out.append("".join(parts).strip())
    return "\n".join(line for line in out if line)


def _is_real_text(text: str) -> bool:
    """Is this string worth waking somebody up about?

    The filters are x-ray's, and each one exists because of a shape of false
    positive seen in the wild:

    * one character repeated - a row of box-drawing glyphs or padding dots that
      happens to sit under a filled shape;
    * nothing word-like at all - punctuation, rules, dingbats;
    * the word printed *under* its own redaction box by a tool that stamps
      "REDACTED" and then covers it.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if len(set(stripped)) == 1:
        return False
    if not _HAS_WORD_CHAR.search(stripped):
        return False
    return not _JUNK_WORD.match(stripped)


def _all_dates(texts: Sequence[str]) -> bool:
    """Is every finding a bare date?

    A page where every box hides a date is a form - a docket, a log, a table of
    filing dates - where the boxes are cells and the "hidden" text is the cell
    contents drawn under a rule. Whereas one date among several names is a
    perfectly ordinary redaction, so the test is over the whole set.

    x-ray applies this per document; this module only ever sees one page, so a
    document whose only page with findings is a date form is suppressed and one
    that mixes forms and letters is not. ``build/site.py`` has the whole
    document and can widen it.
    """
    return bool(texts) and all(_DATE.match(t.strip()) for t in texts)


# --------------------------------------------------------------------------
# hidden text
# --------------------------------------------------------------------------


def _scan_hidden(
    candidates: Sequence[_Shape],
    index: _CharIndex,
    chars: Sequence[_Shape],
    renderer: CropRenderer | None,
) -> tuple[list[HiddenText], list[str], list[bool]]:
    """The hidden-text pass.

    Returns the findings, the things it is unsure of, and one flag per
    candidate rectangle: *did this rectangle obliterate any text*. That last
    list is what the ledger is built from - see :func:`analyse_page` - and it is
    computed here rather than recomputed there because it is the same evidence,
    the pixels included, and two copies of this reasoning would drift apart.

    **Text**, and every question below means it in the same way: characters
    that carry a glyph, never the word spacing between them. See
    :func:`_text_bearing`, which is where that distinction is made and why.
    """
    notes: list[str] = []
    findings: list[HiddenText] = []
    obliterates = [False] * len(candidates)
    unconfirmed = 0
    decided = 0  # boxes that had text under them, findings or not
    # `_shape` has already folded every spelling of "we do not know" - a missing
    # field, and `ingest/pdf.py`'s negative NO_ZORDER sentinel - into None, so
    # this is the whole test and it does not have to know what the sentinel is.
    no_seq = any(shape.seq is None for shape in candidates) or any(
        c.seq is None for c in chars
    )
    no_colour = sum(1 for shape in candidates if not shape.colour_given) + sum(
        1 for c in chars if not c.colour_given
    )

    for position, rect in enumerate(candidates):
        covered = index.covered(rect)
        if not covered:
            continue
        if _text_bearing(covered):
            decided += 1
        by_stream = [c for c in covered if _stream_hides(c, rect)]

        crop = _render(rect.box, renderer, notes)
        if crop is None:
            # No pixels to check. Fall back to the content stream alone, which
            # is x-ray's rule, and say so: an operator who sees this warning
            # knows the finding has not been confirmed by looking at the page,
            # and equally that a *missing* finding has not been ruled out.
            hidden = by_stream
            wiped = _text_bearing(hidden)
            if wiped:
                unconfirmed += 1
        else:
            # Every character is asked about its own pixels. `box_is_flat` is a
            # statement about all of them at once, measured over an area that
            # can be a thousand times bigger than any one of them, so it does
            # not get to answer for a character that can answer for itself: it
            # speaks only for cells too small to measure, and only while no
            # cell contradicts it. That is what separates a page stamped
            # PAGE WITHHELD IN FULL, where the lettering is legible in its own
            # cells, from a page-sized box with a page of text underneath.
            #
            # `legible` is deliberately counted over *every* covered cell,
            # whitespace included, because the question it answers is "does any
            # cell contradict the claim that this rectangle is flat" and not
            # "can any character be read". A space whose cell is textured is a
            # place where something is drawn, whoever put it there.
            per_char = [(c, crop.hides(c.box)) for c in covered]
            legible = sum(1 for _c, flat in per_char if flat is False)
            flat_enough = not legible and crop.box_is_flat()
            hidden = [
                c
                for c, flat in per_char
                if flat or (flat is None and (flat_enough or _stream_hides(c, rect)))
            ]
            wiped = _text_bearing(hidden)
            under = _text_bearing(by_stream)
            if under and not wiped:
                notes.append(
                    f"box {_where(rect.box)} has {len(under)} character(s) painted "
                    f"under it in the content stream, but {legible} of them stand out "
                    "of their own cells in the rendered page, so the text is probably "
                    "visible (a highlight, or lettering drawn on top). Not reported as "
                    "hidden - check this box by hand."
                )

        # Whether this box wiped out text, which is a different question from
        # whether the text is worth reporting: a box over a row of padding dots
        # is still a box, and belongs in the ledger even though nobody needs to
        # be woken up about what it hid.
        #
        # `wiped` and not `hidden`: the word spacing of a heading printed *on*
        # the box is not text the box removed. Counting it here withheld the
        # transcription of every page an agency had stamped PAGE WITHHELD IN
        # FULL, along with the exemption code printed in the box - see
        # `test_a_box_over_the_gaps_between_visible_words_wipes_out_nothing`.
        obliterates[position] = bool(wiped)

        if not wiped:
            continue  # whitespace only: nothing was hidden but the gaps
        text = _assemble(hidden)
        if not _is_real_text(text):
            continue
        findings.append(HiddenText(box=rect.box, text=text))

    if unconfirmed:
        notes.append(
            f"{unconfirmed} hidden-text finding(s) rest on the content stream alone; "
            "no page rendering was available to confirm them"
        )
    if no_seq and decided:
        notes.append(
            f"{decided} box(es) with text under them were judged without a draw order "
            "(ingest/pdf.py could not recover one for this page), so only colour "
            "matching could speak; that is weaker in both directions"
        )
    if no_colour:
        notes.append(
            f"{no_colour} shape(s) on this page carry no fill-colour field at all, so the "
            "same-colour half of the hidden-text test could not run on them: a black box "
            "painted in the same ink as the text under it would not be recognised here. "
            "This is a broken contract with ingest/pdf.py, not a property of the document"
        )
    if _all_dates([f.text for f in findings]):
        notes.append(
            f"suppressed {len(findings)} finding(s): every box on this page hides a "
            "bare date, which is a form rather than a failed redaction"
        )
        findings = []
    return findings, notes, obliterates


def find_hidden_text(page: Any, *, crop_renderer: CropRenderer | None = None) -> list[HiddenText]:
    """Text that is covered up but still in the file.

    *page* is the raw page from ``ingest/pdf.py``: anything carrying ``chars``,
    ``rects``, ``width`` and ``height``, where each char and rect knows its
    ``x0/top/x1/bottom`` in points and its ``seq`` in the content stream.

    **Without a ``crop_renderer``** this falls back to the content-stream test
    alone - a character is hidden if an opaque filled rectangle covers 80% of
    it and is either painted after it or painted in the same colour. That is
    the published x-ray rule and it is what the module is for, so it runs and
    reports rather than refusing to answer. What is lost is precision: a
    semi-transparent highlight that pdfplumber reports as an opaque fill, or a
    box that a later image covers up, will be reported as hidden text when a
    reader can see the words perfectly well. Callers who care should pass a
    renderer; :func:`analyse_page` records the shortfall in its warnings so the
    CLI can tell the operator that a finding is unconfirmed.

    Returns one :class:`~stackroom.model.HiddenText` per rectangle, never
    per character. The text is kept in memory for the operator and must not be
    written anywhere - see the class docstring in ``model.py``.
    """
    rects, chars, _page_w, _page_h = _page_parts(page)
    findings, _notes, _ = _scan_hidden(
        _candidates(rects), _CharIndex(chars), chars, crop_renderer
    )
    return findings


# --------------------------------------------------------------------------
# visible redactions
# --------------------------------------------------------------------------


def find_visible_redactions(image: Any, *, dpi: int = REFERENCE_DPI) -> list[Redaction]:
    """Solid black blocks in a rendered page.

    This is the scan case: no content stream to consult, just pixels. Threshold
    at near-black, label the connected components, and keep the ones that are
    shaped like a redaction and flat inside like a redaction.

    The filter chain, and what each link is there to reject - the decoys are
    from a synthetic page carrying three true redactions, a header rule, the
    black border of a skewed scan and a dark photograph:

    ==================  =========================================
    test                what it rejects
    ==================  =========================================
    size                the header rule (3px tall)
    aspect              rules and columns
    page fraction       an inverted or dark-scanned page
    solidity            the scan border (measured 0.02)
    interior flatness   the photograph (std 14.4, spread 50)
    ==================  =========================================

    All three true boxes survive all five (solidity 1.00, std 0.0).

    *dpi* scales the two size thresholds, which were measured at 150.
    """
    gray = to_gray(image)
    height, width = gray.shape
    if height == 0 or width == 0:
        return []

    scale = max(dpi, 1) / REFERENCE_DPI
    min_w = MIN_BOX_W_PX * scale
    min_h = MIN_BOX_H_PX * scale
    max_area = MAX_PAGE_FRACTION * width * height

    _, components = connected_components(binarise(gray, NEAR_BLACK))
    found: list[Redaction] = []
    for comp in components:
        if comp.w < min_w or comp.h < min_h:
            continue
        if not (ASPECT_MIN <= comp.aspect <= ASPECT_MAX):
            continue
        if comp.area >= max_area:
            continue
        if comp.solidity <= MIN_SOLIDITY:
            continue
        # Two pixels in from the edge at 150 dpi: the rim of a block is
        # antialiased against the paper and is not part of the block.
        inset = max(1, round(2 * scale))
        patch = comp.crop(gray, inset=inset)
        if patch.size == 0:
            patch = comp.crop(gray)
        std, spread, mean = uniformity(patch)
        if std >= MAX_UNIFORM_STD or spread >= MAX_UNIFORM_SPREAD or mean >= MAX_BOX_MEAN:
            continue
        found.append(
            Redaction(
                box=Box(comp.x / width, comp.y / height, comp.w / width, comp.h / height),
                kind=RedactionKind.RASTER,
            )
        )
    return found


# --------------------------------------------------------------------------
# how much was withheld
# --------------------------------------------------------------------------


def _boxes_of(items: Iterable[Any]) -> list[Box]:
    """Boxes out of a sequence of Box, Redaction, Word, or anything with .box."""
    boxes: list[Box] = []
    for item in items or ():
        if isinstance(item, Box):
            boxes.append(item)
            continue
        candidate = _field(item, ("box",), None)
        if isinstance(candidate, Box):
            boxes.append(candidate)
    return boxes


def _paint(grid: np.ndarray, boxes: Iterable[Box]) -> None:
    """Mark every cell any of *boxes* touches.

    Rounded outwards, so a box thinner than a cell still counts. Word boxes on
    a text page are a few cells tall; losing them to rounding would shrink the
    denominator and inflate every ratio on the page.

    The edge arithmetic is done for all the boxes at once because this now runs
    over every page twice - once in :func:`analyse_page` and once more in
    :func:`stackroom.pipeline.summarise`, which needs each page's content area
    to weigh it in the collection figure. The result is the same to the cell:
    each edge is clamped to the page on its own, floored or ceiled, and a box
    that rounds to nothing still gets one cell. Non-finite coordinates are
    folded the way the scalar code folded them - ``nan`` and ``+inf`` to the far
    edge, ``-inf`` to the near one - so a broken box stays a wide box rather
    than becoming an index error.
    """
    rows, cols = grid.shape
    edges = [(box.x, box.y, box.x2, box.y2) for box in boxes]
    if not edges:
        return
    corners = np.array(edges, dtype=np.float64)
    np.nan_to_num(corners, copy=False, nan=1.0, posinf=1.0, neginf=0.0)
    np.clip(corners, 0.0, 1.0, out=corners)
    x0 = np.floor(corners[:, 0] * cols).astype(np.intp)
    y0 = np.floor(corners[:, 1] * rows).astype(np.intp)
    x1 = np.maximum(np.ceil(corners[:, 2] * cols).astype(np.intp), x0 + 1)
    y1 = np.maximum(np.ceil(corners[:, 3] * rows).astype(np.intp), y0 + 1)
    for index in range(len(edges)):
        grid[y0[index] : y1[index], x0[index] : x1[index]] = True


def _grid_counts(
    redactions: Iterable[Any], word_boxes: Iterable[Any], page_size: tuple[float, float]
) -> tuple[int, int, float]:
    """``(redacted cells, inked cells, area of one cell in pt²)``.

    The one measurement behind both :func:`redaction_ratio` and
    :func:`content_area`, so that a share and the areas it came from can never
    be computed two different ways.
    """
    width, height = page_size if page_size else (612.0, 792.0)
    aspect = (height / width) if width else 1.294
    cols = RATIO_GRID
    rows = max(1, min(4000, round(RATIO_GRID * aspect)))

    red_boxes = _boxes_of(redactions)
    ink_boxes = _boxes_of(word_boxes)
    cell = max(0.0, float(width) / cols) * max(0.0, float(height) / rows)
    if not red_boxes and not ink_boxes:
        # A blank page. Worth its own line because `summarise` asks this of
        # every page in the collection, and two megabyte-scale allocations per
        # blank page is a measurable share of a large production's summary.
        return 0, 0, cell

    ink = np.zeros((rows, cols), dtype=bool)
    red = np.zeros((rows, cols), dtype=bool)
    _paint(ink, ink_boxes)
    _paint(ink, red_boxes)
    _paint(red, red_boxes)

    return int(np.count_nonzero(red)), int(np.count_nonzero(ink)), cell


def content_area(
    redactions: Iterable[Any], word_boxes: Iterable[Any], page_size: tuple[float, float]
) -> tuple[float, float]:
    """``(redacted area, inked area)`` for one page, in square points.

    The same union-on-a-grid measurement as :func:`redaction_ratio`, which is
    that pair divided - kept as a pair because a collection figure needs the
    numerator and the denominator, not the quotient. A mean of per-page shares
    gives a one-line page the same say as a dense one; summing areas does not.

    Square points, not grid cells: the grid is always 800 cells wide whatever
    the page is, so a cell on a legal page is not a cell on an A5 one and
    summing raw cell counts across a mixed collection would weigh the small
    pages more heavily. ``(0.0, 0.0)`` for a page with nothing on it, which is a
    page the collection figure has no way to measure rather than a page with
    nothing withheld - see :func:`stackroom.pipeline.summarise`.
    """
    red, ink, cell = _grid_counts(redactions, word_boxes, page_size)
    return red * cell, ink * cell


def redaction_ratio(
    redactions: Iterable[Any], word_boxes: Iterable[Any], page_size: tuple[float, float]
) -> float:
    """Share of the page's *inked content* that is under a redaction.

    The denominator is the union of the surviving words and the redaction boxes
    - the part of the page that carries content - not the page. This matters:

    * Against page area, a letter page blacked out from margin to margin scores
      0.63, because an inch of margin on every side is 37% of the sheet. The
      honest answer is 1.0, and 0.63 in a ledger is a lie about how much of the
      document a reader is allowed to see.
    * Against the bounding box of the surviving text it goes the other way and
      becomes unstable exactly where it matters most: redact the last paragraph
      and the surviving bbox shrinks, so the ratio *falls* as more is withheld.

    Both parts are measured as an area union on a grid, not as bounding boxes,
    so two redactions on the same line are not double-counted and a page of
    double-spaced text does not have its blank leading counted as content.

    Accepts :class:`~stackroom.model.Box`, :class:`~stackroom.model.Redaction`,
    :class:`~stackroom.model.Word`, or anything else carrying a ``.box``.
    Returns 0.0 for a page with no ink at all, which is a blank page, not a
    fully redacted one.
    """
    redacted, inked, _cell = _grid_counts(redactions, word_boxes, page_size)
    if inked == 0:
        return 0.0
    return float(redacted / inked)


def _ink_from_pixels(gray: np.ndarray, boxes: Sequence[Box]) -> tuple[float, Box | None]:
    """Withheld share and inked bounding box, measured from the rendering.

    For a scan there is no text layer to take the content region from, and
    using the redaction boxes alone as the denominator would report every
    scanned page with a box on it as 100% withheld. The ink on the page is
    right there in the pixels, so use that: the share is redacted area over
    (ink area union redacted area), the same fraction :func:`redaction_ratio`
    computes, measured at the resolution of the render rather than on a grid.
    """
    ink = binarise(gray)  # Otsu, i.e. "whatever counts as ink on this scan"
    redacted = np.zeros_like(ink)
    _paint(redacted, boxes)
    content = ink | redacted
    denominator = int(content.sum())
    if denominator == 0:
        return 0.0, None
    rows = np.flatnonzero(content.any(axis=1))
    cols = np.flatnonzero(content.any(axis=0))
    height, width = content.shape
    # float(), not numpy scalars: a Box holding np.float64 survives every test
    # in this file and then fails in json.dumps at the end of the build.
    ink_box = Box(
        float(cols[0] / width),
        float(rows[0] / height),
        float((cols[-1] + 1 - cols[0]) / width),
        float((rows[-1] + 1 - rows[0]) / height),
    )
    return float(int(redacted.sum()) / denominator), ink_box


# --------------------------------------------------------------------------
# the whole page
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RedactionFindings:
    """Everything this module can say about one page."""

    redactions: list[Redaction] = field(default_factory=list)
    """Boxes that did their job, vector and raster, deduplicated."""

    hidden: list[HiddenText] = field(default_factory=list)
    """Boxes that did not. Any entry here should stop the build."""

    covered: list[Box] = field(default_factory=list)
    """Every box that obliterated characters, findings or not.

    Reporting and withholding are different questions. A box over a bare date
    is a form rather than a leak and does not need to wake anybody up - but the
    date is still under a black box, and publishing our transcription of it
    puts back exactly what the box removed. So this list is wider than
    ``hidden`` on purpose: it is what must be kept out of the site, while
    ``hidden`` is what is worth stopping the build over."""

    ratio: float = 0.0
    """Redacted share of the inked region. See :func:`redaction_ratio`."""

    ink_box: Box | None = None
    """Bounding box of everything on the page - surviving words and boxes -
    or ``None`` for a blank page. Useful for cropping and for the ledger."""

    warnings: list[str] = field(default_factory=list)
    """Things a human should know: findings that could not be confirmed against
    the pixels, boxes that look suspicious but were not reported, and the
    known-ambiguous cases below. Never silently swallowed."""

    @property
    def is_leaking(self) -> bool:
        return bool(self.hidden)


def _overlaps_existing(box: Box, existing: Iterable[Box]) -> bool:
    """Is this box already accounted for by one we found in the content stream?"""
    return any(box.overlap_ratio(other) > 0.6 for other in existing)


def analyse_page(
    page: Any, image: Any = None, *, crop_renderer: CropRenderer | None = None
) -> RedactionFindings:
    """Run both passes over one page and report.

    *page* is the raw page from ``ingest/pdf.py``. *image* is the rendered page,
    optional: pass it for a scan, where the boxes exist only as pixels. When
    both are available the vector boxes win and a raster block that lands on
    one is not counted twice.

    The resolution of *image* is derived from its width against the page width
    rather than assumed, so a thumbnail is not mistaken for a 150 dpi render
    and filtered by the wrong size thresholds.

    Which rectangles reach ``redactions``
    -------------------------------------
    Not every opaque filled rectangle in a PDF is a redaction. Born-digital
    documents are full of them: table cell fills, header knockouts, form
    backgrounds, a white box painted over a scanning artefact. Counting those
    would inflate "N pages withheld, X% of content" on essentially every
    born-digital collection, and that number is the most consequential thing
    this tool prints. So a rectangle is in the ledger when either

    * it **obliterated text** - the content stream says characters were painted
      under it, or the pixels say characters inside it are invisible. This is
      the same evidence the leak check uses, taken from the same pass, so a
      white knockout dropped over live text is both a redaction *and* a failed
      one; or
    * its **fill is dark** (:data:`DARK_FILL`), which is a redaction over
      something we have no characters for - a photograph, a signature, a
      scanned region.

    A light rectangle covering nothing is page furniture and is dropped without
    comment. Note that "covers text" is not enough on its own: a table cell
    fill painted *before* the text sitting in it covers those characters
    geometrically and hides nothing, and counting it would reintroduce exactly
    the inflation this rule exists to stop.

    **This filter applies to the ledger only.** :func:`find_hidden_text`
    considers rectangles of every colour, because white-on-white is a whole
    class of failed redaction, and narrowing the ledger must never narrow the
    safety check. ``test_the_ledger_filter_does_not_narrow_the_leak_check``
    pins the two apart.

    Known ambiguities, reported in ``warnings`` rather than resolved:

    * A uniform dark band across a scan - a lid left open, a page photographed
      against a dark surface - is *genuinely indistinguishable* from a
      redaction. It is flat, black, solid and rectangular because it is. It
      will be counted, and it will inflate the ratio.
    * A finding made without a ``crop_renderer`` has not been checked against
      the pixels.
    * A dark bar with lettering reversed out of it - a section header in a
      modern report, or a page stamped ``PAGE WITHHELD IN FULL`` - is counted
      as a redaction. It is dark and we cannot see what is behind it, which is
      the same thing we say about a real box. This is a *ledger* judgement and
      it stops there: the same bar is not a leak, because the lettering on it
      is legible in its own pixels, and :func:`find_hidden_text` says so. The
      two claims are different sizes and only the quiet one is being made.

    The withheld share is measured against the page's surviving text where
    there is a text layer, and against the ink in *image* where there is not -
    never against the area of the sheet. A scan with one box on it must not
    report as fully withheld just because we cannot read it.
    """
    rects, chars, page_w, page_h = _page_parts(page)
    candidates = _candidates(rects)
    hidden, warnings, obliterates = _scan_hidden(
        candidates, _CharIndex(chars), chars, crop_renderer
    )
    # The other alias table that fails quietly. Every box on this page was put
    # into page-relative units against these two numbers, so a page that does
    # not carry them is silently measured as US Letter and every box on it is
    # in the wrong place - which is a black box drawn somewhere it is not.
    missing = [
        what
        for what, names in (("a width", _WIDTH), ("a height", _HEIGHT))
        if not _has_field(page, names)
    ]
    if missing:
        warnings.append(
            f"this page carries no field naming {' or '.join(missing)}, so it was measured as "
            f"{page_w:.0f}x{page_h:.0f}pt; if that is not its size, every black box on it has "
            "been placed wrongly. ingest/pdf.py owes this module the page size"
        )
    hidden_boxes = [h.box for h in hidden]

    # Every rectangle that wiped out characters, whether or not the finding
    # survived the "is this worth reporting" filters. The caller withholds the
    # text under these from the published page; see `covered` above.
    covered = [
        shape.box for shape, wiped in zip(candidates, obliterates, strict=True) if wiped
    ]

    redactions = [
        Redaction(box=shape.box, kind=RedactionKind.VECTOR)
        for shape, wiped in zip(candidates, obliterates, strict=True)
        if wiped or _is_dark(shape.colour)
    ]
    if image is not None:
        gray = to_gray(image)
        dpi = round(gray.shape[1] / (page_w / 72.0)) if page_w > 0 else REFERENCE_DPI
        vector_boxes = [r.box for r in redactions]
        for found in find_visible_redactions(gray, dpi=max(dpi, 1)):
            if not _overlaps_existing(found.box, vector_boxes):
                redactions.append(found)
        from_pixels = len(redactions) - len(vector_boxes)
        if from_pixels:
            warnings.append(
                f"{from_pixels} solid block(s) counted from the rendering alone, with no "
                "filled rectangle behind them; a dark scan band looks exactly like a "
                "redaction and cannot be told apart"
            )

    # Surviving text: what a reader will actually get. Characters we just
    # reported as hidden are not part of it - they are under a box, and they
    # are not going to be published.
    surviving = [
        c.box
        for c in chars
        if c.text.strip() and not any(_covers(b, c.box) for b in hidden_boxes)
    ]
    boxes = [r.box for r in redactions]
    if surviving or image is None:
        ratio = redaction_ratio(boxes, surviving, (page_w, page_h))
        ink_box = None
        for box in surviving + boxes:
            ink_box = box if ink_box is None else ink_box.union(box)
    else:
        # A scan: no text layer, so the content region comes from the ink.
        ratio, ink_box = _ink_from_pixels(to_gray(image), boxes)

    return RedactionFindings(
        redactions=redactions,
        hidden=hidden,
        covered=covered,
        ratio=ratio,
        ink_box=ink_box,
        warnings=warnings,
    )
