"""Production control numbers, and the pages that are not there.

Every serious release stamps a sequential identifier in the margin of every
page - ``ACME000001``, ``DOJ-OGR-00012345``, sometimes a bare ``0852``. Two
things come out of reading them.

The first is citable page identity. A reader who wants to argue about page 43
of a 900-page dump can name the number the agency itself printed on it, and
anyone with the same production can find the same page.

The second is the reason this module is worth writing. The stamp is applied
before anything is pulled, so a **gap** in the sequence is a page that was
withheld in its entirety: nothing on it, not even a black box to count. It is
routinely the most newsworthy fact recoverable from a release, and it is
invisible unless somebody counts.

Nothing about the format is standardised - not the prefix, the padding, the
typeface, nor where on the page it lands - so the evidence here is structural
rather than lexical:

* **Position.** A stamp lands in the same place on every page. That is the
  strongest signal by a distance, stronger than anything about the text.
* **Monotonicity.** The numbers go up. Not by one, necessarily - that is the
  gap - but up.
* **Coverage.** A real stamp is on nearly every page. Something that appears
  on a third of them is a date, a docket line or an exhibit label.

Which is also the defence against the thing that looks most like a control
number and is not: the page number. ``1``…``N`` where ``N`` is the page count
is the printer's furniture, and a module that reports it as a production
sequence will also cheerfully report that no pages are missing.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Protocol

from ..model import Word

__all__ = [
    "NUMERIC_RE",
    "PREFIXED_RE",
    "BatesSeries",
    "PageWords",
    "detect",
]


# --------------------------------------------------------------------------
# tuning
# --------------------------------------------------------------------------

MARGIN_BAND = 0.08
"""Fraction of the page height at top and bottom that counts as margin. The
stamp is furniture; anything found in the body is a number in a sentence."""

GRID = 6
"""Positions are quantised to a GRID x GRID lattice before clustering. Coarse
enough that a stamp jittering by a few points stays in one cell, fine enough
that bottom-left, bottom-centre and bottom-right are three different places."""

MERGE_DISTANCE = 0.05
"""Two cells are the same stamp if their centroids are this close *and* they
never appear on the same page - the signature of a stamp sitting on a cell
boundary. Two stamps on one page are a two-up scan and must stay apart."""

MIN_PAGES = 3
"""Below this, monotonicity means nothing: two ascending numbers are a
coincidence that happens all the time. One page can only ever be a candidate."""

MIN_COVERAGE = 0.80
MIN_MONOTONE = 0.95

BIG_JUMP = 1000
"""A forward step this large is damage rather than a gap, and may be repaired.

Everything smaller is left alone, deliberately. A jump of nine pages is the
single most valuable thing this module can report, and a repair rule that
notices ``000010`` is one character away from ``000019`` would quietly delete
it. Repairs fire on steps that go *backwards* or leap absurdly, and only when
one substitution lands the step on exactly +1."""

# What a scanner returns instead of a digit. Only used to rescue a candidate
# that has already earned its place by position - never to invent one.
OCR_FOLD = {
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "I": "1", "l": "1", "i": "1", "|": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "G": "6",
    "T": "7",
    "B": "8",
    "g": "9", "q": "9",
}
_FOLDABLE = frozenset(OCR_FOLD) | frozenset("0123456789")

PREFIXED_RE = re.compile(r"^([A-Z][A-Z0-9._-]{1,24}?)[ _\-]?(\d{3,10})([A-Z]?)$", re.IGNORECASE)
"""``ACME000001``, ``DOJ-OGR-00012345``, ``d123-002``, ``FBI 1234A``."""

NUMERIC_RE = re.compile(r"^(\d{4,10})$")
"""A bare stamp. Four digits minimum: below that everything is a page number,
a year or a room number."""

_TRIM = " \t\xa0.,;:()[]{}<>\"'"
"""Punctuation and spacing to shave off a token before parsing it.
\xa0 is in there because a PDF text layer will hand you a non-breaking space
and nothing about it looks different in a diff."""


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------


class PageWords(Protocol):
    """The little this module needs from a page.

    :class:`stackroom.model.Page` satisfies it as it stands; so does any
    three-line stand-in a test cares to write. Word boxes are page-relative,
    origin top-left, exactly as :mod:`stackroom.model` defines them, so nothing
    here needs to know the page size.
    """

    number: int
    words: Sequence[Word]


@dataclass(slots=True)
class BatesSeries:
    """One production's control numbers, as read off the pages."""

    prefix: str
    """The literal text before the digits, separator included, so that
    ``prefix + digits + suffix`` reproduces the stamp exactly. Empty for a bare
    numeric series."""

    width: int
    """Zero-padded width of the digits. Constant, by definition: a run whose
    width wanders is a page number that crossed a power of ten."""

    first: str
    last: str
    coverage: float
    """Fraction of the document's pages carrying this stamp."""

    gaps: list[tuple[str, str]] = field(default_factory=list)
    """Missing ranges, inclusive, as formatted control numbers. This is the
    payload: each one is a run of pages withheld in full."""

    page_map: dict[int, str] = field(default_factory=dict)
    """Page number -> control number, for the pages that carry one."""

    confidence: float = 0.0
    suffix: str = ""
    """Trailing letter, if the production uses one (``ACME000123A``)."""

    confirmed: bool = True
    """False for a candidate that could not be verified - a document too short
    to establish a sequence. An unconfirmed series is a guess and must be
    labelled as one wherever it is shown."""

    notes: list[str] = field(default_factory=list)
    """Anything an operator should know: repaired digits, a split at a
    backwards step, a series that only just cleared the thresholds."""

    @property
    def missing_pages(self) -> int:
        """How many control numbers were never delivered."""
        total = 0
        for lo, hi in self.gaps:
            total += _value(hi, self.prefix, self.suffix) - _value(lo, self.prefix, self.suffix) + 1
        return total


# --------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _Candidate:
    page: int
    prefix: str
    digits: str
    suffix: str
    value: int
    cx: float
    cy: float
    bucket: tuple[int, int]
    raw: str
    repaired: bool


def _value(stamp: str, prefix: str, suffix: str) -> int:
    body = stamp[len(prefix):]
    if suffix and body.endswith(suffix):
        body = body[: -len(suffix)]
    return int(body)


def _fold(token: str) -> str | None:
    """Repair a token whose numeric tail came back with letters in it.

    Only the tail is touched - folding the whole token would turn the prefix
    ``SOB`` into ``508`` - and only lightly: at most two substitutions, and
    only where real digits are already in the majority. ``ACME00000S`` is a
    misread stamp. ``LOSS`` is a word.
    """
    i = len(token)
    while i > 0 and token[i - 1] in _FOLDABLE:
        i -= 1
    tail = token[i:]
    if len(tail) < 3:
        return None
    folded = "".join(OCR_FOLD.get(ch, ch) for ch in tail)
    substitutions = sum(1 for a, b in zip(tail, folded, strict=True) if a != b)
    digits = sum(1 for ch in tail if ch.isdigit())
    if substitutions == 0 or substitutions > 2 or digits < len(tail) - 2 or digits < 2:
        return None
    return token[:i] + folded


def _parse(token: str) -> tuple[str, str, str] | None:
    """Split a token into ``(prefix, digits, suffix)``, or return nothing."""
    m = PREFIXED_RE.match(token)
    if m is not None:
        return token[: m.start(2)].upper(), m.group(2), m.group(3).upper()
    m = NUMERIC_RE.match(token)
    if m is not None:
        return "", m.group(1), ""
    return None


def _candidates(page: PageWords) -> list[_Candidate]:
    """Everything in this page's margins that could be a control number."""
    out: list[_Candidate] = []
    for word in page.words:
        if getattr(word, "hidden", False):
            continue
        box = word.box
        cy = box.y + box.h / 2.0
        if MARGIN_BAND < cy < 1.0 - MARGIN_BAND:
            continue
        token = word.text.strip(_TRIM)
        if not token:
            continue
        parsed = _parse(token)
        repaired = False
        if parsed is None:
            folded = _fold(token)
            if folded is None:
                continue
            parsed = _parse(folded)
            if parsed is None:
                continue
            repaired = True
        prefix, digits, suffix = parsed
        cx = box.x + box.w / 2.0
        bucket = (
            min(GRID - 1, max(0, int(cx * GRID))),
            min(GRID - 1, max(0, int(cy * GRID))),
        )
        readings = [(digits, suffix, repaired)]
        if suffix and suffix in OCR_FOLD and not repaired:
            # ``ACME00000S`` parses cleanly as five digits and a trailing S,
            # and it is also ``ACME000005`` with the last digit misread. Both
            # readings go forward; the clusters decide which one the rest of
            # the document agrees with, and a reading nothing else agrees with
            # dies of low coverage. Guessing here, one token at a time, is what
            # produces a phantom gap on the page the scanner fumbled - and a
            # phantom gap is a claim that pages were withheld when they were
            # not.
            readings.append((digits + OCR_FOLD[suffix], "", True))
        for d, sfx, rep in readings:
            out.append(
                _Candidate(
                    page=page.number,
                    prefix=prefix,
                    digits=d,
                    suffix=sfx,
                    value=int(d),
                    cx=cx,
                    cy=cy,
                    bucket=bucket,
                    raw=word.text,
                    repaired=rep,
                )
            )
    return out


# --------------------------------------------------------------------------
# clustering
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Cluster:
    prefix: str
    width: int
    suffix: str
    bucket: tuple[int, int]
    members: list[_Candidate] = field(default_factory=list)

    @property
    def pages(self) -> set[int]:
        return {c.page for c in self.members}

    @property
    def centroid(self) -> tuple[float, float]:
        return (
            statistics.fmean(c.cx for c in self.members),
            statistics.fmean(c.cy for c in self.members),
        )

    @property
    def spread(self) -> float:
        """Mean distance from the centroid: how still the stamp holds."""
        cx, cy = self.centroid
        return statistics.fmean(
            ((c.cx - cx) ** 2 + (c.cy - cy) ** 2) ** 0.5 for c in self.members
        )


def _cluster(cands: Sequence[_Candidate]) -> list[_Cluster]:
    groups: dict[tuple[str, int, str, tuple[int, int]], _Cluster] = {}
    for c in cands:
        key = (c.prefix, len(c.digits), c.suffix, c.bucket)
        cluster = groups.get(key)
        if cluster is None:
            cluster = groups[key] = _Cluster(c.prefix, len(c.digits), c.suffix, c.bucket)
        cluster.members.append(c)
    return list(groups.values())


def _merge_adjacent(clusters: list[_Cluster]) -> list[_Cluster]:
    """Rejoin one stamp that a grid line happened to cut in half.

    Only when the two halves never share a page: a stamp cannot be in two
    places at once, so overlapping page sets mean two stamps - a two-up scan -
    and merging them would fabricate a sequence that jumps back and forth.
    """
    out: list[_Cluster] = []
    for cluster in sorted(clusters, key=lambda c: -len(c.members)):
        for kept in out:
            if (kept.prefix, kept.width, kept.suffix) != (
                cluster.prefix,
                cluster.width,
                cluster.suffix,
            ):
                continue
            if kept.pages & cluster.pages:
                continue
            kx, ky = kept.centroid
            cx, cy = cluster.centroid
            if ((kx - cx) ** 2 + (ky - cy) ** 2) ** 0.5 <= MERGE_DISTANCE:
                kept.members.extend(cluster.members)
                break
        else:
            out.append(cluster)
    return out


def _drop_variable_width(clusters: list[_Cluster]) -> list[_Cluster]:
    """Separate out the runs whose padding wanders.

    Width is part of the cluster key, so a run that crosses a power of ten -
    ``0999`` then ``1000`` - arrives here as two clusters in the same place
    with the same prefix. Padding is the whole point of a Bates stamp; a
    printer's page counter is the thing that does not have it. So if neither
    width covers the document but the two together do, both are dropped.
    """
    by_place: dict[tuple[str, str, tuple[int, int]], list[_Cluster]] = defaultdict(list)
    for c in clusters:
        by_place[(c.prefix, c.suffix, c.bucket)].append(c)
    kept: list[_Cluster] = []
    for group in by_place.values():
        if len(group) > 1:
            union = set().union(*(c.pages for c in group))
            best = max(len(c.pages) for c in group)
            if len(union) > best:
                continue
        kept.extend(group)
    return kept


def _drop_dominated(clusters: list[_Cluster]) -> list[_Cluster]:
    """Where two readings of the same stamp survive, keep the undamaged one.

    A production that really does end its stamps in a letter - ``ACME000123B``
    - produces a repaired twin on every page, because ``B`` is also how a
    scanner spells ``8``. Both explain the same pages from the same spot, so
    the one that needed no repairs wins and the twin is dropped rather than
    reported as a second production that does not exist.
    """
    out: list[_Cluster] = []
    for cluster in sorted(
        clusters, key=lambda c: (sum(m.repaired for m in c.members), -len(c.pages))
    ):
        if any(
            kept.bucket == cluster.bucket
            and kept.prefix == cluster.prefix
            and kept.pages >= cluster.pages
            for kept in out
        ):
            continue
        out.append(cluster)
    return out


# --------------------------------------------------------------------------
# sequences
# --------------------------------------------------------------------------


def _one_per_page(cluster: _Cluster) -> list[_Candidate]:
    """One reading per page, closest to where the stamp usually sits."""
    cx, cy = cluster.centroid
    best: dict[int, _Candidate] = {}
    for c in cluster.members:
        cur = best.get(c.page)
        if cur is None:
            best[c.page] = c
            continue
        d_new = (c.cx - cx) ** 2 + (c.cy - cy) ** 2
        d_old = (cur.cx - cx) ** 2 + (cur.cy - cy) ** 2
        if d_new < d_old:
            best[c.page] = c
    return [best[p] for p in sorted(best)]


def _repair(seq: list[_Candidate], notes: list[str]) -> list[int]:
    """Values in page order, with single-digit misreads corrected.

    A repair only happens where the sequence is already broken - a step
    backwards or an implausible leap - and only when changing one character
    makes the step exactly +1. That is a very narrow door, and it is meant to
    be: the alternative is a module that can make any pile of numbers ascend.
    """
    values = [c.value for c in seq]
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        if 0 <= delta < BIG_JUMP:
            continue
        want = values[i - 1] + 1
        digits = seq[i].digits
        target = f"{want:0{len(digits)}d}"
        if len(target) != len(digits):
            continue
        if sum(1 for a, b in zip(digits, target, strict=True) if a != b) == 1:
            notes.append(f"read {seq[i].raw!r} as {target}: one digit repaired to close the sequence")
            values[i] = want
    return values


def _monotone_runs(pairs: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """Split ``(page, value)`` at every step backwards."""
    runs: list[list[tuple[int, int]]] = [[]]
    for item in pairs:
        if runs[-1] and item[1] < runs[-1][-1][1]:
            runs.append([])
        runs[-1].append(item)
    return [r for r in runs if r]


MAX_GAP_SPAN = 5000
"""Above this, a gap is reported whole rather than checked value by value.
Nobody has that many pages hiding in the other column of a two-up scan."""


def _gap_ranges(a: int, b: int, observed: frozenset[int]) -> list[tuple[int, int]]:
    """The runs of numbers strictly between *a* and *b* that nobody has seen.

    A two-up scan puts the odd numbers down the left of the image and the even
    ones down the right, so read as one sequence the left-hand column appears
    to be missing every second page. It is not missing; it is six inches to the
    right. A number printed anywhere in this document is not a withheld page,
    so it never becomes a gap.
    """
    if b - a - 1 > MAX_GAP_SPAN:
        return [(a + 1, b - 1)]
    runs: list[list[int]] = []
    for value in range(a + 1, b):
        if value in observed:
            continue
        if runs and value == runs[-1][1] + 1:
            runs[-1][1] = value
        else:
            runs.append([value, value])
    return [(lo, hi) for lo, hi in runs]


def _position_prior(cluster: _Cluster) -> float:
    """A small nudge for the places stamps actually land.

    Bottom-right is where most productions put the number, bottom-centre and
    top-right next. It is worth a tiebreak and no more - position *stability*
    is the evidence; position *preference* is a habit.
    """
    cx, cy = cluster.centroid
    bottom, top = cy > 0.5, cy <= 0.5
    right, centre = cx > 0.66, 0.33 <= cx <= 0.66
    if bottom and right:
        return 0.05
    if (bottom and centre) or (top and right):
        return 0.03
    return 0.0


def _build(
    cluster: _Cluster,
    pairs: list[tuple[int, int]],
    total_pages: int,
    *,
    monotone: float,
    confirmed: bool,
    notes: list[str],
    observed: frozenset[int] = frozenset(),
) -> BatesSeries:
    width = cluster.width
    prefix, suffix = cluster.prefix, cluster.suffix

    def fmt(value: int) -> str:
        return f"{prefix}{value:0{width}d}{suffix}"

    gaps: list[tuple[str, str]] = []
    for (_, a), (_, b) in pairwise(pairs):
        if b - a > 1:
            gaps.extend((fmt(lo), fmt(hi)) for lo, hi in _gap_ranges(a, b, observed))

    coverage = len(pairs) / total_pages if total_pages else 0.0
    tightness = max(0.0, 1.0 - cluster.spread * 8.0)
    confidence = (
        0.40 * min(1.0, coverage)
        + 0.25 * monotone
        + 0.25 * tightness
        + (0.10 if prefix else 0.0)
        + _position_prior(cluster)
    )
    if not confirmed:
        confidence = min(confidence, 0.35)
    return BatesSeries(
        prefix=prefix,
        width=width,
        first=fmt(pairs[0][1]),
        last=fmt(pairs[-1][1]),
        coverage=round(coverage, 4),
        gaps=gaps,
        page_map={page: fmt(value) for page, value in pairs},
        confidence=round(min(0.99, confidence), 3),
        suffix=suffix,
        confirmed=confirmed,
        notes=notes,
    )


def _looks_like_page_numbers(pairs: list[tuple[int, int]], cluster: _Cluster, total: int) -> bool:
    """Is this the printer's page counter wearing a stamp's clothes?

    Only bare numbers are suspect. ``ACME000001``-``ACME000012`` across twelve
    pages is a small production, not a page count; ``0001``-``0012`` across
    twelve pages is a page count, every time.
    """
    if cluster.prefix:
        return False
    values = [v for _, v in pairs]
    return min(values) <= 2 and max(values) <= total + 1


def detect(pages: Sequence[PageWords]) -> list[BatesSeries]:
    """Find every control-number series stamped on *pages*.

    Returns one :class:`BatesSeries` per production, most confident first.
    Several is a normal answer: releases get combined, and a page that was
    stamped by two agencies carries both stamps. They are never merged, because
    two interleaved productions merged into one sequence produce gaps that were
    never missing and hide the ones that were.
    """
    total = len(pages)
    if total == 0:
        return []

    cands = [c for page in pages for c in _candidates(page)]
    if not cands:
        return []

    clusters = _merge_adjacent(_cluster(cands))
    clusters = _drop_dominated(_drop_variable_width(clusters))

    # Every number this document printed, keyed by the shape of the stamp it
    # belongs to. A gap is a number nobody saw; this is the record of what was
    # seen, wherever on the page it happened to be.
    observed: dict[tuple[str, int, str], set[int]] = defaultdict(set)
    for c in cands:
        observed[(c.prefix, len(c.digits), c.suffix)].add(c.value)

    out: list[BatesSeries] = []
    for cluster in clusters:
        seq = _one_per_page(cluster)
        if not seq:
            continue
        seen = frozenset(observed[(cluster.prefix, cluster.width, cluster.suffix)])
        notes: list[str] = []
        values = _repair(seq, notes)
        pairs = [(c.page, v) for c, v in zip(seq, values, strict=True)]

        deltas = [b - a for (_, a), (_, b) in pairwise(pairs)]
        monotone = sum(1 for d in deltas if d >= 0) / len(deltas) if deltas else 1.0
        coverage = len(pairs) / total

        if coverage < MIN_COVERAGE:
            continue
        if deltas and not any(d > 0 for d in deltas):
            # It never counts up. Every page of a release carries numbers that
            # hold still - the form number, the docket, the office's fax - and
            # they satisfy every test here except the one that matters: a
            # control number is a counter.
            continue
        if _looks_like_page_numbers(pairs, cluster, total):
            continue

        if total < MIN_PAGES or len(pairs) < MIN_PAGES:
            # Not enough pages to verify anything. Say so rather than pretend.
            out.append(
                _build(
                    cluster,
                    pairs,
                    total,
                    monotone=monotone,
                    confirmed=False,
                    notes=[*notes, "unverified candidate: too few pages to establish a sequence"],
                    observed=seen,
                )
            )
            continue

        if monotone >= MIN_MONOTONE:
            out.append(
                _build(
                    cluster,
                    pairs,
                    total,
                    monotone=monotone,
                    confirmed=True,
                    notes=notes,
                    observed=seen,
                )
            )
            continue

        # It goes backwards. That is not an error - it is a second production
        # stapled to the first, or a scan someone re-ordered - so the runs are
        # reported separately instead of being flattened into one impossible
        # sequence full of invented gaps.
        runs = [r for r in _monotone_runs(pairs) if len(r) >= MIN_PAGES]
        if not runs or sum(len(r) for r in runs) / total < MIN_COVERAGE:
            continue
        for run in runs:
            out.append(
                _build(
                    cluster,
                    run,
                    total,
                    monotone=1.0,
                    confirmed=True,
                    observed=seen,
                    notes=[
                        *notes,
                        "one of several runs at this position: the numbering steps "
                        "backwards, which means another production or a re-ordered scan",
                    ],
                )
            )

    out.sort(key=lambda s: (-s.confidence, s.first))
    if total < MIN_PAGES and out:
        # Nothing here can be verified, so offering a list of guesses would
        # only launder one of them into a fact. Return the likeliest, labelled
        # as unconfirmed, and let the operator decide.
        return out[:1]
    return out
