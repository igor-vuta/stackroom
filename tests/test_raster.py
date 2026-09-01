"""Tests for ``ingest.raster``.

Everything here renders a real PDF with real poppler and encodes real images.
The synthetic documents come from ``tests/synth.py`` so there are no fixtures
in git and no third party's scan to worry about.

Most tests use :func:`fast_spec`, which renders at 72 dpi into one narrow
variant. That is not laziness: the properties under test - file naming, byte
determinism, the grayscale and grain decisions, crop arithmetic - are all
independent of resolution, and a full-size AVIF costs a second per page.
:func:`test_default_spec_produces_both_widths` runs the real defaults once.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

import synth
from stackroom.ingest import raster
from stackroom.ingest.raster import (
    AVIF_AVAILABLE,
    RenderError,
    RenderSpec,
    colourfulness,
    grain_level,
    page_geometry,
    render_page_crop,
    render_pdf,
    supported_formats,
)
from stackroom.model import Box


def fast_spec(**overrides: object) -> RenderSpec:
    """A spec that exercises every code path in about a tenth of the time."""
    settings: dict[str, object] = {
        "dpi": 72,
        "widths": (320,),
        "thumb_width": 96,
        "avif_speed": 9,
        "webp_method": 4,
    }
    settings.update(overrides)
    return RenderSpec(**settings)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def typed_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Three born-digital pages: black text, one black box on page 2."""
    path = tmp_path_factory.mktemp("typed") / "typed.pdf"
    return synth.born_digital_pdf(
        path,
        pages=3,
        redactions={2: [synth.RedactionSpec(x=200, y=500, w=180, h=40, code="(b)(6)")]},
    )


@pytest.fixture(scope="module")
def clean_scan_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("scan") / "clean.pdf"
    return synth.image_only_pdf(path, [synth.typed_page(lines=20)])


@pytest.fixture(scope="module")
def grainy_scan_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("scan") / "grainy.pdf"
    return synth.image_only_pdf(path, [synth.typed_page(lines=20, grain=0.3, scan_border=True)])


@pytest.fixture(scope="module")
def colour_scan_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthetic page with a red stamp on it - the case that must stay RGB."""
    page = synth.typed_page(lines=20).convert("RGB")
    draw = ImageDraw.Draw(page)
    draw.rectangle([820, 130, 1120, 250], outline=(198, 26, 22), width=10)
    draw.text((860, 180), "RELEASED IN PART", fill=(198, 26, 22))
    path = tmp_path_factory.mktemp("scan") / "colour.pdf"
    return synth.image_only_pdf(path, [page])


@pytest.fixture(scope="module")
def awkward_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One document, three page sizes, and a /Rotate 90 on the first page.

    Built here rather than in synth.py because it is a rendering problem, not a
    document problem: nothing downstream of raster.py cares.
    """
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.pdfgen import canvas

    directory = tmp_path_factory.mktemp("awkward")
    flat = directory / "flat.pdf"
    c = canvas.Canvas(str(flat), pagesize=LETTER)
    c.setFont("Helvetica", 36)
    c.drawString(72, 700, "PAGE ONE LETTER")
    c.showPage()
    c.setPageSize(A4)
    c.setFont("Helvetica", 36)
    c.drawString(50, 700, "PAGE TWO A4")
    c.showPage()
    c.setPageSize((2400, 3000))
    c.setFont("Helvetica", 90)
    c.drawString(120, 2800, "PAGE THREE POSTER")
    c.showPage()
    c.save()

    reader = PdfReader(str(flat))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index == 0:
            page.rotate(90)
        writer.add_page(page)
    rotated = directory / "awkward.pdf"
    with rotated.open("wb") as fh:
        writer.write(fh)
    return rotated


def _mean_luma(img: Image.Image) -> float:
    return float(np.asarray(img.convert("L"), dtype=np.float32).mean())


# --------------------------------------------------------------------------
# environment probing
# --------------------------------------------------------------------------


def test_supported_formats_always_offers_webp() -> None:
    assert "webp" in supported_formats()
    assert ("avif" in supported_formats()) is AVIF_AVAILABLE


def test_missing_avif_degrades_to_webp_instead_of_crashing(
    typed_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(raster, "AVIF_AVAILABLE", False)
    assert supported_formats() == ("webp",)
    pages = render_pdf(typed_pdf, tmp_path, fast_spec(), pages=[1])
    assert pages[0].ok
    assert {v.format for v in pages[0].variants} == {"webp"}
    assert not list(tmp_path.glob("*.avif"))


def test_a_spec_nothing_can_encode_fails_the_page_not_the_run(
    typed_pdf: Path, tmp_path: Path
) -> None:
    pages = render_pdf(typed_pdf, tmp_path, fast_spec(formats=("jpeg2000",)), pages=[1])
    assert pages[0].failed
    assert "jpeg2000" in (pages[0].error or "")


# --------------------------------------------------------------------------
# output layout
# --------------------------------------------------------------------------


def test_render_writes_one_file_per_width_and_format(typed_pdf: Path, tmp_path: Path) -> None:
    spec = fast_spec(widths=(320, 200))
    pages = render_pdf(typed_pdf, tmp_path, spec)

    assert [p.number for p in pages] == [1, 2, 3]
    for page in pages:
        assert page.ok, page.error
        for width in spec.widths:
            for fmt in spec.encoded_formats():
                path = tmp_path / f"p{page.number:04d}@{width}.{fmt}"
                assert path.exists(), f"{path.name} missing"
                assert path.stat().st_size > 0
                assert page.variant(width, fmt) is not None
        assert page.thumb is not None
        assert page.thumb.path.name == f"p{page.number:04d}@thumb.{spec.encoded_formats()[0]}"
        assert page.thumb.width == spec.thumb_width


def test_only_the_requested_pages_are_rendered(typed_pdf: Path, tmp_path: Path) -> None:
    pages = render_pdf(typed_pdf, tmp_path, fast_spec(), pages=[3, 1, 1])
    assert [p.number for p in pages] == [1, 3]
    assert not (tmp_path / "p0002@320.webp").exists()


def test_asking_for_a_page_that_is_not_there_is_an_error(typed_pdf: Path, tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="asked for"):
        render_pdf(typed_pdf, tmp_path, fast_spec(), pages=[9])


def test_encoded_files_round_trip_through_the_decoder(typed_pdf: Path, tmp_path: Path) -> None:
    spec = fast_spec()
    page = render_pdf(typed_pdf, tmp_path, spec, pages=[1])[0]
    for variant in page.variants + page.thumbs:
        with Image.open(variant.path) as decoded:
            assert decoded.format == variant.format.upper()
            assert decoded.size == (variant.width, variant.height)
            # The page is mostly paper: a decode that produced garbage would
            # not come back light.
            assert _mean_luma(decoded) > 150


def test_a_slot_is_never_upscaled_into(typed_pdf: Path, tmp_path: Path) -> None:
    """The file name is the slot, not a promise about pixels.

    A tight pixel budget stops the renderer reaching the 5000 px slot, and the
    slot then gets the raster as it is rather than an invented enlargement.
    """
    spec = fast_spec(widths=(320, 5000), max_pixels=1_000_000)
    page = render_pdf(typed_pdf, tmp_path, spec, pages=[1])[0]
    for variant in page.variants:
        slot = int(variant.path.stem.split("@")[1])
        assert variant.width <= slot
    assert page.width_px < 5000
    assert page.variant(5000, "webp").width == page.width_px
    assert page.variant(320, "webp").width == 320


def test_batching_groups_what_poppler_can_render_in_one_process() -> None:
    """One ``-r`` per process, so a run breaks on resolution as well as on gaps."""
    spec = fast_spec(max_batch_pages=3)
    same = dict.fromkeys(range(1, 8), 150)
    free = dict.fromkeys(range(1, 8), 0.0)

    assert list(raster._batches([1, 2, 3], same, free, fast_spec())) == [(1, 3, 150)]
    assert list(raster._batches([1, 2, 5, 6], same, free, fast_spec())) == [
        (1, 2, 150),
        (5, 6, 150),
    ]
    assert list(raster._batches([1, 2, 3, 4, 5], same, free, spec)) == [
        (1, 3, 150),
        (4, 5, 150),
    ]

    mixed = {1: 150, 2: 150, 3: 96, 4: 96}
    assert list(raster._batches([1, 2, 3, 4], mixed, free, fast_spec())) == [
        (1, 2, 150),
        (3, 4, 96),
    ]

    heavy = dict.fromkeys(range(1, 5), 300.0)
    assert list(raster._batches([1, 2, 3], same, heavy, fast_spec(max_batch_megapixels=400))) == [
        (1, 1, 150),
        (2, 2, 150),
        (3, 3, 150),
    ]


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def test_the_same_pdf_renders_to_the_same_bytes(typed_pdf: Path, tmp_path: Path) -> None:
    """Guarantee 6: two people must be able to check they published the same thing."""
    first, second = tmp_path / "a", tmp_path / "b"
    render_pdf(typed_pdf, first, fast_spec())
    render_pdf(typed_pdf, second, fast_spec())

    names = sorted(p.name for p in first.iterdir())
    assert names == sorted(p.name for p in second.iterdir())
    assert names, "nothing was written"
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_source_metadata_cannot_leak_into_the_output(tmp_path: Path) -> None:
    """A PNG carrying dpi and EXIF must encode identically to one carrying none."""
    page = synth.typed_page(lines=6).convert("RGB")
    plain, tagged = tmp_path / "plain.png", tmp_path / "tagged.png"
    page.save(plain, format="PNG")
    exif = Image.Exif()
    exif[271] = "Some Scanner Co"
    page.save(tagged, format="PNG", dpi=(600, 600), exif=exif)

    spec = fast_spec()
    out = []
    for source in (plain, tagged):
        with Image.open(source) as opened:
            stripped = raster._strip(opened)
        target = tmp_path / f"{source.stem}.webp"
        raster._encode(stripped, target, "webp", spec, gray=False)
        out.append(target.read_bytes())
    assert out[0] == out[1]


# --------------------------------------------------------------------------
# image analysis
# --------------------------------------------------------------------------


def test_colourfulness_separates_a_stamp_from_a_grayscale_scan() -> None:
    gray = synth.typed_page(lines=20)
    assert colourfulness(gray) == 0.0

    stamped = gray.convert("RGB")
    ImageDraw.Draw(stamped).rectangle([820, 130, 1120, 250], fill=(198, 26, 22))
    assert colourfulness(stamped) > RenderSpec().grayscale_threshold * 3


def test_colourfulness_ignores_scanner_chroma_noise() -> None:
    """The reason mode-checking is not enough: a gray original still arrives RGB."""
    a = np.asarray(synth.typed_page(lines=20), dtype=np.int16)
    rng = np.random.default_rng(5)
    noisy = np.clip(
        np.stack([a, a, a], axis=-1) + rng.normal(0, 6, (*a.shape, 3)), 0, 255
    ).astype(np.uint8)
    assert colourfulness(Image.fromarray(noisy, "RGB")) < RenderSpec().grayscale_threshold


def test_grain_level_is_zero_on_a_clean_render_and_high_on_a_scan() -> None:
    assert grain_level(synth.typed_page(lines=20)) == 0.0
    assert grain_level(synth.typed_page(lines=20, grain=0.3)) >= RenderSpec().grain_threshold


def test_a_grayscale_page_is_encoded_grayscale(clean_scan_pdf: Path, tmp_path: Path) -> None:
    page = render_pdf(clean_scan_pdf, tmp_path, fast_spec(), pages=[1])[0]
    assert page.is_grayscale
    assert page.colourfulness < RenderSpec().grayscale_threshold


def test_a_page_with_a_stamp_keeps_its_colour(colour_scan_pdf: Path, tmp_path: Path) -> None:
    page = render_pdf(colour_scan_pdf, tmp_path, fast_spec(), pages=[1])[0]
    assert not page.is_grayscale
    with Image.open(page.variant(320, "webp").path) as decoded:
        rgb = np.asarray(decoded.convert("RGB"), dtype=np.int16)
    chroma = np.abs(rgb[:, :, 0] - rgb[:, :, 1]).max()
    assert chroma > 40, "the red stamp did not survive encoding"


# --------------------------------------------------------------------------
# denoising
# --------------------------------------------------------------------------


def test_a_clean_render_is_never_blurred(typed_pdf: Path, tmp_path: Path) -> None:
    page = render_pdf(typed_pdf, tmp_path, fast_spec(), pages=[1])[0]
    assert page.grain == 0.0
    assert not page.denoised


def test_grain_is_detected_and_denoising_pays_for_itself(
    grainy_scan_pdf: Path, tmp_path: Path
) -> None:
    with_denoise = render_pdf(grainy_scan_pdf, tmp_path / "on", fast_spec(), pages=[1])[0]
    # A threshold nothing can reach is how we ask for the same page untouched.
    without = render_pdf(
        grainy_scan_pdf, tmp_path / "off", fast_spec(grain_threshold=1e9), pages=[1]
    )[0]

    assert with_denoise.denoised
    assert not without.denoised
    assert with_denoise.grain >= RenderSpec().grain_threshold

    for fmt in with_denoise.variants[0].format, "webp":
        big = without.variant(320, fmt)
        small = with_denoise.variant(320, fmt)
        assert small.bytes < big.bytes, f"denoising made {fmt} bigger"


# --------------------------------------------------------------------------
# geometry, rotation and awkward pages
# --------------------------------------------------------------------------


def test_geometry_reports_rotation_and_swaps_the_rendered_size(awkward_pdf: Path) -> None:
    geometry = page_geometry(awkward_pdf)
    assert [g.number for g in geometry] == [1, 2, 3]
    assert geometry[0].rotation == 90
    assert geometry[0].rendered_pt == (792.0, 612.0)
    assert geometry[0].pixel_size(150) == (1650, 1275)
    # Poppler rounds up: A4 is 595.276 x 841.89 pt.
    assert geometry[1].pixel_size(72) == (596, 842)


def test_a_rotated_page_renders_landscape(awkward_pdf: Path, tmp_path: Path) -> None:
    page = render_pdf(awkward_pdf, tmp_path, fast_spec(), pages=[1])[0]
    assert page.ok
    assert page.width_px > page.height_px


def test_wildly_different_page_sizes_in_one_document(awkward_pdf: Path, tmp_path: Path) -> None:
    pages = render_pdf(awkward_pdf, tmp_path, fast_spec(), pages=[1, 2, 3])
    assert all(p.ok for p in pages), [p.error for p in pages]
    assert len({p.height_px for p in pages}) == 3
    for page in pages:
        assert page.variant(320, "webp").width == 320


def test_a_huge_page_is_downscaled_instead_of_exhausting_memory(
    awkward_pdf: Path, tmp_path: Path
) -> None:
    """Page 3 is 2400x3000 pt: 31 MP at 150 dpi, 55 MP at 200."""
    spec = fast_spec(dpi=200, widths=(320,), max_pixels=4_000_000)
    page = render_pdf(awkward_pdf, tmp_path, spec, pages=[3])[0]
    assert page.ok
    assert page.dpi < spec.dpi
    assert page.width_px * page.height_px <= spec.max_pixels


def test_dpi_is_raised_so_the_widest_variant_is_not_an_upscale(
    typed_pdf: Path, tmp_path: Path
) -> None:
    """A letter page at 150 dpi is 1275 px; a 1600 px variant needs 189."""
    spec = fast_spec(dpi=150, widths=(1600,), avif_speed=10, formats=("webp",))
    page = render_pdf(typed_pdf, tmp_path, spec, pages=[1])[0]
    assert page.dpi == 189
    assert page.width_px >= 1600
    assert page.variant(1600, "webp").width == 1600


# --------------------------------------------------------------------------
# crop rendering
# --------------------------------------------------------------------------


def test_crop_coordinates_land_on_the_redaction(typed_pdf: Path) -> None:
    """The black box on page 2 sits at (200, 500) 180x40 in PDF points.

    ``Box`` is top-left relative, so the conversion has to flip y as well as
    scale it. If ``-x/-y/-W/-H`` were treated as points rather than pixels the
    crop would land less than half way to the box.
    """
    box = Box(200 / 612, (792 - 540) / 792, 180 / 612, 40 / 792)
    crop = render_page_crop(typed_pdf, 2, box, dpi=150)

    assert crop.size == (math.ceil(380 / 612 * 1275) - math.floor(200 / 612 * 1275), 84)
    pixels = np.asarray(crop.convert("L"))
    assert pixels.mean() < 20, "the crop is not mostly ink"
    assert (pixels < 40).mean() > 0.95, "the crop is not the black box"


def test_a_crop_is_exactly_the_same_pixels_as_the_full_render(
    typed_pdf: Path, tmp_path: Path
) -> None:
    """The strongest statement available about the conversion being right."""
    box = Box(0.2, 0.3, 0.25, 0.1)
    dpi = 110
    crop = render_page_crop(typed_pdf, 1, box, dpi=dpi)

    prefix = tmp_path / "full"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", "-f", "1", "-l", "1", "-singlefile",
         str(typed_pdf), str(prefix)],
        check=True,
        capture_output=True,
    )
    with Image.open(prefix.with_suffix(".png")) as full:
        width, height = page_geometry(typed_pdf)[0].pixel_size(dpi)
        assert full.size == (width, height)
        expected = full.crop(
            (
                math.floor(box.x * width),
                math.floor(box.y * height),
                math.ceil(box.x2 * width),
                math.ceil(box.y2 * height),
            )
        ).convert("RGB")
    assert np.array_equal(np.asarray(expected), np.asarray(crop.convert("RGB")))


def test_crop_of_a_rotated_page_uses_the_rendered_frame(awkward_pdf: Path) -> None:
    """Page 1 is letter with /Rotate 90, so it rasterises 1650x1275."""
    crop = render_page_crop(awkward_pdf, 1, Box(0.0, 0.0, 0.5, 0.5), dpi=72)
    assert crop.size == (396, 306)


def test_an_off_page_crop_is_refused_rather_than_silently_widened(typed_pdf: Path) -> None:
    """pdftoppm answers an off-page crop with the whole page. That must not pass."""
    with pytest.raises(RenderError, match="outside page"):
        render_page_crop(typed_pdf, 1, Box(1.4, 1.4, 0.1, 0.1))
    with pytest.raises(RenderError, match="empty"):
        render_page_crop(typed_pdf, 1, Box(0.5, 0.5, 0.0, 0.0))


def test_crop_beyond_the_last_page_is_an_error(typed_pdf: Path) -> None:
    with pytest.raises(RenderError, match="asked for page"):
        render_page_crop(typed_pdf, 42, Box(0.1, 0.1, 0.1, 0.1))


# --------------------------------------------------------------------------
# failure paths
# --------------------------------------------------------------------------


def test_one_unwritable_page_does_not_abort_the_document(
    typed_pdf: Path, tmp_path: Path
) -> None:
    """Page 2's first output path is occupied by a directory."""
    spec = fast_spec()
    blocker = tmp_path / f"p0002@320.{spec.encoded_formats()[0]}"
    blocker.mkdir(parents=True)

    pages = render_pdf(typed_pdf, tmp_path, spec)
    assert [p.failed for p in pages] == [False, True, False]
    assert "Error" in (pages[1].error or "")
    assert pages[1].variants == []
    assert (tmp_path / "p0003@320.webp").exists()


def test_a_page_poppler_will_not_draw_is_reported_not_raised(
    typed_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = raster._pdftoppm

    def refuse_page_two(pdf, prefix, first, last, dpi, timeout, extra=()):  # type: ignore[no-untyped-def]
        if first <= 2 <= last:
            return subprocess.CompletedProcess(["pdftoppm"], 1, b"", b"Error: damaged page")
        return real(pdf, prefix, first, last, dpi, timeout, extra)

    monkeypatch.setattr(raster, "_pdftoppm", refuse_page_two)
    pages = render_pdf(typed_pdf, tmp_path, fast_spec(), pages=[2])

    assert pages[0].failed
    assert "no image" in (pages[0].error or "")
    # The record still knows how big the page was meant to be, so the site can
    # keep its layout instead of collapsing.
    assert pages[0].width_px == 612 and pages[0].height_px == 792


def test_temporary_files_are_cleaned_up_even_when_a_page_fails(
    typed_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def explode(png, number, out_dir, spec, dpi):  # type: ignore[no-untyped-def]
        seen.append(Path(png))
        raise OSError("disk full")

    monkeypatch.setattr(raster, "_finish_page", explode)
    pages = render_pdf(typed_pdf, tmp_path, fast_spec(), pages=[1])
    assert pages[0].failed and "disk full" in (pages[0].error or "")
    assert seen and not seen[0].exists()
    assert not seen[0].parent.exists(), "the temp directory outlived the call"


def test_a_file_that_is_not_a_pdf_fails_clearly(tmp_path: Path) -> None:
    fake = tmp_path / "not.pdf"
    fake.write_bytes(b"this is not a PDF at all\n")
    with pytest.raises(RenderError):
        render_pdf(fake, tmp_path / "out", fast_spec())


# --------------------------------------------------------------------------
# one run at the real defaults
# --------------------------------------------------------------------------


def test_default_spec_produces_both_widths(clean_scan_pdf: Path, tmp_path: Path) -> None:
    spec = RenderSpec()
    page = render_pdf(clean_scan_pdf, tmp_path, spec, pages=[1])[0]
    assert page.ok
    assert page.is_grayscale
    assert page.variant(1600, "webp").width == 1600
    assert page.variant(900, "webp").width == 900
    assert page.thumb is not None and page.thumb.width == 240
    if AVIF_AVAILABLE:
        # The whole reason AVIF is first in the preference order.
        assert page.variant(1600, "avif").bytes < page.variant(1600, "webp").bytes
