"""From a folder of files to a :class:`~stackroom.model.Collection`.

This is the part that decides *what to do*, page by page: whether the PDF's own
text can be trusted or the page has to be read from the image, whether what came
back is worth publishing, what is blacked out, and whether anything blacked out
is still recoverable from the file.

Everything expensive happens in :func:`process_page`, which is a pure function
of a picklable job. That is not an accident: it lets the whole collection be
processed by a pool of workers with no shared state, and it lets a single
misbehaving page be reproduced on its own from one line in a log.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import __version__ as _pkg_version
from . import cache as cache_mod
from .config import Config
from .imaging import to_gray
from .ingest import bates, discover, exemptions, ocr, pdf, quality, raster, redaction
from .lang import (
    detect_language,
    language_names,
    normalize_language_codes,
    script_of,
    stopwords_apply,
)
from .model import (
    Box,
    BuildInfo,
    Collection,
    CollectionStats,
    Document,
    HiddenText,
    ImageVariant,
    Page,
    PageVerdict,
    TextSource,
    Word,
)

Progress = Callable[["ProgressEvent"], None]


@dataclass(slots=True)
class ProgressEvent:
    kind: str
    """``discover`` | ``page`` | ``document`` | ``note``"""
    done: int = 0
    total: int = 0
    label: str = ""
    detail: str = ""


class SafetyStop(RuntimeError):
    """The build found something it will not publish.

    Carries the findings so the CLI can show the operator exactly which page
    and, in outline, what leaked - without that text ever reaching a file.
    """

    def __init__(self, message: str, findings: list[tuple[str, int, list[HiddenText]]]):
        super().__init__(message)
        self.findings = findings


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PageJob:
    """Everything one worker needs, and nothing that cannot be pickled."""

    pdf: str
    doc_id: str
    number: int
    """1-based, as printed and as it appears in the URL."""
    media_dir: str
    media_prefix: str
    """Where the images will live relative to the site root."""

    dpi: int = 150
    widths: tuple[int, ...] = (1600, 900)
    thumb_width: int = 240
    formats: tuple[str, ...] = ("avif", "webp")
    max_megapixels: float = 40.0

    ocr_mode: str = "auto"
    ocr_languages: tuple[str, ...] = ("eng",)
    psm: int = 3
    auto_rotate: bool = True
    ocr_timeout: float = 120.0

    is_image: bool = False
    """The source is a single image file rather than a PDF page."""


@dataclass(slots=True)
class PageOutcome:
    doc_id: str
    number: int
    page: Page
    hidden: list[HiddenText] = field(default_factory=list)
    """Never serialised into the site. Held only long enough to report."""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0

    analysis_failed: bool = False
    """The redaction check itself crashed on this page.

    This is not the same as "no redactions found". If the check could not run,
    we do not know whether the page hides recoverable text, and treating an
    unknown as a clean bill of health is exactly how a leak gets published. The
    safety gate stops on this for the same reason it stops on a real finding."""


FULL_PAGE = Box(0.0, 0.0, 1.0, 1.0)

SAFETY_NOTES = (
    "black box",
    "could not render",
    "check this box by hand",
    "no page rendering was available to confirm",
    "without a draw order",
    "could not read the text layer",
    "crop renderer failed",
    "too small to judge",
    "suppressed",
)
"""Fragments that mark a page note as bearing on whether the page was checked.

Ordering the operator's notes needs to know which of them are about safety and
which are about legibility, and the notes themselves are plain sentences
written for a person. This is the seam: the strings live here, next to the code
in this module and in :mod:`stackroom.ingest.redaction` that writes them, and
:func:`note_is_about_safety` is the only thing allowed to read them. Adding a
warning that says "we could not check this" and forgetting to add it here
demotes it to a legibility note, which is why
``test_the_ambiguous_box_warning_is_ranked_as_a_safety_note`` pins the one that
matters most."""


def note_is_about_safety(message: str) -> bool:
    """Is this page note about whether the redaction check ran, or about text?

    Both kinds go to the operator. Only one of them is a reason to stop and
    look at the document before publishing it, so only one of them is allowed
    to be printed first and in red.
    """
    lowered = message.casefold()
    return any(fragment in lowered for fragment in SAFETY_NOTES)

_NOT_DOCUMENTS = frozenset(
    {"about.md", "stackroom.toml", "readme.md", "readme.txt", "notes.md", "license", "license.txt"}
)
"""Files that live beside a collection without being part of it."""


# --------------------------------------------------------------------------
# the worker
# --------------------------------------------------------------------------


def process_page(job: PageJob) -> PageOutcome:
    """Read, judge, render and analyse one page.

    Never raises for a bad page. A page that cannot be processed comes back
    with ``error`` set and an otherwise-empty :class:`Page`, because a
    collection that refuses to build over one corrupt scan out of 3,000 is a
    collection nobody publishes.
    """
    started = time.perf_counter()
    page = Page(number=job.number)
    outcome = PageOutcome(doc_id=job.doc_id, number=job.number, page=page)
    source = Path(job.pdf)

    try:
        image, dpi = _rasterise(job, source)
    except Exception as exc:  # one bad page must not stop 2,999 good ones
        outcome.error = f"could not render: {_describe(exc)}"
        # We only say a page is clean when we actually looked at it, with both
        # passes. A page we could not draw is a page we could not check, and
        # returning here with analysis_failed still False is exactly what turns
        # an unreadable page into a clean bill of health.
        outcome.analysis_failed = True
        outcome.warnings.append(
            "this page could not be rendered, so it was never checked for text "
            "hidden under a black box"
        )
        outcome.seconds = time.perf_counter() - started
        return outcome

    raw = None
    if not job.is_image:
        try:
            with pdf.open_pdf(source) as handle:
                raw = pdf.read_page(handle, job.number - 1)
        except Exception as exc:
            outcome.warnings.append(f"could not read the text layer: {_describe(exc)}")

    if raw is not None:
        page.width_pt, page.height_pt = raw.width_pt, raw.height_pt
    elif image is not None:
        # An image file has no points; treat one pixel as one point at 72 dpi so
        # the aspect ratio - the only thing the layout actually needs - is right.
        page.width_pt, page.height_pt = float(image.width), float(image.height)

    if dpi != job.dpi:
        outcome.warnings.append(
            f"this page is too large to rasterise at {job.dpi} dpi within "
            f"render.max_megapixels ({job.max_megapixels:g}), so it was rendered at "
            f"{dpi} dpi instead; the scan is softer than the rest of the collection"
        )

    words, page.source, notes = _choose_text(job, raw, image, dpi)
    outcome.warnings.extend(notes)

    # `ocr.languages` reaches the judge as a *prior*, not as a filter: it can
    # only raise a page's stopword ratio, never lower it. A collection declared
    # ["eng"] that turns out to hold Russian is a collection with Russian in
    # it, not a collection of failures - see `lang.stopword_ratio`.
    page.quality = quality.score_page(
        words,
        image,
        languages=list(job.ocr_languages),
        had_glyphs=bool(raw is not None and raw.chars),
    )

    # A page whose embedded text is garbage is worth reading again from the
    # pixels. This is the single most common repair in real collections: a PDF
    # produced by someone else's bad OCR, shipped with a text layer that says
    # nothing a search will ever match.
    if (
        page.source is TextSource.EMBEDDED
        and page.quality.verdict.is_failure
        and job.ocr_mode != "never"
        and image is not None
    ):
        retry, retry_notes = _ocr(job, image, dpi)
        outcome.warnings.extend(retry_notes)
        if retry:
            rescored = quality.score_page(retry, image, languages=list(job.ocr_languages))
            if not rescored.verdict.is_failure:
                words, page.source, page.quality = retry, TextSource.OCR_OVERRIDE, rescored
                outcome.warnings.append(
                    "the PDF's own text layer was unusable; read the page from the image instead"
                )

    if page.source is TextSource.EMBEDDED and raw is not None:
        words, invisible_notes = _withhold_invisible(words, raw, image)
        outcome.warnings.extend(invisible_notes)

    findings, analysed = _analyse_redactions(raw, image, page, is_image=job.is_image)
    page.redactions = findings.redactions
    page.redaction_ratio = findings.ratio
    outcome.warnings.extend(findings.warnings)
    outcome.hidden = list(findings.hidden)
    outcome.analysis_failed = not analysed

    # Text hidden under a box never reaches the site. Even in `warn` mode, where
    # the operator has chosen to publish anyway, the recovered text stays out of
    # the HTML, the JSON and the index; what they publish is the document, not
    # our transcription of what someone tried to remove from it.
    #
    # `covered` and not `hidden`: suppression means "not worth stopping the
    # build over", never "safe to publish". A box over a bare date is a form
    # rather than a leak and does not need to wake anybody up - but the date was
    # under a black box either way, and republishing our transcription of it
    # undoes exactly what the box did.
    if findings.covered:
        words = _drop_hidden(words, findings.covered)

    words = _split_cjk(words)
    page.words = words
    page.lines = _lines(words)
    code, confidence = detect_language([w.text for w in words])
    page.language = code if confidence >= 0.20 else ""
    outcome.warnings.extend(_unjudgeable_script_note(page.words))
    outcome.warnings.extend(_undeclared_language_note(page.language, job.ocr_languages))

    if image is not None:
        _publish_images(job, image, page)

    outcome.seconds = time.perf_counter() - started
    return outcome


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _unjudgeable_script_note(words: Sequence[Word]) -> list[str]:
    """Say so when a page is written in a script no word list covers.

    Arabic, Hebrew, Greek, Devanagari, Thai, Japanese and Korean score a
    stopword ratio of zero against every list this project ships, for a reason
    that has nothing to do with the quality of the page. ``ingest/quality.py``
    knows that, judges such a page on its other signals, and records it in
    :attr:`~stackroom.model.OcrQuality.reasons`. The operator is owed it as
    well: "judged on other signals" is a weaker check than the one every other
    page in their collection got, and only they can decide whether that matters.

    Identical notes are grouped by the build, so a release entirely in Hindi
    produces one line with a count against it, exactly like
    :func:`_undeclared_language_note`.
    """
    text = " ".join(w.text for w in words)
    if not text.strip() or stopwords_apply(text):
        return []
    return [
        "no stopword list covers the script on this page, so its text was judged on "
        "other signals rather than on how many common words it holds"
    ]


def _undeclared_language_note(detected: str, declared: Sequence[str]) -> list[str]:
    """Say so when a page reads as a language the collection did not declare.

    One line, and it earns its place: an operator with four hundred Russian
    pages in a collection they told Stackroom was English has a real decision
    to make. Nothing is wrong with the page in front of them - that is the
    whole point of the change this note comes with - but ``ocr.languages`` is
    what the recogniser is given, so any of those pages that is a *scan* was
    read by an English-only Tesseract. They cannot weigh that if nobody tells
    them. The build groups identical notes, so four hundred pages produce one
    line with a count against it.

    Silent in the two cases where there is nothing to say: a page that reads as
    nothing in particular (:func:`lang.detect_language` returns ``und``, or the
    confidence was too low for ``page.language`` to be set at all), and a
    collection whose declared codes this module has no word list for, where
    "not one of yours" would be an artefact of our vocabulary rather than a
    fact about the page.
    """
    if not detected:
        return []
    known = normalize_language_codes(declared)
    if not known or detected in known:
        return []
    names = language_names()
    expected = ", ".join(names.get(code, code) for code in known)
    return [
        f"this page reads as {names.get(detected, detected)}, which is not among the "
        f"languages this collection declares ({expected}); nothing is wrong with this "
        "page, but a scan in that language would be recognised without it"
    ]


def _rasterise(job: PageJob, source: Path) -> tuple[Image.Image | None, int]:
    """Get the page as pixels, once, for everything that needs them.

    Returns the resolution actually used alongside the image, because it is not
    always the one that was asked for: a page over ``render.max_megapixels``
    comes back smaller, and recognition needs to know that or it will not
    upscale far enough.
    """
    if job.is_image:
        with Image.open(source) as opened:
            return opened.convert("L" if opened.mode in ("1", "L", "LA") else "RGB"), job.dpi
    budget = int(job.max_megapixels * 1_000_000) or None
    # `_page_count` queues the union of what the two parsers can see, so a page
    # that exists only in `/Count` reaches this function. Poppler reports such a
    # page as 0 x 0 pt, and `pixel_size` clamps that to one pixel - so pdftoppm
    # returns a 1x1 image, exit status 0, no complaint. Rendering it publishes a
    # one-pixel scan in a square frame and scores the page BLANK, which tells
    # the reader "this page is empty" about a page nobody could draw. The 1x1
    # guard in `render_page_crop` cannot catch it: there the requested size is
    # 1x1 too, so the answer matches the question.
    geometry = raster.page_geometry(source)
    if job.number <= len(geometry) and min(geometry[job.number - 1].rendered_pt) <= 0:
        raise raster.RenderError(
            f"page {job.number} has no page box - poppler reports it as 0 x 0 pt, "
            "which means the document's page tree claims a page that is not "
            "there. Nothing can be rendered or checked for it."
        )
    image = raster.render_page_crop(
        source, job.number, FULL_PAGE, dpi=job.dpi, max_pixels=budget
    )
    geom = geometry[job.number - 1]
    dpi = job.dpi
    if budget and math.prod(geom.pixel_size(job.dpi)) > budget:
        dpi = raster.fit_dpi(geom, job.dpi, budget)
    return image, dpi


def _choose_text(
    job: PageJob, raw: object | None, image: Image.Image | None, dpi: int
) -> tuple[list[Word], TextSource, list[str]]:
    """Decide where this page's text comes from.

    A text layer is kept when :mod:`stackroom.ingest.pdf` can vouch for it and
    read again from the pixels when it cannot. Nothing here second-guesses that
    verdict, and the reason is worth recording because a second opinion did
    live here: ``embedded_text_verdict`` used to reject a layer on a stopword
    count taken over a private union of five European languages, which has no
    opinion at all about Devanagari, Thai or Japanese and threw away perfectly
    good text layers in those scripts. This function asked
    :func:`quality.embedded_layer_broken` for a second reading and kept the
    layer when nothing else was wrong with it.

    That check now goes through :func:`stackroom.lang.stopwords_apply` and does
    not fire on a script it has no words for, so there is nothing left to
    overrule - and overruling was never free. The same branch also rescued
    layers rejected for overprinted glyphs and for unrecoverable draw order,
    neither of which is a language problem and both of which are pages
    recognition should read.
    """
    embedded: list[Word] = list(getattr(raw, "words", []) or [])
    embedded_ok = bool(getattr(raw, "embedded_text_ok", False))
    reasons: list[str] = list(getattr(raw, "embedded_text_reasons", []) or [])
    notes: list[str] = []

    if job.ocr_mode == "never":
        if embedded:
            return embedded, TextSource.EMBEDDED, notes
        return [], TextSource.NONE, ["recognition is switched off and this page has no text layer"]

    if job.ocr_mode == "auto" and embedded and embedded_ok:
        return embedded, TextSource.EMBEDDED, notes

    if embedded and not embedded_ok and reasons:
        notes.append("the PDF's text layer looks broken: " + "; ".join(reasons[:3]))

    if image is None:
        return embedded, (TextSource.EMBEDDED if embedded else TextSource.NONE), notes

    words, ocr_notes = _ocr(job, image, dpi)
    notes.extend(ocr_notes)
    if not words and embedded:
        notes.append("recognition found nothing; kept the PDF's own text layer")
        return embedded, TextSource.EMBEDDED, notes
    if not words:
        return [], TextSource.NONE, notes
    source = TextSource.OCR_OVERRIDE if embedded else TextSource.OCR
    return words, source, notes


def _ocr(job: PageJob, image: Image.Image, dpi: int) -> tuple[list[Word], list[str]]:
    try:
        result = ocr.ocr_image(
            image,
            languages=list(job.ocr_languages),
            psm=job.psm,
            auto_rotate=job.auto_rotate,
            # The resolution the image really came back at, not the one that was
            # asked for: recognition upscales to about 300 dpi from this number.
            source_dpi=float(dpi),
            timeout=job.ocr_timeout,
        )
    except ocr.MissingLanguageError:
        raise
    except Exception as exc:
        return [], [f"recognition failed: {_describe(exc)}"]
    notes: list[str] = []
    if getattr(result, "rotated_by", 0):
        notes.append(f"the page was scanned {result.rotated_by} degrees out of upright")
    return list(result.words), notes


def _analyse_redactions(
    raw: object | None,
    image: Image.Image | None,
    page: Page,
    *,
    is_image: bool = False,
) -> tuple[redaction.RedactionFindings, bool]:
    """Look for redactions, and say whether the look actually happened.

    The second return value is the honest part. Everything else in this module
    treats a failure as "carry on without it"; this check is the one place
    where that would be dangerous, because the absence of a finding is what the
    operator will read as safety.

    **We only say a page is clean when we actually looked at it, with both
    passes** - the content stream and the pixels. A PDF whose text layer will
    not parse gets only the visible-redaction pass, which cannot see text at
    all, so it is reported as unchecked however well it rendered. *is_image*
    is what tells that apart from a page image, where there is no content
    stream to read and the pixel pass is the whole of the answer.
    """
    empty = redaction.RedactionFindings(
        redactions=[], hidden=[], ratio=0.0, ink_box=None, warnings=[]
    )
    if raw is None and image is None:
        empty.warnings.append(
            "neither the page's text layer nor its pixels could be read, so nothing "
            "was checked for text hidden under a black box"
        )
        return empty, False
    cropper = _in_memory_cropper(image) if image is not None else None
    try:
        if raw is not None:
            return redaction.analyse_page(raw, image, crop_renderer=cropper), True
        boxes = redaction.find_visible_redactions(image)
        ratio = redaction.redaction_ratio(boxes, [], (page.width_pt, page.height_pt))
        findings = redaction.RedactionFindings(
            redactions=boxes, hidden=[], ratio=ratio, ink_box=None, warnings=[]
        )
        if not is_image:
            findings.warnings.append(
                "this page's text layer could not be read, so only the visible-redaction "
                "pass ran: nothing here rules out text hidden under a black box"
            )
        # No content stream means the hidden-text pass never ran. That is
        # correct and expected for an image file, and it is a hole for a PDF.
        return findings, is_image
    except Exception as exc:
        empty.warnings.append(
            f"the redaction check failed on this page ({_describe(exc)}), so we do not "
            "know whether anything is hidden underneath a black box here"
        )
        return empty, False


def _in_memory_cropper(image: Image.Image) -> Callable[[Box], Image.Image]:
    """Crop the page we already have instead of asking poppler to draw it again.

    The rounding has to match ``raster.render_page_crop`` exactly - outwards on
    every edge - because the redaction check insets by a pixel before measuring
    uniformity, and a crop that came back one pixel short would show it a sliver
    of paper and clear a real leak.
    """
    w, h = image.width, image.height

    def crop(box: Box) -> Image.Image:
        x0 = max(0, min(w, math.floor(box.x * w)))
        y0 = max(0, min(h, math.floor(box.y * h)))
        x1 = max(0, min(w, math.ceil(box.x2 * w)))
        y1 = max(0, min(h, math.ceil(box.y2 * h)))
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"crop {box} is empty against a {w}x{h} page")
        return image.crop((x0, y0, x1, y1))

    return crop


def _drop_hidden(words: Sequence[Word], covered: Sequence[Box]) -> list[Word]:
    """Remove every token that any opaque shape touches.

    Any overlap at all, not a majority of one. Findings are assembled from
    *characters* that are 80% covered, so a box across half a token yields a
    finding for that half; a word-level majority test then keeps the token and
    publishes the covered half with it - which is how ``check`` came to report
    ``##########`` while the site published ``ALPHABRAVOCHARLIEDELTAECHO``.

    Withholding a word a box merely clips costs a reader one word. Publishing
    half a redacted name costs somebody rather more, and names, case numbers
    and email addresses are exactly the tokens a redaction lands halfway
    across.
    """
    kept: list[Word] = []
    for word in words:
        if any(word.box.intersection(box) is not None for box in covered):
            continue
        kept.append(word)
    return kept


INK_LEVEL = 160
"""Grey level at or below which a pixel counts as ink rather than paper.

Between a scan's paper (230-250 with noise) and its text (under 100), and low
enough that a JPEG halo around a letter does not read as a word of its own."""

INK_SHARE = 0.002
"""Share of a word's pixels that must be ink before we believe something is
drawn there. Two in a thousand: a single antialiased stroke clears it, and an
empty sheet does not."""


def _withhold_invisible(
    words: Sequence[Word], raw: object, image: Image.Image | None
) -> tuple[list[Word], list[str]]:
    """Drop text that is in the file but not on the page.

    Render mode 3 paints nothing. pdfminer does not implement it, so the glyphs
    come through, get grouped into words, and are published as the page's
    transcription and indexed for search - which turns "recoverable with
    pdftotext" into "crawlable". A reader proof-reads the scan; what they
    cannot see, a search engine can.

    It cannot simply be dropped, because render mode 3 is also how every
    searchable scan in existence is built: an image of the page with an
    invisible OCR transcription behind it, and that transcription is the only
    text the page has. So the discriminator is the pixels. An invisible word
    with ink where it sits is an OCR layer doing its job. An invisible word
    over blank paper is text nobody can see, and it is withheld.

    Without a rendering there is nothing to compare against and the words are
    kept: this is a publication filter, not the safety check, and guessing the
    other way would empty the text layer of every scan on a machine with no
    poppler.
    """
    chars = [c for c in getattr(raw, "chars", []) if c.text.strip()]
    if not words or not chars or image is None:
        return list(words), []
    if not any(c.invisible for c in chars):
        return list(words), []

    # Which glyphs a word is made of, by glyph centre rather than by box
    # overlap: a character is a small fraction of the word that contains it, so
    # comparing the two boxes directly answers the wrong question. Vectorised
    # because an OCR-under-image collection hits this on every page.
    cx = np.array([c.box.x + c.box.w / 2 for c in chars])
    cy = np.array([c.box.y + c.box.h / 2 for c in chars])
    hidden = np.array([c.invisible for c in chars], dtype=bool)

    gray = to_gray(image)
    height, width = gray.shape
    kept: list[Word] = []
    dropped = 0
    for word in words:
        box = word.box
        inside = (cx >= box.x) & (cx <= box.x2) & (cy >= box.y) & (cy <= box.y2)
        if not inside.any() or (inside & ~hidden).any():
            kept.append(word)  # ordinary text, or a word the stamp is part of
            continue
        x0 = max(0, min(width - 1, math.floor(box.x * width)))
        y0 = max(0, min(height - 1, math.floor(box.y * height)))
        x1 = max(x0 + 1, min(width, math.ceil(box.x2 * width)))
        y1 = max(y0 + 1, min(height, math.ceil(box.y2 * height)))
        patch = gray[y0:y1, x0:x1]
        if patch.size and float((patch <= INK_LEVEL).mean()) >= INK_SHARE:
            kept.append(word)  # there is ink here: an OCR layer over a scan
            continue
        dropped += 1
    if not dropped:
        return kept, []
    return kept, [
        f"{dropped} word(s) on this page are painted in an invisible text render mode "
        "over blank paper - in the file but not on the page. They are kept out of the "
        "published text and the search index; a reader proof-reads the scan, and what "
        "they cannot see a search engine otherwise can"
    ]


def _split_cjk(words: Sequence[Word]) -> list[Word]:
    """Break CJK runs into one token per character.

    The search index re-segments CJK itself and reports match positions against
    *its* tokens. If a word here spans three characters and the index counts
    three, every highlight after it on the page lands on the wrong glyph. So the
    split happens once, here, and the rest of the system can keep its promise
    that token order is the same everywhere.

    Boxes are divided proportionally. For the square, evenly-spaced glyphs this
    applies to, that is very nearly exact.
    """
    out: list[Word] = []
    for word in words:
        if len(word.text) < 2 or script_of(word.text) not in ("han", "kana", "hangul"):
            out.append(word)
            continue
        n = len(word.text)
        step = word.box.w / n
        for i, ch in enumerate(word.text):
            out.append(
                Word(
                    text=ch,
                    box=Box(word.box.x + i * step, word.box.y, step, word.box.h),
                    conf=word.conf,
                    line=word.line,
                )
            )
    return out


def _lines(words: Sequence[Word]) -> list[str]:
    if not words:
        return []
    lines: dict[int, list[str]] = {}
    for word in words:
        lines.setdefault(word.line, []).append(word.text)
    return [" ".join(lines[k]) for k in sorted(lines)]


def _publish_images(job: PageJob, image: Image.Image, page: Page) -> None:
    spec = raster.RenderSpec(
        dpi=job.dpi,
        widths=tuple(job.widths),
        thumb_width=job.thumb_width,
        formats=tuple(job.formats),
        max_pixels=int(job.max_megapixels * 1_000_000),
    )
    rendered = raster.encode_page(image, job.number, Path(job.media_dir), spec, job.dpi)
    prefix = job.media_prefix.rstrip("/")
    page.images = [
        ImageVariant(f"{prefix}/{v.path.name}", v.format, v.width, v.height, v.bytes)
        for v in rendered.variants
    ]
    page.thumbs = [
        ImageVariant(f"{prefix}/{v.path.name}", v.format, v.width, v.height, v.bytes)
        for v in rendered.thumbs
    ]
    page.placeholder = rendered.placeholder


# --------------------------------------------------------------------------
# document-level passes
# --------------------------------------------------------------------------


def _scan_text_and_offsets(page: Page) -> tuple[str, list[int]]:
    """The page as one string, plus where each token starts in it.

    Exemption codes are found in this string and have to be put back on the
    page, so the offsets are how a character span becomes a rectangle.
    """
    parts: list[str] = []
    offsets: list[int] = []
    cursor = 0
    for word in page.words:
        offsets.append(cursor)
        parts.append(word.text)
        cursor += len(word.text) + 1
    return " ".join(parts), offsets


def _locate(span: tuple[int, int], page: Page, offsets: Sequence[int]) -> Box | None:
    """Union of the boxes of every token the span touches."""
    start, end = span
    box: Box | None = None
    for word, offset in zip(page.words, offsets, strict=False):
        if offset >= end:
            break
        if offset + len(word.text) <= start:
            continue
        box = word.box if box is None else box.union(word.box)
    return box


def _width_over_height(page: Page) -> float:
    """This page's width divided by its height - 0.773 on US Letter.

    :func:`exemptions.associate` scales horizontal distances by this to compare
    them with a threshold expressed in page heights, and
    :attr:`~stackroom.model.Page.aspect` is the other ratio: height over width,
    which is what the layout, the negative and the compare diagrams all want.
    Handing ``page.aspect`` straight over - which this function exists to stop
    anyone doing again - inverted the near field, making it 1.67x tighter
    horizontally than the 40pt it is documented as, about 24pt. That could only
    ever lose a stamp, never invent one, so it cost recall and nothing else;
    the line rule now carries most of what it was dropping. It was still the
    wrong number.

    A page with no height falls back to Letter rather than dividing by zero,
    which is the same fallback ``Page.aspect`` makes for a page with no width.
    """
    return page.width_pt / page.height_pt if page.height_pt else exemptions.LETTER_ASPECT


def annotate_document(doc: Document, jurisdiction: str = "us") -> None:
    """Everything that can only be decided by looking at the whole document."""
    texts_and_offsets = [_scan_text_and_offsets(p) for p in doc.pages]
    per_page = exemptions.scan_document(
        [t for t, _ in texts_and_offsets], jurisdiction=jurisdiction
    )

    for page, hits, (_, offsets) in zip(doc.pages, per_page, texts_and_offsets, strict=False):
        boxes = [_locate(h.span, page, offsets) for h in hits]
        exemptions.associate(hits, boxes, page.redactions, aspect=_width_over_height(page))
        page.exemptions = sorted({h.code for h in hits})

    series = bates.detect(doc.pages)
    if series:
        best = series[0]
        doc.bates_prefix = best.prefix or None
        doc.bates_gaps = list(best.gaps)
        for number, stamp in best.page_map.items():
            if 1 <= number <= len(doc.pages):
                doc.pages[number - 1].bates = stamp


# --------------------------------------------------------------------------
# the whole collection
# --------------------------------------------------------------------------


def _jobs_for(source: discover.SourceFile, cfg: Config, media_root: Path) -> list[PageJob]:
    media_dir = media_root / source.slug
    common = {
        "pdf": str(source.path),
        "doc_id": source.slug,
        "media_dir": str(media_dir),
        "media_prefix": f"media/{source.slug}",
        "dpi": cfg.render.dpi,
        "widths": tuple(cfg.render.widths),
        "thumb_width": cfg.render.thumb_width,
        "formats": tuple(cfg.render.formats),
        "max_megapixels": cfg.render.max_megapixels,
        "ocr_mode": cfg.ocr.mode,
        "ocr_languages": tuple(cfg.ocr.languages),
        "psm": cfg.ocr.psm,
        "auto_rotate": cfg.ocr.auto_rotate,
        "ocr_timeout": cfg.ocr.timeout,
    }
    if source.kind == "image":
        return [PageJob(number=1, is_image=True, **common)]
    return [PageJob(number=n, **common) for n in range(1, _page_count(source.path) + 1)]


def _page_count(path: Path) -> int:
    """How many pages to queue: the larger of what the two parsers can see.

    pdfminer walks ``/Kids``; poppler believes ``/Count``. They disagree on
    ordinary damage as well as on crafted files, and taking either one alone is
    the same mistake in two directions. Trusting pdfminer alone means a page
    poppler would render is never queued, never rendered and never checked -
    and the archive publishes a truncated document without saying so, which is
    F1 wearing a different hat.

    Queueing the union is the honest answer: a page neither parser can produce
    comes back from :func:`process_page` with ``analysis_failed`` set, which is
    "we could not check this", not "there was nothing here".
    """
    counted = pdf.page_count(path)
    try:
        seen = len(raster.page_geometry(path))
    except Exception:
        # No pdfinfo, or a file it will not open. The text layer's count is
        # then all we have, and it is what every earlier release used.
        return counted
    return max(counted, seen)


_GENERIC_TITLES = frozenset(
    {"untitled", "untitled document", "document", "document1", "scan", "scanned document",
     "new document", "pdf document", "print", "printout", "unknown", "no title", "title"}
)


def _title_for(source: discover.SourceFile, meta: dict[str, str]) -> str:
    """A human title: the document's own, if it has one worth using.

    PDF ``/Title`` is very often the scanner's file name, a UUID, or the word
    "Microsoft Word - final(2).doc". The file name a person chose is usually
    better than any of those, so the embedded title has to earn its place.
    """
    claimed = (meta.get("title") or "").strip()
    stem = source.path.stem.replace("_", " ").replace("-", " ").strip()
    if not claimed or len(claimed) < 3 or claimed.lower() in _GENERIC_TITLES:
        return stem or source.slug
    lowered = claimed.lower()
    if lowered.endswith((".doc", ".docx", ".pdf", ".rtf")) or lowered.startswith("microsoft word"):
        return stem or source.slug
    if claimed.count("-") >= 4 and " " not in claimed:  # a UUID or a scanner serial
        return stem or source.slug
    return claimed


def _remember(
    cache: cache_mod.PageCache | None, job: PageJob, outcome: PageOutcome
) -> None:
    """Offer one outcome to the cache, exactly as it came back.

    Called here and not after the document-level passes, because
    :func:`annotate_document` writes exemption codes and control numbers *into*
    the pages: those are decided by looking at every page of the document at
    once, and a page stored with them would be a page whose stored answer its
    own job does not determine. The cache refuses an annotated page as well,
    but the order is the fix and that check is the alarm.
    """
    if cache is not None and cache.enabled:
        cache.put(job, outcome)


def build_collection(
    root: Path,
    cfg: Config,
    out_dir: Path,
    *,
    progress: Progress | None = None,
    workers: int | None = None,
    cache: cache_mod.PageCache | None = None,
    on_counted: Callable[[int], None] | None = None,
) -> tuple[Collection, list[PageOutcome]]:
    """Read everything under *root* and return the collection it describes.

    Rendered images are written under ``out_dir`` as they are produced; nothing
    else is. Turning a :class:`Collection` into a website is
    :mod:`stackroom.build.site`'s job, and keeping the two apart is what makes
    it possible to test this function without looking at any HTML.

    With a *cache*, a page whose job and source bytes have been seen before is
    restored - outcome and encoded images both - instead of being processed
    again. It changes nothing about what comes out: see ``docs/CACHING.md`` and
    ``test_cache.py::test_a_warm_build_is_byte_identical_to_a_cold_one``.

    *on_counted* is called with the total number of pages the moment discovery
    knows it, and before the first page is rendered. It is how a caller refuses
    a collection that is too large to publish without first spending hours
    reading it: raising from it stops the build with nothing rasterised. The
    count is exact - it is the queue - and it costs one ``/Count`` and one
    ``pdfinfo`` per file, which discovery has already paid for.
    """
    started = time.perf_counter()
    emit = progress or (lambda _event: None)

    usable, skipped = discover.discover(root, include=cfg.include or None, exclude=cfg.exclude or None)
    emit(ProgressEvent("discover", done=len(usable), total=len(usable) + len(skipped)))
    if not usable:
        message = f"no readable documents under {root}.\n  Stackroom reads PDFs and page images."
        if skipped:
            kinds = ", ".join(sorted({Path(s.path).suffix.lstrip(".") or "no extension" for s in skipped})[:6])
            message += f"\n  {len(skipped)} file(s) were skipped ({kinds})."
        raise FileNotFoundError(message)

    media_root = Path(out_dir) / "media"
    jobs: list[PageJob] = []
    documents: dict[str, Document] = {}

    for source in usable:
        if source.kind not in ("pdf", "image"):
            # Text and Markdown files in the folder are the operator's own
            # notes - about.md most of all - not documents in the collection.
            # Publishing the operator's working notes as page one of the
            # archive would be a memorable way to fail.
            continue
        if source.path.name.lower() in _NOT_DOCUMENTS:
            continue
        try:
            meta = pdf.document_meta(source.path) if source.kind == "pdf" else {}
            source_jobs = _jobs_for(source, cfg, media_root)
        except Exception as exc:
            emit(ProgressEvent("note", label=source.slug, detail=f"skipped: {_describe(exc)}"))
            continue
        documents[source.slug] = Document(
            id=source.slug,
            # Names enter the model here and nowhere else, so this is where
            # bytes that are not valid UTF-8 stop being a problem. Left alone,
            # they reach SiteBuilder.write() as surrogate escapes and kill the
            # build with a raw UnicodeEncodeError - after the output directory
            # has already been emptied and the whole ingest has already run.
            title=discover.printable(_title_for(source, meta)),
            filename=discover.printable(source.path.name),
            # What `discover` decided this file *is*, by its magic number. The
            # site publishes the original under an extension derived from this
            # and never from the name above, which the producer chose.
            kind=source.kind,
            sha256=source.sha256,
            size_bytes=source.size,
            source_path=str(source.path),
            meta=meta,
            pages=[],
        )
        jobs.extend(source_jobs)

    total = len(jobs)
    # Before anything is rendered. `_page_count` has already asked both parsers
    # how many pages each file has, so the size of the collection is known here
    # for free - and a 60,000-page collection refused *after* the ingest is a
    # refusal that cost the operator an afternoon.
    if on_counted is not None:
        on_counted(total)
    emit(ProgressEvent("page", done=0, total=total, label="reading"))

    outcomes: list[PageOutcome] = []
    done = 0

    # A page already in the cache is not work. Discovery has just hashed every
    # file it ingested, so hand those digests over rather than reading tens of
    # gigabytes a second time to learn what we were already told.
    if cache is not None and cache.enabled:
        cache.reset()
        cache.note_digests({str(s.path): s.sha256 for s in usable})
        remaining: list[PageJob] = []
        for job in jobs:
            restored = cache.get(job)
            if restored is None:
                remaining.append(job)
                continue
            outcomes.append(restored)
            done += 1
            emit(
                ProgressEvent(
                    "page", done=done, total=total, label=restored.doc_id, detail="cached"
                )
            )
        jobs = remaining

    workers = workers if workers is not None else _default_workers()
    if workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            outcome = process_page(job)
            _remember(cache, job, outcome)
            outcomes.append(outcome)
            done += 1
            emit(ProgressEvent("page", done=done, total=total, label=outcome.doc_id))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(process_page, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = PageOutcome(
                        doc_id=job.doc_id,
                        number=job.number,
                        page=Page(number=job.number),
                        error=_describe(exc),
                    )
                else:
                    _remember(cache, job, outcome)
                outcomes.append(outcome)
                done += 1
                emit(ProgressEvent("page", done=done, total=total, label=outcome.doc_id))

    for outcome in outcomes:
        doc = documents.get(outcome.doc_id)
        if doc is not None:
            doc.pages.append(outcome.page)
    for doc in documents.values():
        doc.pages.sort(key=lambda p: p.number)
        annotate_document(doc, cfg.jurisdiction)
        emit(ProgressEvent("document", label=doc.id, detail=f"{doc.page_count} pages"))

    collection = Collection(
        title=cfg.title,
        description=cfg.description,
        documents=[documents[s.slug] for s in usable if s.slug in documents],
        base_url=cfg.base_url,
        language=cfg.language,
        jurisdiction=cfg.jurisdiction,
    )
    collection.stats = summarise(collection)
    collection.build = BuildInfo(
        version=_pkg_version,
        source_digest=_digest_of(usable),
        tool_versions=_tool_versions(),
        duration_seconds=round(time.perf_counter() - started, 2),
    )
    return collection, outcomes


def check_safety(
    outcomes: Iterable[PageOutcome], cfg: Config
) -> tuple[list[tuple[str, int, list[HiddenText]]], list[tuple[str, int]]]:
    """Collect failed redactions, and stop the build if that is the policy.

    Two kinds of trouble, both blocking by default: pages where we found text
    under a black box, and pages where the check itself could not run. The
    second is quieter and more dangerous, because it looks like good news.
    """
    outcomes = list(outcomes)
    findings = [(o.doc_id, o.number, o.hidden) for o in outcomes if o.hidden]
    unchecked = [(o.doc_id, o.number) for o in outcomes if o.analysis_failed]

    if cfg.safety.hidden_text == "stop" and (findings or unchecked):
        parts: list[str] = []
        if findings:
            passages = sum(len(f[2]) for f in findings)
            parts.append(
                f"{passages} passage(s) on {len(findings)} page(s) are covered by a black box "
                "but still readable in the file"
            )
        if unchecked:
            parts.append(
                f"{len(unchecked)} page(s) could not be checked at all, so we cannot say "
                "whether they hide anything"
            )
        raise SafetyStop("; ".join(parts) + ".", findings)
    return findings, unchecked


def summarise(collection: Collection) -> CollectionStats:
    """Everything the front page prints, computed once from the pages.

    The withheld share
    ------------------
    This used to be ``sum(ratios) / len(ratios)`` over the pages that had any
    text, and it was wrong in three ways at once, all of them in the direction
    that makes a release look better than it is:

    * A mean of per-page shares gives a page holding one line the same say as a
      page of dense type. The right weight is how much content is on the page.
    * A page blacked out end to end has no surviving words, so ``if page.words``
      dropped it - and those are exactly the pages a reader most wants counted.
    * Every clean page went into the denominator, so the figure was neither the
      share of the release withheld nor the share of the redacted pages
      withheld. It was a mean of a set nobody would choose.

    What is computed instead is an area. Each page's *content area* is the union
    of its surviving word boxes and its redaction boxes, measured on the same
    grid ``ingest/redaction.py`` measures a page's own share on, in square
    points so that pages of different sizes can be added together. The withheld
    area of a page is its own share of that - :attr:`Page.redaction_ratio`,
    which is the number printed beside that page on ``withheld/index.html`` -
    times its content area. Two divisions come out of the sum:

    ``redaction_ratio``
        over the pages that carry redactions. This is what the front page and
        the ledger print, and both name that denominator in the sentence
        beside the number.
    ``redaction_ratio_collection``
        over every page. A different fact, published in ``manifest.json`` and
        printed by the CLI: one page withheld in full out of a thousand is 100%
        of that page and 0.1% of the release.

    Taking each page's share from the page, rather than re-measuring the boxes
    here, is deliberate: it means the collection figure is exactly the
    content-weighted mean of the per-page figures the site prints, so the two
    can never disagree - including on a scan, where the page's own share was
    measured against the ink in the pixels because there was no text layer to
    measure against.

    A page with neither words nor boxes has no measurable content and is
    counted in ``unmeasured_pages`` rather than being silently dropped.
    """
    stats = CollectionStats(documents=len(collection.documents))
    languages: dict[str, int] = {}
    withheld_area = 0.0
    redacted_pages_area = 0.0
    collection_area = 0.0
    for doc in collection.documents:
        stats.bytes_total += doc.size_bytes
        for page in doc.pages:
            stats.pages += 1
            stats.words += len(page.words)
            _redacted, content = redaction.content_area(
                page.redactions,
                [w.box for w in page.words],
                (page.width_pt, page.height_pt),
            )
            collection_area += content
            if content <= 0:
                stats.unmeasured_pages += 1
            if page.redactions:
                stats.pages_with_redactions += 1
                stats.redaction_boxes += len(page.redactions)
                redacted_pages_area += content
                withheld_area += page.redaction_ratio * content
            if page.source in (TextSource.OCR, TextSource.OCR_OVERRIDE):
                stats.ocr_pages += 1
            verdict = page.quality.verdict
            if verdict.is_failure:
                stats.unreadable_pages += 1
            elif verdict is PageVerdict.PICTORIAL:
                stats.pictorial_pages += 1
            elif verdict is PageVerdict.BLANK:
                stats.blank_pages += 1
            for code in page.exemptions:
                stats.exemption_counts[code] = stats.exemption_counts.get(code, 0) + 1
            if page.language:
                languages[page.language] = languages.get(page.language, 0) + 1
    stats.withheld_area_pt = withheld_area
    stats.redacted_pages_area_pt = redacted_pages_area
    stats.collection_area_pt = collection_area
    stats.redaction_ratio = withheld_area / redacted_pages_area if redacted_pages_area else 0.0
    stats.redaction_ratio_collection = (
        withheld_area / collection_area if collection_area else 0.0
    )
    stats.languages = sorted(languages, key=lambda k: -languages[k])
    stats.exemption_counts = dict(
        sorted(stats.exemption_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return stats


def _default_workers() -> int:
    # Tesseract is already pinned to one thread per process (see ingest/ocr.py),
    # so the pool is where parallelism comes from. Leave one core for the
    # operator's machine to stay usable during a long build.
    cpus = os.cpu_count() or 2
    return max(1, min(8, cpus - 1)) if cpus > 2 else 1


def _digest_of(sources: Sequence[discover.SourceFile]) -> str:
    h = hashlib.sha256()
    for source in sorted(sources, key=lambda s: s.sha256):
        h.update(source.sha256.encode("ascii"))
    return h.hexdigest()


def _tool_versions() -> dict[str, str]:
    versions = {"stackroom": _pkg_version}
    # A version string is never worth failing a build over.
    with contextlib.suppress(Exception):
        versions["tesseract"] = ocr.tesseract_version()
    with contextlib.suppress(Exception):
        import pdfplumber

        versions["pdfplumber"] = pdfplumber.__version__
    return versions


__all__ = [
    "PageJob",
    "PageOutcome",
    "ProgressEvent",
    "SafetyStop",
    "annotate_document",
    "build_collection",
    "check_safety",
    "process_page",
    "summarise",
]
