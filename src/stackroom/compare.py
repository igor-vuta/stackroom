"""Two releases of the same documents, and what changed between them.

An agency answers a request, withholds half of it, loses an appeal, and sends
the pages again with less blacked out. A court unseals exhibits in stages. A
memo is requested four times over twelve years and four different reviewers
draw the boxes in four different places. The interesting text is exactly the
text that changed status, and until now finding it meant putting two stacks of
paper side by side and reading both.

This module does that mechanically. It is the most dangerous thing in this
project, because its output is a *claim about an agency* - "they released this
sentence in 2024 and withheld it in 2019" - and a wrong claim of that shape is
worse than no tool at all. Everything below is arranged around not making one.

Three ideas hold the whole design up
------------------------------------

**Geometry leads, text corroborates.** A disclosure is caused by a black box
going away. So the primary evidence is a redaction box present in the old
release and absent in the new one; the *region* it used to cover is then read
off the new page, and only text lying inside that region is ever reported as
newly disclosed. Text differences with no accompanying change in the boxes are
counted and shown as what they almost always are - two OCR passes disagreeing -
and never as a disclosure. Re-scanning the same page at a worse resolution
changes thousands of tokens and no boxes, so it produces no claims at all.

**Alignment is a claim too.** Deciding that page 14 of the new release is page
12 of the old one is an inference, it can be wrong, and everything downstream
inherits its error. So the alignment carries its own evidence and confidence,
it is shown to the reader in full, pairs below a confidence floor produce no
findings at all, and a pair of documents that do not look like two releases of
the same thing is *refused* rather than aligned badly.

**Only what an agency released.** The comparison reads ``Page.words`` and
``Page.lines``, which :mod:`stackroom.pipeline` has already stripped of any
token sitting under an opaque shape. It never touches ``Page.hidden``. Every
character this module can put on a page was visible on paper in one of the two
productions. See :func:`hidden_text_is_unreachable` for the enforcement, and
``tests/test_compare.py`` for the test that a planted leak stays out.

What it cannot do
-----------------

It compares *renderings of documents*, not documents. If the two productions
are photocopies of photocopies at different skews, the boxes will not register
and the geometry will be wrong; the module measures that offset and says so.
If a release re-typesets a page, nothing here will align its redactions. If
OCR fails on both copies, there is no text to compare and the module reports
geometry alone. Each of those is stated on the built page, next to the finding
it weakens, rather than in a footnote.

**Every sentence it writes is a message key.** This module composes prose - a
refusal, the evidence for a pairing, the note beside a doubted passage - and
that prose is interface text like any other, so it comes from
``locales/<code>.json`` through a :class:`~stackroom.i18n.Translator` and not
from a literal in a function. The translator arrives the way
``build/negative.py``'s does: keyword-only, defaulting to English, so a caller
that only wanted the algorithms keeps working and keeps producing exactly the
sentences it produced before.

What does *not* go through the catalogue, deliberately: the two release labels
and the document titles, which an operator and an agency wrote; the statutory
exemption codes; the passages quoted out of the documents; and the identifiers
in ``PagePair.evidence`` and ``PagePair.confidence``, which are **data** - the
aligner tests them (``"control number" not in evidence``), the site keys CSS
classes off them, and a comparison whose logic changed with the interface
language would be a different tool in every language. They are translated on
the way out, by :data:`EVIDENCE_KEYS` and :data:`CONFIDENCE_KEYS`.

``docs/COMPARING.md`` is the operator's version of this docstring, including
the ways it fails and how a reader would know.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from markupsafe import Markup, escape

from .i18n import Translator, translator_for
from .lang import is_garbage_token
from .model import Box, Collection, Document, Page, Redaction

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

__all__ = [
    "CONFIDENCE_KEYS",
    "EVIDENCE_KEYS",
    "Alignment",
    "BoxChange",
    "Comparison",
    "DocumentComparison",
    "PageDiff",
    "PageFingerprint",
    "PagePair",
    "ReleaseRef",
    "Similarity",
    "Sketch",
    "TextFinding",
    "align_pages",
    "bates_regime",
    "build",
    "compare_collections",
    "diff_pages",
    "fingerprint_page",
    "fold_bates",
    "hamming_similarity",
    "hidden_text_is_unreachable",
    "image_dhash",
    "iou",
    "jaccard",
    "layout_profile",
    "layout_similarity",
    "match_boxes",
    "page_diagram",
    "page_order_pairs",
    "page_similarity",
    "page_text_for_shingles",
    "pair_documents",
    "registration_offset",
    "run_comparison",
    "shingle_sketch",
    "skeleton",
    "subtract",
    "token_sketch",
]


# ==========================================================================
# the words, and the identifiers that are not words
# ==========================================================================

EVIDENCE_KEYS: dict[str, str] = {
    # pages
    "text": "compare.evidence_text",
    "word order": "compare.evidence_order",
    "layout": "compare.evidence_layout",
    "image": "compare.evidence_image",
    "control number": "compare.evidence_stamp",
    "control numbers differ": "compare.evidence_stamp_differs",
    "position": "compare.evidence_position",
    "out of order": "compare.evidence_out_of_order",
    "a better match for one of these is elsewhere in the document": (
        "compare.evidence_dominated"
    ),
    # documents
    "control numbers": "compare.evidence_stamps",
    "filename": "compare.evidence_filename",
    "same filename": "compare.evidence_same_filename",
    "identical file": "compare.evidence_identical",
}
"""What each evidence identifier is called on the page, in the reader's language.

The identifiers themselves stay English and stay in the data, because they are
not prose: ``_confidence`` asks whether ``"control numbers differ"`` is among
them, ``align_pages`` asks whether ``"control number"`` is, ``PageDiff`` asks
whether ``"text"`` is, and ``tests/test_compare.py`` hashes the tuple to prove
the alignment does not move with the interpreter's hash seed. Translating them
where they are made would make the aligner behave differently in Polish.

So the boundary is here, at the edge: identifiers in, words out. An identifier
with no entry falls through as itself rather than being dropped - a missing
word is a bug, and an evidence column that quietly lost a column is worse.
"""

CONFIDENCE_KEYS: dict[str, str] = {
    "certain": "compare.sure_certain",
    "high": "compare.sure_high",
    "medium": "compare.sure_medium",
    "low": "compare.sure_low",
}
"""The four confidence levels as words. Same rule as :data:`EVIDENCE_KEYS`, with
one more reason: the level is also a CSS class (``cmp-tag--low``) and the class
cannot be in the reader's language."""


def _terms(items: Sequence[str], t: Translator) -> str:
    """A list of terms, punctuated the way this locale punctuates a list."""
    return str(t("compare.evidence_join")).join([p for p in items if p])


def _words(items: Sequence[str], t: Translator) -> list[str]:
    """Evidence identifiers as words. An unknown one falls through as itself."""
    return [str(t(EVIDENCE_KEYS[e])) if e in EVIDENCE_KEYS else e for e in items if e]


def _evidence(items: Sequence[str], t: Translator) -> str:
    """The evidence identifiers as a phrase inside a sentence.

    The last two are joined with a word rather than a comma, because this one
    is read as prose - "matched on control numbers and text" - where the
    alignment table's column is read as a list.
    """
    words = _words(items, t)
    if len(words) < 2:
        return _terms(words, t)
    return _terms(words[:-1], t) + str(t("compare.evidence_and")) + words[-1]


def _confidence_word(level: str, t: Translator) -> str:
    key = CONFIDENCE_KEYS.get(level)
    return str(t(key)) if key else level


# ==========================================================================
# thresholds
# --------------------------------------------------------------------------
# Every number here is a decision about what the tool is willing to claim, so
# each one says what it costs to move it. They are deliberately in one block:
# an operator who does not trust a finding should be able to read the whole
# policy in one screen.
# ==========================================================================

SHINGLE = 6
"""Characters per shingle. Character n-grams rather than words because OCR noise
is per-character: one misread letter destroys six shingles out of a page's two
thousand, where word shingles would lose the whole word and its neighbours.

Six rather than four, and the difference is measured. Short shingles saturate on
a repetitive production: most four-grams of a page written from a small
vocabulary are *inside* words, every page uses every word, and two entirely
different pages of such a document score 0.56 alike - close enough to two copies
of one page under a bad scan (0.69) that nothing can be told from the gap. At
six, more of each shingle spans a word boundary and so carries word order: the
two different pages fall to 0.35 while the badly-scanned copy holds 0.59.
Measured on the fixtures in ``tests/test_compare.py``:

======  ======  =========  ===========  ==========
width   same    30% noise  another page  separation
======  ======  =========  ===========  ==========
4       1.00    0.69       0.56          0.13
6       1.00    0.59       0.35          0.25
8       1.00    0.65       0.21          0.44
======  ======  =========  ===========  ==========

Eight separates better still and is not taken, because this table's noise model
substitutes characters and real recognition also merges and splits words - and a
merge destroys every shingle spanning it, which costs a width of eight twice
what it costs a width of six."""

ORDER_MIN_TOKENS = 12
"""Tokens a page needs before its word order is compared at all. Under a dozen
words there are not enough adjacent pairs for the comparison to mean anything."""

SKETCH_K = 192
"""Bottom-k sketch size. The standard error of a Jaccard estimate is about
sqrt(J(1-J)/k), so 192 gives +/-0.036 at J=0.5 - an order of magnitude finer
than the 0.45/0.85 thresholds it feeds. Doubling it doubles the cost of the
alignment matrix and moves no decision."""

GRID_COLS = 8
GRID_ROWS = 12
"""The layout profile: a coarse map of where there is *anything* on the page -
surviving text or a box that replaced it. It is the channel that still works
when recognition failed on one side, and it is computed from the boxes the
pipeline already produced, so it costs no image I/O. Twelve rows is about two
text lines per row on a letter page, which is fine enough to distinguish two
memos and coarse enough to survive a skewed scan."""

TEXT_MIN_CHARS = 60
"""Below this much recognised text, a page's text channel is not used. Sixty
characters is roughly one line: less than that and a Jaccard estimate is noise
with a decimal point on it."""

TEXT_MIN_RATIO = 0.34
"""And the text channels are also dropped when one page has less than this share
of the other's text, however much that is in absolute terms.

The case is a page withheld down to its letterhead. Six surviving words against
four hundred give a Jaccard of about 0.02 whatever the six words say, because
the denominator is the other page - so the channel would report "these are
different pages" when what happened is that one of them was blacked out. The
comparison is not *dissimilar*, it is *not available*, and the difference
decides whether the most valuable page in a release is aligned or thrown away.
The shape of the page still identifies it; the confidence is capped accordingly
and the built page says which evidence was used."""

W_TEXT, W_ORDER, W_LAYOUT, W_IMAGE = 3.0, 1.5, 1.5, 1.0
"""Channel weights. Text carries twice any other because it is the only channel
that can tell apart two pages of the same shape, which is most pages of most
documents; word order is half of it because it is the same evidence read a
second way and should not be counted twice over."""

ORDER_DISAGREES = 0.10
"""Mean word-order similarity below which two documents are declared not to be
two releases of one document, however alike their vocabulary and their shape.

This is the backstop for the case that defeats every other signal: a production
of forms, or of computer-generated reports, where two entirely different
documents share their words, their margins and their line spacing. Their word
*order* does not overlap at all - measured at 0.03 against 0.19 for two copies
of one page read at 45% token error, which is a scan far worse than any this
tool would call readable. Below the floor the comparison is refused, which
costs a reader a comparison; above it a wrong one would be published."""

NO_EVIDENCE = 0.5
"""What we say about two pages when every channel is unavailable - two blank
pages, say. Deliberately at the fence: the alignment may pair them on position,
and the confidence floor below stops anything being claimed about them."""

MATCH_FLOOR = 0.45
GAP = -0.05
"""The alignment's arithmetic. A pair contributes ``similarity - MATCH_FLOOR``
and a skipped page costs ``GAP``, so two gaps beat any pair below about 0.35
and the aligner walks past a bad match instead of taking it."""

ALIGN_CERTAIN = 0.85
ALIGN_HIGH = 0.65
ALIGN_MEDIUM = 0.45
FINDING_MIN_CONFIDENCE = ("certain", "high", "medium")
"""Only pairs at these confidences produce findings. A "low" pair is still
shown in the alignment table - the reader should see that we do not know - but
nothing is claimed about what changed on it."""

DOMINATED_MARGIN = -0.15
"""How much *worse* than an available alternative a pair has to be before the
monotone alignment's answer is set aside and reconsidered.

A global alignment cannot represent a swap: two pages that changed places look
to it like two ordinary pairings that are each somewhat wrong, and if the
document is repetitive enough - a form, a run of schedules - "somewhat wrong"
still scores above the match floor and the swap is silently absorbed. A pair
this far below a rival is released back into the out-of-order pass; if that
pass cannot place it either, the original pairing is restored at the lowest
confidence, which claims nothing about the page rather than claiming it was
removed and a different one added."""

AMBIGUOUS_MARGIN = 0.06
"""How much better a page's chosen partner has to be than its next-best
candidate before content can be said to have chosen it. Under this, the pages
around it are interchangeable - a form, a log, a run of near-identical
schedules - and what actually decided the pairing was position. That is often
still the right answer, and it is never a certain one."""

MOVE_MIN = 0.72
"""A page matched *out of order* is a stronger claim than one matched in place,
because position is no longer supporting it. It needs to carry itself."""

REFUSE_COVERAGE = 0.34
REFUSE_MEAN = 0.50
"""When fewer than a third of the shorter document's pages find a partner, or
the partners that were found average worse than a coin flip, we say these are
not two releases of the same document rather than aligning them badly."""

DOC_PAIR_MIN = 0.30
"""Floor for pairing two *documents*. Lower than the page floor on purpose: a
document is a bag of many pages and a release that discloses half of a heavily
withheld file legitimately scores low."""

BOX_MATCH_IOU = 0.20
BOX_SAME_IOU = 0.88
BOX_SAME_AREA = 0.10
BOX_SAME_SHIFT = 0.008
"""When two boxes are the same box. 0.008 of the page is about eight pixels on
a 1000-pixel-wide render: below the registration error between two scans of the
same sheet, above the jitter of the raster detector on a clean one."""

MIN_BOX_AREA = 2.5e-4
"""Boxes smaller than this - 0.025% of the page, about a 5mm by 1mm sliver -
are not compared. Under it the raster detector's own noise floor is larger than
the difference being measured."""

REGION_INSIDE = 0.60
"""How much of a word must lie inside an uncovered region before we say the
word was under the box. Six-tenths, so a word clipped by the edge of a box
counts and a word on the next line does not."""

UNMATCHED_RUN = 0.70
"""Share of a run's tokens that must be absent from the other release before a
geometrically-corroborated finding is called corroborated rather than
suspected. Below it, the two scans are probably not registered to each other
and the region is being read off the wrong part of the page."""

MAX_ALIGN_CELLS = 6_000_000
"""Ceiling on the alignment matrix, in page-pairs. At 2,400 pages a side this
is about 50 MB and half a minute; past it the module says the document is too
long to align rather than quietly running for an hour."""

WINDOW_TRIGGER = 200_000
"""Above this many cells, similarity is only computed near the diagonal. The
window is generous, the out-of-order pass below picks up anything outside it,
and the built page says the window was applied."""

MOVE_BUDGET = 250_000
"""Pairs the out-of-order pass will consider. Past it, it is skipped and said
to have been skipped."""


# ==========================================================================
# text normalisation
# ==========================================================================

# OCR confusions, folded one way so that the two spellings of a token collide.
# This is deliberately lossy, and lossy in the safe direction: every collision
# it creates makes two tokens look *the same*, which can only ever suppress a
# claim that text is new. It can never invent one.
_CONFUSE = str.maketrans(
    {
        "0": "o", "1": "l", "2": "z", "3": "e", "4": "a", "5": "s",
        "6": "g", "7": "t", "8": "b", "9": "g",
        "|": "l", "!": "l", "$": "s", "@": "a", "£": "e",
    }
)
_DIGRAPHS = (("rn", "m"), ("vv", "w"), ("cl", "d"))
_WS = re.compile(r"\s+")
_NOT_ALNUM = re.compile(r"[^0-9a-zÀ-￿]+")
_EMPTY_TOKEN = "·"
"""Stand-in for a token that normalises away to nothing. It is one shared
value rather than a unique one per token so that a comma matches a comma."""


def _strip_marks(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def skeleton(token: str) -> str:
    """The shape of a token, with everything OCR gets wrong taken out.

    Two tokens with the same skeleton are treated as the same word. Casefold,
    accents off, punctuation off, digits folded onto the letters they are
    misread as, then the three digraph confusions that survive all of that.
    """
    if not token:
        return _EMPTY_TOKEN
    folded = _NOT_ALNUM.sub("", _strip_marks(token).casefold()).translate(_CONFUSE)
    for a, b in _DIGRAPHS:
        folded = folded.replace(a, b)
    return folded or _EMPTY_TOKEN


def page_order_pairs(page: Page) -> list[str]:
    """Every adjacent pair of tokens on the page, as a list of strings.

    The word-order channel, and it is hashed as *whole pairs* rather than as
    character windows over them - which was tried, and saturates for exactly the
    reason character shingles saturate on a repetitive page: a six-character
    window over "correspondence between" usually lies inside "correspondence"
    and carries no order at all.

    Whole pairs separate cleanly. Two different pages written from one small
    vocabulary share about 3% of their adjacent pairs; two copies of one page
    read at 45% token error still share 19%, because an error destroys the two
    pairs that touch it and no more.
    """
    tokens = [skeleton(word.text) for word in page.words if word.text]
    return [f"{a}\x1f{b}" for a, b in pairwise(tokens)]


def page_text_for_shingles(page: Page) -> str:
    """The page's published text, normalised the way the sketch wants it.

    Reads ``page.words`` - which the pipeline has already stripped of anything
    sitting under a box - and never ``page.lines``, so the string here is the
    same tokens the search index sees.
    """
    joined = " ".join(skeleton(word.text) for word in page.words if word.text)
    return _WS.sub(" ", joined).strip()


# ==========================================================================
# sketches
# ==========================================================================


@dataclass(frozen=True, slots=True)
class Sketch:
    """A bottom-k sample of a page's shingle set.

    ``complete`` means the page had fewer than *k* distinct shingles, so the
    sample is the whole set and the Jaccard below is exact rather than
    estimated. Short pages are common - a cover sheet, a page withheld down to
    its letterhead - and being exact about them is free.
    """

    values: frozenset[int]
    size: int
    complete: bool

    @property
    def empty(self) -> bool:
        return self.size == 0


_EMPTY_SKETCH = Sketch(frozenset(), 0, True)


def _shingle_hash(text: str) -> int:
    """A stable 64-bit hash. Not ``hash()``: that is salted per process, and a
    tool whose output changes between two runs of the same command cannot be
    used to make a claim about an agency."""
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def shingle_sketch(text: str, *, k: int = SKETCH_K, width: int = SHINGLE) -> Sketch:
    """Bottom-*k* sketch of the character *width*-grams of *text*."""
    if len(text) < width:
        return _EMPTY_SKETCH
    hashes = {_shingle_hash(text[i : i + width]) for i in range(len(text) - width + 1)}
    if not hashes:
        return _EMPTY_SKETCH
    if len(hashes) <= k:
        return Sketch(frozenset(hashes), len(hashes), True)
    return Sketch(frozenset(sorted(hashes)[:k]), len(hashes), False)


def token_sketch(tokens: list[str], *, k: int = SKETCH_K) -> Sketch:
    """Bottom-*k* sketch over whole strings, for things already tokenised."""
    if not tokens:
        return _EMPTY_SKETCH
    hashes = {_shingle_hash(token) for token in tokens}
    if len(hashes) <= k:
        return Sketch(frozenset(hashes), len(hashes), True)
    return Sketch(frozenset(sorted(hashes)[:k]), len(hashes), False)


def jaccard(a: Sketch, b: Sketch) -> float:
    """Estimated Jaccard similarity of the two shingle sets.

    Exact when both sketches are complete. Otherwise the standard bottom-k
    estimator: take the *k* smallest hashes of the union of the two samples and
    ask what share of them are in both.
    """
    if a.empty or b.empty:
        return 0.0
    if a.complete and b.complete:
        inter = len(a.values & b.values)
        union = a.size + b.size - inter
        return inter / union if union else 0.0
    k = min(len(a.values), len(b.values))
    if k == 0:
        return 0.0
    smallest = sorted(a.values | b.values)[:k]
    both = a.values & b.values
    return sum(1 for h in smallest if h in both) / k


# ==========================================================================
# layout and image fingerprints
# ==========================================================================


def layout_profile(page: Page) -> tuple[float, ...]:
    """Where there is content on this page, as a coarse occupancy grid.

    Surviving words and redaction boxes count the same, which is the point: a
    passage that was blacked out still occupied the space it occupied, so this
    channel is roughly invariant to *whether* a page was redacted and sensitive
    to *which page* it is.
    """
    cells = [0.0] * (GRID_COLS * GRID_ROWS)
    cell_w = 1.0 / GRID_COLS
    cell_h = 1.0 / GRID_ROWS
    cell_area = cell_w * cell_h
    boxes = [w.box for w in page.words] + [r.box for r in page.redactions]
    for box in boxes:
        if box.w <= 0 or box.h <= 0:
            continue
        c0 = max(0, min(GRID_COLS - 1, int(box.x / cell_w)))
        c1 = max(0, min(GRID_COLS - 1, int((box.x2 - 1e-9) / cell_w)))
        r0 = max(0, min(GRID_ROWS - 1, int(box.y / cell_h)))
        r1 = max(0, min(GRID_ROWS - 1, int((box.y2 - 1e-9) / cell_h)))
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                cell = Box(col * cell_w, row * cell_h, cell_w, cell_h)
                inter = box.intersection(cell)
                if inter is not None:
                    cells[row * GRID_COLS + col] += inter.area / cell_area
    return tuple(min(1.0, v) for v in cells)


CELL_OCCUPIED = 0.04
"""Occupancy above which a grid cell counts as having something in it. One word
in a cell of a letter page measures about 0.09, so this is comfortably below one
word and comfortably above the rounding of a box that clips a cell's corner."""


def layout_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float | None:
    """How alike two pages' occupancy grids are, on two readings of "alike".

    The first is Ruzicka - sum of minimums over sum of maximums - which asks
    whether the same *amount* of content is in the same places. The second is
    the Jaccard of the same grids binarised, which asks only whether content is
    in the same places at all.

    They are averaged because each is blind where the other sees. Ruzicka alone
    cannot recognise a page against its own fully-redacted self: a solid black
    box fills its cells completely and the text it replaced filled perhaps a
    third of them, so the most important page in a comparison scores 0.35 and
    is thrown away. Presence alone is too generous - every page of a memo has
    ink in the same twelve rows - so it cannot be used on its own either.

    ``None``, not zero, when neither page has any content: "both blank" and
    "different pages" are different findings and the caller has to tell them
    apart.
    """
    if len(a) != len(b):
        return None
    lo = sum(min(x, y) for x, y in zip(a, b, strict=True))
    hi = sum(max(x, y) for x, y in zip(a, b, strict=True))
    if hi <= 0:
        return None
    proportion = lo / hi
    on_a = {i for i, v in enumerate(a) if v >= CELL_OCCUPIED}
    on_b = {i for i, v in enumerate(b) if v >= CELL_OCCUPIED}
    union = on_a | on_b
    presence = len(on_a & on_b) / len(union) if union else proportion
    return 0.5 * proportion + 0.5 * presence


def image_dhash(path: str | Path, *, size: int = 8) -> int | None:
    """A 64-bit difference hash of a rendered page.

    The one channel that keeps working when both copies failed recognition:
    it looks at the shape of the ink, not at what the ink says. Resolution and
    JPEG generation loss wash out at eight by eight; a new black box moves ten
    to twenty bits, which lowers the similarity without destroying it.

    Returns ``None`` rather than raising if the file is missing or unreadable -
    a fingerprint channel is an optimisation, and losing one must not take the
    comparison down with it.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            small = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
            data = np.asarray(small, dtype=np.int16)
    except Exception:
        return None
    bits = data[:, 1:] > data[:, :-1]
    value = 0
    for bit in bits.reshape(-1):
        value = (value << 1) | int(bit)
    return value


def hamming_similarity(a: int, b: int, *, bits: int = 64) -> float:
    return 1.0 - (bin(a ^ b).count("1") / bits)


# ==========================================================================
# page fingerprints
# ==========================================================================


@dataclass(frozen=True, slots=True)
class PageFingerprint:
    number: int
    bates: str | None
    bates_folded: str | None
    sketch: Sketch
    order: Sketch
    chars: int
    tokens: int
    layout: tuple[float, ...]
    aspect: float
    readable: bool
    image: int | None = None


_BATES_NOISE = re.compile(r"[^0-9A-Za-z]+")


def fold_bates(stamp: str | None) -> str | None:
    """A control number with the punctuation two scans disagree about removed."""
    if not stamp:
        return None
    folded = _BATES_NOISE.sub("", stamp).upper()
    return folded or None


def fingerprint_page(page: Page, *, image_path: str | Path | None = None) -> PageFingerprint:
    """Everything the aligner knows about one page, and nothing it does not."""
    text = page_text_for_shingles(page)
    return PageFingerprint(
        number=page.number,
        bates=page.bates,
        bates_folded=fold_bates(page.bates),
        sketch=shingle_sketch(text),
        order=token_sketch(page_order_pairs(page)),
        chars=len(text),
        tokens=len(page.words),
        layout=layout_profile(page),
        aspect=page.aspect,
        readable=not page.quality.verdict.is_failure,
        image=image_dhash(image_path) if image_path else None,
    )


def bates_regime(old: list[PageFingerprint], new: list[PageFingerprint]) -> str:
    """Whether the two productions were stamped under the same scheme.

    Three answers, and the middle one is the one everybody forgets. ``absent``:
    one side or both carry no control numbers. ``shared``: the two sets of
    numbers overlap, so a stamp identifies a page across both releases and is
    the strongest signal available. ``disjoint``: both productions carry
    numbers and they have nothing in common, which means the second production
    was re-stamped - and a stamp is then *worse* than useless, because equal
    numbers would be a coincidence and unequal ones prove nothing.

    Treating a re-stamped production as if the numbers meant something is one of
    the two ways this module could confidently align the wrong pages, so the
    question is asked explicitly rather than assumed.
    """
    old_set = {f.bates_folded for f in old if f.bates_folded}
    new_set = {f.bates_folded for f in new if f.bates_folded}
    if not old_set or not new_set:
        return "absent"
    if len(old_set) < 0.5 * max(1, len(old)) or len(new_set) < 0.5 * max(1, len(new)):
        return "absent"
    overlap = len(old_set & new_set)
    if overlap and overlap >= 0.25 * min(len(old_set), len(new_set)):
        return "shared"
    return "disjoint"


@dataclass(frozen=True, slots=True)
class Similarity:
    score: float
    channels: tuple[tuple[str, float], ...]
    evidence: tuple[str, ...]


def page_similarity(
    a: PageFingerprint, b: PageFingerprint, *, regime: str = "absent"
) -> Similarity:
    """How much these two pages look like the same page.

    A weighted mean over whatever channels are *available for this pair*, not
    over a fixed list: a page with no readable text contributes no text
    channel rather than a text similarity of zero, because "we could not read
    it" and "it is a different page" are different findings and collapsing
    them is how an aligner ends up confidently wrong.
    """
    channels: list[tuple[str, float]] = []
    weights: list[float] = []
    evidence: list[str] = []

    lean = min(a.chars, b.chars) >= TEXT_MIN_RATIO * max(a.chars, b.chars, 1)
    if a.chars >= TEXT_MIN_CHARS and b.chars >= TEXT_MIN_CHARS and lean:
        text = jaccard(a.sketch, b.sketch)
        channels.append(("text", text))
        weights.append(W_TEXT)
        if text >= 0.6:
            evidence.append("text")

        if a.tokens >= ORDER_MIN_TOKENS and b.tokens >= ORDER_MIN_TOKENS:
            order = jaccard(a.order, b.order)
            channels.append(("order", order))
            weights.append(W_ORDER)
            if order >= 0.4:
                evidence.append("word order")

    lay = layout_similarity(a.layout, b.layout)
    if lay is not None:
        channels.append(("layout", lay))
        weights.append(W_LAYOUT)
        if lay >= 0.6:
            evidence.append("layout")

    if a.image is not None and b.image is not None:
        img = hamming_similarity(a.image, b.image)
        channels.append(("image", img))
        weights.append(W_IMAGE)
        if img >= 0.75:
            evidence.append("image")

    if weights:
        score = sum(w * s for (_, s), w in zip(channels, weights, strict=True)) / sum(weights)
    else:
        score = NO_EVIDENCE

    # Pages of visibly different proportions are different pages: a landscape
    # exhibit is not a portrait memo however similar their words.
    if a.aspect > 0 and b.aspect > 0:
        ratio = min(a.aspect, b.aspect) / max(a.aspect, b.aspect)
        if ratio < 0.9:
            score *= ratio

    if regime == "shared" and a.bates_folded and b.bates_folded:
        if a.bates_folded == b.bates_folded:
            score = max(score, 0.90)
            evidence.insert(0, "control number")
        else:
            score = min(score, 0.30)
            evidence.append("control numbers differ")

    return Similarity(max(0.0, min(1.0, score)), tuple(channels), tuple(evidence))


# ==========================================================================
# sequence alignment
# ==========================================================================


@dataclass(slots=True)
class PagePair:
    """One row of the alignment: which page of each release, and how sure."""

    old: int | None
    """Index into the old document's page list, or ``None`` for a page that is
    only in the new release."""
    new: int | None
    score: float = 0.0
    confidence: str = "low"
    evidence: tuple[str, ...] = ()
    moved: bool = False
    """True when the two pages are not in the same relative order - the page was
    shifted rather than merely surrounded by insertions."""
    margin: float = 1.0
    """How much better this partner scored than the next-best candidate. Near
    zero means the pages around it are interchangeable and position, not
    content, is what chose this one."""

    @property
    def both(self) -> bool:
        return self.old is not None and self.new is not None

    @property
    def usable(self) -> bool:
        """Whether findings may be derived from this pair."""
        return self.both and self.confidence in FINDING_MIN_CONFIDENCE


@dataclass(slots=True)
class Alignment:
    pairs: list[PagePair] = field(default_factory=list)
    aligned: bool = True
    refusal: str = ""
    regime: str = "absent"
    matched: int = 0
    mean_score: float = 0.0
    windowed: int = 0
    """Width of the diagonal window, or 0 when every pair was considered."""
    notes: list[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for p in self.pairs if p.old is None)

    @property
    def removed(self) -> int:
        return sum(1 for p in self.pairs if p.new is None)

    @property
    def moved(self) -> int:
        return sum(1 for p in self.pairs if p.moved)

    @property
    def usable_pairs(self) -> list[PagePair]:
        return [p for p in self.pairs if p.usable]


def _similarity_matrix(
    old: list[PageFingerprint], new: list[PageFingerprint], regime: str
) -> tuple[np.ndarray, list[list[Similarity | None]], int]:
    n, m = len(old), len(new)
    scores = np.zeros((n, m), dtype=np.float64)
    detail: list[list[Similarity | None]] = [[None] * m for _ in range(n)]
    window = 0
    if n * m > WINDOW_TRIGGER:
        window = max(96, abs(n - m) + 48)
    for i in range(n):
        lo, hi = (0, m) if not window else (max(0, i - window), min(m, i + window + 1))
        for j in range(lo, hi):
            sim = page_similarity(old[i], new[j], regime=regime)
            detail[i][j] = sim
            scores[i, j] = sim.score
    return scores, detail, window


def _needleman_wunsch(scores: np.ndarray, gap: float = GAP) -> list[tuple[int | None, int | None]]:
    """Global alignment over the page-similarity matrix.

    Monotone by construction, which is the property that matters: a run of
    twelve inserted pages costs twelve gaps and then the alignment carries on
    correctly, where a nearest-neighbour matcher would desynchronise and
    mis-pair every page after them.

    The row recurrence is written as a running maximum so each row is a handful
    of numpy operations rather than a Python loop. ``D[i][j] = max(D[i-1][j-1] +
    s, D[i-1][j] + g, D[i][j-1] + g)``, and unrolling the third term gives
    ``max over t <= j of (best_without_left[t] + (j - t) * g)``, which is an
    accumulate once the constant ``g`` is pulled out. ``tests/test_compare.py``
    checks it against a plain three-way-max implementation.
    """
    n, m = scores.shape
    if n == 0 or m == 0:
        return [(i, None) for i in range(n)] + [(None, j) for j in range(m)]

    payoff = scores - MATCH_FLOOR
    js = np.arange(m + 1, dtype=np.float64)
    grid = np.empty((n + 1, m + 1), dtype=np.float64)
    grid[0] = js * gap
    for i in range(1, n + 1):
        prev = grid[i - 1]
        without_left = np.empty(m + 1, dtype=np.float64)
        without_left[0] = prev[0] + gap
        np.maximum(prev[:-1] + payoff[i - 1], prev[1:] + gap, out=without_left[1:])
        grid[i] = np.maximum.accumulate(without_left - js * gap) + js * gap

    eps = 1e-9
    out: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i == 0:
            out.append((None, j - 1))
            j -= 1
            continue
        if j == 0:
            out.append((i - 1, None))
            i -= 1
            continue
        diag = grid[i - 1, j - 1] + payoff[i - 1, j - 1]
        up = grid[i - 1, j] + gap
        left = grid[i, j - 1] + gap
        # Fixed priority on ties, so the same two folders always produce the
        # same alignment. The traceback runs backwards, so taking the left
        # move first here lists a page dropped from the release before a page
        # added to it, which is the order they read in.
        if diag >= up - eps and diag >= left - eps:
            out.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif left >= up - eps:
            out.append((None, j - 1))
            j -= 1
        else:
            out.append((i - 1, None))
            i -= 1
    out.reverse()
    return out


def _margin(scores: np.ndarray, i: int, j: int) -> float:
    """How much better this partner is than the next-best on either side.

    Read off the similarity matrix that has already been computed, so it costs
    two row scans per pair and no extra comparisons. A page whose margin is
    near zero was not identified by its content - something else chose it - and
    :func:`_confidence` refuses to call such a pair certain.
    """
    chosen = float(scores[i, j])
    row = np.delete(scores[i], j)
    col = np.delete(scores[:, j], i)
    rival = 0.0
    if row.size:
        rival = max(rival, float(row.max()))
    if col.size:
        rival = max(rival, float(col.max()))
    return chosen - rival


def _confidence(sim: Similarity, *, margin: float | None = None) -> str:
    """How sure we are that these two pages are the same page.

    Not a band of the score. The score says how alike two pages are; the
    confidence has to say how much that is worth, and three things can make a
    high score worth less than it looks.

    A **control number** shared by both productions settles it outright.
    Recognition **failing on one side** leaves nothing but the shape of the
    page, which is enough to pair pages and never enough to be certain about
    them. And a **small margin** over the next-best candidate means the content
    did not decide this at all - the pages are interchangeable and what chose
    between them was their position in the sequence, which is worth saying out
    loud on a page whose whole subject is what an agency did to one sheet.
    """
    channels = dict(sim.channels)
    if "control numbers differ" in sim.evidence:
        return "low"
    if "control number" in sim.evidence and sim.score >= 0.6:
        return "certain"
    shape_only = "text" not in channels
    interchangeable = margin is not None and margin < AMBIGUOUS_MARGIN
    ceiling = "medium" if (shape_only or interchangeable) else "certain"
    if sim.score >= ALIGN_CERTAIN:
        level = "certain"
    elif sim.score >= ALIGN_HIGH:
        level = "high"
    elif sim.score >= ALIGN_MEDIUM:
        level = "medium"
    else:
        level = "low"
    order = ("low", "medium", "high", "certain")
    return level if order.index(level) <= order.index(ceiling) else ceiling


def _bracket(pairs: list[PagePair]) -> None:
    """Lift a weak pair that is pinned in place by strong neighbours.

    The most valuable page in a comparison is often the one whose content
    similarity is *lowest*: a page withheld in full last time and released in
    full this time shares no text with itself. What identifies it is not its
    content but its position - it is the page between two pages we are sure
    about, with nothing else it could be.

    That is an inference and it is labelled as one, in the data ("position")
    and on the built page. It only fires when the pages either side are matched
    at high confidence *and* are immediately adjacent on both sides, so there
    is no room for an unnoticed insertion to have moved it.
    """
    strong = {"certain", "high"}
    for index, pair in enumerate(pairs):
        if not pair.both or pair.confidence != "low" or pair.moved:
            continue
        before = pairs[index - 1] if index > 0 else None
        after = pairs[index + 1] if index + 1 < len(pairs) else None
        if not (before and before.both and before.confidence in strong):
            continue
        if not (after and after.both and after.confidence in strong):
            continue
        if before.old != pair.old - 1 or before.new != pair.new - 1:
            continue
        if after.old != pair.old + 1 or after.new != pair.new + 1:
            continue
        pair.confidence = "medium"
        pair.evidence = (*pair.evidence, "position")


def _release_dominated(
    pairs: list[PagePair],
) -> tuple[list[PagePair], list[tuple[int, int, float]]]:
    """Unpick a pairing that a clearly better one was available to displace.

    Returns the loosened alignment and a record of what was let go, so that
    :func:`_restore_released` can put back anything the out-of-order pass could
    not improve on.
    """
    out: list[PagePair] = []
    released: list[tuple[int, int, float]] = []
    for pair in pairs:
        if pair.both and pair.margin < DOMINATED_MARGIN:
            assert pair.old is not None and pair.new is not None
            released.append((pair.old, pair.new, pair.score))
            out.append(PagePair(pair.old, None))
            out.append(PagePair(None, pair.new))
        else:
            out.append(pair)
    return out, released


def _restore_released(
    pairs: list[PagePair], released: list[tuple[int, int, float]]
) -> list[PagePair]:
    """Put back a released pairing the out-of-order pass could not improve on.

    Two gaps say "this page was dropped from the release and a different one
    added", which is a claim about an agency, and a wrong one. The restored
    pair says "these are probably the same page and we are not sure", which is
    the truth, and it carries the lowest confidence, so nothing is derived from
    it.
    """
    if not released:
        return pairs
    still_old = {p.old for p in pairs if p.old is not None and p.new is None}
    still_new = {p.new for p in pairs if p.new is not None and p.old is None}
    restorable = {
        old: (new, score)
        for old, new, score in released
        if old in still_old and new in still_new
    }
    if not restorable:
        return pairs
    consumed = {new for new, _ in restorable.values()}
    out: list[PagePair] = []
    for pair in pairs:
        if pair.old is not None and pair.new is None and pair.old in restorable:
            new, score = restorable[pair.old]
            out.append(
                PagePair(
                    pair.old,
                    new,
                    score=score,
                    confidence="low",
                    evidence=("a better match for one of these is elsewhere in the document",),
                    margin=DOMINATED_MARGIN,
                )
            )
            continue
        if pair.new is not None and pair.old is None and pair.new in consumed:
            continue
        out.append(pair)
    return out


def _find_moves(
    pairs: list[PagePair],
    old: list[PageFingerprint],
    new: list[PageFingerprint],
    regime: str,
    notes: list[str],
    t: Translator,
) -> list[PagePair]:
    """Pair up pages the monotone alignment had to leave behind.

    A reordered page appears to a global alignment as one deletion and one
    insertion. Rather than let the alignment break its own monotonicity to
    catch it - which would let a single coincidence desynchronise everything
    after it - the out-of-order pass runs afterwards, over the leftovers only,
    and demands a much higher similarity because position is no longer
    vouching for anything.
    """
    lone_old = [p for p in pairs if p.new is None]
    lone_new = [p for p in pairs if p.old is None]
    if not lone_old or not lone_new:
        return pairs
    if len(lone_old) * len(lone_new) > MOVE_BUDGET:
        notes.append(
            str(
                t(
                    "compare.note_move_budget",
                    old=t("compare.unmatched_pages", count=len(lone_old)),
                    new=t("compare.unmatched_pages", count=len(lone_new)),
                )
            )
        )
        return pairs

    candidates: list[tuple[float, int, int, Similarity]] = []
    for a in lone_old:
        for b in lone_new:
            assert a.old is not None and b.new is not None
            sim = page_similarity(old[a.old], new[b.new], regime=regime)
            if sim.score >= MOVE_MIN:
                candidates.append((sim.score, a.old, b.new, sim))
    if not candidates:
        return pairs

    # Deterministic: best score first, then by page order on both sides.
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    taken_old: set[int] = set()
    taken_new: set[int] = set()
    joined: dict[tuple[int, int], Similarity] = {}
    for _score, i, j, sim in candidates:
        if i in taken_old or j in taken_new:
            continue
        taken_old.add(i)
        taken_new.add(j)
        joined[(i, j)] = sim

    if not joined:
        return pairs

    out: list[PagePair] = []
    consumed_new: set[int] = set()
    lookup = {i: (j, sim) for (i, j), sim in joined.items()}
    for pair in pairs:
        if pair.old is not None and pair.new is None and pair.old in lookup:
            j, sim = lookup[pair.old]
            out.append(
                PagePair(
                    old=pair.old,
                    new=j,
                    score=sim.score,
                    confidence=_confidence(sim),
                    evidence=(*sim.evidence, "out of order"),
                    moved=True,
                )
            )
            consumed_new.add(j)
            continue
        if pair.new is not None and pair.old is None and pair.new in consumed_new:
            continue
        out.append(pair)
    # A moved page consumed later in the sequence than its old partner leaves a
    # stale insertion row behind; drop those too.
    return [p for p in out if not (p.old is None and p.new in consumed_new and not p.moved)]


def align_pages(
    old: list[PageFingerprint],
    new: list[PageFingerprint],
    *,
    regime: str | None = None,
    t: Translator | None = None,
) -> Alignment:
    """Decide which page of the new release is which page of the old one.

    *t* is the build's translator and writes the refusal and the notes. It is
    keyword-only and defaults to English, so a caller that only wanted the
    aligner keeps working and keeps producing the sentences it produced before.
    """
    t = t or translator_for(None)
    regime = regime if regime is not None else bates_regime(old, new)
    alignment = Alignment(regime=regime)

    if not old or not new:
        alignment.pairs = [PagePair(i, None) for i in range(len(old))]
        alignment.pairs += [PagePair(None, j) for j in range(len(new))]
        alignment.aligned = False
        alignment.refusal = str(t("compare.refuse_no_pages"))
        return alignment

    if len(old) * len(new) > MAX_ALIGN_CELLS:
        alignment.aligned = False
        alignment.refusal = str(
            t(
                "compare.refuse_too_big",
                old=len(old),
                new=len(new),
                limit=MAX_ALIGN_CELLS,
            )
        )
        return alignment

    scores, detail, window = _similarity_matrix(old, new, regime)
    alignment.windowed = window
    if window:
        alignment.notes.append(str(t("compare.note_window", window=window)))

    raw = _needleman_wunsch(scores)
    pairs: list[PagePair] = []
    for i, j in raw:
        if i is None or j is None:
            pairs.append(PagePair(i, j))
            continue
        sim = detail[i][j] or page_similarity(old[i], new[j], regime=regime)
        margin = _margin(scores, i, j)
        evidence = sim.evidence
        if margin < AMBIGUOUS_MARGIN and "control number" not in evidence:
            evidence = (*evidence, "position")
        pairs.append(
            PagePair(i, j, sim.score, _confidence(sim, margin=margin), evidence, margin=margin)
        )

    pairs, released = _release_dominated(pairs)
    pairs = _find_moves(pairs, old, new, regime, alignment.notes, t)
    pairs = _restore_released(pairs, released)
    _bracket(pairs)
    alignment.pairs = pairs

    matched = [p for p in pairs if p.both]
    alignment.matched = len(matched)
    alignment.mean_score = (
        sum(p.score for p in matched) / len(matched) if matched else 0.0
    )

    smaller = min(len(old), len(new))
    if not matched:
        alignment.aligned = False
        alignment.refusal = str(t("compare.refuse_nothing_alike"))
    elif alignment.matched < REFUSE_COVERAGE * smaller:
        alignment.aligned = False
        alignment.refusal = str(
            t("compare.refuse_too_few", matched=alignment.matched, total=smaller)
        )
    elif alignment.mean_score < REFUSE_MEAN:
        alignment.aligned = False
        alignment.refusal = str(
            t("compare.refuse_weak", score=t.n(alignment.mean_score, digits=2))
        )
    else:
        order = _order_agreement(matched, detail)
        if order is not None and order < ORDER_DISAGREES:
            alignment.aligned = False
            alignment.refusal = str(
                t("compare.refuse_order", score=t.n(order, digits=2))
            )
    return alignment


def _order_agreement(
    matched: list[PagePair], detail: list[list[Similarity | None]]
) -> float | None:
    """Mean word-order similarity over the pairs that had text on both sides.

    ``None`` when too few pairs had text to say anything, which is not a licence
    to refuse: a release of scanned photographs has no word order and its
    comparison is perfectly sound without one.
    """
    values: list[float] = []
    for pair in matched:
        if pair.old is None or pair.new is None:
            continue
        sim = detail[pair.old][pair.new]
        if sim is None:
            continue
        channels = dict(sim.channels)
        if "order" in channels and "text" in channels:
            values.append(channels["order"])
    if len(values) < 0.6 * len(matched) or not values:
        return None
    return sum(values) / len(values)


# ==========================================================================
# redaction geometry
# ==========================================================================


def iou(a: Box, b: Box) -> float:
    inter = a.intersection(b)
    if inter is None:
        return 0.0
    union = a.area + b.area - inter.area
    return inter.area / union if union > 0 else 0.0


def _centre_shift(a: Box, b: Box) -> float:
    dx = (a.x + a.w / 2) - (b.x + b.w / 2)
    dy = (a.y + a.h / 2) - (b.y + b.h / 2)
    return float(np.hypot(dx, dy))


def subtract(a: Box, b: Box) -> list[Box]:
    """The parts of *a* that *b* does not cover, as up to four rectangles."""
    inter = a.intersection(b)
    if inter is None:
        return [a]
    pieces: list[Box] = []
    if inter.y > a.y:
        pieces.append(Box(a.x, a.y, a.w, inter.y - a.y))
    if inter.y2 < a.y2:
        pieces.append(Box(a.x, inter.y2, a.w, a.y2 - inter.y2))
    if inter.x > a.x:
        pieces.append(Box(a.x, inter.y, inter.x - a.x, inter.h))
    if inter.x2 < a.x2:
        pieces.append(Box(inter.x2, inter.y, a.x2 - inter.x2, inter.h))
    return [p for p in pieces if p.area > 0]


@dataclass(slots=True)
class BoxChange:
    """What happened to one black box between the two releases."""

    kind: str
    """``unchanged`` | ``shrunk`` | ``grown`` | ``moved`` | ``removed`` | ``added``"""
    old: Box | None = None
    new: Box | None = None
    old_codes: tuple[str, ...] = ()
    new_codes: tuple[str, ...] = ()
    iou: float = 0.0
    shift: float = 0.0

    @property
    def area_delta(self) -> float:
        return (self.new.area if self.new else 0.0) - (self.old.area if self.old else 0.0)

    @property
    def codes_changed(self) -> bool:
        return bool(self.old and self.new) and self.old_codes != self.new_codes

    @property
    def uncovered(self) -> list[Box]:
        """Page area this release no longer covers."""
        if self.old is None:
            return []
        if self.new is None:
            return [self.old]
        return subtract(self.old, self.new)

    @property
    def covered(self) -> list[Box]:
        """Page area this release covers and the earlier one did not."""
        if self.new is None:
            return []
        if self.old is None:
            return [self.new]
        return subtract(self.new, self.old)


def _significant(redactions: list[Redaction]) -> list[Redaction]:
    return [r for r in redactions if r.box.area >= MIN_BOX_AREA]


def match_boxes(old: list[Redaction], new: list[Redaction]) -> list[BoxChange]:
    """Pair up the black boxes on two renderings of the same page.

    Greedy on intersection-over-union, best first. Optimal assignment would be
    tidier and would change nothing: redaction boxes on a page are sparse and
    rarely overlap, so the greedy choice is the optimal one except in cases
    that do not occur. Greedy is also easy to read, and this function's output
    is the evidence for every claim the module makes.
    """
    old_boxes = _significant(old)
    new_boxes = _significant(new)
    scored: list[tuple[float, int, int]] = []
    for i, a in enumerate(old_boxes):
        for j, b in enumerate(new_boxes):
            overlap = iou(a.box, b.box)
            if overlap >= BOX_MATCH_IOU:
                scored.append((overlap, i, j))
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))

    used_old: set[int] = set()
    used_new: set[int] = set()
    changes: list[BoxChange] = []
    for overlap, i, j in scored:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        a, b = old_boxes[i], new_boxes[j]
        shift = _centre_shift(a.box, b.box)
        larger = max(a.box.area, b.box.area) or 1.0
        same_area = abs(a.box.area - b.box.area) / larger <= BOX_SAME_AREA
        if overlap >= BOX_SAME_IOU or (same_area and shift <= BOX_SAME_SHIFT):
            kind = "unchanged"
        elif b.box.area < a.box.area * (1 - BOX_SAME_AREA):
            kind = "shrunk"
        elif b.box.area > a.box.area * (1 + BOX_SAME_AREA):
            kind = "grown"
        else:
            kind = "moved"
        changes.append(
            BoxChange(
                kind=kind,
                old=a.box,
                new=b.box,
                old_codes=tuple(a.codes),
                new_codes=tuple(b.codes),
                iou=overlap,
                shift=shift,
            )
        )
    for i, a in enumerate(old_boxes):
        if i not in used_old:
            changes.append(BoxChange("removed", old=a.box, old_codes=tuple(a.codes)))
    for j, b in enumerate(new_boxes):
        if j not in used_new:
            changes.append(BoxChange("added", new=b.box, new_codes=tuple(b.codes)))
    changes.sort(key=lambda c: ((c.old or c.new or Box(0, 0, 0, 0)).y, (c.old or c.new or Box(0, 0, 0, 0)).x, c.kind))
    return changes


def registration_offset(changes: list[BoxChange]) -> float:
    """Median displacement of the boxes that stayed put.

    If the two scans are cropped or skewed differently, every box moves by
    about the same amount. That is not a redaction being adjusted; it is the
    page not being where we think it is, and it is the number that decides
    whether the geometry on this page can be trusted at all.
    """
    shifts = sorted(c.shift for c in changes if c.kind in ("unchanged", "moved", "shrunk", "grown"))
    if not shifts:
        return 0.0
    return shifts[len(shifts) // 2]


# ==========================================================================
# text diff
# ==========================================================================


@dataclass(slots=True)
class TextFinding:
    """A passage whose disclosure status changed, and the evidence for it."""

    direction: str
    """``disclosed`` - readable now, covered before. ``withheld`` - the other way."""
    text: str
    before: str = ""
    after: str = ""
    """The rest of the line either side, so the passage can be read in place."""
    box: Box | None = None
    confidence: str = "corroborated"
    """``corroborated`` - the boxes changed and the text agrees.
    ``suspected`` - the boxes changed but the text does not clearly agree.
    ``geometry`` - the boxes changed and there was no text to read."""
    codes: tuple[str, ...] = ()
    tokens: int = 0
    unmatched: float = 1.0
    """Share of this passage's tokens that are absent from the other release."""
    note: str = ""


def _matched_masks(old: Page, new: Page) -> tuple[list[bool], list[bool]]:
    """For every token on both pages, whether the other page has it too.

    ``difflib`` rather than a hand-written longest-common-subsequence: it is in
    the standard library, it is deterministic, and it has had thirty years of
    people finding its edge cases. ``autojunk`` is off because it discards
    tokens appearing in more than 1% of a long sequence, which on a page of
    prose is every occurrence of "the".
    """
    old_sk = [skeleton(w.text) for w in old.words]
    new_sk = [skeleton(w.text) for w in new.words]
    old_hit = [False] * len(old_sk)
    new_hit = [False] * len(new_sk)
    matcher = difflib.SequenceMatcher(None, old_sk, new_sk, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for offset in range(size):
            old_hit[a + offset] = True
            new_hit[b + offset] = True
    return old_hit, new_hit


def _words_in(page: Page, regions: list[Box]) -> list[int]:
    """Indices of the words lying inside any of *regions*."""
    if not regions:
        return []
    found: list[int] = []
    for index, word in enumerate(page.words):
        if word.box.area <= 0:
            continue
        share = sum(word.box.overlap_ratio(region) for region in regions)
        if share >= REGION_INSIDE:
            found.append(index)
    return found


def _runs(indices: list[int]) -> list[list[int]]:
    """Split a sorted index list into reading-order runs."""
    runs: list[list[int]] = []
    for index in indices:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def _context(page: Page, run: list[int]) -> tuple[str, str]:
    """What is on the same line either side of *run*, for reading it in place."""
    first, last = run[0], run[-1]
    line = page.words[first].line
    before = [w.text for i, w in enumerate(page.words) if w.line == line and i < first]
    after = [w.text for i, w in enumerate(page.words) if w.line == page.words[last].line and i > last]
    return " ".join(before[-9:]), " ".join(after[:9])


def _readable_run(page: Page, run: list[int]) -> bool:
    """Is there a real word in here, or only marks the recogniser invented?"""
    for index in run:
        token = page.words[index].text
        if len(token) >= 2 and any(ch.isalnum() for ch in token) and not is_garbage_token(token):
            return True
    return False


def _bounding(page: Page, run: list[int]) -> Box:
    box = page.words[run[0]].box
    for index in run[1:]:
        box = box.union(page.words[index].box)
    return box


# ==========================================================================
# the page diff
# ==========================================================================


@dataclass(slots=True)
class PageDiff:
    old_number: int
    new_number: int
    boxes: list[BoxChange] = field(default_factory=list)
    disclosed: list[TextFinding] = field(default_factory=list)
    withheld: list[TextFinding] = field(default_factory=list)
    exemption_added: tuple[str, ...] = ()
    exemption_removed: tuple[str, ...] = ()
    code_changes: list[BoxChange] = field(default_factory=list)
    noise_tokens: int = 0
    """Tokens read differently on the two scans with no change in the boxes.
    Recognition noise, counted rather than reported, and shown as a number so a
    reader can see how much of it there is."""
    offset: float = 0.0
    old_readable: bool = True
    new_readable: bool = True
    pair_confidence: str = "certain"
    pair_evidence: tuple[str, ...] = ()
    """How sure the alignment was that these are the same page, carried down so
    that a finding can never look more certain than the pairing it rests on."""
    notes: list[str] = field(default_factory=list)

    @property
    def lifted(self) -> int:
        return sum(1 for c in self.boxes if c.kind in ("removed", "shrunk"))

    @property
    def imposed(self) -> int:
        return sum(1 for c in self.boxes if c.kind in ("added", "grown"))

    @property
    def geometry_changed(self) -> bool:
        return any(c.kind != "unchanged" for c in self.boxes)

    @property
    def changed(self) -> bool:
        return bool(
            self.disclosed
            or self.withheld
            or self.geometry_changed
            or self.exemption_added
            or self.exemption_removed
        )

    @property
    def matched_on_position(self) -> bool:
        """True when these two pages were paired by where they sit rather than
        by what is on them. Everything below inherits that, and the built page
        says so above the findings rather than under them."""
        return "text" not in self.pair_evidence

    @property
    def corroborated(self) -> int:
        return sum(
            1
            for f in (*self.disclosed, *self.withheld)
            if f.confidence == "corroborated"
        )


def _findings_for(
    source: Page,
    source_matched: list[bool],
    regions: list[Box],
    direction: str,
    codes: tuple[str, ...],
    *,
    other_readable: bool,
    t: Translator,
) -> list[TextFinding]:
    """Read the text out of a region whose covering changed.

    *source* is the release in which the text is visible - the new one for a
    disclosure, the old one for a re-withholding. Every character returned came
    from ``Page.words``, which the pipeline emptied of anything under a box, so
    this can only ever surface text one of the two agencies actually released.
    """
    findings: list[TextFinding] = []
    for run in _runs(_words_in(source, regions)):
        if not _readable_run(source, run):
            continue
        unmatched = sum(1 for i in run if not source_matched[i]) / len(run)
        before, after = _context(source, run)
        note = ""
        if unmatched >= UNMATCHED_RUN:
            confidence = "corroborated"
        else:
            confidence = "suspected"
            note = str(t("compare.finding_doubted"))
        if not other_readable and confidence == "corroborated":
            note = str(t("compare.finding_blind"))
        findings.append(
            TextFinding(
                direction=direction,
                text=" ".join(source.words[i].text for i in run),
                before=before,
                after=after,
                box=_bounding(source, run),
                confidence=confidence,
                codes=codes,
                tokens=len(run),
                unmatched=unmatched,
                note=note,
            )
        )
    return findings


def diff_pages(
    old: Page,
    new: Page,
    *,
    pair: PagePair | None = None,
    t: Translator | None = None,
) -> PageDiff:
    """What changed between two renderings of the same page.

    Geometry first, always. The regions that stopped being covered and the
    regions that started being covered are computed from the black boxes alone;
    the text is then read out of those regions and checked against the other
    release's tokens. Text that differs *outside* a changed region is counted
    as recognition noise and never reported as a change in what was disclosed.

    *t* writes the notes and the per-finding caveats, and defaults to English
    for a caller that has no catalogue in hand.
    """
    t = t or translator_for(None)
    diff = PageDiff(
        old_number=old.number,
        new_number=new.number,
        old_readable=not old.quality.verdict.is_failure,
        new_readable=not new.quality.verdict.is_failure,
        pair_confidence=pair.confidence if pair else "certain",
        pair_evidence=pair.evidence if pair else ("text",),
    )
    diff.boxes = match_boxes(old.redactions, new.redactions)
    diff.offset = registration_offset(diff.boxes)
    diff.code_changes = [c for c in diff.boxes if c.codes_changed]

    old_hit, new_hit = _matched_masks(old, new)

    for change in diff.boxes:
        if change.kind in ("removed", "shrunk", "moved"):
            regions = change.uncovered
            if regions:
                diff.disclosed.extend(
                    _findings_for(
                        new, new_hit, regions, "disclosed", change.old_codes,
                        other_readable=diff.old_readable, t=t,
                    )
                )
        if change.kind in ("added", "grown", "moved"):
            regions = change.covered
            if regions:
                diff.withheld.extend(
                    _findings_for(
                        old, old_hit, regions, "withheld", change.new_codes,
                        other_readable=diff.new_readable, t=t,
                    )
                )

    # A lifted box with nothing legible under it is still a finding: the agency
    # changed its mind about something and we cannot say what. Saying so is the
    # honest form of "we do not know".
    for change in diff.boxes:
        if change.kind not in ("removed", "shrunk"):
            continue
        area = sum(r.area for r in change.uncovered)
        if area <= 0:
            continue
        already = any(
            f.box is not None and any(f.box.overlap_ratio(r) > 0.2 for r in change.uncovered)
            for f in diff.disclosed
        )
        if already:
            continue
        diff.disclosed.append(
            TextFinding(
                direction="disclosed",
                text="",
                box=change.old,
                confidence="geometry",
                codes=change.old_codes,
                tokens=0,
                note=str(
                    t(
                        "compare.blind_no_text"
                        if diff.new_readable
                        else "compare.blind_unreadable"
                    )
                ),
            )
        )

    changed_regions = [r for c in diff.boxes for r in (*c.uncovered, *c.covered)]

    def _outside(page: Page, hit: list[bool]) -> int:
        count = 0
        for index, flag in enumerate(hit):
            if flag:
                continue
            word = page.words[index]
            if changed_regions and any(
                word.box.overlap_ratio(region) >= REGION_INSIDE for region in changed_regions
            ):
                continue
            count += 1
        return count

    diff.noise_tokens = _outside(old, old_hit) + _outside(new, new_hit)

    old_codes = {c for c in old.exemptions}
    new_codes = {c for c in new.exemptions}
    diff.exemption_added = tuple(sorted(new_codes - old_codes))
    diff.exemption_removed = tuple(sorted(old_codes - new_codes))

    if diff.offset > BOX_SAME_SHIFT * 2 and diff.geometry_changed:
        diff.notes.append(
            str(
                t(
                    "compare.note_registration",
                    percent=t.pct(diff.offset, digits=1, of_one=True),
                )
            )
        )
    if not diff.old_readable:
        diff.notes.append(str(t("compare.note_old_unreadable")))
    if not diff.new_readable:
        diff.notes.append(str(t("compare.note_new_unreadable")))
    if diff.matched_on_position and diff.changed:
        diff.notes.insert(0, str(t("compare.note_position")))
    return diff


# ==========================================================================
# documents and collections
# ==========================================================================


@dataclass(slots=True)
class ReleaseRef:
    """Enough about one side of the comparison to say what was compared."""

    label: str
    folder: str = ""
    digest: str = ""
    built_at: str = ""
    documents: int = 0
    pages: int = 0


@dataclass(slots=True)
class DocumentComparison:
    doc_id: str
    title: str
    old_title: str = ""
    old_id: str = ""
    identical: bool = False
    """The two files are byte-for-byte the same. Nothing is aligned, nothing is
    claimed, and saying so is useful: it means the agency re-sent the same PDF."""
    identical_pages: int = 0
    """How many pages that was, since there is no alignment to count them in."""
    pair_score: float = 0.0
    pair_evidence: tuple[str, ...] = ()
    alignment: Alignment = field(default_factory=Alignment)
    diffs: list[PageDiff] = field(default_factory=list)
    old_only_pages: list[int] = field(default_factory=list)
    new_only_pages: list[int] = field(default_factory=list)
    unusable_pairs: int = 0
    """Pages we matched but were not confident enough about to compare."""

    @property
    def disclosed(self) -> list[TextFinding]:
        return [f for d in self.diffs for f in d.disclosed if f.confidence != "geometry"]

    @property
    def withheld(self) -> list[TextFinding]:
        return [f for d in self.diffs for f in d.withheld if f.confidence != "geometry"]

    @property
    def lifted(self) -> int:
        return sum(d.lifted for d in self.diffs)

    @property
    def imposed(self) -> int:
        return sum(d.imposed for d in self.diffs)

    @property
    def noise_tokens(self) -> int:
        return sum(d.noise_tokens for d in self.diffs)

    @property
    def changed_pages(self) -> list[PageDiff]:
        return [d for d in self.diffs if d.changed]

    @property
    def changed(self) -> bool:
        return bool(
            self.changed_pages
            or self.old_only_pages
            or self.new_only_pages
            or not self.alignment.aligned
        )


@dataclass(slots=True)
class Comparison:
    old: ReleaseRef
    new: ReleaseRef
    documents: list[DocumentComparison] = field(default_factory=list)
    unpaired_old: list[tuple[str, str, int]] = field(default_factory=list)
    """Documents in the earlier release with no counterpart here: id, title, pages."""
    unpaired_new: list[tuple[str, str, int]] = field(default_factory=list)
    regime: str = "absent"
    notes: list[str] = field(default_factory=list)
    old_media_root: Path | None = None
    """Where the earlier release's renderings are, while the site is being
    written. Never published as a path; only used to copy thumbnails."""

    @property
    def changed_documents(self) -> list[DocumentComparison]:
        return [d for d in self.documents if d.changed]

    @property
    def disclosed(self) -> int:
        return sum(len(d.disclosed) for d in self.documents)

    @property
    def withheld(self) -> int:
        return sum(len(d.withheld) for d in self.documents)

    @property
    def lifted(self) -> int:
        return sum(d.lifted for d in self.documents)

    @property
    def imposed(self) -> int:
        return sum(d.imposed for d in self.documents)

    @property
    def matched_pages(self) -> int:
        """Pages of this release that were tied to a page of the earlier one.

        A document that arrived byte-for-byte identical is never aligned - there
        is nothing to align - but every one of its pages *is* matched, and
        leaving them out of this count makes a release that changed in one file
        look like one we barely understood.
        """
        return sum(
            entry.identical_pages if entry.identical else entry.alignment.matched
            for entry in self.documents
        )

    @property
    def pages_added(self) -> int:
        return sum(len(d.new_only_pages) for d in self.documents)

    @property
    def pages_removed(self) -> int:
        return sum(len(d.old_only_pages) for d in self.documents)

    @property
    def code_changes(self) -> int:
        return sum(len(p.code_changes) for d in self.documents for p in d.diffs)

    @property
    def noise_tokens(self) -> int:
        return sum(d.noise_tokens for d in self.documents)

    @property
    def anything(self) -> bool:
        return bool(self.changed_documents or self.unpaired_old or self.unpaired_new)


def _document_sketch(doc: Document) -> Sketch:
    return shingle_sketch(" ".join(page_text_for_shingles(p) for p in doc.pages))


def _document_order(doc: Document) -> Sketch:
    """The whole document's adjacent token pairs, sketched.

    Pairing documents needs the same second opinion pairing pages does, and for
    the same reason: two files of forms from one agency share their vocabulary
    completely and their word order not at all.
    """
    pairs: list[str] = []
    for page in doc.pages:
        pairs.extend(page_order_pairs(page))
    return token_sketch(pairs)


def _name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower(), autojunk=False).ratio()


def pair_documents(
    old: Collection, new: Collection
) -> tuple[list[tuple[Document, Document, float, tuple[str, ...]]], list[Document], list[Document]]:
    """Decide which document in the new release is which in the old one.

    Filenames change between productions, so the name is a tiebreak and never
    the evidence. What decides it is the control numbers when both productions
    carry them, and the document's whole text otherwise. A pair is only made
    when it is the best available option *for both documents*, so a short
    covering letter cannot be swallowed by whichever long document happens to
    share the most vocabulary with it.
    """
    old_bates = {d.id: {fold_bates(p.bates) for p in d.pages if p.bates} for d in old.documents}
    new_bates = {d.id: {fold_bates(p.bates) for p in d.pages if p.bates} for d in new.documents}
    old_sketch = {d.id: _document_sketch(d) for d in old.documents}
    new_sketch = {d.id: _document_sketch(d) for d in new.documents}
    old_order = {d.id: _document_order(d) for d in old.documents}
    new_order = {d.id: _document_order(d) for d in new.documents}

    scored: list[tuple[float, str, str, tuple[str, ...]]] = []
    for a in old.documents:
        for b in new.documents:
            evidence: list[str] = []
            if a.sha256 and a.sha256 == b.sha256:
                scored.append((1.0, a.id, b.id, ("identical file",)))
                continue
            stamps = 0.0
            shared = old_bates[a.id] & new_bates[b.id]
            if shared:
                stamps = len(shared) / max(1, min(len(old_bates[a.id]), len(new_bates[b.id])))
                evidence.append("control numbers")
            text = jaccard(old_sketch[a.id], new_sketch[b.id])
            order = jaccard(old_order[a.id], new_order[b.id])
            content = 0.6 * text + 0.4 * order
            if content >= 0.3:
                evidence.append("text")
            same_name = a.filename.casefold() == b.filename.casefold()
            name = 1.0 if same_name else _name_similarity(a.filename, b.filename)
            if name >= 0.7:
                evidence.append("filename")
            score = max(stamps, 0.78 * content + 0.22 * name)
            if same_name:
                # Two files with the same name in two productions of one request
                # are almost always the same document, even when one of them is
                # a scan nothing could be read from. Pairing them and letting
                # the page alignment refuse says "these have the same name and
                # we could not line them up", which is a finding; leaving them
                # unpaired says "two unrelated documents", which is not true.
                score = max(score, 0.45)
                evidence.append("same filename")
            scored.append((score, a.id, b.id, tuple(dict.fromkeys(evidence))))

    scored.sort(key=lambda s: (-s[0], s[1], s[2]))
    taken_old: set[str] = set()
    taken_new: set[str] = set()
    pairs: list[tuple[Document, Document, float, tuple[str, ...]]] = []
    by_old = {d.id: d for d in old.documents}
    by_new = {d.id: d for d in new.documents}
    for score, a_id, b_id, evidence in scored:
        if score < DOC_PAIR_MIN or a_id in taken_old or b_id in taken_new:
            continue
        taken_old.add(a_id)
        taken_new.add(b_id)
        pairs.append((by_old[a_id], by_new[b_id], score, evidence))
    pairs.sort(key=lambda p: p[1].id)
    lonely_old = [d for d in old.documents if d.id not in taken_old]
    lonely_new = [d for d in new.documents if d.id not in taken_new]
    return pairs, lonely_old, lonely_new


def _release_ref(collection: Collection, label: str, folder: str = "") -> ReleaseRef:
    return ReleaseRef(
        label=label,
        folder=folder,
        digest=collection.build.source_digest,
        built_at=collection.build.built_at,
        documents=len(collection.documents),
        pages=sum(d.page_count for d in collection.documents),
    )


def _image_for(page: Page, root: Path | None) -> str | None:
    """The smallest rendering of a page on disk, for the image channel."""
    if root is None or not page.thumbs:
        return None
    variant = min(page.thumbs, key=lambda v: (v.width, v.format))
    path = root / variant.path
    return str(path) if path.is_file() else None


def compare_collections(
    old: Collection,
    new: Collection,
    *,
    old_label: str = "",
    new_label: str = "",
    old_folder: str = "",
    new_folder: str = "",
    old_media_root: Path | None = None,
    new_media_root: Path | None = None,
    t: Translator | None = None,
) -> Comparison:
    """Compare two ingested releases, end to end.

    *t* is the build's translator, and everything downstream of here takes it
    from this one argument, so a comparison cannot end up half in one language
    and half in another. It defaults to English for a caller with no catalogue.
    """
    t = t or translator_for(None)
    # A label names a particular batch of paper and is the operator's to write,
    # so it is never translated - but a caller that supplies none still has to
    # get a name for each side, and that name is interface text like any other.
    # `stackroom compare` always passes the folder name or `--old-label`, so
    # this is the library caller's path.
    old_label = old_label or str(t("compare.release_old"))
    new_label = new_label or str(t("compare.release_new"))
    comparison = Comparison(
        old=_release_ref(old, old_label, old_folder),
        new=_release_ref(new, new_label, new_folder),
        old_media_root=old_media_root,
    )
    pairs, lonely_old, lonely_new = pair_documents(old, new)
    comparison.unpaired_old = [(d.id, d.title, d.page_count) for d in lonely_old]
    comparison.unpaired_new = [(d.id, d.title, d.page_count) for d in lonely_new]

    regimes: list[str] = []
    for old_doc, new_doc, score, evidence in pairs:
        entry = DocumentComparison(
            doc_id=new_doc.id,
            title=new_doc.title,
            old_title=old_doc.title,
            old_id=old_doc.id,
            pair_score=score,
            pair_evidence=evidence,
            identical=bool(old_doc.sha256) and old_doc.sha256 == new_doc.sha256,
        )
        entry.identical_pages = new_doc.page_count if entry.identical else 0
        if entry.identical:
            comparison.documents.append(entry)
            continue

        old_fps = [
            fingerprint_page(p, image_path=_image_for(p, old_media_root)) for p in old_doc.pages
        ]
        new_fps = [
            fingerprint_page(p, image_path=_image_for(p, new_media_root)) for p in new_doc.pages
        ]
        regime = bates_regime(old_fps, new_fps)
        regimes.append(regime)
        entry.alignment = align_pages(old_fps, new_fps, regime=regime, t=t)

        if entry.alignment.aligned:
            for pair in entry.alignment.pairs:
                if pair.old is None and pair.new is not None:
                    entry.new_only_pages.append(new_doc.pages[pair.new].number)
                elif pair.new is None and pair.old is not None:
                    entry.old_only_pages.append(old_doc.pages[pair.old].number)
                elif pair.both:
                    if not pair.usable:
                        entry.unusable_pairs += 1
                        continue
                    assert pair.old is not None and pair.new is not None
                    entry.diffs.append(
                        diff_pages(
                            old_doc.pages[pair.old], new_doc.pages[pair.new], pair=pair, t=t
                        )
                    )
        comparison.documents.append(entry)

    comparison.documents.sort(key=lambda d: (not d.changed, d.doc_id))
    if regimes:
        comparison.regime = (
            "shared" if "shared" in regimes else ("disjoint" if "disjoint" in regimes else "absent")
        )
    if comparison.regime == "disjoint":
        comparison.notes.append(str(t("compare.note_renumbered")))
    elif comparison.regime == "absent":
        comparison.notes.append(str(t("compare.note_no_stamps")))
    return comparison


# ==========================================================================
# the safety guarantee
# ==========================================================================


def hidden_text_is_unreachable(comparison: Comparison) -> bool:
    """Assert the one property this feature must never lose.

    Everything a comparison can render passes through :class:`TextFinding`, and
    every :class:`TextFinding` is built from ``Page.words``, which
    :mod:`stackroom.pipeline` has already emptied of tokens sitting under an
    opaque shape - see ``_drop_hidden`` there. ``Page.hidden`` is read nowhere
    in this module; ``tests/test_compare.py`` greps for that and also plants a
    real leak in a fixture and checks the built site for it.

    This function exists so the guarantee has a name a caller can invoke, and
    returns ``True`` unconditionally by construction. If it ever needs a body,
    the design has already been broken somewhere else.
    """
    del comparison
    return True


# ==========================================================================
# rendering
# ==========================================================================

SVG_W = 1000.0
"""Width of a page diagram's viewBox. Heights follow the page's own aspect, so
a rectangle in one of these is at its true relative size and place - the same
promise ``build/negative.py`` makes, for the same reason."""

MAX_FIGURES = 40
"""Page diagrams drawn per document. Each is about a kilobyte; past forty the
page costs more to fetch than the fortieth diagram is worth, and the table
under it carries the same findings in words."""


def _n(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def _rects(boxes: list[Box], klass: str, height: float) -> str:
    out = []
    for box in boxes:
        out.append(
            f'<rect class="{klass}" x="{_n(box.x * SVG_W)}" y="{_n(box.y * height)}" '
            f'width="{_n(max(box.w * SVG_W, 1.2))}" height="{_n(max(box.h * height, 1.2))}"/>'
        )
    return "".join(out)


def page_diagram(diff: PageDiff, aspect: float, t: Translator | None = None) -> Markup:
    """Both releases' black boxes on one outline of the page.

    Solid for what this release still covers, an outline for what it stopped
    covering, and a hatch for what it started covering. Drawn here in Python
    rather than assembled by a script, so the picture is correct before
    anything runs and stays correct in a saved copy of the page.

    *t* writes the ``aria-label``, which is the whole of this picture for a
    reader who cannot see it.
    """
    t = t or translator_for(None)
    height = SVG_W * (aspect if aspect > 0 else 1.294)
    kept: list[Box] = []
    lifted: list[Box] = []
    imposed: list[Box] = []
    for change in diff.boxes:
        if change.kind == "unchanged" and change.new:
            kept.append(change.new)
        else:
            lifted.extend(change.uncovered)
            imposed.extend(change.covered)
            if change.kind in ("shrunk", "grown", "moved") and change.new:
                kept.append(change.new)
    parts = [
        f'<rect class="cmp-sheet" x="0" y="0" width="{_n(SVG_W)}" height="{_n(height)}"/>',
        _rects(kept, "cmp-kept", height),
        _rects(imposed, "cmp-imposed", height),
        _rects(lifted, "cmp-lifted", height),
    ]
    return Markup(
        '<svg class="cmp-diagram" viewBox="0 0 %s %s" role="img" aria-label="%s">'
    ) % (_n(SVG_W), _n(height), escape(_diagram_label(diff, t))) + Markup(
        "".join(parts) + "</svg>"
    )


def _diagram_label(diff: PageDiff, t: Translator) -> str:
    bits: list[str] = []
    if diff.lifted:
        bits.append(str(t("compare.diagram_lifted", count=diff.lifted)))
    if diff.imposed:
        bits.append(str(t("compare.count_imposed", count=diff.imposed)))
    kept = sum(1 for c in diff.boxes if c.kind == "unchanged")
    if kept:
        bits.append(str(t("compare.diagram_unchanged", count=kept)))
    if not bits:
        return str(t("compare.diagram_none", number=diff.new_number))
    return str(t("compare.diagram_label", number=diff.new_number, what=_terms(bits, t)))


def _thumb_path(page: Page) -> str | None:
    if not page.thumbs:
        return None
    webp = [t for t in page.thumbs if t.format == "webp"] or page.thumbs
    return min(webp, key=lambda v: (v.width, v.format)).path


def _copy_old_thumbs(
    builder: Any, comparison: Comparison, wanted: dict[str, list[tuple[str, Page]]]
) -> dict[str, str]:
    """Publish the earlier release's thumbnail for each page that changed.

    Only the pages with findings, and only the thumbnail. A reader has to be
    able to look at both sheets to check a claim; a claim about six pages should
    not drag a second copy of a 400-page production into the archive.

    Which file to copy is read off the page's own ``thumbs``, not guessed from a
    naming convention: the earlier release was rendered by the same pipeline
    that recorded those paths, and a guess that lands on the 1600-pixel variant
    would quietly publish a megabyte per page.

    A rendering is safe to publish for the same reason this archive's own scans
    are: it is a picture of the sheet the agency produced, black box and all.
    Text that a failed redaction left in the *file* is not in the *pixels*.
    """
    root = comparison.old_media_root
    if root is None:
        return {}
    out: dict[str, str] = {}
    for doc_id, pages in sorted(wanted.items()):
        for key, page in pages:
            relative = _thumb_path(page)
            if relative is None:
                continue
            source = root / relative
            if not source.is_file():
                continue
            target = builder.out / "compare" / "earlier" / doc_id / Path(relative).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            # `files_written` and `bytes_written`, not `media_bytes`: the build
            # recomputes that one at the end from an inventory of `out/media`,
            # and these do not live there. Being counted in the wrong column of
            # the report is better than being counted and then discarded.
            builder.report.files_written += 1
            builder.report.bytes_written += target.stat().st_size
            out[key] = f"compare/earlier/{doc_id}/{Path(relative).name}"
    return out


def _document_context(
    entry: DocumentComparison,
    comparison: Comparison,
    old_doc: Document | None,
    new_doc: Document | None,
    thumbs: dict[str, str],
    root: str,
    t: Translator,
) -> dict[str, Any]:
    """Everything ``compare.html.jinja`` needs, and nothing it does not.

    Every page-shaped fact is copied out here into a number, a string or a
    plain dict. No ``Page`` object crosses into the template, which is what
    makes the guarantee in :func:`hidden_text_is_unreachable` a property of the
    context and not only of this file's source - see the test of the same name
    in ``tests/test_compare.py``.
    """
    figures: list[dict[str, Any]] = []
    new_pages = {p.number: p for p in (new_doc.pages if new_doc else [])}
    old_pages = {p.number: p for p in (old_doc.pages if old_doc else [])}
    for diff in entry.changed_pages[:MAX_FIGURES]:
        new_page = new_pages.get(diff.new_number)
        old_page = old_pages.get(diff.old_number)
        aspect = new_page.aspect if new_page else 1.294
        figures.append(
            {
                "diff": diff,
                "diagram": page_diagram(diff, aspect, t),
                "new_url": f"{root}d/{entry.doc_id}/p/{diff.new_number}/index.html",
                "new_thumb": (f"{root}{_thumb_path(new_page)}" if new_page and _thumb_path(new_page) else None),
                "old_thumb": (
                    f"{root}{thumbs[f'{entry.old_id}/{diff.old_number}']}"
                    if f"{entry.old_id}/{diff.old_number}" in thumbs
                    else None
                ),
                "old_state": _page_state_words(old_page, t),
                "new_state": _page_state_words(new_page, t),
            }
        )
    rows = []
    for pair in entry.alignment.pairs:
        rows.append(
            {
                "old": old_doc.pages[pair.old].number if old_doc and pair.old is not None else None,
                "new": new_doc.pages[pair.new].number if new_doc and pair.new is not None else None,
                "old_bates": old_doc.pages[pair.old].bates if old_doc and pair.old is not None else None,
                "new_bates": new_doc.pages[pair.new].bates if new_doc and pair.new is not None else None,
                "score": pair.score,
                # The identifier keys the row's CSS class; the word beside it is
                # for the reader. They are different things and both are needed.
                "confidence": pair.confidence,
                "confidence_word": _confidence_word(pair.confidence, t),
                "evidence": _terms(_words(pair.evidence, t), t),
                "moved": pair.moved,
                "usable": pair.usable,
            }
        )
    return {
        "entry": entry,
        "comparison": comparison,
        "figures": figures,
        "rows": rows,
        "pair_evidence": _evidence(entry.pair_evidence, t),
        "truncated": max(0, len(entry.changed_pages) - MAX_FIGURES),
        "nav": "compare",
        "page_description": str(
            t(
                "compare.doc_description",
                title=entry.title,
                old=comparison.old.label,
                new=comparison.new.label,
            )
        ),
    }


def _page_state_words(page: Page | None, t: Translator) -> str:
    """How much of one sheet is blacked out, in a caption's worth of words."""
    if page is None:
        return str(t("compare.tag_gone"))
    if page.redaction_ratio >= 0.9:
        return str(t("compare.state_almost_all"))
    if page.redactions:
        return str(
            t("withheld.row_share", percent=t.pct(page.redaction_ratio, of_one=True))
        )
    return str(t("compare.state_clear"))


def build(builder: Any, *, t: Translator | None = None) -> None:
    """Write the comparison section, if this build has one.

    Called once from :meth:`SiteBuilder.run`, in the same shape as
    ``build/negative.py``: it takes the builder because the builder owns the
    output path, the template environment and the shared context. A build with
    no comparison attached sets the flag to false and writes nothing, so the
    call is unconditional and the wiring is one line.

    The translator comes the same way, from the builder, with an explicit *t*
    for a caller that has one and no builder. Defaulting to the builder's own
    means these pages and the masthead above them can never end up in two
    different languages.
    """
    t = t if t is not None else getattr(builder, "t", None) or translator_for(None)
    comparison: Comparison | None = getattr(builder, "comparison", None)
    builder.env.globals["compare_enabled"] = comparison is not None
    builder.compare_written = comparison is not None
    if comparison is None:
        return

    old_docs = {d.id: d for d in getattr(builder, "compare_old_documents", [])}
    new_docs = {d.id: d for d in builder.collection.documents}

    wanted: dict[str, list[tuple[str, Page]]] = {}
    for entry in comparison.changed_documents:
        old_doc = old_docs.get(entry.old_id)
        if old_doc is None:
            continue
        by_number = {p.number: p for p in old_doc.pages}
        rows = [
            (f"{entry.old_id}/{d.old_number}", by_number[d.old_number])
            for d in entry.changed_pages[:MAX_FIGURES]
            if d.old_number in by_number
        ]
        if rows:
            wanted[entry.old_id] = rows
    thumbs = (
        _copy_old_thumbs(builder, comparison, wanted)
        if getattr(builder, "compare_old_scans", True)
        else {}
    )

    builder.render(
        "compare_index.html.jinja",
        "compare/index.html",
        nav="compare",
        comparison=comparison,
        entries=comparison.documents,
        # Keyed by document rather than passed as a callable, so the template
        # stays a template: it looks a string up, it does not call into Python.
        evidence={
            entry.doc_id: _evidence(entry.pair_evidence, t)
            for entry in comparison.documents
        },
        page_description=str(
            t(
                "compare.description",
                old=comparison.old.label,
                new=comparison.new.label,
            )
        ),
    )
    for entry in comparison.documents:
        if not entry.changed:
            continue
        builder.render(
            "compare.html.jinja",
            f"compare/{entry.doc_id}/index.html",
            **_document_context(
                entry,
                comparison,
                old_docs.get(entry.old_id),
                new_docs.get(entry.doc_id),
                thumbs,
                "../../",
                t,
            ),
        )


# ==========================================================================
# the command
# ==========================================================================


def run_comparison(
    old_dir: Path,
    new_dir: Path,
    out_dir: Path,
    cfg: Config,
    *,
    old_cfg: Config | None = None,
    old_label: str = "",
    new_label: str = "",
    workers: int | None = None,
    progress: Any = None,
    on_ingest: Any = None,
    on_counted: Any = None,
    publish_old_scans: bool = True,
    scratch: Path | None = None,
) -> tuple[Comparison, Any]:
    """Read two folders, compare them, and write the archive plus the comparison.

    What comes out is the *new* release as a normal Stackroom archive - because
    that is the thing the operator is publishing - with a ``compare/`` section
    reporting what changed since the old one. That shape is chosen deliberately
    over "a diff site":

    - every finding has to lead back to the page it is a finding about, and
      those pages only exist in the archive of the new release;
    - the old release is usually already published somewhere, or is not
      publishable at all, and a tool that silently mirrors it would be a
      surprise;
    - the operator's real sentence is "here is this morning's release, and here
      is what is new in it", which is one artefact, not two.

    The earlier release is read into a temporary folder, which is deleted on
    the way out. Its page thumbnails - and nothing else of it - are copied into
    the site for the pages where something changed, so a reader can check a
    claim against both sheets.

    Both releases go through the same failed-redaction check the ordinary build
    runs, and it applies to the earlier one too: a leak in last year's files is
    a leak.

    *on_counted* is :func:`stackroom.pipeline.build_collection`'s hook, and it
    is handed to **both** reads. This command was the one path with no page
    ceiling on it at all: a comparison reads two collections and can be twice
    the work of the build that is guarded, and the release being published here
    gets the same search index, with the same cold start, as one built by
    ``stackroom build``. Each release is checked on its own count, the moment
    discovery has it - the earlier one first, so an over-large pair is refused
    before a page of either is rasterised.
    """
    import tempfile

    from . import pipeline
    from .build import site as site_mod

    old_dir = Path(old_dir).expanduser().resolve()
    new_dir = Path(new_dir).expanduser().resolve()
    out_dir = Path(out_dir).expanduser().resolve()
    old_cfg = old_cfg or cfg

    parent = str(scratch) if scratch else None
    with tempfile.TemporaryDirectory(prefix="stackroom-compare-", dir=parent) as tmp:
        old_root = Path(tmp)
        old_collection, old_outcomes = pipeline.build_collection(
            old_dir, old_cfg, old_root, progress=progress, workers=workers,
            on_counted=on_counted,
        )
        # The earlier release is checked with the same gate and the same
        # severity. A failed redaction in last year's files is a failed
        # redaction, and this is very often the first time anyone has looked.
        if on_ingest is not None:
            on_ingest(old_label or old_dir.name, old_outcomes)
        pipeline.check_safety(old_outcomes, old_cfg)

        new_collection, new_outcomes = pipeline.build_collection(
            new_dir, cfg, out_dir, progress=progress, workers=workers,
            on_counted=on_counted,
        )
        if on_ingest is not None:
            on_ingest(new_label or new_dir.name, new_outcomes)
        pipeline.check_safety(new_outcomes, cfg)

        # The builder is made before the comparison, for one reason: it owns
        # the build's translator, and the sentences `compare_collections`
        # composes - every refusal, every note - are interface text in the same
        # language as the pages they land on. One translator, made once.
        site_mod.attach_about(new_collection, cfg)
        builder = site_mod.SiteBuilder(new_collection, cfg, out_dir)

        comparison = compare_collections(
            old_collection,
            new_collection,
            old_label=old_label or old_dir.name,
            new_label=new_label or new_dir.name,
            old_folder=old_dir.name,
            new_folder=new_dir.name,
            old_media_root=old_root,
            new_media_root=out_dir,
            t=getattr(builder, "t", None),
        )
        builder.comparison = comparison
        builder.compare_old_documents = old_collection.documents
        builder.compare_old_scans = publish_old_scans
        report = builder.run()

        # `SiteBuilder.run` is expected to call `build(self)` - two lines, see
        # docs/COMPARING.md. It is not required to. If it has not, the section
        # is written here instead so that `stackroom compare` works on an
        # unmodified builder; what is lost by arriving late is real and is
        # reported rather than hidden, because both of the things that run at
        # the end of a build take an inventory of what is already on disk.
        if not getattr(builder, "compare_written", False):
            build(builder)
            note = (
                "the comparison was written after the rest of the site, so its pages are "
                "not in the search index or the offline bundle - add `compare_mod.build(self)` "
                "to SiteBuilder.run()"
            )
            if builder.report.warnings is None:
                builder.report.warnings = [note]
            else:
                builder.report.warnings.append(note)
    comparison.old_media_root = None
    return comparison, report
