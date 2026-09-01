"""Decide, honestly, whether a page's text can be trusted.

An archive that silently fails to index two hundred pages is worse than one
that says so. A reader who searches for a phrase and gets nothing concludes the
phrase is absent, when in fact it was never read. This module's job is to make
that distinction visible: *good*, *blank*, *pictorial*, *suspect*,
*unreadable* - and to say why in words an operator can act on.

What we learned by measuring
----------------------------
**Mean confidence is the least trustworthy number available.** Tesseract emits
nothing at all for text it fails to segment, so lost text never enters the
average - survivorship bias, and it runs the wrong way. A page downsampled 4x
scored a mean confidence of 96.0 with perfect output; a heavily noised page
returned *zero* words, which no confidence statistic can see. Confidence is
reported because it is useful corroboration; it never decides alone.

**The stopword ratio is the best single signal.** The finding that put it
here: clean page 0.38, the same page rotated 90 degrees 0.00 (mean confidence
48.7, output ``pue Jo ay} Se YONS``), heavy blur 0.00 (mean confidence 30.3).
This module's own measurement of the same fixtures reads higher, 0.47 against
0.03, because it keeps numerals out of the denominator and scores against the
best-matching language rather than English - the separation is the same and the
margin is wider.

**Alphabetic ratio is nearly useless.** The blurred page scored 0.96 on it. OCR
garbage is mostly letters, so this module does not use it.

**Nothing here is told what language a page is in.** It is told what the
operator declared in ``ocr.languages``, and that is a hint it may act on to a
page's advantage and never to its cost: :func:`stackroom.lang.stopword_ratio`
takes the maximum over every word list it has, so a Russian page in a
collection declared ``["eng"]`` scores as Russian. The judge should not need to
be told what language a page is in to notice that it *is* language, and when it
was told - when the declared list was a filter rather than a prior - a
perfectly good born-digital page in an undeclared language scored zero, was
told its text layer was broken, and was re-OCR'd into something worse.

**A page in a script no word list covers is reported as unjudged, not as
garbage.** Those are different answers and only one of them is a reason to do
anything. Arabic, Hebrew, Greek, Devanagari, Thai, Japanese and Korean all
score zero against every list this project ships, and the honest thing to
publish about such a page is that its text was judged on other signals. See
:func:`stackroom.lang.stopwords_apply`, which is the test - and which is
deliberately not "is the dominant script one we have a list for", because
``script_of`` folds kana and Hangul in with the Han ideographs and folds
Devanagari, Thai and Georgian in with genuinely bilingual pages.

**Ink coverage against word count is what separates the three no-text cases**,
which otherwise look identical from the text side because all three produce no
text and only one of them is a problem:

==============  =====  ============================================
ink (grey<128)  words  what it is
==============  =====  ============================================
~0                  0  genuinely blank - not a failure
0.5% to 40%         0  failed OCR - there are marks and we read none
>40%, few blobs     0  a photograph or diagram - not a failure
anything           >0  garbage, if the stopword ratio is ~0
==============  =====  ============================================

Measured on the fixtures: blank 0.0% ink and 0 words, heavy grain 21.8%/0,
pure noise 50.0%/0, a photographic page 50.9%/0. The last two are separated by
the connected-component size distribution, *not* by local variance: pure pixel
noise has the highest local variance a page can have and is not a photograph.
See :func:`component_profile`.

Re-tuning
---------
Every threshold below carries the measurement or citation it came from. Running
``python tests/test_quality.py`` prints the full metric table for every fixture
in ``tests/synth.py``, which is how these numbers were chosen and how they
should be re-chosen when the numbers argue otherwise.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from stackroom.lang import (
    SCRIPTS_WITH_STOPWORDS,
    is_garbage_token,
    normalize_token,
    script_of,
    stopword_ratio,
    stopwords_apply,
)
from stackroom.model import OcrQuality, PageVerdict, Word

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image as PILImage

__all__ = [
    "ComponentProfile",
    "component_profile",
    "embedded_layer_broken",
    "ink_coverage",
    "score_page",
]


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------

INK_THRESHOLD = 128
"""Grey level below which a pixel counts as ink.

A fixed mid-grey rather than Otsu. Otsu needs two populations to separate and
hallucinates a threshold in the sensor noise of a blank page, which is the
exact page we most need to call blank. Measured on the fixtures: background 246
or 250, text 28 - the cut is nowhere near either.
"""

BLANK_INK = 0.005
"""Below 0.5% ink with no words, the page really is empty.

Measured: the blank fixture is 0.000, a full typed page is 0.042 over 34 lines,
so one line of text is about 0.001 and a lone page number about 0.0002. The
suggested boundary for "has marks" was 5%, but the fixture ``typed_page(grain=0.5)``
measures 4.9% ink and reads zero words - a page destroyed by grain that a 5%
rule would have called blank. 0.5% keeps that page a failure while still
absorbing a stray speck of scanner dirt.
"""

PICTORIAL_LARGEST_SHARE = 0.25
PICTORIAL_TOP10_SHARE = 0.50
"""Share of ink in the largest (and in the ten largest) connected components
that marks a page as pictorial rather than failed.

Measured: the photographic fixture puts 44.6% of its ink in its largest
component and 77.6% in its top ten. Pure pixel noise puts 0.2% and 1.5%; typed
text 0.1% and 1.1%. Either statistic separates them by more than an order of
magnitude, and we accept either, because a picture made of two equal halves - a
map beside a photograph - splits its largest component without becoming any
less pictorial.
"""

PICTORIAL_MIN_INK = 0.02
"""A page must have real marks before "photograph" is a credible explanation.

Judgement: below 2% ink there is not enough on the page for the largest-blob
statistic to mean anything, and "blank" is the better answer.
"""

MIN_TOKENS_FOR_STOPWORDS = 20
"""Fewest tokens over which a stopword ratio is worth believing.

Judgement, and generous: with 10 tokens a single caption of proper nouns scores
zero and would be libelled as garbage. 20 tokens is about a line and a half.
"""

LOW_STOPWORD_RATIO = 0.10
"""Below this the text does not look like language.

Measured: clean page 0.47, rotated 90 degrees 0.03 (the heavily blurred page
produces no words at all, so it never reaches this test). The eleven-language
prose samples in the test suite score 0.25 to 0.56. Nothing real lands near
0.10, so the number is not delicate.
"""

LOW_MEDIAN_CONF = 70.0
"""Median per-word confidence below which OCR is telling us it struggled.

Measured: clean 96.0, rotated 28.5. Median, not mean - one salvaged word at 95
should not rescue a page of 30s.
"""

LOW_CONF = 60
"""Per-word confidence that counts as low, from Tesseract's own conventions."""

LOW_CONF_FRACTION = 0.30
"""Share of words below :data:`LOW_CONF` that indicates a bad page."""

GARBAGE_RATE = 0.08
"""Reference corpora sit at or below 0.08 (Taghva et al. 2001; Cuper 2022)."""

MEAN_LEN_RANGE = (4.15, 6.25)
"""Healthy mean word length (Cuper et al.). Measured clean fixture: 5.64."""

MEDIAN_LEN_RANGE = (3.0, 6.0)
"""Healthy median word length (Cuper et al.). Measured clean fixture: 5.0."""

SUSPECT_SIGNALS = 2
"""How many independent signals must fire before we call text garbage.

Any one of them alone has a failure mode: a page of proper nouns has no
stopwords, a page of statutory citations scores badly on garbage rules, a faint
but perfectly readable carbon copy has low confidence throughout. Two
independent signals agreeing is the cheapest defence against each of those.
"""

_MAX_ANALYSIS_PIXELS = 700_000
"""Working resolution for connected components. See :func:`component_profile`."""

_MIN_COMPONENT_AREA = 4
"""Ink pixels below which a component is a speck, not a mark.

Measured at the working resolution: this drops 47% of the components on a
typed page and 67% on a noisy one. The largest thing it discards anywhere is
three pixels, so no glyph is at risk - the smallest mark in the fixtures, a
full stop, survives at 6.
"""


# --------------------------------------------------------------------------
# ink
# --------------------------------------------------------------------------


def _as_gray(image: PILImage | np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Grayscale uint8 array from a PIL image (or an array we were handed)."""
    if isinstance(image, np.ndarray):
        arr = image
        if arr.ndim == 3:  # average the channels; exactness does not matter here
            arr = arr.mean(axis=2)
        return arr.astype(np.uint8, copy=False)
    return np.asarray(image.convert("L"), dtype=np.uint8)


DARK_PAGE = 0.60
"""Share of dark pixels above which we suspect the page is printed in reverse.

Measured: a white-on-black page is 95.8% dark here, and 97.9% on the scan that
prompted this check - a real inversion is nowhere near the boundary. The
obvious cut of 0.5 is wrong: a photographic page sits at almost exactly 0.5 by
construction and flipping it on a coin toss produces a nonsense explanation for
the operator.
"""

MIN_INVERTED_INK = 0.002
"""Marks needed on a dark page before we believe it is white-on-black text.

A page can be mostly dark for two reasons and they need opposite handling. A
white-on-black scan measured 97.9% ink and OCR'd perfectly: invert it or every
coverage number is upside down. A page blacked out by a redaction is also
mostly dark, but inverting *that* leaves ~0% ink and the page would be reported
as blank, which is the opposite of true. So: invert only when the light side
carries at least 0.2% of the page - i.e. only when there is something written
in it.
"""


def ink_coverage(image: PILImage | np.ndarray[Any, Any]) -> tuple[float, bool]:
    """Fraction of the page that is ink, and whether we had to invert it.

    Returns ``(coverage, was_inverted)``. Coverage is always measured on the
    "marks" side of the page, so a white-on-black page reports the coverage of
    its *text*, not of its background - 4% and honest, rather than 98% and
    upside down.
    """
    gray = _as_gray(image)
    if gray.size == 0:
        return (0.0, False)
    dark = float((gray < INK_THRESHOLD).mean())
    if dark > DARK_PAGE:
        light = 1.0 - dark
        if light >= MIN_INVERTED_INK:
            return (light, True)
        # Almost entirely dark with nothing written in it: a fully redacted or
        # blackened page. Report it as it is rather than inverting it to blank.
    return (dark, False)


def _ink_mask(gray: np.ndarray[Any, Any], inverted: bool) -> np.ndarray[Any, Any]:
    return (gray >= INK_THRESHOLD) if inverted else (gray < INK_THRESHOLD)


# --------------------------------------------------------------------------
# connected components
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ComponentProfile:
    """The size distribution of the marks on a page.

    Text and photographs are both "ink", and no amount of thresholding tells
    them apart. Their *shapes* do: a page of text is thousands of small
    components of near-identical height, a photograph is a handful of large
    ones, and pixel noise is a fog of specks with no large structure anywhere.
    """

    count: int = 0
    """Components left after specks are dropped."""

    median_height: float = 0.0
    """Median component height, rescaled to the original image's pixels."""

    height_spread: float = 0.0
    """Interquartile range of heights over the median - 0 for a page of one
    type size, large where the marks have no common scale."""

    largest_share: float = 0.0
    """Share of all ink held by the single largest component. The photograph
    detector: measured 0.50 on a photographic page against 0.002 on noise."""

    top10_share: float = 0.0
    """Same for the ten largest together. Measured 0.85 against 0.015."""

    ink_pixels: int = 0
    """Ink pixels at the working resolution, for weighting the shares."""

    scale: int = 1
    """Downsample factor used, so heights can be read back to page pixels."""


def _runs(mask: np.ndarray[Any, Any]) -> tuple[np.ndarray[Any, Any], ...]:
    """Row index, start column and end column (exclusive) of every ink run.

    A row-major flatten with a one-pixel gutter on each side means a run can
    never straddle a row boundary, so the whole image is two ``flatnonzero``
    calls. ``np.argwhere`` on the 2-D difference does the same job and measured
    35% slower.
    """
    h, w = mask.shape
    pad = np.zeros((h, w + 2), dtype=bool)
    pad[:, 1:-1] = mask
    flat = pad.ravel()
    stride = w + 2
    starts = np.flatnonzero(flat[1:] & ~flat[:-1]) + 1
    ends = np.flatnonzero(~flat[1:] & flat[:-1]) + 1
    return (starts // stride, (starts % stride) - 1, (ends % stride) - 1)


def _label_runs(
    rows: np.ndarray[Any, Any],
    starts: np.ndarray[Any, Any],
    ends: np.ndarray[Any, Any],
    height: int,
) -> np.ndarray[Any, Any]:
    """Union-find over runs; returns the root run index for each run.

    Two passes. First, for every pair of adjacent rows, find which runs overlap
    in columns: because the runs of one row are disjoint and sorted, the runs of
    row *r* that touch run *j* of row *r+1* form a contiguous range, and
    ``searchsorted`` finds both of its ends at once. Second, resolve the
    resulting graph by hook-and-compress - scatter each edge's minimum label
    onto both endpoints, then square the parent array until it stops changing.
    That converges in four rounds on the worst fixture; a scalar union-find
    loop over half a million runs does not finish in time.

    OpenCV would do this in one call and is not a dependency of this project.
    """
    n = len(rows)
    parent = np.arange(n, dtype=np.int64)
    if n == 0:
        return parent

    per_row = np.bincount(rows, minlength=height)
    bounds = np.concatenate(([0], np.cumsum(per_row)))
    left: list[np.ndarray[Any, Any]] = []
    right: list[np.ndarray[Any, Any]] = []
    for r in range(height - 1):
        a0, a1 = bounds[r], bounds[r + 1]
        b0, b1 = bounds[r + 1], bounds[r + 2]
        if a0 == a1 or b0 == b1:
            continue
        # 4-connectivity: runs touch when they share at least one column.
        lo = np.searchsorted(ends[a0:a1], starts[b0:b1], side="right")
        hi = np.searchsorted(starts[a0:a1], ends[b0:b1], side="left")
        counts = np.maximum(hi - lo, 0)
        total = int(counts.sum())
        if total == 0:
            continue
        offsets = np.concatenate(([0], np.cumsum(counts)[:-1]))
        left.append(np.arange(total) - np.repeat(offsets, counts) + np.repeat(lo, counts) + a0)
        right.append(np.repeat(np.arange(len(lo)), counts) + b0)
    if not left:
        return parent

    a = np.concatenate(left)
    b = np.concatenate(right)
    for _ in range(64):  # a safety net; four rounds is the observed worst case
        pa = parent[a]
        pb = parent[b]
        low = np.minimum(pa, pb)
        np.minimum.at(parent, pa, low)
        np.minimum.at(parent, pb, low)
        while True:  # pointer jumping: parent = parent[parent] to a fixed point
            nxt = parent[parent]
            if np.array_equal(nxt, parent):
                break
            parent = nxt
        if np.all(parent[a] == parent[b]):
            break
    return parent


def component_profile(image: PILImage | np.ndarray[Any, Any]) -> ComponentProfile:
    """Size distribution of the connected ink components on a page.

Timing, measured on this machine (Python 3.11, numpy 2.4, 1275x1650 page,
    median of five runs): clean typed page 32 ms, a page half-destroyed by
    grain 38 ms, and an all-noise page - 526,000 ink runs, the worst input this
    can be given - 67 ms. Roughly half of the clean-page figure is the
    greyscale conversion and downsample rather than the labelling itself.

    At full 2.1-megapixel resolution the same noise page took 205 ms, over the
    150 ms budget, so the profile is computed at a working resolution of at
    most 0.7 megapixels; a letter page at 150 dpi is reduced by 2, which leaves
    a 22 px glyph 11 px tall, still far above the scale being measured. Ink
    coverage is *not* downsampled: box-averaging a noisy page turns grain into
    midtones and understated the grain fixture's ink by a factor of four.
    """
    gray = _as_gray(image)
    if gray.size == 0:
        return ComponentProfile()

    _, inverted = ink_coverage(gray)
    scale = 1
    while (gray.size // (scale * scale)) > _MAX_ANALYSIS_PIXELS:
        scale += 1
    if scale > 1:
        h = (gray.shape[0] // scale) * scale
        w = (gray.shape[1] // scale) * scale
        block = gray[:h, :w].reshape(h // scale, scale, w // scale, scale)
        gray = block.mean(axis=(1, 3)).astype(np.uint8)
    mask = _ink_mask(gray, inverted)

    rows, starts, ends = _runs(mask)
    if len(rows) == 0:
        return ComponentProfile(scale=scale)
    parent = _label_runs(rows, starts, ends, mask.shape[0])

    _, index = np.unique(parent, return_inverse=True)
    k = int(index.max()) + 1
    widths = (ends - starts).astype(np.float64)
    area = np.bincount(index, weights=widths, minlength=k)
    top = np.full(k, 1 << 30, dtype=np.int64)
    bottom = np.zeros(k, dtype=np.int64)
    np.minimum.at(top, index, rows)
    np.maximum.at(bottom, index, rows)
    heights = (bottom - top + 1).astype(np.float64)

    keep = area >= _MIN_COMPONENT_AREA
    kept_heights = heights[keep]
    total_ink = float(area.sum())
    ordered = np.sort(area)[::-1]

    if kept_heights.size:
        median_h = float(np.median(kept_heights))
        q1, q3 = (float(v) for v in np.percentile(kept_heights, [25, 75]))
        spread = (q3 - q1) / median_h if median_h else 0.0
    else:
        median_h, spread = 0.0, 0.0

    return ComponentProfile(
        count=int(keep.sum()),
        median_height=median_h * scale,
        height_spread=spread,
        largest_share=float(ordered[0] / total_ink) if total_ink else 0.0,
        top10_share=float(ordered[:10].sum() / total_ink) if total_ink else 0.0,
        ink_pixels=int(total_ink),
        scale=scale,
    )


def _looks_pictorial(profile: ComponentProfile, ink: float) -> bool:
    """Is this ink a picture rather than failed text?

    One dominant component is the whole test. The tempting alternative - high
    local pixel variance - fails on the case it most needs to get right: pure
    noise has the highest local variance a page can have and is not a
    photograph. Measured largest-component share: photographic page 0.45,
    noise 0.002, typed text 0.001.
    """
    if ink < PICTORIAL_MIN_INK:
        return False
    return (
        profile.largest_share >= PICTORIAL_LARGEST_SHARE
        or profile.top10_share >= PICTORIAL_TOP10_SHARE
    )


# --------------------------------------------------------------------------
# the embedded text layer
# --------------------------------------------------------------------------

_PUA = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))
_BROKEN_CHAR_SHARE = 0.10
"""Share of replacement/private-use characters that condemns a text layer.

Judgement: a font with a missing or wrong ``ToUnicode`` map does not fail
subtly, it fails for a whole font, so the share is either near zero or very
large. 10% is chosen well below anything a healthy document reaches.
"""

_MIN_SPACE_SHARE = 0.05
"""Spaces per character in real prose is about 0.17 (one space per ~5.7
characters, from the healthy mean word length). A layer that emits glyph
positions but no space glyphs comes out as one enormous run-on token; 0.05 is
comfortably below any real text and far above such a layer's zero.
"""

_MIN_EXTRACTION_SHARE = 0.5
"""Share of the page's glyphs that must survive into extracted text.

When a font has no usable encoding, pdfminer drops those glyphs entirely, so
the page reports many characters and yields half a line of text. Judgement.
"""


def embedded_layer_broken(
    text: str, chars: int, ocr_words: Sequence[Word] | None
) -> tuple[bool, list[str]]:
    """Is the PDF's own text layer unusable, so the page must be re-OCR'd?

    *text* is what was extracted, *chars* the number of glyphs the page claims
    to draw, and *ocr_words* an optional OCR reading of the same page to
    compare against. Returns ``(broken, reasons)``; the reasons are shown to
    the operator, so they say what is wrong in words.

    A broken layer is not a rare curiosity. Scanners bundle OCR into their
    output with fonts whose ``ToUnicode`` tables are wrong, and the result looks
    like a perfectly good searchable PDF until you copy a line out of it.
    """
    reasons: list[str] = []
    stripped = text.strip()

    if chars > 0 and not stripped:
        reasons.append(f"the text layer draws {chars} characters but yields no text")
        return (True, reasons)
    if not stripped:
        return (False, reasons)

    bad = sum(
        1
        for ch in stripped
        if ch == "�" or any(lo <= ord(ch) <= hi for lo, hi in _PUA)
    )
    if bad / len(stripped) > _BROKEN_CHAR_SHARE:
        pct = round(100 * bad / len(stripped))
        reasons.append(f"{pct}% of the text layer is unmapped or replacement characters")

    if chars > 0 and len(stripped) < chars * _MIN_EXTRACTION_SHARE:
        reasons.append(
            f"only {len(stripped)} of {chars} drawn characters could be decoded"
        )

    if len(stripped) > 40 and sum(ch.isspace() for ch in stripped) / len(stripped) < _MIN_SPACE_SHARE:
        reasons.append("the text layer has no word spacing")

    tokens = stripped.split()
    ratio = stopword_ratio(tokens)
    # Same guard as `score_page`: the ratio is evidence only where a word list
    # covers the script. Without it a perfectly good Hindi or Japanese text
    # layer is condemned here, thrown away, and replaced with OCR of a script
    # the recogniser was never asked to read.
    judgeable = stopwords_apply(stripped)
    if judgeable and len(tokens) >= MIN_TOKENS_FOR_STOPWORDS and ratio < LOW_STOPWORD_RATIO:
        reasons.append(
            f"only {round(ratio * 100)}% of the text layer's words are common words"
        )

    if ocr_words:
        ocr_ratio = stopword_ratio([w.text for w in ocr_words])
        # OCR reading the page as language while the embedded layer does not is
        # the clearest evidence available that the layer, not the page, is
        # wrong - and it is evidence in any script, because it compares two
        # readings of the same page rather than one reading against a
        # dictionary. So it is not behind the guard above.
        if ocr_ratio >= LOW_STOPWORD_RATIO * 2 and ratio < LOW_STOPWORD_RATIO:
            reasons.append("OCR reads this page as language and the text layer does not")

    return (bool(reasons), reasons)


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Metrics:
    """Everything measured about a page, before any of it is judged."""

    tokens: list[str] = field(default_factory=list)
    """Normalised tokens that contain at least one letter."""
    raw_count: int = 0
    """Every non-empty token, letters or not. A page of figures has raw tokens
    and no alphabetic ones, and it is emphatically not blank."""
    script: str = "latin"
    """Dominant script of the page as a whole."""
    judgeable: bool = True
    """Whether a stopword ratio means anything for this page's script.

    Not ``script in SCRIPTS_WITH_STOPWORDS``: see :func:`stackroom.lang.stopwords_apply`
    for why that test is wrong for Japanese, Korean, Hindi and Thai alike."""
    stopword_ratio: float = 0.0
    garbage_ratio: float = 0.0
    mean_len: float = 0.0
    median_len: float = 0.0
    has_conf: bool = False
    """False for an embedded text layer, which carries no confidences at all.
    Distinguishing that from "every word scored zero" matters: one is a page we
    cannot judge on confidence, the other is a page that failed."""
    median_conf: float = 0.0
    low_conf_fraction: float = 0.0


def _measure(words: Sequence[Word], languages: Sequence[str] | None) -> _Metrics:
    m = _Metrics()
    raw = [w.text for w in words if w.text and w.text.strip()]
    m.raw_count = len(raw)
    m.tokens = [t for t in (normalize_token(t) for t in raw) if t and any(c.isalpha() for c in t)]
    joined = " ".join(raw)
    m.script = script_of(joined)
    m.judgeable = stopwords_apply(joined)
    # *languages* is a prior here and not a filter - `stopword_ratio` takes the
    # maximum over every list it has whatever it is told, so a page in a
    # language the operator did not declare still scores as the language it is.
    m.stopword_ratio = stopword_ratio(raw, languages)
    if raw:
        # Each token is judged under *its own* script, not the page's. A page
        # of Chinese with an English letterhead is still Chinese token by
        # token, and judging those characters under a Latin vowel rule would
        # condemn the page.
        m.garbage_ratio = sum(is_garbage_token(t, script_of(t)) for t in raw) / len(raw)
    if m.tokens:
        lengths = [len(t) for t in m.tokens]
        m.mean_len = statistics.fmean(lengths)
        m.median_len = float(statistics.median(lengths))

    # CONF_UNKNOWN (-1) marks an embedded text layer, and ingest/ocr.py warns
    # that every Tesseract row above level 5 also reports -1. Averaging those
    # in silently drags a perfect page to a failing score.
    confs = [w.conf for w in words if w.conf >= 0]
    if confs:
        m.has_conf = True
        m.median_conf = float(statistics.median(confs))
        m.low_conf_fraction = sum(1 for c in confs if c < LOW_CONF) / len(confs)
    return m


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def score_page(
    words: Sequence[Word],
    image: PILImage | None = None,
    *,
    languages: Sequence[str] | None = None,
    had_glyphs: bool = False,
) -> OcrQuality:
    """Judge one page and explain the judgement.

    *words* are the page's tokens in reading order, *image* the rendered page
    if we have one, and *had_glyphs* true when the source PDF drew characters
    on this page - which, corroborated by other evidence that the text is not
    language, means the embedded layer is broken and the page should be
    re-OCR'd rather than published as it stands.

    *languages* is what the operator declared in ``ocr.languages``, and it is a
    **prior, not a filter**. It can raise a page's stopword ratio - a genuinely
    bilingual page scores for both halves at once - and it can never lower it,
    because a page in a language nobody declared is a page in a language, not a
    failure. That distinction is the whole of this parameter: ``ocr.languages``
    is a filter where it belongs, on the recogniser, where every extra alphabet
    costs accuracy on the others; here, where being wrong should cost nothing,
    it is a hint. See :func:`stackroom.lang.stopword_ratio`.

    Every field of :class:`~stackroom.model.OcrQuality` is filled, and
    ``reasons`` is written for a person: "read 0 words from a page that is 26%
    ink", not "ERR_OCR_LOW_CONF". Those strings go to the operator's console,
    grouped and counted; the reader gets a notice keyed off the *verdict*
    instead, from the message catalogue and in their own language, because
    these sentences are written in English here and are not translated
    (``build/site.py::_quality_note``).
    """
    m = _measure(words, languages)
    reasons: list[str] = []

    ink = 0.0
    profile = ComponentProfile()
    if image is not None:
        ink, was_inverted = ink_coverage(image)
        if was_inverted:
            # Worth saying out loud: the raw number is 98% and looks alarming.
            reasons.append(f"light text on a dark page; ink measured after inverting ({_pct(ink)})")
        # The component profile only ever changes the verdict for a page with
        # (almost) no text, and it is the most expensive thing here, so skip it
        # when there is plenty of text to judge instead.
        if m.raw_count < MIN_TOKENS_FOR_STOPWORDS:
            profile = component_profile(image)

    quality = OcrQuality(
        # Tokens, not list length: a "word" of pure whitespace is not a word,
        # and the count here is the one quoted back in the reasons below.
        word_count=m.raw_count,
        median_conf=round(m.median_conf, 1),
        low_conf_fraction=round(m.low_conf_fraction, 4),
        stopword_ratio=round(m.stopword_ratio, 4),
        garbage_ratio=round(m.garbage_ratio, 4),
        mean_word_length=round(m.mean_len, 2),
        ink_coverage=round(ink, 4),
        reasons=reasons,
    )

    # ---- nothing was read ------------------------------------------------
    # Note "no tokens at all", not "no words": a page of a budget table is all
    # figures and no letters, and calling that blank would be a lie about a
    # page anyone can read.
    if not m.raw_count:
        if image is None:
            # Without a picture of the page we cannot tell an empty page from a
            # failed one. Say which way we guessed and why.
            if had_glyphs:
                quality.verdict = PageVerdict.UNREADABLE
                reasons.append("the page has a text layer but no words could be read from it")
            else:
                quality.verdict = PageVerdict.BLANK
                reasons.append("no text and no page image to check against")
            return quality
        if ink < BLANK_INK:
            quality.verdict = PageVerdict.BLANK
            reasons.append("the page is empty")
        elif _looks_pictorial(profile, ink):
            quality.verdict = PageVerdict.PICTORIAL
            reasons.append(
                f"a picture, not text: {_pct(ink)} ink in {profile.count} shapes, "
                f"the largest holding {_pct(profile.largest_share)} of it"
            )
        else:
            quality.verdict = PageVerdict.UNREADABLE
            reasons.append(f"read 0 words from a page that is {_pct(ink)} ink")
            if had_glyphs:
                reasons.append("the source PDF has characters here that could not be read")
        return quality

    # ---- text was read: is it language? ----------------------------------
    signals: list[str] = []

    # Every signal below is a distribution, and a distribution over eight
    # tokens is an anecdote. Below the sample floor the page is judged on
    # confidence alone - which is weak, and is why the floor is low.
    enough_text = len(m.tokens) >= MIN_TOKENS_FOR_STOPWORDS

    # The stopword ratio is only evidence where we have a word list for the
    # script in front of us. A page we cannot check scores zero for a reason
    # that has nothing to do with its quality, and saying "garbage" about it is
    # not a conservative guess, it is a wrong answer that re-OCRs a page nobody
    # needed to re-OCR. `lang.stopwords_apply` is the test, and it is
    # deliberately not "is the dominant script one we have a list for": that
    # question is wrong for Japanese and Korean, which `script_of` folds into
    # "han" for the vowel rule's sake, and wrong again for Devanagari, Thai and
    # Georgian, which come back "mixed" - the same answer a bilingual
    # Latin/Cyrillic page gives, and that page we can and must still judge,
    # because OCR spraying Cyrillic across a Latin page is exactly the damage
    # being looked for.
    if not m.judgeable:
        # Not a signal and not a complaint: an admission. The operator is owed
        # the difference between "we read this and it is nonsense" and "we have
        # no words for this alphabet and cannot say".
        #
        # Name the script only where the name is the whole explanation - Greek
        # and Arabic, which `script_of` names and `STOPWORDS` has no list for.
        # Saying "han script: no stopword list" about a page of kana would be a
        # lie about the Chinese list, and saying "mixed" about a page of
        # Devanagari says nothing at all.
        if m.script in SCRIPTS_WITH_STOPWORDS or m.script in ("mixed", "other"):
            reasons.append(
                "no stopword list covers the script here, so the text is judged on other signals"
            )
        else:
            reasons.append(
                f"{m.script} script: no stopword list, so text is judged on other signals"
            )
    elif enough_text and m.stopword_ratio < LOW_STOPWORD_RATIO:
        signals.append(
            f"only {_pct(m.stopword_ratio)} of {len(m.tokens)} words are common words"
        )

    if m.has_conf and m.median_conf < LOW_MEDIAN_CONF:
        signals.append(f"median OCR confidence {m.median_conf:.0f} out of 100")
    if m.low_conf_fraction > LOW_CONF_FRACTION:
        signals.append(f"{_pct(m.low_conf_fraction)} of words scored below {LOW_CONF} confidence")
    if enough_text and m.garbage_ratio > GARBAGE_RATE:
        signals.append(f"{_pct(m.garbage_ratio)} of tokens look invented rather than read")
    if enough_text and (
        not (MEAN_LEN_RANGE[0] <= m.mean_len <= MEAN_LEN_RANGE[1])
        or not (MEDIAN_LEN_RANGE[0] <= m.median_len <= MEDIAN_LEN_RANGE[1])
    ):
        signals.append(
            f"word lengths are unlike prose (mean {m.mean_len:.1f}, median {m.median_len:.0f})"
        )

    # A PDF that drew characters here and yielded nothing resembling language
    # has a broken text layer - a wrong ToUnicode map, usually - and should be
    # re-OCR'd.
    #
    # This used to be the one single-signal condemnation in the module, on the
    # argument that a text layer is either right or wrong and not a matter of
    # degree. That was true of the layer and false of the *evidence*: a low
    # stopword ratio also means "a language we have no list for", and on that
    # reading a born-digital Russian page in a collection declared ``["eng"]``
    # was told to re-OCR itself, which produced a worse transcription of a page
    # that was already perfect. Taking the maximum over every list removes most
    # of that, but not the residue - Vietnamese is Latin and scores zero
    # against all eleven - so the rule now wants corroboration like every other
    # verdict here. A genuinely broken ToUnicode map supplies it easily: it
    # emits tokens that fail the garbage rules and word lengths unlike prose,
    # and often replacement characters that `embedded_layer_broken` catches
    # first. A page in Hindi supplies none of them, because there is nothing
    # wrong with it.
    broken_layer = (
        had_glyphs
        and m.judgeable
        and enough_text
        and m.stopword_ratio < LOW_STOPWORD_RATIO
        and len(signals) >= SUSPECT_SIGNALS
    )

    if broken_layer:
        quality.verdict = PageVerdict.SUSPECT
        reasons.append("the PDF's own text layer does not read as language; re-OCR this page")
        reasons.extend(signals)
    elif len(signals) >= SUSPECT_SIGNALS:
        quality.verdict = PageVerdict.SUSPECT
        reasons.append("the text on this page does not read as language")
        reasons.extend(signals)
    else:
        quality.verdict = PageVerdict.GOOD
        reasons.extend(signals)  # noted, but not enough to condemn the page

    # A page with a couple of stray tokens over a photograph is a photograph.
    if (
        quality.verdict is not PageVerdict.GOOD
        and image is not None
        and m.raw_count < 5
        and _looks_pictorial(profile, ink)
    ):
        quality.verdict = PageVerdict.PICTORIAL
        reasons.insert(0, "a picture with a few stray marks read as text")

    return quality
