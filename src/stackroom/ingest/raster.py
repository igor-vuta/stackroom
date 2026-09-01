"""Rendering PDF pages to images.

Poppler does the rasterising and we invoke it as a subprocess. That is a
licensing decision before it is an engineering one: poppler is GPL, Stackroom
is MIT, and running a program is not linking against it. It is also why this
module talks to ``pdftoppm`` in pixels and PNG files rather than in objects.

What this module is careful about
---------------------------------

*Batching.* ``pdftoppm -f/-l`` renders a run of pages in one process. Measured
here (2 cores, poppler 24.02): 12 near-empty letter pages take 772 ms batched
against 1027 ms at one process per page - 15.5 vs 11.7 pages/s, a 1.33x win.
Twenty text-heavy pages take 5.94 s against 6.21 s - 3.4 vs 3.2 pages/s, 1.05x.
So spawn cost is a flat ~20 ms per page and it only dominates when the page
itself is nearly free. Batching stays the default because it is free, but
"process spawn dominates otherwise" is only true of thin documents.

*Determinism.* Guarantee 6 in ARCHITECTURE.md says the same input bytes must
give the same output bytes. ``pdftoppm`` is already deterministic. Pillow is
not, quite: libavif divides the frame among its worker threads, so a 1600 px
page encodes to 105,560 bytes on two threads and 105,279 on one. Every encoder
option that could vary is therefore pinned, thread count included, and the
decoded PNG is rebuilt from raw pixels so no PNG metadata chunk can survive
into the output.

*Not lying about pixels.* A letter page at 150 dpi is 1275 px wide, so a
"1600 px" variant made from it would be an upscale: a bigger file carrying no
more detail. ``dpi`` is therefore a floor. The renderer raises it until the
raster covers the widest requested variant, then downscales.
"""

from __future__ import annotations

import base64
import io
import logging
import math
import re
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, features

from ..model import Box

__all__ = [
    "AVIF_AVAILABLE",
    "PageGeometry",
    "RenderError",
    "RenderSpec",
    "RenderedPage",
    "Variant",
    "colourfulness",
    "fit_dpi",
    "grain_level",
    "page_geometry",
    "render_page_crop",
    "render_pdf",
    "supported_formats",
]

_LOG = logging.getLogger(__name__)

POINTS_PER_INCH = 72.0

_PAGE_SIZE_RE = re.compile(r"^Page\s+(\d+)\s+size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts")
_PAGE_ROT_RE = re.compile(r"^Page\s+(\d+)\s+rot:\s+(-?\d+)")
_SINGLE_SIZE_RE = re.compile(r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts")
_SINGLE_ROT_RE = re.compile(r"^Page rot:\s+(-?\d+)")
_PAGES_RE = re.compile(r"^Pages:\s+(\d+)")
_OUTPUT_RE = re.compile(r"-(\d+)\.png$")


def _probe_avif() -> bool:
    """Ask Pillow whether it can write AVIF, tolerating older Pillows.

    ``features.check`` raises rather than returning False for a feature name it
    has never heard of, and a missing codec has to degrade the build, not stop
    it.
    """
    try:
        return bool(features.check("avif"))
    except (ValueError, AttributeError):  # pragma: no cover - depends on the Pillow build
        return False


AVIF_AVAILABLE: bool = _probe_avif()
if not AVIF_AVAILABLE:  # pragma: no cover - depends on the Pillow build
    _LOG.warning(
        "Pillow has no AVIF encoder, so pages will be published as WebP only; "
        "expect roughly 30%% larger images. Install a Pillow built against "
        "libavif to restore it."
    )


class RenderError(RuntimeError):
    """A page could not be rasterised.

    ``render_pdf`` catches this per page and records it; ``render_page_crop``
    lets it out, because a caller that asked for one specific region has no
    sensible fallback.
    """


# --------------------------------------------------------------------------
# specs and results
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Variant:
    """One encoded image on disk."""

    path: Path
    format: str
    width: int
    height: int
    bytes: int


@dataclass(slots=True)
class RenderSpec:
    """Everything that decides what comes out of the renderer.

    Two machines given the same spec and the same PDF must write the same
    bytes, so every field here that reaches an encoder is pinned rather than
    left to a library default that may move under us.
    """

    dpi: int = 150
    """*Minimum* rasterisation resolution. Raised per page when a requested
    width needs more pixels, lowered when the page would blow the budget."""

    widths: tuple[int, ...] = (1600, 900)
    thumb_width: int = 240
    formats: tuple[str, ...] = ("avif", "webp")
    """In preference order: the first the environment supports is the one the
    page will link first."""

    quality: dict[str, int] = field(default_factory=lambda: {"avif": 50, "webp": 78})

    grayscale_threshold: float = 0.02
    """Colourfulness (see :func:`colourfulness`) below which the page is
    encoded as a single channel. 0.02 is about five levels of chroma out of
    255: below a scanner's own colour noise, above nothing."""

    grain_threshold: float = 3.0
    """Grain level (see :func:`grain_level`) at or above which the page is
    denoised before encoding. A ``pdftoppm`` render of a born-digital page
    measures exactly 0.0; a visibly grainy photocopy measures 10 to 27."""

    max_pixels: int = 40_000_000
    """Hard cap on the raster. Forty megapixels of RGB is 120 MB resident
    before anything is encoded, and poster-sized pages exist that would
    cheerfully ask for five hundred."""

    avif_speed: int = 7
    """libavif effort, 0 slowest to 10 fastest.

    Seven, not six. Measured over all 48 AVIF variants of the demo collection,
    the runs interleaved so a busy machine costs each speed alike: speed 6
    takes 32.24 s and writes 1,890,446 bytes, speed 7 takes 19.02 s and writes
    1,901,789 - 41% less encoding time for 0.6% more bytes. On two of the three
    sample pages speed 7 is strictly better than 6, smaller as well as faster:

    ======  ============  ============  ==========  ==========
    speed   born-digital  (bytes)       scan p1     scan p3
    ======  ============  ============  ==========  ==========
    4       5189 ms       41,130        7193 ms     7597 ms
    6       831 ms        53,255        1258 ms     1082 ms
    7       553 ms        53,879        711 ms      647 ms
    8       263 ms        90,931        343 ms      369 ms
    9       72 ms         110,670       75 ms       78 ms
    ======  ============  ============  ==========  ==========

    Eight is the wrong side of the knee: 12.28 s over the collection but
    2,330,244 bytes, +23% on every page image in the archive - which is
    downloaded far more often than it is built.

    The older figures this docstring used to quote (105 KB at speed 6, 108 KB
    at speed 8) were taken on an RGB page and do not reproduce here at all:
    these pages are grayscale and encode as 4:0:0, where the 6-to-8 step is
    53 KB to 91 KB.

    Both speeds are byte-for-byte deterministic over repeated encodes with
    ``encoder_threads = 1``, so guarantee 6 is unaffected. See
    docs/PERFORMANCE.md sections 4.2 and 8.2 for the full table and how to
    re-measure it."""

    webp_method: int = 6
    """libwebp effort, 0 to 6. Same page: method 0 = 212 KB in 65 ms, method 4
    = 160 KB in 202 ms, method 6 = 154 KB in 339 ms."""

    encoder_threads: int = 1
    """Pinned to one because libavif's output bytes depend on it. Parallelism
    belongs at the page level, where it is reproducible."""

    max_batch_pages: int = 32
    max_batch_megapixels: float = 400.0
    """A batch writes every page to a temp directory before we read any of
    them, so batch size is really a disk budget."""

    subprocess_timeout: float = 300.0

    def encoded_formats(self) -> tuple[str, ...]:
        """The requested formats this environment can actually write."""
        return tuple(f for f in self.formats if f in supported_formats())


@dataclass(slots=True)
class RenderedPage:
    """What came out for one page.

    A page that failed still gets one of these. Losing one corrupt page out of
    5,000 must not lose the other 4,999, and the site has to be able to say
    *this page did not render* rather than quietly skipping it.
    """

    number: int
    width_px: int
    height_px: int
    variants: list[Variant] = field(default_factory=list)
    """Full-size renderings, one per (width, format) pair."""

    thumb: Variant | None = None
    """The preferred thumbnail. ``thumbs`` has the rest."""

    thumbs: list[Variant] = field(default_factory=list)

    placeholder: str = ""
    """A 24 px-wide WebP of this page as a ``data:`` URI. See
    :func:`_placeholder`; empty when it could not be made."""

    is_grayscale: bool = False
    dpi: int = 0
    """The resolution actually used, which is not necessarily ``spec.dpi``."""

    denoised: bool = False
    colourfulness: float = 0.0
    grain: float = 0.0
    failed: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed

    def variant(self, width: int, fmt: str) -> Variant | None:
        """The full-size variant filed under a given width slot and format."""
        suffix = f"@{width}"
        for v in self.variants:
            if v.format == fmt and v.path.stem.endswith(suffix):
                return v
        return None


@dataclass(slots=True, frozen=True)
class PageGeometry:
    """A page's size as poppler will rasterise it."""

    number: int
    width_pt: float
    height_pt: float
    rotation: int
    """``/Rotate``, normalised to 0, 90, 180 or 270."""

    @property
    def rendered_pt(self) -> tuple[float, float]:
        """Size in points *after* ``/Rotate``, which is the frame every pixel
        coordinate in this module lives in."""
        if self.rotation % 180 == 90:
            return self.height_pt, self.width_pt
        return self.width_pt, self.height_pt

    def pixel_size(self, dpi: float) -> tuple[int, int]:
        """The exact dimensions ``pdftoppm -r dpi`` will produce.

        Poppler rounds *up*: A4 at 72 dpi is 596x842, not 595x842. Rounding it
        the other way puts every crop box a pixel out at the far edge.
        """
        w_pt, h_pt = self.rendered_pt
        return (
            max(1, math.ceil(w_pt * dpi / POINTS_PER_INCH)),
            max(1, math.ceil(h_pt * dpi / POINTS_PER_INCH)),
        )


def supported_formats() -> tuple[str, ...]:
    """Formats this process can encode, in the order we prefer them."""
    return ("avif", "webp") if AVIF_AVAILABLE else ("webp",)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


def _run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:  # pragma: no cover - depends on the host
        raise RenderError(
            f"{argv[0]} not found. Stackroom rasterises with poppler-utils; install it "
            "(Debian/Ubuntu: apt install poppler-utils, macOS: brew install poppler)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"{argv[0]} timed out after {timeout:g}s") from exc


def _parse_pdfinfo(text: str) -> list[PageGeometry]:
    """Read ``pdfinfo`` output into per-page geometry.

    ``pdfinfo -f/-l`` prints a size and rotation line per page, but collapses to
    a single ``Page size:`` when every page agrees, so both shapes are parsed
    and the collapsed one is used as the default for any page not listed.
    """
    sizes: dict[int, tuple[float, float]] = {}
    rots: dict[int, int] = {}
    count = 0
    fallback_size: tuple[float, float] | None = None
    fallback_rot = 0
    for line in text.splitlines():
        if m := _PAGE_SIZE_RE.match(line):
            sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
        elif m := _PAGE_ROT_RE.match(line):
            rots[int(m.group(1))] = int(m.group(2))
        elif m := _SINGLE_SIZE_RE.match(line):
            fallback_size = (float(m.group(1)), float(m.group(2)))
        elif m := _SINGLE_ROT_RE.match(line):
            fallback_rot = int(m.group(1))
        elif m := _PAGES_RE.match(line):
            count = int(m.group(1))
    if count <= 0:
        raise RenderError("pdfinfo reported no pages; the file is probably not a PDF")

    default = fallback_size or (612.0, 792.0)
    return [
        PageGeometry(n, *sizes.get(n, default), rots.get(n, fallback_rot) % 360)
        for n in range(1, count + 1)
    ]


@lru_cache(maxsize=64)
def _geometry_cached(path: str, _mtime_ns: int, _size: int, timeout: float) -> tuple[PageGeometry, ...]:
    # Keyed on mtime and size as well as path, so a rewritten file is never
    # served stale. The cache exists for redaction.py, which calls
    # render_page_crop once per candidate box and would otherwise pay a ~15 ms
    # pdfinfo round trip every time.
    proc = _run(["pdfinfo", "-f", "1", "-l", "2147483647", path], timeout)
    if proc.returncode != 0:
        raise RenderError(
            f"pdfinfo failed on {path}: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
        )
    return tuple(_parse_pdfinfo(proc.stdout.decode("utf-8", "replace")))


def page_geometry(pdf: Path, timeout: float = 60.0) -> list[PageGeometry]:
    """Per-page size in points and ``/Rotate``, in page order."""
    path = Path(pdf).resolve()
    st = path.stat()
    return list(_geometry_cached(str(path), st.st_mtime_ns, st.st_size, timeout))


# --------------------------------------------------------------------------
# image analysis
# --------------------------------------------------------------------------


def colourfulness(img: Image.Image) -> float:
    """How much real colour the page carries, 0.0 (neutral) to 1.0.

    Checking ``img.mode`` would answer the wrong question: ``pdftoppm`` emits
    RGB for everything, black type on white paper included, and a scanner puts
    a few levels of chroma noise on every pixel of a grayscale original.

    So: shrink with box averaging, which is where most of the sensor noise
    dies; take the larger of |R-G| and |G-B| per pixel; median-filter away
    whatever speckle survived; report the 99.9th percentile. The percentile is
    high on purpose - a small red stamp in a corner is exactly the colour an
    archive must not throw away.

    Measured: born-digital render 0.000, page with a colour block 0.698, a
    60x30 px red stamp 0.126, grayscale scan with sigma-6 chroma noise 0.012.
    """
    if img.mode in ("L", "1", "I", "F"):
        return 0.0
    rgb = img.convert("RGB")
    width = min(256, rgb.width)
    height = max(1, round(width * rgb.height / rgb.width))
    small = rgb.resize((width, height), Image.Resampling.BOX)
    a = np.asarray(small, dtype=np.int16)
    chroma = np.maximum(
        np.abs(a[:, :, 0] - a[:, :, 1]), np.abs(a[:, :, 1] - a[:, :, 2])
    ).astype(np.uint8)
    smoothed = np.asarray(Image.fromarray(chroma, "L").filter(ImageFilter.MedianFilter(3)))
    return float(np.percentile(smoothed, 99.9)) / 255.0


def _laplacian_median(tile: np.ndarray) -> float:
    """Median |3x3 Laplacian| over a tile: an estimate of the noise floor.

    The *median* is the point. Text edges ring the Laplacian too, but they are
    a minority of the pixels, so the median reports the background - which on a
    clean render is perfectly flat and on a grainy scan is not.
    """
    if tile.shape[0] < 3 or tile.shape[1] < 3:
        return 0.0
    a = tile.astype(np.float32)
    lap = (
        a[:-2, :-2]
        + a[:-2, 2:]
        + a[2:, :-2]
        + a[2:, 2:]
        - 2 * (a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:])
        + 4 * a[1:-1, 1:-1]
    )
    return float(np.median(np.abs(lap)))


def grain_level(img: Image.Image, tile: int = 512) -> float:
    """High-frequency energy in the flat areas: the scan-grain detector.

    Sampled from four tiles rather than the whole page, because grain is
    stationary and measuring 40 megapixels would cost 160 MB of float32. Four
    tiles rather than one, because a page can be half black redaction and the
    median of four ignores the tile that landed on it.

    Measured on a 1600 px page: born-digital render 0.0; ``typed_page(grain=
    0.05)`` 1.0; ``0.12`` 3.0; ``0.25`` 11.0; ``0.4`` 27.0.
    """
    gray = np.asarray(img.convert("L"))
    h, w = gray.shape
    ty, tx = min(tile, h), min(tile, w)
    ys = [0, max(0, (h - ty) // 3), max(0, 2 * (h - ty) // 3), max(0, h - ty)]
    xs = [0, max(0, (w - tx) // 3), max(0, 2 * (w - tx) // 3), max(0, w - tx)]
    scores = [_laplacian_median(gray[y : y + ty, x : x + tx]) for y, x in zip(ys, xs, strict=True)]
    return float(np.median(scores))


def _denoise(img: Image.Image) -> Image.Image:
    """A 3x3 median: the cheapest filter that kills grain and keeps edges.

    A Gaussian blur would cost fewer cycles and more legibility. Anything wider
    than 3x3 starts eating the counters of 8 pt type, and this image is
    evidence before it is a picture.
    """
    return img.filter(ImageFilter.MedianFilter(3))


def _strip(img: Image.Image) -> Image.Image:
    """Rebuild from raw pixels, dropping every scrap of metadata.

    ``Image.open`` carries an ``info`` dict - dpi, gamma, ICC profile - that
    ``save`` will happily re-embed, and ``resize`` copies it forward. Rebuilding
    costs one memcpy and makes "no metadata" true by construction instead of
    true as long as somebody remembers the right keyword for three encoders.
    """
    img.load()
    return Image.frombytes(img.mode, img.size, img.tobytes())


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------


def _encode(img: Image.Image, path: Path, fmt: str, spec: RenderSpec, gray: bool) -> Variant:
    quality = spec.quality.get(fmt, 75)
    if fmt == "avif":
        img.save(
            path,
            format="AVIF",
            quality=quality,
            speed=spec.avif_speed,
            # 4:0:0 is genuine monochrome YUV400 - of the formats here, AVIF is
            # the only one that can really drop the chroma planes.
            subsampling="4:0:0" if gray else "4:2:0",
            max_threads=spec.encoder_threads,
            range="full",
        )
    elif fmt == "webp":
        # WebP has no grayscale mode; VP8 is always YUV. Handing it an "L"
        # image still helps, because the chroma planes come out constant.
        img.save(path, format="WEBP", quality=quality, method=spec.webp_method, exif=b"")
    else:
        raise ValueError(f"unsupported image format {fmt!r}; expected 'avif' or 'webp'")
    return Variant(path, fmt, img.width, img.height, path.stat().st_size)


def _resize(img: Image.Image, width: int) -> Image.Image:
    """Downscale to *width*, never up.

    Upscaling a 1275 px render into a 1600 px slot invents no detail and costs
    real bytes. When the raster is already narrower, the slot gets the raster
    as it is: the file name is the slot, not a promise about pixel counts.
    """
    if width >= img.width:
        return img
    height = max(1, round(img.height * width / img.width))
    return img.resize((width, height), Image.Resampling.LANCZOS)


def _write_variants(
    img: Image.Image, out_dir: Path, number: int, spec: RenderSpec, gray: bool
) -> tuple[list[Variant], list[Variant]]:
    formats = spec.encoded_formats()
    if not formats:
        raise RenderError(
            f"none of the requested formats {spec.formats!r} can be encoded here; "
            f"this Pillow supports {supported_formats()!r}"
        )
    variants: list[Variant] = []
    for width in spec.widths:
        # Resize once per width, then hand the same pixels to every encoder, so
        # the formats are comparable and the resampling is done once.
        scaled = _resize(img, width)
        for fmt in formats:
            variants.append(
                _encode(scaled, out_dir / f"p{number:04d}@{width}.{fmt}", fmt, spec, gray)
            )

    thumb_img = _resize(img, spec.thumb_width)
    thumbs = [
        _encode(thumb_img, out_dir / f"p{number:04d}@thumb.{fmt}", fmt, spec, gray)
        for fmt in formats
    ]
    return variants, thumbs


PLACEHOLDER_WIDTH = 24
"""Pixels across, for the inline placeholder. Small enough that no word on it
is legible, which is the point: it is furniture, never evidence."""

PLACEHOLDER_QUALITY = 40


def _placeholder(img: Image.Image) -> str:
    """A 24 px-wide grayscale WebP of the page, as a ``data:`` URI.

    Inlined in the page's own HTML so the scan's frame holds a picture of the
    page while the real image is still on the wire. Measured on the demo
    collection: 93 raw bytes a page, 148 in the ``data:`` URI, ~140 after gzip.

    Grayscale because at 24 pixels colour buys nothing; WebP because at this
    size AVIF's container costs more than the picture does (341 B against 93 B,
    measured on the same nine pages). Deterministic: ``method=6`` and the empty
    EXIF block are pinned like every other encoder setting in this module, and
    the same page encodes to the same bytes every time.

    Returns ``""`` if it cannot be made. A page without a placeholder renders
    exactly as it did before this existed, so failing here is never fatal.
    """
    try:
        small = img.convert("L")
        # Halve with a box filter until the raster is close to the target, then
        # resample once. A single LANCZOS step from a 1275 px page to 24 px is
        # 13.6 ms and almost all of this function; the same picture arrives in
        # 1.7 ms this way, which at the documented 20,000-page ceiling is the
        # difference between four minutes of build and forty seconds. `reduce`
        # is an exact box average, so it stays deterministic.
        while small.width >= PLACEHOLDER_WIDTH * 4:
            small = small.reduce(2)
        height = max(1, round(small.height * PLACEHOLDER_WIDTH / small.width))
        small = small.resize((PLACEHOLDER_WIDTH, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        small.save(
            buffer, format="WEBP", quality=PLACEHOLDER_QUALITY, method=6, exif=b""
        )
    except (OSError, ValueError):
        return ""
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/webp;base64,{encoded}"


# --------------------------------------------------------------------------
# rasterising
# --------------------------------------------------------------------------


def _effective_dpi(geom: PageGeometry, spec: RenderSpec) -> int:
    """Pick the resolution for one page.

    Two forces pull on it: the widest variant wants at least its own width in
    pixels, and the pixel budget wants the page to fit in memory. The budget
    wins, because a page rendered slightly soft beats a page not rendered.
    """
    w_pt, h_pt = geom.rendered_pt
    if w_pt <= 0 or h_pt <= 0:
        return spec.dpi
    target = max((*spec.widths, spec.thumb_width))
    wanted = max(spec.dpi, math.ceil(target * POINTS_PER_INCH / w_pt))
    budget = POINTS_PER_INCH * math.sqrt(spec.max_pixels / (w_pt * h_pt))
    return max(1, min(wanted, math.floor(budget)))


def fit_dpi(geom: PageGeometry, dpi: int, max_pixels: int) -> int:
    """The highest resolution at or below *dpi* that keeps *geom* in budget.

    :func:`_effective_dpi` does the arithmetic; the loop afterwards exists
    because ``pixel_size`` rounds *up* to match poppler, so the closed form can
    land a pixel over the line on a page whose points do not divide neatly.
    """
    fitted = min(dpi, _effective_dpi(geom, RenderSpec(dpi=dpi, max_pixels=max_pixels)))
    while fitted > 1 and math.prod(geom.pixel_size(fitted)) > max_pixels:
        fitted -= 1
    return max(1, fitted)


def _batches(
    pages: Sequence[int], dpi_of: dict[int, int], megapixels_of: dict[int, float], spec: RenderSpec
) -> Iterator[tuple[int, int, int]]:
    """Split the page list into ``(first, last, dpi)`` runs for ``-f/-l``.

    A run breaks on a gap in the numbering, on a change of resolution (one
    process, one ``-r``), and on the size limits. Documents whose pages are all
    the same size - which is nearly all of them - come out as a single run.
    """
    run: list[int] = []
    dpi = 0
    megapixels = 0.0
    for page in pages:
        if run and (
            page != run[-1] + 1
            or dpi_of[page] != dpi
            or len(run) >= spec.max_batch_pages
            or megapixels + megapixels_of[page] > spec.max_batch_megapixels
        ):
            yield (run[0], run[-1], dpi)
            run, megapixels = [], 0.0
        if not run:
            dpi = dpi_of[page]
        run.append(page)
        megapixels += megapixels_of[page]
    if run:
        yield (run[0], run[-1], dpi)


def _pdftoppm(
    pdf: Path,
    prefix: Path,
    first: int,
    last: int,
    dpi: int,
    timeout: float,
    extra: Sequence[str] = (),
) -> subprocess.CompletedProcess[bytes]:
    argv = [
        "pdftoppm",
        "-r",
        str(dpi),
        "-png",
        # pdfinfo measures the CropBox, so pdftoppm must draw it. Without this,
        # every crop coordinate computed from page_geometry() is expressed in a
        # different frame from the pixels it is applied to, and on any document
        # whose CropBox differs from its MediaBox - every Acrobat "crop pages",
        # a great many scanners - the redaction check confirms its boxes
        # against the wrong part of the page.
        "-cropbox",
        "-f",
        str(first),
        "-l",
        str(last),
        *extra,
        str(pdf),
        str(prefix),
    ]
    return _run(argv, timeout)


def _collect(prefix: Path) -> dict[int, Path]:
    """Map page number to PNG.

    Poppler zero-pads the suffix to the width of the document's page count, so
    the same page is ``-7.png`` in a nine-page file and ``-007.png`` in a
    nine-hundred-page one. Reading the number back out is the only stable way
    to find it.
    """
    found: dict[int, Path] = {}
    for path in prefix.parent.glob(f"{prefix.name}-*.png"):
        if m := _OUTPUT_RE.search(path.name):
            found[int(m.group(1))] = path
    return found


def _retry_single(pdf: Path, tmpdir: Path, number: int, dpi: int, spec: RenderSpec) -> Path | None:
    """Render one page on its own, after a batch failed to produce it.

    A batch that dies part way through takes its remaining pages with it, and
    one damaged page is enough to do that. Retrying alone keeps the damage
    where it belongs.
    """
    prefix = tmpdir / f"s{number:08d}"
    proc = _pdftoppm(
        pdf, prefix, number, number, dpi, spec.subprocess_timeout, extra=["-singlefile"]
    )
    single = prefix.with_suffix(".png")
    if single.exists():
        return single
    found = _collect(prefix)
    if number in found:
        return found[number]
    _LOG.warning(
        "page %d of %s did not render: %s",
        number,
        pdf.name,
        proc.stderr.decode("utf-8", "replace").strip()[:200],
    )
    return None


def _finish_page(png: Path, number: int, out_dir: Path, spec: RenderSpec, dpi: int) -> RenderedPage:
    """Analyse one rendered PNG and write its encoded variants."""
    with Image.open(png) as opened:
        img = _strip(opened)
    return encode_page(img, number, out_dir, spec, dpi)


def encode_page(
    img: Image.Image,
    number: int,
    out_dir: Path,
    spec: RenderSpec | None = None,
    dpi: int = 0,
) -> RenderedPage:
    """Analyse and encode a page that has already been rasterised.

    The pipeline renders each page once, at full size, and uses those same
    pixels for OCR, redaction analysis *and* publication. Rasterising is 441 ms
    a page; doing it twice because the encoder insisted on reading from disk
    would double the cost of every build for nothing.

    *img* is consumed, not modified: denoising and grayscale conversion produce
    copies.
    """
    spec = spec or RenderSpec()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    colour = colourfulness(img)
    gray = colour < spec.grayscale_threshold
    grain = grain_level(img)
    denoised = grain >= spec.grain_threshold
    if denoised:
        img = _denoise(img)
    if gray and img.mode != "L":
        img = img.convert("L")
    variants, thumbs = _write_variants(img, out_dir, number, spec, gray)
    return RenderedPage(
        number=number,
        width_px=img.width,
        height_px=img.height,
        variants=variants,
        thumb=thumbs[0] if thumbs else None,
        thumbs=thumbs,
        placeholder=_placeholder(img),
        is_grayscale=gray,
        dpi=dpi,
        denoised=denoised,
        colourfulness=colour,
        grain=grain,
    )


def render_pdf(
    pdf: Path,
    out_dir: Path,
    spec: RenderSpec | None = None,
    pages: Iterable[int] | None = None,
) -> list[RenderedPage]:
    """Rasterise *pages* of *pdf* into *out_dir*, one record per page.

    Files land as ``p0007@1600.avif`` and ``p0007@thumb.webp``. A page that
    cannot be rendered comes back with ``failed=True`` and an ``error`` string;
    the rest of the document still renders.
    """
    pdf = Path(pdf)
    out_dir = Path(out_dir)
    spec = spec or RenderSpec()
    out_dir.mkdir(parents=True, exist_ok=True)

    geometry = page_geometry(pdf, spec.subprocess_timeout)
    by_number = {g.number: g for g in geometry}
    wanted = sorted(set(pages)) if pages is not None else [g.number for g in geometry]
    unknown = [p for p in wanted if p not in by_number]
    if unknown:
        raise RenderError(f"{pdf} has {len(geometry)} pages; asked for {unknown}")

    dpi_of = {p: _effective_dpi(by_number[p], spec) for p in wanted}
    megapixels_of = {
        p: math.prod(by_number[p].pixel_size(dpi_of[p])) / 1e6 for p in wanted
    }
    results: dict[int, RenderedPage] = {}

    # One temp directory for the whole call, removed however we leave it.
    # Poppler writes full-size PNGs, and a 5,000 page build that leaks them
    # fills the disk long before it finishes.
    with tempfile.TemporaryDirectory(prefix="stackroom-raster-") as tmp:
        tmpdir = Path(tmp)
        for first, last, dpi in _batches(wanted, dpi_of, megapixels_of, spec):
            prefix = tmpdir / f"b{first:08d}"
            proc = _pdftoppm(pdf, prefix, first, last, dpi, spec.subprocess_timeout)
            rendered = _collect(prefix)
            if proc.returncode != 0 and not rendered:
                _LOG.warning(
                    "pdftoppm failed on %s pages %d-%d: %s",
                    pdf.name,
                    first,
                    last,
                    proc.stderr.decode("utf-8", "replace").strip()[:200],
                )
            for number in range(first, last + 1):
                png = rendered.get(number) or _retry_single(pdf, tmpdir, number, dpi, spec)
                if png is None:
                    w_px, h_px = by_number[number].pixel_size(dpi)
                    results[number] = RenderedPage(
                        number=number,
                        width_px=w_px,
                        height_px=h_px,
                        dpi=dpi,
                        failed=True,
                        error=f"pdftoppm produced no image for page {number}",
                    )
                    continue
                try:
                    results[number] = _finish_page(png, number, out_dir, spec, dpi)
                except (OSError, ValueError, RenderError) as exc:
                    results[number] = RenderedPage(
                        number=number,
                        width_px=0,
                        height_px=0,
                        dpi=dpi,
                        failed=True,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                finally:
                    # Free the PNG as we go: a 32-page batch of poster pages is
                    # gigabytes, and the temp dir only empties at the end.
                    png.unlink(missing_ok=True)

    return [results[p] for p in wanted]


def render_page_crop(
    pdf: Path,
    page: int,
    box: Box,
    dpi: int = 150,
    *,
    max_pixels: int | None = None,
) -> Image.Image:
    """Rasterise only *box* of *page* and return it as a PIL image.

    ``redaction.py`` calls this once per candidate rectangle, to confirm that
    the pixels under a suspected redaction really are uniform - so it renders
    the region and not the page: a one-centimetre box on a poster is a few
    thousand pixels instead of forty million.

    The conversion is the part worth testing. ``pdftoppm``'s ``-x -y -W -H``
    are **pixels at the chosen resolution**, not points, and they are measured
    in the frame poppler outputs, so ``/Rotate 90`` has already been applied
    and the page's width and height are swapped. :class:`Box` is page-relative
    against that same rotated frame - which is also the frame the site
    displays, so the two agree by construction.

    *max_pixels* caps the whole page's raster, lowering the resolution until it
    fits. The budget has to be enforced here and not only in :func:`render_pdf`,
    because this is the function the pipeline actually calls: a 200-inch page
    asks poppler for 900 megapixels, and ``render.max_megapixels`` was being
    read by nothing on that path.
    """
    pdf = Path(pdf)
    geometry = page_geometry(pdf)
    if not 1 <= page <= len(geometry):
        raise RenderError(f"{pdf} has {len(geometry)} pages; asked for page {page}")
    geom = geometry[page - 1]
    page_w, page_h = geom.pixel_size(dpi)
    if max_pixels and page_w * page_h > max_pixels:
        dpi = fit_dpi(geom, dpi, max_pixels)
        page_w, page_h = geom.pixel_size(dpi)

    # Round outwards: a box covering 30.2 px must come back with all 31 of
    # them, or the caller's uniformity check sees a sliver of paper and decides
    # the redaction is not solid after all.
    x0 = max(0, min(page_w, math.floor(box.x * page_w)))
    y0 = max(0, min(page_h, math.floor(box.y * page_h)))
    x1 = max(0, min(page_w, math.ceil(box.x2 * page_w)))
    y1 = max(0, min(page_h, math.ceil(box.y2 * page_h)))
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        # pdftoppm answers an off-page crop with the *whole page*, silently.
        # Refusing here is the difference between a caller learning its box was
        # wrong and a caller measuring the wrong pixels forever.
        raise RenderError(
            f"crop {box} is empty or falls outside page {page}, which is "
            f"{page_w}x{page_h} px at {dpi} dpi"
        )

    with tempfile.TemporaryDirectory(prefix="stackroom-crop-") as tmp:
        prefix = Path(tmp) / "crop"
        proc = _pdftoppm(
            pdf,
            prefix,
            page,
            page,
            dpi,
            300.0,
            extra=[
                "-singlefile",
                "-x", str(x0),
                "-y", str(y0),
                "-W", str(width),
                "-H", str(height),
            ],
        )
        png = prefix.with_suffix(".png")
        if not png.exists():
            raise RenderError(
                f"pdftoppm produced no crop for page {page} of {pdf}: "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}"
            )
        with Image.open(png) as opened:
            # Poppler answers an allocation it will not make with "Bogus memory
            # allocation size" on stderr, exit status 0, and a 1x1 PNG. Nothing
            # else about the call says it failed, so treating that as a rendered
            # page publishes a one-pixel scan and clears the redaction check on
            # a page nobody looked at. The guard is on the *requested* size:
            # a genuinely tiny crop of a few pixels is a real answer.
            if width > 4 and height > 4 and (opened.width <= 1 or opened.height <= 1):
                raise RenderError(
                    f"pdftoppm returned a {opened.width}x{opened.height} image for a "
                    f"{width}x{height} crop of page {page} of {pdf}: it refused the "
                    f"allocation. That page is {page_w}x{page_h} px at {dpi} dpi; "
                    "lower render.dpi or render.max_megapixels."
                )
            return _strip(opened)
