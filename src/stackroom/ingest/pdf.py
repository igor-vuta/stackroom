"""Read one page of a PDF: geometry, characters, filled shapes, and draw order.

This module answers four questions about a page, and nothing else. Where is the
text. Where are the filled shapes. In what order were they painted. And is the
text layer worth believing. Everything downstream - the redaction check, the
quality verdict, the rendered page - is built out of those four answers, so they
are produced here once and produced carefully.

Draw order
----------
pdfplumber gives tidy lists of chars and rects and throws away the one fact the
redaction check depends on: which was painted first. A black rectangle drawn
*after* the characters beneath it is a failed redaction - the text is still in
the file and anyone who downloads it can read it. The same rectangle drawn
*before* them is a highlight. Same geometry, opposite meaning. So we run
pdfminer's own layout analyser ourselves and count the calls as the content
stream executes; the counter *is* the draw order. :data:`NO_ZORDER` marks the
degraded case where we could not recover it, and callers must treat that as
"unknown", never as "zero".

Coordinates
-----------
pdfminer folds ``/Rotate`` into the page CTM before any content runs, so the
device space it reports is already the frame a reader sees: a landscape scan
marked ``/Rotate 90`` comes back landscape, with the text where the eye expects
it. That leaves two transforms to do. PDF measures y upwards from the bottom
and :class:`~stackroom.model.Box` measures it downwards from the top; and
pdfminer runs the content stream against the **MediaBox** while poppler,
``pdfinfo`` and every viewer show the **CropBox**. Both are applied once, in
:func:`_display_frame`, so that every box this module reports is in the same
frame as the pixels the redaction check will confirm it against. Getting that
wrong is not a rounding error - it points the whole pixel check at a different
part of the page.

Licensing
---------
``PyMuPDF``/``fitz`` is AGPL and is not imported here or anywhere in this
project. ``pdfplumber``, ``pdfminer.six`` and ``pypdf`` only.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import math
import shutil
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from types import TracebackType
from typing import Any, BinaryIO

from pdfminer.converter import PDFLayoutAnalyzer, PDFPageAggregator
from pdfminer.layout import LTChar, LTContainer, LTCurve, LTPage
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfplumber.utils import extract_words

from stackroom.lang import stopword_ratio, stopwords_apply
from stackroom.model import Box, Word

__all__ = [
    "NO_ZORDER",
    "PdfDamagedError",
    "PdfEncryptedError",
    "PdfError",
    "PdfHandle",
    "PublishedFile",
    "RawChar",
    "RawPage",
    "RawRect",
    "document_meta",
    "open_pdf",
    "page_count",
    "publish_pdf",
    "read_page",
]

INVISIBLE_MODES = frozenset({3, 7})
"""Text render modes that paint nothing: 3 is "invisible", 7 is "clip only"."""

NO_ZORDER = -1
"""``seq`` value meaning "we could not recover the draw order for this page".

It is negative so that a caller who forgets to check cannot silently read it as
"painted first"; a comparison against a real sequence number will always put it
before everything, which is wrong in a visible way rather than a quiet one.

That was the theory. In practice a page from :func:`_paint_without_order` has
this on *every* shape, so ``char.seq < rect.seq`` compares two sentinels, is
False for every pair, and the draw-order test answers "no" for the whole page -
quietly, which is the failure mode this constant was shaped to avoid. The
module that depends on it now folds it into its own "unknown" before anything
compares it (``ingest/redaction.py``: :data:`~stackroom.ingest.redaction.NO_DRAW_ORDER`
and ``_shape``), treats *any* negative ``seq`` the same way, and reads
:attr:`RawPage.has_zorder` as a second, independent statement of the same fact.
Changing the value here needs no change there; removing the sign would.
"""


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class PdfError(RuntimeError):
    """A PDF we cannot read at all. Always carries the path and the reason."""


class PdfEncryptedError(PdfError):
    """The file is encrypted and the empty password does not open it."""


class PdfDamagedError(PdfError):
    """The file is not a readable PDF: truncated, empty, or structurally broken."""


def _describe(exc: BaseException) -> str:
    """``TypeName: message``, or just the type when the message is empty.

    pdfminer raises plenty of exceptions whose ``str()`` is the empty string.
    An operator staring at "failed: " learns nothing; the type name at least
    names the failure.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# --------------------------------------------------------------------------
# what one page holds
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RawRect:
    """One painted path, as its bounding box.

    Not every painted path is a rectangle. Paths that pdfminer could not prove
    square - a rounded box, a rect drawn as four lines whose corners miss by a
    thousandth of a point - arrive here as their bounding box, which overstates
    what they actually cover. That is the safe direction: the redaction check
    confirms a candidate by looking at the rendered pixels, so an over-wide box
    costs a little work, and a missing one can burn a source.
    """

    box: Box
    fill_color: tuple[float, float, float] | None
    """Non-stroking colour, normalised to RGB in 0..1. ``None`` when the colour
    came from a space we cannot reduce - a pattern, a Separation, an ICC space
    with an unusual component count."""

    seq: int
    """Content-stream draw order, or :data:`NO_ZORDER`."""

    stroke: bool
    fill: bool


@dataclass(slots=True)
class RawChar:
    """One decoded glyph, with the box it occupies and when it was painted."""

    text: str
    box: Box
    seq: int
    color: tuple[float, float, float] | None
    fontname: str
    size: float

    invisible: bool = False
    """Painted in text render mode 3 or 7 - in the file, not on the page.

    Normal, and load-bearing, under an OCR layer: every searchable scan in
    existence is an image with an invisible transcription behind it, and that
    transcription is the only text the page has. It is *also* the shape a
    failed redaction takes when a tool stamps a replacement over the original
    and leaves the original where it was. Which of the two this is depends on
    what else is on the page, so the flag is recorded here and judged in
    :func:`read_page`."""


@dataclass(slots=True)
class RawPage:
    """Everything :mod:`stackroom.ingest.pdf` knows about a single page.

    A page that failed to parse still comes back as one of these, with empty
    lists and the reason recorded. One unreadable page in a five-hundred-page
    production must not cost the other four hundred and ninety-nine.
    """

    number: int
    """1-based, matching the URL and what is printed on the page."""

    width_pt: float
    height_pt: float
    """Dimensions *after* ``/Rotate``, i.e. as displayed."""

    rotation: int
    chars: list[RawChar] = field(default_factory=list)
    rects: list[RawRect] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    """Reading order: by line, then left to right. See :func:`_build_words`."""

    embedded_text_ok: bool = False
    embedded_text_reasons: list[str] = field(default_factory=list)
    """Everything worth telling an operator about this page, in plain words: why
    the text layer is or is not believable, and any damage we worked around.

    A non-empty list does not imply ``embedded_text_ok`` is False. A page whose
    text reads perfectly but whose draw order could not be recovered carries a
    note here and still passes, because those are two different questions.
    """

    @property
    def has_zorder(self) -> bool:
        """False when draw order could not be recovered for this page.

        Read by ``ingest/redaction.py`` through its page alias table, which
        drops every ``seq`` on a page that says False here. It is a whole-page
        statement because :func:`read_page` decides per page: either the
        instrumented device ran and every shape has its position, or it did not
        and every shape carries :data:`NO_ZORDER`.
        """
        return not any(
            item.seq == NO_ZORDER for item in (*self.chars, *self.rects)
        )


# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------


def _as_rgb(value: Any) -> tuple[float, float, float] | None:
    """Normalise a pdfminer colour to RGB in 0..1, or ``None`` if we cannot.

    pdfminer reports whatever the colour space handed it: a bare number for
    ``g``, three for ``rg``, four for ``k``, a ``PSLiteral`` for a pattern.
    The redaction check compares a shape's fill against the colour of the text
    under it, so everything reducible is reduced to one comparable space and
    everything else is honestly reported as unknown.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        grey = float(value)
        return (grey, grey, grey)
    if not isinstance(value, (tuple, list)):
        return None
    try:
        parts = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if len(parts) == 1:
        return (parts[0], parts[0], parts[0])
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2])
    if len(parts) == 4:
        c, m, y, k = parts
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


# --------------------------------------------------------------------------
# the z-order device
# --------------------------------------------------------------------------

# pdfminer has changed `render_char`'s signature more than once - `ncs` and
# `graphicstate` were added years after the method existed. Ask the installed
# copy what it takes rather than guessing, so a version bump degrades into a
# missing colour instead of a TypeError on every page.
try:
    _BASE_RENDER_CHAR_PARAMS = frozenset(
        inspect.signature(PDFLayoutAnalyzer.render_char).parameters
    )
except (TypeError, ValueError):  # pragma: no cover - only on an exotic build
    _BASE_RENDER_CHAR_PARAMS = frozenset({"ncs", "graphicstate"})

_RENDER_CHAR_TAKES_COLOUR = "graphicstate" in _BASE_RENDER_CHAR_PARAMS


class _ZOrderDevice(PDFLayoutAnalyzer):
    """A pdfminer device that counts paint operations as they happen.

    Everything it collects is in pdfminer device space - origin bottom-left, y
    upwards, ``/Rotate`` already applied - and gets converted once, in
    :func:`read_page`, where the page height is known.
    """

    def __init__(self, rsrcmgr: PDFResourceManager) -> None:
        # laparams=None skips pdfminer's layout analysis entirely. We do our own
        # word grouping from the chars, and analysis is the expensive half of a
        # pdfminer page.
        super().__init__(rsrcmgr, laparams=None)
        self.seq = 0
        self.paths: list[tuple[int, tuple[float, ...], bool, bool, Any]] = []
        self.glyphs: list[tuple[int, LTChar, Any]] = []
        self.page_bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self._painting = False
        self._render_mode = 0

    # -- collection --------------------------------------------------------

    def paint_path(
        self,
        gstate: Any,
        stroke: bool,
        fill: bool,
        evenodd: bool,
        path: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if self._painting:
            # pdfminer recurses into itself to split a multi-subpath shape.
            # Let the outer call account for whatever the recursion produces,
            # so one `re re f` does not inflate the counter unpredictably.
            return super().paint_path(gstate, stroke, fill, evenodd, path, *args, **kwargs)

        self._painting = True
        try:
            before = self._object_count()
            result = super().paint_path(gstate, stroke, fill, evenodd, path, *args, **kwargs)
            for obj in self._objects_since(before):
                self.seq += 1
                self.paths.append(
                    (self.seq, obj.bbox, bool(stroke), bool(fill), getattr(gstate, "ncolor", None))
                )
            return result
        finally:
            self._painting = False

    def render_string(
        self,
        textstate: Any,
        seq: Any,
        ncs: Any = None,
        graphicstate: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        # The text render mode lives on the *text* state, which render_char is
        # never handed. This is the only place both are in scope, so the mode is
        # stashed for the glyphs this string is about to produce and put back
        # afterwards - form XObjects nest, and a leaked mode would mislabel
        # every glyph after them.
        previous = self._render_mode
        try:
            self._render_mode = int(getattr(textstate, "render", 0) or 0)
        except (TypeError, ValueError):  # pragma: no cover - a hostile text state
            self._render_mode = 0
        try:
            return super().render_string(textstate, seq, ncs, graphicstate, *args, **kwargs)
        finally:
            self._render_mode = previous

    def render_char(
        self,
        matrix: Any,
        font: Any,
        fontsize: float,
        scaling: float,
        rise: float,
        cid: int,
        ncs: Any = None,
        graphicstate: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> float:
        if _RENDER_CHAR_TAKES_COLOUR:
            adv = super().render_char(
                matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate, *args, **kwargs
            )
        else:  # pragma: no cover - only on pdfminer builds older than 2019
            adv = super().render_char(matrix, font, fontsize, scaling, rise, cid)

        self.seq += 1
        # super() has just appended the LTChar it built. Taking it back off the
        # container gives us pdfminer's own device-space bbox, which accounts
        # for the font matrix, the rise and the horizontal scaling - all things
        # that are wrong if you compute the box yourself from the text matrix.
        objs = getattr(self.cur_item, "_objs", None)
        if objs:
            self.glyphs.append(
                (self.seq, objs[-1], graphicstate, self._render_mode in INVISIBLE_MODES)
            )
        return adv

    def receive_layout(self, ltpage: LTPage) -> None:
        self.page_bbox = ltpage.bbox

    # -- helpers -----------------------------------------------------------

    def _object_count(self) -> int:
        objs = getattr(self.cur_item, "_objs", None)
        return len(objs) if objs is not None else 0

    def _objects_since(self, before: int) -> list[Any]:
        objs = getattr(self.cur_item, "_objs", None)
        return list(objs[before:]) if objs is not None else []


# --------------------------------------------------------------------------
# the handle
# --------------------------------------------------------------------------


class PdfHandle:
    """An open PDF, parsed down to its page list and no further.

    Opening resolves the cross-reference table, the catalogue and the page tree
    once. Content streams - the expensive part, and by far the largest - are
    parsed one page at a time by :func:`read_page`, so reading page 400 of a
    500-page production costs one page, not five hundred.

    Measured on a 50-page synthetic memo, 2,100 characters a page, letter size,
    CPython 3.11: ``open_pdf`` 15 ms, then about 25 ms a page - and page 49
    costs the same 25 ms as page 0, which is the proof that nothing is re-parsed
    per page. Opening and reading one page is 36 ms against roughly 1.3 s for
    the whole document, so the split is worth about 35x on the page-viewer path.
    There is deliberately no result cache: the pipeline reads each page once,
    and holding fifty pages of character boxes to save a 25 ms recompute would
    trade a lot of memory for nothing.

    Use it as a context manager, or call :meth:`close` yourself.
    """

    def __init__(
        self,
        path: Path,
        fp: BinaryIO,
        document: PDFDocument,
        pages: list[PDFPage],
    ) -> None:
        self.path = path
        self.document = document
        self.pages = pages
        self.page_count = len(pages)
        self._fp = fp
        # One resource manager for the whole file, so an embedded font is
        # decoded once rather than once per page that uses it.
        self._rsrcmgr = PDFResourceManager()
        self._closed = False

    def __enter__(self) -> PdfHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._fp.close()

    def __repr__(self) -> str:
        return f"<PdfHandle {self.path.name} pages={self.page_count}>"


def open_pdf(path: Path) -> PdfHandle:
    """Open *path* for page-by-page reading.

    Raises :class:`PdfEncryptedError` when the file is encrypted and the empty
    password does not open it, and :class:`PdfDamagedError` when it is empty,
    truncated or otherwise not a PDF. A zero-page PDF is not an error: it opens
    with ``page_count == 0``, because a production can legitimately contain one.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PdfDamagedError(f"{path}: cannot stat file: {exc.strerror or exc}") from exc
    if size == 0:
        raise PdfDamagedError(f"{path}: file is empty (0 bytes)")

    fp = path.open("rb")
    try:
        # Empty password first: an owner-password-only file - the common shape
        # for a government release - opens with it and needs no help.
        document = PDFDocument(PDFParser(fp), password="")
        pages = list(PDFPage.create_pages(document))
    except Exception as exc:
        fp.close()
        if _is_password_error(exc):
            raise PdfEncryptedError(_encryption_message(path)) from exc
        raise PdfDamagedError(f"{path}: {_describe(exc)}") from exc
    return PdfHandle(path, fp, document, pages)


def _is_password_error(exc: BaseException) -> bool:
    """True for pdfminer's encryption exceptions, by name.

    By name because the class has moved between modules across releases and an
    import that fails at module load would take the whole ingest with it.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"PDFPasswordIncorrect", "PDFEncryptionError"})


def _encryption_message(path: Path) -> str:
    """Say what is actually wrong, and what to do about it."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted and reader.decrypt(""):
            return (
                f"{path}: encrypted with a scheme pdfminer cannot open, although the "
                "empty password is accepted. Decrypt it first, e.g. "
                "`qpdf --decrypt in.pdf out.pdf`."
            )
    except Exception:  # pypdf is only here to improve the message
        pass
    return (
        f"{path}: encrypted, and the empty password does not open it. Supply the "
        "password and decrypt it first, e.g. "
        "`qpdf --password=... --decrypt in.pdf out.pdf`."
    )


def page_count(path: Path) -> int:
    """Number of pages, counted the same way :func:`read_page` indexes them."""
    with open_pdf(path) as handle:
        return handle.page_count


# --------------------------------------------------------------------------
# reading a page
# --------------------------------------------------------------------------


def read_page(handle: PdfHandle, index: int) -> RawPage:
    """Read one page, 0-based.

    Never raises for a bad page. A page whose content stream is broken comes
    back empty with the reason in ``embedded_text_reasons``; only an index
    outside the document is a programming error and raises.
    """
    if not 0 <= index < handle.page_count:
        raise IndexError(
            f"{handle.path}: page index {index} out of range "
            f"(document has {handle.page_count} pages)"
        )

    page = handle.pages[index]
    rotation = _rotation(page)
    number = index + 1

    try:
        glyphs, paths, page_bbox = _paint(handle, page)
        seq_ok = True
    except Exception as exc:  # one bad page must never stop the document
        try:
            glyphs, paths, page_bbox = _paint_without_order(handle, page)
            seq_ok = False
        except Exception as fallback_exc:
            width, height = _mediabox_size(page, rotation)
            return RawPage(
                number=number,
                width_pt=width,
                height_pt=height,
                rotation=rotation,
                embedded_text_reasons=[
                    f"page {number} could not be parsed: {_describe(fallback_exc)}"
                ],
            )
        reasons_prefix = [
            f"draw order unavailable on page {number} ({_describe(exc)}); "
            "hidden-text detection cannot run here"
        ]
    else:
        reasons_prefix = []

    # pdfminer runs the content stream against the MediaBox; poppler and every
    # viewer show the CropBox. Boxes measured in one frame and confirmed in the
    # other are the bug this line exists to prevent - see F3 in
    # docs/THREAT-MODEL.md - so everything below is expressed in the displayed
    # frame, offset included.
    width, height, dx, dy = _display_frame(page, rotation, page_bbox)

    chars: list[RawChar] = []
    char_dicts: list[dict[str, Any]] = []
    for seq, glyph, gstate, invisible in glyphs:
        top, bottom, left, right = _flip(glyph.bbox, height, dx, dy)
        colour = _as_rgb(
            getattr(gstate, "ncolor", None)
            if gstate is not None
            else getattr(getattr(glyph, "graphicstate", None), "ncolor", None)
        )
        text = glyph.get_text()
        chars.append(
            RawChar(
                text=text,
                box=Box.from_pdf_rect(left, top, right, bottom, width, height),
                seq=seq if seq_ok else NO_ZORDER,
                color=colour,
                fontname=str(getattr(glyph, "fontname", "") or ""),
                size=float(getattr(glyph, "size", 0.0) or 0.0),
                invisible=invisible,
            )
        )
        # pdfplumber's word grouper works in points, so it gets the unscaled
        # geometry; `doctop` only matters for multi-page extraction, which this
        # is not, so it equals `top`.
        char_dicts.append(
            {
                "text": text,
                "x0": left,
                "x1": right,
                "top": top,
                "bottom": bottom,
                "doctop": top,
                "upright": bool(getattr(glyph, "upright", True)),
                "size": float(getattr(glyph, "size", 0.0) or 0.0),
            }
        )

    overprinted = _overprinted(chars)
    if overprinted:
        reasons_prefix.append(
            f"{len(overprinted)} invisible character(s) on this page sit underneath "
            "visible ones - the shape a redaction tool leaves when it stamps a "
            "replacement over the original instead of removing it. They are kept out "
            "of the published text and the search index, and still checked for "
            "hidden text"
        )
        char_dicts = [d for i, d in enumerate(char_dicts) if i not in overprinted]

    rects: list[RawRect] = []
    for seq, bbox, stroke, fill, ncolor in paths:
        top, bottom, left, right = _flip(bbox, height, dx, dy)
        rects.append(
            RawRect(
                box=Box.from_pdf_rect(left, top, right, bottom, width, height),
                fill_color=_as_rgb(ncolor),
                seq=seq if seq_ok else NO_ZORDER,
                stroke=stroke,
                fill=fill,
            )
        )

    reasons = list(reasons_prefix)
    try:
        words = _build_words(char_dicts, width, height)
    except Exception as exc:  # chars and rects are still worth keeping
        words = []
        reasons.append(f"word grouping failed: {_describe(exc)}")

    ok, text_reasons = embedded_text_verdict(chars, words)
    return RawPage(
        number=number,
        width_pt=width,
        height_pt=height,
        rotation=rotation,
        chars=chars,
        rects=rects,
        words=words,
        embedded_text_ok=ok,
        embedded_text_reasons=reasons + text_reasons,
    )


def _overprinted(chars: Sequence[RawChar]) -> set[int]:
    """Indexes of invisible glyphs that a visible glyph is painted over.

    Invisible text alone is not a leak and must not be treated as one: every
    searchable scan in existence is an image with an invisible OCR
    transcription behind it, and dropping that would leave the page with no
    text at all. What *is* a leak is an invisible glyph with a visible one on
    top of it, because there is only one way a page comes to have both in the
    same place - a tool painted ``[REDACTED]`` over the name and left the name
    where it was. pdfminer does not implement render mode, so the name is
    extracted, interleaved with the stamp by the word grouper, and published.

    Positional rather than proportional on purpose. A scanned page routinely
    carries a visible Bates stamp over an invisible OCR layer, and a rule of
    the form "mostly invisible means OCR" would drop the whole transcription of
    any page where that balance tipped. Overlap only fires where the two layers
    genuinely disagree about the same piece of paper.
    """
    visible = [c.box for c in chars if not c.invisible and c.text.strip()]
    if not visible:
        return set()
    return {
        i
        for i, char in enumerate(chars)
        if char.invisible
        and char.text.strip()
        and any(char.box.overlap_ratio(box) > 0.5 for box in visible)
    }


def _rotation(page: PDFPage) -> int:
    try:
        return int(page.rotate) % 360
    except (TypeError, ValueError):
        return 0


def _mediabox_size(page: PDFPage, rotation: int) -> tuple[float, float]:
    """Page size in points as displayed, without touching the content stream."""
    try:
        x0, y0, x1, y1 = (float(v) for v in page.mediabox)
    except (TypeError, ValueError):
        return (612.0, 792.0)
    width, height = abs(x1 - x0), abs(y1 - y0)
    if rotation in (90, 270):
        width, height = height, width
    return (width or 612.0, height or 792.0)


def _rect(value: Any) -> tuple[float, float, float, float] | None:
    """A PDF rectangle as ``(x0, y0, x1, y1)`` with the corners the right way up.

    pdfminer hands back whatever the file said, and the specification allows
    either diagonal, so ``[900 900 -100 -100]`` is a legal spelling of the
    rectangle from (-100, -100) to (900, 900). Normalising here is what lets
    the intersection below agree with poppler, which does the same.
    """
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (x0, y0, x1, y1))):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _display_frame(
    page: PDFPage, rotation: int, page_bbox: tuple[float, ...]
) -> tuple[float, float, float, float]:
    """The rectangle a reader sees, in pdfminer's device coordinates.

    Returns ``(width, height, dx, dy)``: the size of the displayed page and the
    offset of its lower-left corner within the frame pdfminer ran the content
    stream in.

    pdfminer maps the **MediaBox** onto ``(0, 0, W, H)`` and executes the page
    there, with ``/Rotate`` already applied. Poppler, ``pdfinfo`` and every
    viewer show the **CropBox** intersected with the MediaBox. Where the two
    differ, a box measured against pdfminer's frame and confirmed against
    poppler's pixels is looking somewhere else on the page entirely - so the
    crop is folded in here, once, and every coordinate in :func:`read_page` is
    relative to it.

    The intersection, the normalisation and the fall back to the MediaBox on a
    degenerate CropBox are all chosen to match what poppler does, because
    "matches poppler" is the whole property: verified against poppler 24.02 for
    a crop inside the media box, one written back to front, one larger than the
    page, and one under ``/Rotate 90``.
    """
    x0, y0, x1, y1 = page_bbox
    width = abs(x1 - x0) or _mediabox_size(page, rotation)[0]
    height = abs(y1 - y0) or _mediabox_size(page, rotation)[1]

    media = _rect(getattr(page, "mediabox", None))
    crop = _rect(getattr(page, "cropbox", None))
    if media is None or crop is None:
        return width, height, 0.0, 0.0
    mx0, my0, mx1, my1 = media
    if mx1 <= mx0 or my1 <= my0:
        # A media box with no area: pdfminer has already fallen back to
        # something usable and second-guessing it here would only add a frame.
        return width, height, 0.0, 0.0
    cx0, cy0 = max(mx0, crop[0]), max(my0, crop[1])
    cx1, cy1 = min(mx1, crop[2]), min(my1, crop[3])
    if cx1 <= cx0 or cy1 <= cy0:
        return width, height, 0.0, 0.0  # empty overlap: poppler draws the media box

    # The same transform pdfminer applies to the content stream, applied to the
    # crop rectangle: x rightwards, y upwards, origin at the media box corner
    # that ``/Rotate`` puts bottom-left.
    if rotation == 90:
        dx, dy, w, h = cy0 - my0, mx1 - cx1, cy1 - cy0, cx1 - cx0
    elif rotation == 180:
        dx, dy, w, h = mx1 - cx1, my1 - cy1, cx1 - cx0, cy1 - cy0
    elif rotation == 270:
        dx, dy, w, h = my1 - cy1, cx0 - mx0, cy1 - cy0, cx1 - cx0
    else:
        dx, dy, w, h = cx0 - mx0, cy0 - my0, cx1 - cx0, cy1 - cy0
    if w <= 0 or h <= 0:  # pragma: no cover - excluded by the checks above
        return width, height, 0.0, 0.0
    return w, h, dx, dy


def _flip(
    bbox: tuple[float, ...], page_height: float, dx: float = 0.0, dy: float = 0.0
) -> tuple[float, float, float, float]:
    """pdfminer bbox to ``(top, bottom, left, right)`` measured downwards.

    *dx*/*dy* move the origin from the media box's corner to the displayed
    page's - see :func:`_display_frame`. They are zero for the overwhelming
    majority of pages, where the two are the same rectangle.

    Boxes are deliberately *not* clamped to the page. Content that spills past
    the media box is real - full-bleed scans do it constantly - and clamping
    would quietly move a redaction box away from what it covers.
    """
    x0, y0, x1, y1 = bbox
    return (
        page_height - (max(y0, y1) - dy),
        page_height - (min(y0, y1) - dy),
        min(x0, x1) - dx,
        max(x0, x1) - dx,
    )


# --------------------------------------------------------------------------
# running the content stream
# --------------------------------------------------------------------------


def _paint(
    handle: PdfHandle, page: PDFPage
) -> tuple[list[tuple[int, LTChar, Any, bool]], list[tuple[int, tuple[float, ...], bool, bool, Any]], tuple[float, float, float, float]]:
    """Execute one page's content stream, keeping draw order."""
    device = _ZOrderDevice(handle._rsrcmgr)
    PDFPageInterpreter(handle._rsrcmgr, device).process_page(page)
    return device.glyphs, device.paths, device.page_bbox


def _paint_without_order(
    handle: PdfHandle, page: PDFPage
) -> tuple[list[tuple[int, LTChar, Any, bool]], list[tuple[int, tuple[float, ...], bool, bool, Any]], tuple[float, float, float, float]]:
    """Same, using stock pdfminer, when the instrumented device would not run.

    The layout tree happens to be in insertion order today, but nested figures
    and form XObjects make that an implementation detail rather than a promise,
    so everything from here is stamped :data:`NO_ZORDER`. Text and geometry
    still come through; only the redaction check loses its evidence.
    """
    device = PDFPageAggregator(handle._rsrcmgr, laparams=None)
    PDFPageInterpreter(handle._rsrcmgr, device).process_page(page)
    ltpage = device.get_result()

    glyphs: list[tuple[int, LTChar, Any, bool]] = []
    paths: list[tuple[int, tuple[float, ...], bool, bool, Any]] = []

    def walk(container: LTContainer) -> None:
        for obj in container:
            if isinstance(obj, LTChar):
                # Stock pdfminer does not keep the render mode, so this path
                # cannot tell an invisible glyph from a visible one and says so
                # by claiming nothing.
                glyphs.append((NO_ZORDER, obj, getattr(obj, "graphicstate", None), False))
            elif isinstance(obj, LTCurve):
                paths.append(
                    (
                        NO_ZORDER,
                        obj.bbox,
                        bool(getattr(obj, "stroke", False)),
                        bool(getattr(obj, "fill", False)),
                        getattr(obj, "non_stroking_color", None),
                    )
                )
            elif isinstance(obj, LTContainer):
                walk(obj)

    walk(ltpage)
    return glyphs, paths, ltpage.bbox


# --------------------------------------------------------------------------
# words
# --------------------------------------------------------------------------


def _build_words(
    char_dicts: list[dict[str, Any]], width: float, height: float
) -> list[Word]:
    """Group characters into words and lines, in reading order.

    The grouping itself is pdfplumber's, which knows about ligatures, rotated
    runs and the difference between a wide space and a word gap. What it does
    not produce is a line index, and the page HTML needs one to reflow the text
    for a reader with no JavaScript. So lines are clustered here on the vertical
    centre of each word, with a tolerance of 0.4 of the median character height
    - loose enough to hold a line containing a superscript, tight enough to keep
    consecutive lines apart at any font size.

    Order is line, then left to right, and that order is load-bearing: it is the
    order the page HTML emits tokens in, and the search index reports hits as
    indices into it (ARCHITECTURE.md, guarantee 3).

    One caveat for whoever builds the pages. The search contract says CJK must
    be emitted one character per token, or Pagefind re-segments and the indices
    drift. pdfplumber returns CJK runs as multi-character words, so a builder
    that splits them itself would break guarantee 3. The split has to happen
    here, on the ``Word`` list, or not at all - it is not implemented yet, and a
    CJK collection will mis-highlight until it is.

    Known wrong for right-to-left scripts. Arabic and Hebrew words come out
    boxed correctly and ordered backwards, because "left to right" is hardcoded
    here. Fixing it means detecting the dominant script per line and reversing,
    which changes the search contract as much as it changes this function.

    Known wrong, for the same reason, on a page that displays upside down: text
    marked ``/Rotate 180`` whose content was *not* written upside down reads
    right to left on screen, and comes back with the letters of each word
    reversed. A page rotated to *correct* upside-down content - the reason
    ``/Rotate 180`` normally exists - is handled correctly.
    """
    if not char_dicts:
        return []

    raw = extract_words(char_dicts)
    if not raw:
        return []

    heights = [c["bottom"] - c["top"] for c in char_dicts if c["bottom"] > c["top"]]
    tolerance = 0.4 * median(heights) if heights else 1.0

    by_centre = sorted(raw, key=lambda w: ((w["top"] + w["bottom"]) / 2.0, w["x0"]))
    lines: list[int] = [0] * len(by_centre)
    previous: float | None = None
    line = 0
    for i, word in enumerate(by_centre):
        centre = (word["top"] + word["bottom"]) / 2.0
        if previous is not None and centre - previous > tolerance:
            line += 1
        lines[i] = line
        previous = centre

    numbered = sorted(
        zip(by_centre, lines, strict=True), key=lambda pair: (pair[1], pair[0]["x0"])
    )
    return [
        Word(
            text=word["text"],
            box=Box.from_pdf_rect(
                word["x0"], word["top"], word["x1"], word["bottom"], width, height
            ),
            line=line_index,
        )
        for word, line_index in numbered
    ]


# --------------------------------------------------------------------------
# is the text layer worth believing
# --------------------------------------------------------------------------

# The word lists live in `stackroom.lang`, which is the module that answers
# "does this look like language" for the whole project. This file used to keep
# a private five-language union of its own, on the theory that a check running
# on every page must not import anything expensive - but `lang` is stdlib-only
# literals and imports in half a millisecond, against the 135ms of pdfminer and
# pdfplumber this module already costs, and it is loaded once per process
# rather than once per page. The duplicate cost a Hindi text layer: a hand-made
# union of five European languages has no opinion about Devanagari and said so
# in the words "not prose in any language we check", which then threw the layer
# away before `ingest/quality.py` was ever asked.
MIN_WORDS_FOR_STOPWORDS = 25
"""Below this the stopword ratio says nothing. A cover sheet, a table of
figures or a title page can hold twenty real words and not one function word."""

LOW_STOPWORD_RATIO = 0.05
"""Below this share of function words, a decoded text layer is not language.

Deliberately half of ``ingest/quality.py``'s threshold for the same signal. The
question there is whether a page is worth publishing; the question here is only
whether the glyphs decoded to *anything*, and a layer this file rejects is one
recognition then has to redo. Real prose scores 0.25 to 0.38 against these
lists, so nothing near the line is a close call.
"""

PUA_LIMIT = 0.30
CONTROL_LIMIT = 0.20


def _is_private_use(ch: str) -> bool:
    return "" <= ch <= ""


def _is_control(ch: str) -> bool:
    """Control codes and the replacement character; tab and newline excluded."""
    if ch == "\ufffd":
        return True
    if ch in "\t\n\r\f\v":
        return False
    return unicodedata.category(ch) == "Cc"


def embedded_text_verdict(
    chars: list[RawChar], words: list[Word]
) -> tuple[bool, list[str]]:
    """Decide whether the page's own text layer can be published.

    The failure this catches is a page that *looks* fine in a viewer and yields
    nonsense when copied: a symbolic font with no usable ``ToUnicode`` map, a
    subset font whose encoding was dropped, a scanner that wrote a text layer
    made of replacement characters. Search over such a page is worse than no
    search, because it returns nothing and reports that the page was searched.

    Returns ``(ok, reasons)``. ``reasons`` is populated whenever ``ok`` is
    False, and empty otherwise.
    """
    if not chars:
        return False, ["no embedded text layer on this page"]

    reasons: list[str] = []
    text = "".join(c.text for c in chars)
    total = len(text)

    pua = sum(1 for ch in text if _is_private_use(ch))
    if total and pua / total > PUA_LIMIT:
        reasons.append(
            f"{pua / total:.0%} of characters are in the Unicode private-use area "
            "(a symbolic font, or a missing ToUnicode map)"
        )

    control = sum(1 for ch in text if _is_control(ch))
    if total and control / total > CONTROL_LIMIT:
        reasons.append(
            f"{control / total:.0%} of characters are control codes or U+FFFD"
        )

    if not text.strip():
        reasons.append(f"{total} glyphs on the page decode to nothing but whitespace")

    tokens = [w.text for w in words if w.text and w.text.strip()]
    # Only where a word list covers the script. A stopword count has no opinion
    # at all about an alphabet it has no words for, and the zero it returns
    # there means "we cannot say", not "this is garbage" - see
    # :func:`stackroom.lang.stopwords_apply`. Without the guard a perfectly good
    # Devanagari, Thai or Japanese text layer is condemned here and replaced
    # with OCR run in languages nobody asked for.
    if len(tokens) >= MIN_WORDS_FOR_STOPWORDS and stopwords_apply(" ".join(tokens)):
        ratio = stopword_ratio(tokens)
        if ratio < LOW_STOPWORD_RATIO:
            reasons.append(
                f"stopword ratio {ratio:.3f} across {len(tokens)} words - the "
                "decoded text is not prose in any language we check"
            )

    return (not reasons), reasons


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------

_META_KEYS = {
    "/Title": "title",
    "/Author": "author",
    "/Subject": "subject",
    "/Keywords": "keywords",
    "/Creator": "creator",
    "/Producer": "producer",
    "/CreationDate": "created",
    "/ModDate": "modified",
}


def document_meta(path: Path) -> dict[str, str]:
    """Whatever the file claims about itself, as plain lowercase-keyed strings.

    Claims, not facts: ``/Author`` is written by whichever tool produced the
    file and is routinely the name of the person who scanned the release rather
    than the person who wrote the memo. It is recorded because it is evidence
    about the production, not because it is true.
    """
    path = Path(path)
    raw = _meta_from_pypdf(path)
    if raw is None:
        raw = _meta_from_pdfminer(path)
    if raw is None:
        raise PdfDamagedError(f"{path}: no readable metadata; the file is not a usable PDF")

    out: dict[str, str] = {}
    for key, name in _META_KEYS.items():
        value = raw.get(key)
        if value is None:
            continue
        text = _pdf_date(str(value)) if name in ("created", "modified") else str(value)
        text = text.strip()
        if text:
            out[name] = text
    return out


def _meta_from_pypdf(path: Path) -> dict[str, Any] | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        info = reader.metadata
        return {} if info is None else dict(info)
    except Exception:  # pdfminer gets a turn before we give up
        return None


def _meta_from_pdfminer(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as fp:
            document = PDFDocument(PDFParser(fp), password="")
            info = document.info[0] if document.info else {}
            return {
                f"/{k}" if not k.startswith("/") else k: _decode(v)
                for k, v in dict(info).items()
            }
    except Exception:  # the caller turns this into PdfDamagedError
        return None


def _decode(value: Any) -> str:
    """pdfminer hands back raw bytes for text strings; PDF allows two encodings."""
    if isinstance(value, bytes):
        if value[:2] in (b"\xfe\xff", b"\xff\xfe"):
            return value.decode("utf-16", errors="replace")
        return value.decode("latin-1", errors="replace")
    return str(value)


def _pdf_date(value: str) -> str:
    """``D:20240115093000+01'00'`` to ``2024-01-15T09:30:00+01:00``.

    Anything that does not fit the pattern is returned untouched. A date we
    cannot parse is still evidence; silently dropping it would not be.
    """
    text = value.strip()
    if not text.startswith("D:"):
        return text
    digits = text[2:]
    if len(digits) < 8 or not digits[:8].isdigit():
        return text
    stamp = f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) >= 14 and digits[8:14].isdigit():
        stamp += f"T{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
    elif len(digits) >= 12 and digits[8:12].isdigit():
        stamp += f"T{digits[8:10]}:{digits[10:12]}:00"
    else:
        return stamp
    zone = digits[14:]
    if zone[:1] == "Z":
        return stamp + "+00:00"
    if zone[:1] in "+-" and len(zone) >= 3:
        return f"{stamp}{zone[0]}{zone[1:3]}:{zone[4:6] or '00'}"
    return stamp


# --------------------------------------------------------------------------
# publishing a sanitised copy
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PublishedFile:
    """What was actually written into ``files/``, and how it differs.

    The digest is of the bytes on disk, which is *not* the digest of the source
    once anything has been stripped. Both belong in the manifest and both have
    to be labelled, because a reader checking an archive against what the
    agency sent needs to know which number is which.
    """

    sha256: str
    """SHA-256 of the file that was written."""

    stripped: bool
    """True when metadata and revision history were removed on the way."""

    note: str = ""
    """Empty on success. Otherwise a sentence for the operator saying what did
    not happen, because a *silently* unstripped original is how this option
    becomes worse than not having it."""


def publish_pdf(source: Path, destination: Path, *, strip: bool = False) -> PublishedFile:
    """Copy *source* to *destination*, optionally without its metadata.

    With ``strip=False`` this is a byte-for-byte copy and the archive stays
    verifiable against what the agency sent.

    With ``strip=True`` the file is **rewritten from its page tree**, which is
    the part that matters: an incremental save keeps every earlier revision of
    every object it replaced, so a "corrected" release routinely still contains
    the uncorrected text, and no amount of deleting dictionary entries removes
    it. Writing a new file from the pages is what leaves the earlier revisions
    behind. Document ``/Info`` and XMP go with them.

    What that costs, stated plainly because both halves are true:

    * The published file is **no longer byte-identical to the source**, so its
      SHA-256 no longer matches the one in the manifest. That is why this
      returns the digest of what it wrote.
    * A rewrite is not a sanitiser. Page annotations survive. Anything hanging
      off the document catalogue - bookmarks, embedded attachments, the
      AcroForm field tree - does not, because a fresh catalogue is what drops
      the revision history.

    Never raises. A file that cannot be rewritten is copied unchanged with the
    reason in ``note``, because failing the build over a courtesy would be a
    worse answer than publishing what the operator already has.
    """
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if strip:
        note = (
            _rewrite_without_metadata(source, destination)
            if _is_pdf(source)
            # Reported rather than passed over in silence. An operator who set
            # the option believes something happened to every file in the
            # release, and a page image carries EXIF that this does not touch.
            else "it is not a PDF, so nothing was stripped from it"
        )
        if not note:
            return PublishedFile(sha256=_digest(destination), stripped=True)
    shutil.copy2(source, destination)
    return PublishedFile(sha256=_digest(destination), stripped=False, note=note)


def _is_pdf(path: Path) -> bool:
    """By the header, not the extension: the name is the attacker's to choose."""
    try:
        with Path(path).open("rb") as fh:
            return b"%PDF-" in fh.read(1024)
    except OSError:
        return False


def _rewrite_without_metadata(source: Path, destination: Path) -> str:
    """Write *source* to *destination* stripped. Returns "" on success.

    The return value is a reason rather than a bool so the caller has something
    to print. Every failure mode here ends in "so the file was published
    unchanged", which the operator has to be told about.
    """
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject

        reader = PdfReader(str(source))
        if reader.is_encrypted and not reader.decrypt(""):
            return "it is encrypted, and stripping it would need the password"
        writer = PdfWriter()
        for page in reader.pages:
            # Unlinked before the page is copied, not after: pypdf writes every
            # object it has been handed, so a page-level XMP stream that is
            # merely dereferenced still lands in the output.
            for key in ("/Metadata", "/PieceInfo"):
                if key in page:
                    del page[NameObject(key)]
            writer.add_page(page)
        writer.metadata = None  # otherwise pypdf stamps itself as /Producer
        with destination.open("wb") as fh:
            writer.write(fh)
    except Exception as exc:  # a courtesy that fails must not stop a build
        with contextlib.suppress(OSError):
            destination.unlink(missing_ok=True)
        return f"it could not be rewritten ({_describe(exc)})"
    return ""


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
