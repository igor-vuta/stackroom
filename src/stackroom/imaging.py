"""Small image primitives, shared by the modules that look at pixels.

Three modules need to ask the same questions of a rendered page - is this patch
one flat colour, where are the solid blocks, how much ink is there - so the
answers live here rather than being reimplemented three times with three
different thresholds.

Everything here takes and returns plain ``numpy`` arrays. Nothing here opens a
file, shells out, or knows what a PDF is, so it can be exercised on a
hand-built 6x6 array in a test.

Two deliberate omissions:

* **No OpenCV.** It is a 60 MB wheel for the four functions below, and this
  project has to stay installable on a laptop with a bad connection. The
  connected-component labeller is therefore written out in full - see
  :func:`connected_components` for the algorithm and the measured cost.
* **No SciPy.** ``scipy.ndimage.label`` would do the same job, but SciPy is a
  heavier dependency than the whole rest of the ingest pipeline combined.

Conventions
-----------
Greyscale arrays are ``uint8``, 0 = black, 255 = white, indexed ``[row, col]``
with the origin at the top-left, matching PIL and the page image on disk.
Masks are ``bool`` arrays where ``True`` means *ink* (dark), because every
caller here is looking for marks on paper rather than paper itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = [
    "Component",
    "binarise",
    "connected_components",
    "is_uniform",
    "otsu_threshold",
    "to_gray",
    "uniformity",
]


# --------------------------------------------------------------------------
# greyscale
# --------------------------------------------------------------------------

# Rec. 601 luma weights. Not Rec. 709: these documents are photocopies and
# scans, and 601 is what every scanner, fax and JPEG encoder in that chain
# already used, so it is the transform that best matches "how dark did the
# machine that made this think each pixel was".
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def to_gray(img: Any) -> np.ndarray:
    """Return *img* as a 2-D ``uint8`` greyscale array.

    Accepts a PIL image in any mode, or a numpy array that is already 2-D, or
    3-D with 1, 3 or 4 channels. An alpha channel is dropped rather than
    composited: a page rendering has no meaningful transparency, and silently
    compositing onto black would turn every transparent margin into a
    redaction-shaped block.
    """
    arr = np.asarray(img.convert("L") if hasattr(img, "convert") else img)

    if arr.ndim == 3:
        if arr.shape[2] == 1:
            arr = arr[:, :, 0]
        else:
            rgb = arr[:, :, :3].astype(np.float32)
            arr = (rgb @ _LUMA).round()
    elif arr.ndim != 2:
        raise ValueError(f"expected a 2-D or 3-D image, got shape {arr.shape}")

    if arr.dtype == np.bool_:
        arr = arr.astype(np.uint8) * 255
    elif arr.dtype != np.uint8:
        # Float images are conventionally 0..1; integer images are already
        # 0..255 and only need clipping.
        if np.issubdtype(arr.dtype, np.floating) and float(arr.max(initial=0.0)) <= 1.0:
            arr = arr * 255.0
        arr = np.clip(arr, 0, 255).astype(np.uint8)

    return np.ascontiguousarray(arr)


def otsu_threshold(gray: np.ndarray) -> int:
    """Otsu's threshold: the cut that minimises within-class variance.

    Returned as the *highest* value still counted as ink, so callers use
    ``gray <= t``. On a page that is genuinely all one colour Otsu has nothing
    to separate; we return 0 there, which yields an empty ink mask rather than
    calling half the paper ink.
    """
    gray = to_gray(gray)
    hist = np.bincount(gray.reshape(-1), minlength=256).astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 0

    levels = np.arange(256, dtype=np.float64)
    w0 = np.cumsum(hist)
    w1 = total - w0
    sum_all = float(levels @ hist)
    m0 = np.cumsum(levels * hist)
    m1 = sum_all - m0

    with np.errstate(invalid="ignore", divide="ignore"):
        mean0 = np.where(w0 > 0, m0 / np.maximum(w0, 1), 0.0)
        mean1 = np.where(w1 > 0, m1 / np.maximum(w1, 1), 0.0)
    between = w0 * w1 * (mean0 - mean1) ** 2
    between[w0 == 0] = 0.0
    between[w1 == 0] = 0.0

    if not np.any(between > 0):
        return 0
    return int(np.argmax(between))


def binarise(gray: np.ndarray, threshold: int | None = None) -> np.ndarray:
    """Ink mask: ``True`` where the page is darker than *threshold*.

    With ``threshold=None`` the cut is chosen by :func:`otsu_threshold`, which
    is right when you want "whatever counts as ink on this particular scan".
    Redaction hunting passes an explicit, much darker threshold instead - see
    ``ingest/redaction.py`` - because a redaction is not merely darker than the
    paper, it is *black*, and Otsu on a text page happily calls grey toner ink.
    """
    gray = to_gray(gray)
    t = otsu_threshold(gray) if threshold is None else int(threshold)
    return gray <= t


# --------------------------------------------------------------------------
# connected components
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Component:
    """One connected blob of ink, described by its bounding box.

    ``area`` is the number of pixels actually set, which is what separates a
    filled block from a hollow frame: see :attr:`solidity`.
    """

    label: int
    """Value this component carries in the label image; 1-based."""

    x: int
    y: int
    w: int
    h: int
    area: int

    @property
    def solidity(self) -> float:
        """Set pixels over bounding-box pixels. 1.0 for a filled rectangle.

        This single number is what tells a redaction box (≈1.0) from the black
        border of a badly aligned scan (measured 0.02 on our synthetic page),
        which otherwise passes every size and darkness test there is.
        """
        denom = self.w * self.h
        return self.area / denom if denom else 0.0

    @property
    def aspect(self) -> float:
        """Width over height. Wider than tall is > 1."""
        return self.w / self.h if self.h else 0.0

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        """``(x0, y0, x1, y1)``, x1/y1 exclusive."""
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    def crop(self, image: np.ndarray, inset: int = 0) -> np.ndarray:
        """The component's bounding box cut out of *image*.

        *inset* trims that many pixels off every edge, which is how callers
        avoid measuring the antialiased rim of a block and concluding that a
        perfectly flat rectangle has texture. Returns an empty array if the
        inset eats the whole component.
        """
        x0, y0, x1, y1 = self.bbox
        x0, y0 = x0 + inset, y0 + inset
        x1, y1 = x1 - inset, y1 - inset
        if x1 <= x0 or y1 <= y0:
            return image[:0, :0]
        return image[y0:y1, x0:x1]


def connected_components(
    mask: np.ndarray, *, connectivity: int = 8
) -> tuple[np.ndarray, list[Component]]:
    """Label the ``True`` regions of *mask*.

    Returns ``(labels, components)`` where ``labels`` is an ``int32`` array the
    same shape as *mask* holding 0 for background and 1..n for components, and
    ``components`` is that list in raster order of each blob's first pixel -
    top to bottom, left to right - so the output is deterministic, which the
    build depends on.

    Algorithm
    ---------
    Run-length encode each row, connect runs that touch a run on the row above,
    then resolve those links by label propagation with pointer jumping. Every
    step is a whole-array numpy operation; there is no per-pixel Python.

    1. Runs come from a single ``np.diff`` over the row-padded mask.
    2. Row-to-row adjacency uses two ``searchsorted`` calls. Runs are keyed by
       ``row * (width + 2) + column``, which is globally sorted, so a search
       for a key one row back cannot stray outside that row - the padding of 2
       guarantees the neighbouring row's keys can never reach into the next.
       That removes the per-row Python loop the obvious implementation needs.
    3. Runs are joined by Shiloach-Vishkin hooking: each round, every tree root
       adopts the smallest root any of its members is linked to, and then the
       forest is flattened by pointer jumping. Labels only ever decrease and
       are bounded below, so it terminates; a round that changes nothing means
       every edge already joins equal labels, which is exactly "one label per
       component". Each component ends up labelled by its lowest run index, so
       the output order is a property of the mask and not of the schedule.

    Cost, measured on this container for a 1275x1650 page at 150 dpi:

    ======================================  =========  ========
    page                                    components  time
    ======================================  =========  ========
    typed text, box, photo, scan border      1,486      **24 ms**
    the same page with heavy scanner grain   1,607      23 ms
    two redaction boxes on blank paper           2      12 ms
    ======================================  =========  ========

    The budget is 150 ms. The pathological input is salt-and-pepper noise -
    half a million runs, e.g. ``synth.noise_page()`` - which takes about
    290 ms; the hooking still converges in four rounds there, where plain label
    propagation needs about fifty and 2.7 s. Peak extra memory is a handful of
    ``int64`` arrays the length of the run list.

    On NumPy older than 1.25, ``np.minimum.at`` has no fast path and that
    pathological case gets several times slower; ordinary pages are unaffected
    because their run lists are twenty times shorter.

    ``connectivity`` is 8 by default so that diagonally touching strokes of a
    glyph count as one letter; pass 4 when you want blocks separated by a
    single-pixel diagonal to stay separate.
    """
    if connectivity not in (4, 8):
        raise ValueError("connectivity must be 4 or 8")

    mask = np.ascontiguousarray(np.asarray(mask, dtype=bool))
    if mask.ndim != 2:
        raise ValueError(f"expected a 2-D mask, got shape {mask.shape}")
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    if h == 0 or w == 0 or not mask.any():
        return labels, []

    # -- 1. run-length encode -------------------------------------------
    # Pad one column of False on each side so every run has a start and an end
    # edge inside the diff, including runs that touch the page margin.
    padded = np.zeros((h, w + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    edges = np.diff(padded, axis=1)
    starts = np.argwhere(edges == 1)  # C order: sorted by row, then column
    ends = np.argwhere(edges == -1)
    run_row = starts[:, 0].astype(np.int64)
    run_start = starts[:, 1].astype(np.int64)  # inclusive
    run_end = ends[:, 1].astype(np.int64)  # exclusive
    run_len = run_end - run_start
    n = run_row.size

    # -- 2. adjacency with the row above --------------------------------
    adj = 1 if connectivity == 8 else 0
    stride = w + 2  # > w + adj, which is what keeps the searches in one row
    key_start = run_row * stride + run_start
    key_end = run_row * stride + run_end
    # Runs on the previous row overlapping run i (within `adj` columns):
    #   run_end[j] > run_start[i] - adj  and  run_start[j] < run_end[i] + adj
    lo = np.searchsorted(key_end, key_start - stride - adj, side="right")
    hi = np.searchsorted(key_start, key_end - stride + adj, side="left")
    counts = np.maximum(hi - lo, 0)
    total = int(counts.sum())

    lab = np.arange(n, dtype=np.int64)
    if total:
        src = np.repeat(np.arange(n, dtype=np.int64), counts)
        offset = np.arange(total, dtype=np.int64) - np.repeat(
            np.cumsum(counts) - counts, counts
        )
        dst = np.repeat(lo, counts) + offset
        # Both directions, so that "the larger root always learns about the
        # smaller one" holds for every edge and a round that changes nothing
        # really means there is nothing left to change.
        a = np.concatenate([src, dst])
        b = np.concatenate([dst, src])

        # -- 3. hook roots together, then flatten (Shiloach-Vishkin) -----
        while True:
            root = lab.copy()
            # Hook: every root takes the smallest root any of its members is
            # joined to. Writing at `lab[a]` rather than at `a` is the whole
            # trick - it merges *trees* per round instead of moving each label
            # one edge along, which is the difference between ~4 rounds and
            # ~50 on a page of scanner noise.
            np.minimum.at(root, lab[a], lab[b])
            nxt = np.minimum(lab, np.minimum(root, root[lab]))
            while True:  # shortcut until every node points straight at a root
                jumped = nxt[nxt]
                if np.array_equal(jumped, nxt):
                    break
                nxt = jumped
            if np.array_equal(nxt, lab):
                break
            lab = nxt

    # -- 4. dense, deterministic label ids -------------------------------
    # Each component's representative is the index of its first run, so sorting
    # the representatives puts components in raster order.
    _, inverse = np.unique(lab, return_inverse=True)
    run_label = (inverse.reshape(-1) + 1).astype(np.int32)
    count = int(run_label.max())

    # The True pixels of the mask in row-major order are exactly the runs
    # concatenated in order, so one repeat fills the label image.
    labels.reshape(-1)[mask.reshape(-1)] = np.repeat(run_label, run_len)

    # -- 5. per-component geometry, from the runs, not the pixels --------
    order = np.argsort(run_label, kind="stable")
    sorted_label = run_label[order]
    seg = np.searchsorted(sorted_label, np.arange(1, count + 1), side="left")
    y0 = np.minimum.reduceat(run_row[order], seg)
    y1 = np.maximum.reduceat(run_row[order], seg) + 1
    x0 = np.minimum.reduceat(run_start[order], seg)
    x1 = np.maximum.reduceat(run_end[order], seg)
    area = np.bincount(run_label, weights=run_len, minlength=count + 1)[1:]

    components = [
        Component(
            label=i + 1,
            x=int(x0[i]),
            y=int(y0[i]),
            w=int(x1[i] - x0[i]),
            h=int(y1[i] - y0[i]),
            area=int(area[i]),
        )
        for i in range(count)
    ]
    return labels, components


# --------------------------------------------------------------------------
# uniformity
# --------------------------------------------------------------------------


def uniformity(patch: np.ndarray) -> tuple[float, float, float]:
    """Return ``(std, p95 - p5, mean)`` for *patch*.

    Two spread numbers, because either one alone is easy to fool. Standard
    deviation is dominated by the bulk of the pixels and shrugs off a thin
    bright line; the 5th-to-95th percentile span is dominated by the extremes
    and shrugs off a low-amplitude gradient. A true solid block is flat on
    both. Percentiles rather than min/max so that one stuck sensor pixel or one
    JPEG ring does not decide the answer.

    An **empty patch reports infinite spread**. It is not uniform, because we
    have not seen anything: for a redaction candidate that means "reject", and
    for hidden text it means "raster confirmation was unavailable, fall back to
    the content stream". Both are the safe direction.
    """
    arr = np.asarray(patch)
    if arr.size == 0:
        return (float("inf"), float("inf"), 0.0)
    flat = arr.reshape(-1).astype(np.float64)
    p5, p95 = np.percentile(flat, (5, 95))
    return (float(flat.std()), float(p95 - p5), float(flat.mean()))


def is_uniform(
    patch: np.ndarray, *, max_std: float = 12.0, max_spread: float = 25.0
) -> bool:
    """Is *patch* flat enough to be one solid block of colour?

    Defaults measured on synthetic pages at 150 dpi: a true redaction box reads
    std 0.0 / spread 0, a JPEG-ish dark photograph reads std 14.4 / spread 50,
    and toner-grained black on a photocopy sits around std 8. The cuts at 12
    and 25 sit in the gap, nearer the photograph than the box, because the
    grain of a real scan is the thing we must tolerate.

    A patch whose whole range is under *max_std* is answered from that range
    alone, without computing either statistic. It is not an approximation:
    standard deviation can never exceed half the range (Popoviciu), and the
    p95-p5 span can never exceed the range at all, so such a patch is uniform
    on both counts whatever the numbers work out to. This matters because
    ``ingest/redaction.py`` now asks this of every character under every box
    rather than once per box, and on the ordinary case - a solid rectangle,
    every pixel identical - the two percentiles cost about ten times what the
    range does. Measured over a page-sized box with 3,360 characters under it:
    337 ms before, 47 ms after.
    """
    arr = np.asarray(patch)
    if arr.size == 0:
        return False  # nothing seen is not the same as nothing there
    lo, hi = arr.min(), arr.max()
    if float(hi) - float(lo) < max_std:
        return True
    std, spread, _ = uniformity(arr)
    return std < max_std and spread < max_spread
