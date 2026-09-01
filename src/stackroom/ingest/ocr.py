"""Recognising text with Tesseract.

Tesseract is a subprocess here too, reached through ``pytesseract``, which
writes the image to a temp file and reads TSV back. That shape is why this
module is written to be called from a *process* pool and never a thread pool -
see :func:`ocr_image`.

The one thing to get right
--------------------------

``image_to_data`` returns a flat table with a ``level`` column: 1 page,
2 block, 3 paragraph, 4 line, 5 word. **Only level 5 carries a confidence.**
Levels 1 to 4 all report ``conf == -1``, every time, on every page. Verified
here on a 324-row page: 264 rows at level 5, confidences 80 to 97, and 60 rows
at levels 1 to 4 whose confidence set is exactly ``{-1}``. Averaging the raw
table instead of the word rows drops the mean from 96 to 74 on a page that was
read perfectly, and every quality number downstream inherits the damage -
silently, and in the direction of "this page is terrible". So the filter is
``level == 5 and conf >= 0``, plus a check that the text is not blank:
Tesseract also emits empty word rows for whitespace it segments but cannot
read.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
import pytesseract
from PIL import Image
from pytesseract import Output, TesseractError

from ..model import Box, Word

__all__ = [
    "MissingLanguageError",
    "OcrResult",
    "available_languages",
    "ocr_image",
]

_LOG = logging.getLogger(__name__)

# Tesseract parallelises inside a page with OpenMP, and on any machine where we
# are also running one page per core that is pure oversubscription. Measured on
# this two-core container, one 1275x1650 page: 6809 ms with the OpenMP default,
# 1222 ms at two threads, 1101 ms at one. Six times faster for turning
# threading off. ``setdefault`` so an operator who really wants it can still
# say so; it is read by the tesseract child process, not by us, so it is safe
# to set once at import and never touch again.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

TARGET_DPI = 300
"""Tesseract's models are trained around 300 dpi. Below roughly 200 it starts
losing whole words; above 300 it just costs time."""

MAX_UPSCALE = 4.0
MAX_PIXELS = 40_000_000
"""Ceilings on the upscale, so a mis-detected dpi cannot turn one page into a
gigabyte of pixels."""

ASSUMED_PAGE_INCHES = 8.5
"""Fallback for working out the source resolution when the caller does not say
and the image carries no dpi: assume the image's *short* side is one page
across. Short side rather than width, because that survives a page arriving
sideways and a genuinely landscape page alike - letter is 8.5 in across, A4
8.27. Wrong for a crop, which is why ``source_dpi`` exists."""

MIN_ORIENTATION_CONF = 1.0
"""OSD reports a confidence for its guess. Real pages here score 7 to 16; a
page of pure noise scored 2.06 while claiming Bengali. A wrong 90-degree turn
costs the whole page, so a low-confidence *non-zero* rotation is declined. The
gate stays loose on purpose - refusing to straighten a genuinely sideways page
is the more expensive mistake, because nothing downstream can read it."""

BINARISE_BELOW_SPREAD = 40.0
"""Ink-to-paper spread, out of 255, below which we binarise. Set low on
purpose: see :func:`_prepare`."""

MIN_BINARISE_SPREAD = 2.0
"""...and below which we do not, because there is nothing to separate. A blank
page has a one-valued histogram, and Otsu on a one-valued histogram is 0/0."""

_DEBIAN_LANG_PACKAGES = {"osd": "tesseract-ocr-osd"}
"""Only the irregular ones need listing; the rule is tesseract-ocr-<lang>."""


class MissingLanguageError(RuntimeError):
    """A requested language is not installed.

    Carries the OS package name, because "KeyError: 'rus'" three hours into a
    build tells an operator nothing they can act on.
    """


@dataclass(slots=True)
class OcrResult:
    """Everything one page of OCR produced.

    An empty result is not necessarily a failure - a blank page is blank - so
    ``reason`` distinguishes "nothing there" (``None``) from "we gave up"
    (a string).
    """

    words: list[Word] = field(default_factory=list)
    """Page-relative boxes, confidence 0-100, ``line`` set. Order is reading
    order and is load-bearing: guarantee 3 in ARCHITECTURE.md ties it to the
    token order in the page HTML and thence to every search highlight."""

    lines: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    tesseract_version: str = ""
    psm: int = 3
    rotated_by: int = 0
    """Degrees clockwise applied before recognition. The boxes above have
    already been mapped back out of it, into the frame of the image that was
    passed in - which is the frame the site displays."""

    reason: str | None = None
    """Why this result is empty, when it is empty because something broke."""

    @property
    def ok(self) -> bool:
        return self.reason is None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _tesseract_version() -> str:
    # Cached because it costs a subprocess and cannot change inside a process.
    # An lru_cache over a nullary pure function is not state a concurrent
    # caller can observe: each worker process fills its own.
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception as exc:  # pragma: no cover - depends on the host
        _LOG.warning("could not read the tesseract version: %s", exc)
        return "unknown"


@lru_cache(maxsize=1)
def _installed_languages() -> tuple[str, ...]:
    try:
        # This runs `tesseract --list-langs`.
        return tuple(sorted(pytesseract.get_languages(config="")))
    except pytesseract.TesseractNotFoundError as exc:
        raise MissingLanguageError(
            "tesseract is not on PATH. Install it (Debian/Ubuntu: "
            "apt install tesseract-ocr tesseract-ocr-eng, macOS: brew install tesseract)."
        ) from exc


def tesseract_version() -> str:
    """The Tesseract build this machine will use, for the build stamp."""
    return _tesseract_version()


def available_languages() -> list[str]:
    """Language codes this Tesseract can load, sorted.

    Includes ``osd``, which is not a language but an orientation model; it is
    reported as installed because :func:`ocr_image` needs it to auto-rotate.
    """
    return list(_installed_languages())


def _package_for(lang: str) -> str:
    # Debian names the data packages tesseract-ocr-<lang> with underscores
    # turned into hyphens: chi_sim ships as tesseract-ocr-chi-sim.
    return _DEBIAN_LANG_PACKAGES.get(lang, f"tesseract-ocr-{lang.replace('_', '-')}")


def _check_languages(languages: Sequence[str]) -> list[str]:
    if not languages:
        raise MissingLanguageError("no language requested; pass at least one, e.g. ('eng',)")
    installed = _installed_languages()
    missing = [lang for lang in languages if lang not in installed]
    if missing:
        packages = " ".join(_package_for(lang) for lang in missing)
        raise MissingLanguageError(
            f"Tesseract has no data for {', '.join(missing)}. "
            f"Installed: {', '.join(installed) or 'none'}. "
            f"Install it with `apt install {packages}` on Debian/Ubuntu, "
            f"`brew install tesseract-lang` on macOS, or drop the matching "
            f".traineddata into $TESSDATA_PREFIX."
        )
    return list(languages)


# --------------------------------------------------------------------------
# preprocessing
# --------------------------------------------------------------------------


def _otsu_threshold(hist: np.ndarray) -> int:
    """Classic between-class variance maximisation over a 256-bin histogram."""
    total = hist.sum()
    weight = np.cumsum(hist)
    moment = np.cumsum(hist * np.arange(256))
    with np.errstate(invalid="ignore", divide="ignore"):
        between = (moment[-1] * weight / total - moment) ** 2 / (weight * (total - weight))
    if not np.any(np.isfinite(between)):
        return 127
    return int(np.nanargmax(between))


def _ink_paper_spread(hist: np.ndarray, threshold: int) -> float:
    """Mean paper level minus mean ink level, either side of *threshold*.

    Percentiles are the obvious way to measure contrast and the wrong one here:
    text covers well under 1% of a sparse page, so a 1st-percentile "ink" level
    is just more paper, and a faded memo scores the same as a blank sheet.
    Splitting at Otsu's threshold and averaging each side asks the question
    that actually matters - how far apart are the ink and the paper - however
    little ink there is.
    """
    levels = np.arange(256, dtype=np.float64)
    dark, light = hist[: threshold + 1], hist[threshold + 1 :]
    if dark.sum() <= 0 or light.sum() <= 0:
        return 0.0
    ink = float((levels[: threshold + 1] * dark).sum() / dark.sum())
    paper = float((levels[threshold + 1 :] * light).sum() / light.sum())
    return paper - ink


def _source_dpi(img: Image.Image, hint: float | None) -> float:
    if hint and hint > 0:
        return float(hint)
    dpi = img.info.get("dpi")
    if isinstance(dpi, (tuple, list)) and dpi and float(dpi[0]) > 0:
        return float(dpi[0])
    return min(img.width, img.height) / ASSUMED_PAGE_INCHES


def _clamp_dpi(dpi: float) -> int:
    """Keep the hint inside the range Tesseract will accept."""
    return max(70, min(2400, round(dpi)))


def _prepare(img: Image.Image, source_dpi: float | None) -> tuple[Image.Image, int]:
    """Grayscale, upscale to ~300 dpi if the input is coarser, maybe binarise.

    What each step was actually worth, measured on a ``tests/synth.py`` typed
    page of 264 words:

    * **Grayscale**: no accuracy change, ever. Kept because it is what
      binarisation and the contrast measurement need, and it halves the bytes
      written to the temp file.
    * **Upscale**: the only step that moved a number. A full page downsampled
      to 637x825 - 75 dpi equivalent - read 253 of its 264 words as it stood
      and all 264 once upscaled to 300. The same page at 150 dpi was already
      at 264/264, and upscaling it anyway cost 69% more time for nothing.
      Hence: raise to 300, never past, and pass ``source_dpi`` when the caller
      knows it so the guess cannot be wrong.
    * **Binarisation**: never helped. Tesseract 5 does its own thresholding,
      and held every word down to an ink-to-paper spread of five levels out of
      255 - far past the point a human would call the page readable. It stays,
      gated at a spread under 40, for the pathological scan that thresholds
      badly, and the gate is set low enough that a healthy page never sees it.
    * **Sharpening**: not implemented, deliberately. It amplifies JPEG ringing
      into shapes Tesseract reads as punctuation.
    """
    gray = img.convert("L") if img.mode != "L" else img

    dpi = _source_dpi(img, source_dpi)
    scale = min(MAX_UPSCALE, TARGET_DPI / dpi) if dpi > 0 else 1.0
    # Trim the scale to the pixel budget rather than abandoning the upscale:
    # half the resolution a page wanted still beats none of it.
    pixels = max(1, gray.width * gray.height)
    scale = min(scale, math.sqrt(MAX_PIXELS / pixels))
    if scale > 1.01:
        gray = gray.resize(
            (max(1, round(gray.width * scale)), max(1, round(gray.height * scale))),
            Image.Resampling.LANCZOS,
        )
        dpi *= scale

    a = np.asarray(gray)
    histogram = np.bincount(a.ravel(), minlength=256).astype(np.float64)
    threshold = _otsu_threshold(histogram)
    if MIN_BINARISE_SPREAD <= _ink_paper_spread(histogram, threshold) < BINARISE_BELOW_SPREAD:
        gray = Image.fromarray(((a > threshold) * 255).astype(np.uint8), "L")

    return gray, _clamp_dpi(dpi)


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------


def _detect_rotation(img: Image.Image, dpi: int, timeout: float) -> int:
    """Degrees *clockwise* the page must be turned to stand upright.

    That is Tesseract's own convention, confirmed against synthetic pages: an
    image turned 90 degrees clockwise comes back as ``Rotate: 270``.

    Told the resolution, OSD is four times faster - 2349 ms guessing versus
    659 ms told, on a 1275x1650 page, for identical answers. Left to guess it
    runs its own resolution estimator first.

    Do not be tempted to shrink the page first to save more: at 900 px wide OSD
    starts failing on sideways pages, and at 700 px it confidently returns
    *wrong* answers. It needs the character heights.

    OSD fails constantly on real archives - a page with a stamp and two lines
    of type raises ``TesseractError: Too few characters. Skipping this page`` -
    so a failure here means "leave it alone", not "stop".
    """
    try:
        osd: dict[str, Any] = pytesseract.image_to_osd(
            img,
            output_type=Output.DICT,
            config=f"-c user_defined_dpi={dpi}",
            timeout=timeout,
        )
    except (TesseractError, RuntimeError, ValueError) as exc:
        _LOG.debug("orientation detection declined: %s", exc)
        return 0
    try:
        rotate = int(osd.get("rotate", 0)) % 360
        conf = float(osd.get("orientation_conf", 0.0))
    except (TypeError, ValueError):
        return 0
    if rotate not in (0, 90, 180, 270):
        return 0
    if rotate and conf < MIN_ORIENTATION_CONF:
        _LOG.debug("declining a %d degree turn on orientation confidence %.2f", rotate, conf)
        return 0
    return rotate


def _unrotate_box(box: Box, rotated_by: int) -> Box:
    """Map a box out of the upright frame back into the original image's frame.

    The site shows the scan as it was filed - crooked, sideways, upside down -
    because that is the document. So a box found on the page we straightened
    has to be turned back before anyone draws it.

    Everything below is in page-relative units, so the width and height swap
    for free; only the origin needs care.
    """
    if rotated_by == 90:
        return Box(box.y, 1.0 - box.x - box.w, box.h, box.w)
    if rotated_by == 180:
        return Box(1.0 - box.x - box.w, 1.0 - box.y - box.h, box.w, box.h)
    if rotated_by == 270:
        return Box(1.0 - box.y - box.h, box.x, box.h, box.w)
    return box


# --------------------------------------------------------------------------
# recognition
# --------------------------------------------------------------------------


def _as_int(value: Any, default: int = -1) -> int:
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return default


def _words_from_data(data: dict[str, list[Any]], size: tuple[int, int], min_conf: int) -> list[Word]:
    """Turn the TSV table into words, in reading order.

    Tesseract already emits the table in reading order, so the dense line
    numbering is just "bump the counter whenever the block/paragraph/line
    triple changes". Renumbering matters because the triple is sparse - blocks
    and paragraphs are skipped - and ``Word.line`` has to index ``lines``.
    """
    width, height = size
    if width <= 0 or height <= 0:
        return []
    rows = len(data.get("level", []))
    words: list[Word] = []
    line_index: dict[tuple[int, int, int], int] = {}
    for i in range(rows):
        if _as_int(data["level"][i]) != 5:
            continue
        conf = _as_int(data["conf"][i])
        if conf < 0 or conf < min_conf:
            continue
        text = str(data["text"][i]).strip()
        if not text:
            continue
        key = (
            _as_int(data["block_num"][i], 0),
            _as_int(data["par_num"][i], 0),
            _as_int(data["line_num"][i], 0),
        )
        if key not in line_index:
            line_index[key] = len(line_index)
        box = Box(
            _as_int(data["left"][i], 0) / width,
            _as_int(data["top"][i], 0) / height,
            _as_int(data["width"][i], 0) / width,
            _as_int(data["height"][i], 0) / height,
        )
        words.append(Word(text=text, box=box, conf=conf, line=line_index[key]))
    return words


def _lines_from_words(words: Sequence[Word]) -> list[str]:
    if not words:
        return []
    buckets: list[list[str]] = [[] for _ in range(max(w.line for w in words) + 1)]
    for w in words:
        buckets[w.line].append(w.text)
    return [" ".join(parts) for parts in buckets]


def ocr_image(
    img: Image.Image,
    *,
    languages: Sequence[str] = ("eng",),
    psm: int = 3,
    auto_rotate: bool = True,
    min_conf: int = 0,
    source_dpi: float | None = None,
    timeout: float = 120.0,
) -> OcrResult:
    """Recognise the text on one page image.

    *img* is the page as the site will display it. Boxes come back relative to
    *that* frame even when the page had to be turned upright to be read.

    ``source_dpi`` lets a caller that knows the rendering resolution say so -
    ``raster.py`` always does. Without it the resolution comes from the image's
    own dpi tag, and failing that from its short side on the assumption that it
    is one page across.

    On a Tesseract failure or timeout this returns an empty result with
    ``reason`` set, rather than raising. A single unreadable page must not take
    down a 5,000-page build - but a missing *language* does raise, because that
    is a mistake in the run, not in the document.

    Safe to call from a **process** pool, and only a process pool. Nothing here
    keeps mutable state between calls, but Tesseract does its own OpenMP
    threading inside a page, and stacking Python threads on top of that
    oversubscribes the machine badly - six times slower, measured above.
    Processes give the operating system one scheduling problem instead of two,
    and they also contain a segfault in the C++ recogniser, which real archives
    do produce.
    """
    langs = _check_languages(languages)
    version = _tesseract_version()
    lang_arg = "+".join(langs)

    # Resolve the resolution once, off the image as it arrived: the estimate
    # is based on the short side, so turning the page cannot change it.
    incoming_dpi = _source_dpi(img, source_dpi)
    rotated_by = (
        _detect_rotation(img, _clamp_dpi(incoming_dpi), timeout) if auto_rotate else 0
    )
    # PIL rotates counter-clockwise; Tesseract reports clockwise.
    upright = img.rotate(-rotated_by, expand=True) if rotated_by else img

    prepared, dpi = _prepare(upright, incoming_dpi)
    config = f"--psm {int(psm)} -c user_defined_dpi={dpi}"

    try:
        data = pytesseract.image_to_data(
            prepared,
            lang=lang_arg,
            config=config,
            output_type=Output.DICT,
            timeout=timeout,
        )
    except (TesseractError, RuntimeError) as exc:
        # TesseractError is a RuntimeError and so is pytesseract's timeout;
        # TesseractNotFoundError is an OSError and deliberately not caught,
        # because a broken install is not a broken page.
        _LOG.warning("OCR failed: %s", exc)
        return OcrResult(
            languages=langs,
            tesseract_version=version,
            psm=psm,
            rotated_by=rotated_by,
            reason=f"{type(exc).__name__}: {str(exc).strip()[:200]}",
        )

    words = _words_from_data(data, prepared.size, min_conf)
    if rotated_by:
        for w in words:
            w.box = _unrotate_box(w.box, rotated_by)

    return OcrResult(
        words=words,
        lines=_lines_from_words(words),
        languages=langs,
        tesseract_version=version,
        psm=psm,
        rotated_by=rotated_by,
    )
