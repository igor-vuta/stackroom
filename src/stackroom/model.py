"""The data model.

This module is the contract every other module writes against. Everything the
ingest pipeline learns about a document ends up here, and everything the site
builder renders comes from here. Nothing in this file touches the filesystem,
the network, or a PDF; it is plain data, so it can be constructed in a test in
three lines.

Coordinates
-----------
Every box in this file is expressed in *page-relative* units: ``x`` and ``w``
are fractions of page width, ``y`` and ``h`` fractions of page height, origin
at the top-left, y growing downwards (screen convention, not PDF convention).
That way a box is valid whatever resolution the page is later rendered at, and
the browser can use it directly as a CSS percentage.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Box",
    "BuildInfo",
    "Collection",
    "CollectionStats",
    "Document",
    "HiddenText",
    "ImageVariant",
    "OcrQuality",
    "Page",
    "PageVerdict",
    "Redaction",
    "RedactionKind",
    "TextSource",
    "Word",
    "build_timestamp",
    "source_date_epoch",
]

SCALE = 10_000
"""Fixed-point scale used when boxes are serialised to JSON.

Page-relative floats are multiplied by this and rounded to integers. Four
decimal places is finer than a single pixel on a 5000px-wide scan, and integers
compress far better than floats in gzipped JSON.
"""


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Box:
    """A rectangle in page-relative coordinates, origin top-left."""

    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    def intersection(self, other: Box) -> Box | None:
        x1 = max(self.x, other.x)
        y1 = max(self.y, other.y)
        x2 = min(self.x2, other.x2)
        y2 = min(self.y2, other.y2)
        if x2 <= x1 or y2 <= y1:
            return None
        return Box(x1, y1, x2 - x1, y2 - y1)

    def union(self, other: Box) -> Box:
        x1 = min(self.x, other.x)
        y1 = min(self.y, other.y)
        x2 = max(self.x2, other.x2)
        y2 = max(self.y2, other.y2)
        return Box(x1, y1, x2 - x1, y2 - y1)

    def overlap_ratio(self, other: Box) -> float:
        """Fraction of *self* that falls inside *other*."""
        if self.area <= 0:
            return 0.0
        inter = self.intersection(other)
        return 0.0 if inter is None else inter.area / self.area

    def to_ints(self) -> tuple[int, int, int, int]:
        return (
            round(self.x * SCALE),
            round(self.y * SCALE),
            round(self.w * SCALE),
            round(self.h * SCALE),
        )

    @classmethod
    def from_ints(cls, xs: tuple[int, int, int, int]) -> Box:
        return cls(xs[0] / SCALE, xs[1] / SCALE, xs[2] / SCALE, xs[3] / SCALE)

    @classmethod
    def from_pdf_rect(
        cls, x0: float, top: float, x1: float, bottom: float, pw: float, ph: float
    ) -> Box:
        """Build from pdfplumber's ``(x0, top, x1, bottom)`` in points.

        pdfplumber already gives ``top``/``bottom`` measured downwards from the
        top of the page, which is what we want.
        """
        if pw <= 0 or ph <= 0:
            return cls(0.0, 0.0, 0.0, 0.0)
        return cls(x0 / pw, top / ph, (x1 - x0) / pw, (bottom - top) / ph)


# --------------------------------------------------------------------------
# words
# --------------------------------------------------------------------------

CONF_UNKNOWN = -1
"""Confidence value for text that was not produced by OCR."""


@dataclass(slots=True)
class Word:
    """One token of the page's text layer, with the box it occupies.

    The *order* of ``Page.words`` is load-bearing. The page HTML emits these
    tokens in exactly this order, whitespace-separated, and the search index
    reports matches as indices into that same sequence. If the order here and
    the order in the HTML ever diverge, highlights land on the wrong words.
    """

    text: str
    box: Box
    conf: int = CONF_UNKNOWN
    """0-100 from OCR, or ``CONF_UNKNOWN`` for an embedded text layer."""

    line: int = 0
    """Index of the line this word belongs to, for reflowing the text layer."""

    hidden: bool = False
    """True when this word is covered by an opaque shape - i.e. a failed
    redaction. Hidden words are never written into the published site."""


# --------------------------------------------------------------------------
# renderings
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ImageVariant:
    """One rendering of a page, at one width in one format."""

    path: str
    """Relative to the site root, e.g. ``media/memo-2019/p0001@1600.avif``."""
    format: str
    width: int
    height: int
    bytes: int = 0


# --------------------------------------------------------------------------
# redactions
# --------------------------------------------------------------------------


class RedactionKind(str, Enum):
    VECTOR = "vector"
    """A filled rectangle found in the PDF content stream."""
    RASTER = "raster"
    """A solid block found by analysing the rendered page image."""


@dataclass(slots=True)
class Redaction:
    """One blacked-out region on a page."""

    box: Box
    kind: RedactionKind
    codes: list[str] = field(default_factory=list)
    """Exemption codes associated with this box, e.g. ``["b(6)", "b(7)(C)"]``."""


@dataclass(slots=True)
class HiddenText:
    """Text that is visually covered but still present in the file.

    This is a failed redaction: anyone who downloads the original PDF can
    recover the text. Stackroom treats a single instance of this as a reason to
    stop the build.
    """

    box: Box
    text: str
    """Kept in memory so the CLI can show the operator what leaked. It is never
    written to the published site, and never to any file on disk."""

    def redacted_repr(self) -> str:
        """A shape-preserving stand-in, for logs that may be pasted publicly."""
        return "".join("#" if ch.isalnum() else ch for ch in self.text)


# --------------------------------------------------------------------------
# OCR quality
# --------------------------------------------------------------------------


class PageVerdict(str, Enum):
    """What we believe about this page's text, in plain terms."""

    GOOD = "good"
    """Text was read with reasonable confidence."""
    BLANK = "blank"
    """The page really is empty. Not a failure."""
    PICTORIAL = "pictorial"
    """A photograph, map or chart with no meaningful text. Not a failure."""
    SUSPECT = "suspect"
    """Text was produced but looks like garbage. Search will mislead here."""
    UNREADABLE = "unreadable"
    """The page has ink on it and we read nothing. Search cannot find it."""

    @property
    def is_failure(self) -> bool:
        return self in (PageVerdict.SUSPECT, PageVerdict.UNREADABLE)


@dataclass(slots=True)
class OcrQuality:
    """Evidence behind a :class:`PageVerdict`.

    Mean confidence is deliberately *not* the headline number. Tesseract emits
    nothing at all for text it fails to segment, so the mean is computed only
    over words it was already confident about - it is high precisely when it is
    least informative. The stopword ratio is the load-bearing signal: real prose
    in any language is full of short function words, and OCR garbage is not.
    """

    verdict: PageVerdict = PageVerdict.GOOD
    word_count: int = 0
    median_conf: float = 0.0
    low_conf_fraction: float = 0.0
    stopword_ratio: float = 0.0
    garbage_ratio: float = 0.0
    mean_word_length: float = 0.0
    ink_coverage: float = 0.0
    reasons: list[str] = field(default_factory=list)
    """Human-readable notes, e.g. ``["stopword ratio 0.02", "median confidence 41"]``."""


# --------------------------------------------------------------------------
# pages and documents
# --------------------------------------------------------------------------


class TextSource(str, Enum):
    EMBEDDED = "embedded"
    """Text came from the PDF's own text layer."""
    OCR = "ocr"
    """Text was recognised from the rendered image."""
    OCR_OVERRIDE = "ocr-override"
    """The embedded layer was broken, so OCR was used instead."""
    NONE = "none"
    """No text was recovered."""


@dataclass(slots=True)
class Page:
    number: int
    """1-based, as printed on the page and as it appears in the URL."""

    width_pt: float = 612.0
    height_pt: float = 792.0

    source: TextSource = TextSource.NONE
    words: list[Word] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    """The text layer reflowed into lines, for display and for no-JS reading."""

    redactions: list[Redaction] = field(default_factory=list)
    hidden: list[HiddenText] = field(default_factory=list)
    exemptions: list[str] = field(default_factory=list)
    bates: str | None = None
    quality: OcrQuality = field(default_factory=OcrQuality)

    redaction_ratio: float = 0.0
    """Redacted area as a fraction of the page's *inked region* - the union of
    surviving text and the redaction boxes themselves. Page area is the wrong
    denominator: a fully blacked-out letter page has one-inch margins, so it
    would score 63% when the honest answer is 100%."""

    images: list[ImageVariant] = field(default_factory=list)
    """Renderings of the page, one per (width, format). The page template turns
    these into a ``<picture>`` with a ``srcset`` per format, so a phone on a
    slow connection fetches 54 KB and a desktop fetches 96 KB."""

    thumbs: list[ImageVariant] = field(default_factory=list)

    placeholder: str = ""
    """A 24 px-wide WebP of this page as a ``data:`` URI, or ``""``.

    Inline in the page's own HTML so the scan's frame holds a picture of the
    page while the real image is still arriving - measured at 622 ms against
    2,950 ms on a Fast 3G connection, for about 140 gzipped bytes. It buys
    nothing for layout stability: ``aspect-ratio`` already reserves the exact
    box, so there is no shift left to remove.

    It is a placeholder and never evidence. Twenty-four pixels wide, quantised
    to a handful of grey levels: no word on it is legible, and nothing should
    ever be read off it. A page without one renders exactly as it did before
    this field existed."""

    language: str = ""
    """Detected language of this page's text, when we are confident enough to
    name one. Blank means we are not."""

    @property
    def aspect(self) -> float:
        return self.height_pt / self.width_pt if self.width_pt else 1.294

    @property
    def image_width(self) -> int:
        return max((v.width for v in self.images), default=0)

    @property
    def image_height(self) -> int:
        return max((v.height for v in self.images), default=0)

    def variants(self, fmt: str) -> list[ImageVariant]:
        """All renderings in one format, widest last."""
        return sorted((v for v in self.images if v.format == fmt), key=lambda v: v.width)

    @property
    def formats(self) -> list[str]:
        """Formats present, best first. AVIF is ~31% smaller than WebP on a
        typed page, so it is offered first and browsers that cannot read it
        fall through."""
        order = {"avif": 0, "webp": 1, "jpeg": 2, "png": 3}
        seen = {v.format for v in self.images}
        return sorted(seen, key=lambda f: order.get(f, 9))

    @property
    def fallback(self) -> ImageVariant | None:
        """The widest rendering in the most compatible format, for ``<img src>``."""
        if not self.images:
            return None
        compat = [v for v in self.images if v.format in ("webp", "jpeg", "png")]
        return max(compat or self.images, key=lambda v: v.width)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def token_text(self) -> str:
        """The exact whitespace-joined token string the search index sees."""
        return " ".join(w.text for w in self.words)

    @property
    def has_redactions(self) -> bool:
        return bool(self.redactions)


@dataclass(slots=True)
class Document:
    id: str
    """URL-safe slug, unique within the collection."""

    title: str
    filename: str
    sha256: str
    """SHA-256 of the file as it arrived. Not necessarily the digest of the
    copy in ``files/``: see ``safety.strip_metadata``, which rewrites it."""

    size_bytes: int
    pages: list[Page] = field(default_factory=list)

    kind: str = ""
    """What :mod:`stackroom.ingest.discover` decided this file *is*, by its
    magic number: ``"pdf"``, ``"image"``, ``"text"``. Empty on a document
    assembled by hand.

    Carried this far for one reason: the original is published under an
    extension derived from this, never from the name the producer chose. A file
    can be a valid PDF and a valid HTML document at once, and published as
    ``annual-report.html`` it is served as ``text/html`` from this archive's
    own origin - which, unlike every page the builder generates, carries no
    Content-Security-Policy."""

    meta: dict[str, str] = field(default_factory=dict)
    """Whatever the file claimed about itself: author, producer, dates."""

    source_path: str | None = None
    """Where the original file was read from, on the machine that built the
    site. Used to copy the original into the output; never published."""

    source_url: str | None = None
    notes: str | None = None
    """Operator-written context, rendered above the page grid. Markdown."""

    bates_prefix: str | None = None
    bates_gaps: list[tuple[str, str]] = field(default_factory=list)
    """Missing ranges in the control-number sequence. A gap means pages were
    withheld in full, which is usually the most interesting thing on the page
    that is not there."""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def redacted_pages(self) -> int:
        return sum(1 for p in self.pages if p.has_redactions)

    @property
    def unreadable_pages(self) -> int:
        return sum(1 for p in self.pages if p.quality.verdict.is_failure)

    @property
    def has_hidden_text(self) -> bool:
        return any(p.hidden for p in self.pages)


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------


SOURCE_DATE_EPOCH = "SOURCE_DATE_EPOCH"
"""The reproducible-builds convention: a Unix timestamp to use as *now*.

https://reproducible-builds.org/docs/source-date-epoch/. Honoured because
``built_at`` is the one thing in a built site that is not a function of the
input bytes, and it is written into ``manifest.json`` and printed into the
footer of every page - so without this, two people who build the same folder on
different days differ in every file, and guarantee 6 in ``docs/ARCHITECTURE.md``
is false as stated.
"""


def source_date_epoch() -> int | None:
    """``SOURCE_DATE_EPOCH`` as a timestamp, or ``None`` if it is not usable.

    None means both "not set" and "set to something that is not a timestamp".
    The two are told apart by the caller: :func:`build_timestamp` treats a
    malformed value as unset so that a stray environment variable can never
    stop a build, and the CLI checks it up front and refuses, so a person who
    meant to pin the date is not quietly given the clock instead.
    """
    raw = os.environ.get(SOURCE_DATE_EPOCH, "").strip()
    if not raw:
        return None
    try:
        stamp = int(raw)
    except ValueError:
        return None
    try:
        _dt.datetime.fromtimestamp(stamp, _dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp


def build_timestamp() -> str:
    """The build's timestamp: ``SOURCE_DATE_EPOCH`` where it is set, else now.

    UTC and whole seconds either way, so the string is the same shape whichever
    it came from.
    """
    stamp = source_date_epoch()
    when = (
        _dt.datetime.fromtimestamp(stamp, _dt.timezone.utc)
        if stamp is not None
        else _dt.datetime.now(_dt.timezone.utc)
    )
    return when.replace(microsecond=0).isoformat()


@dataclass(slots=True)
class BuildInfo:
    """What was built, when, by which version, from which bytes.

    An archive earns trust by being checkable. This block is rendered in the
    footer of every page and written to ``manifest.json``.
    """

    version: str = "0.0.0"
    built_at: str = field(default_factory=build_timestamp)
    """When the build ran, UTC. Set from ``SOURCE_DATE_EPOCH`` where that is
    set - see :func:`build_timestamp` - which is what makes a byte-identical
    rebuild possible at all."""
    source_digest: str = ""
    """SHA-256 over the sorted per-file digests: one number for the whole input."""
    tool_versions: dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0


@dataclass(slots=True)
class CollectionStats:
    """The numbers the front page prints and ``manifest.json`` publishes.

    The withheld share is the most consequential of them: it is the one a
    reporter puts in a first paragraph, and until it was fixed it was a mean of
    per-page ratios over the pages that happened to have text - which gave a
    one-line page the same say as a dense one, and dropped a page blacked out
    end to end out of the arithmetic entirely, because it has no surviving
    words. :func:`stackroom.pipeline.summarise` computes what is here; the four
    area fields are carried so that anyone can check the division.
    """

    documents: int = 0
    pages: int = 0
    words: int = 0
    pages_with_redactions: int = 0
    redaction_boxes: int = 0

    redaction_ratio: float = 0.0
    """Share of the content **on the pages that carry redactions** that is
    withheld: :attr:`withheld_area_pt` over :attr:`redacted_pages_area_pt`.

    This is the number on the front page and on ``withheld/index.html``, and
    both say what it is a share of in the sentence beside it. Not a mean of
    per-page shares: it is those shares weighted by how much content is on each
    page, which is the same thing as total withheld area over total inked area
    wherever a page's share was measured from its own text layer."""

    redaction_ratio_collection: float = 0.0
    """The same division taken over **every** page, redacted or not:
    :attr:`withheld_area_pt` over :attr:`collection_area_pt`.

    A different fact from :attr:`redaction_ratio` and not a substitute for it -
    one page withheld in full out of a thousand is 100% of that page and 0.1%
    of the release, and a reader needs to be told which one they are looking
    at. Published in ``manifest.json`` and printed by the CLI."""

    withheld_area_pt: float = 0.0
    """Withheld content across the collection, in square points."""

    redacted_pages_area_pt: float = 0.0
    """Inked content - surviving text plus the boxes that replaced text - on
    the pages that carry redactions, in square points."""

    collection_area_pt: float = 0.0
    """The same, over every page in the collection."""

    unmeasured_pages: int = 0
    """Pages with no measurable content at all: no words and no boxes.

    A blank page, or a scan whose text could not be recognised and that has no
    black box on it. They contribute to neither side of the division, which is
    the only honest thing to do with a page whose content cannot be measured -
    but it has to be *said*, or the denominator quietly excludes them."""

    unreadable_pages: int = 0
    pictorial_pages: int = 0
    blank_pages: int = 0
    ocr_pages: int = 0
    exemption_counts: dict[str, int] = field(default_factory=dict)
    languages: list[str] = field(default_factory=list)
    bytes_total: int = 0


@dataclass(slots=True)
class Collection:
    title: str = "Untitled collection"
    description: str = ""
    about_html: str = ""
    """Rendered from ``about.md``. The provenance narrative: who released these
    documents, when, under what request, and what is missing."""

    documents: list[Document] = field(default_factory=list)
    stats: CollectionStats = field(default_factory=CollectionStats)
    build: BuildInfo = field(default_factory=BuildInfo)

    base_url: str = ""
    language: str = "en"
    jurisdiction: str = "us"
    """Which exemption vocabulary to use when labelling redactions."""

    def document(self, doc_id: str) -> Document | None:
        for d in self.documents:
            if d.id == doc_id:
                return d
        return None


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------


def to_jsonable(obj: Any) -> Any:
    """Convert dataclasses, enums and boxes into plain JSON types.

    Boxes become four integers; that is the only clever part.
    """
    if isinstance(obj, Box):
        return list(obj.to_ints())
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            out[f.name] = to_jsonable(getattr(obj, f.name))
        return out
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
