"""Tests for ``ingest.ocr``.

Real Tesseract, real synthetic pages. This container ships ``eng`` and ``osd``
only, so anything that needs another language skips rather than fails - a test
suite that goes red because of a missing OS package teaches contributors to
ignore red.
"""

from __future__ import annotations

import collections
import random
import statistics
from concurrent.futures import ProcessPoolExecutor

import pytesseract
import pytest
from PIL import Image
from pytesseract import Output

import synth
from stackroom.ingest import ocr
from stackroom.ingest.ocr import (
    MissingLanguageError,
    OcrResult,
    available_languages,
    ocr_image,
)
from stackroom.model import Box

# synth.typed_page seeds its own Random(11), so the words on the page are
# known before it is drawn. Reproducing the draw here gives us a real "known
# word in a known place" without a fixture file.
_TEXT_START_XY = (140, 150)
_LINE_STEP = 40


def expected_line(index: int, words_per_line: int = 8) -> list[str]:
    rnd = random.Random(11)
    line: list[str] = []
    for _ in range(index + 1):
        line = [rnd.choice(synth.LOREM) for _ in range(words_per_line)]
    return line


@pytest.fixture(scope="module")
def page() -> Image.Image:
    return synth.typed_page(lines=6)


@pytest.fixture(scope="module")
def result(page: Image.Image) -> OcrResult:
    return ocr_image(page, auto_rotate=False)


def has_language(code: str) -> bool:
    return code in available_languages()


# --------------------------------------------------------------------------
# the environment
# --------------------------------------------------------------------------


def test_available_languages_reports_what_tesseract_has() -> None:
    langs = available_languages()
    assert "eng" in langs
    assert langs == sorted(langs)


def test_result_carries_the_settings_it_was_run_with(result: OcrResult) -> None:
    assert result.languages == ["eng"]
    assert result.psm == 3
    assert result.tesseract_version and result.tesseract_version != "unknown"
    assert result.ok and result.reason is None


def test_a_missing_language_names_the_package_to_install(page: Image.Image) -> None:
    """Not a KeyError three hours into a 5,000-page build."""
    if has_language("rus"):  # pragma: no cover - depends on the host
        pytest.skip("this machine really does have Russian installed")
    with pytest.raises(MissingLanguageError) as caught:
        ocr_image(page, languages=("rus",))
    message = str(caught.value)
    assert "tesseract-ocr-rus" in message
    assert "eng" in message, "the error should say what IS installed"


def test_asking_for_no_language_is_an_error(page: Image.Image) -> None:
    with pytest.raises(MissingLanguageError):
        ocr_image(page, languages=())


@pytest.mark.skipif(not has_language("deu"), reason="needs tesseract-ocr-deu")
def test_a_second_language_can_be_combined() -> None:  # pragma: no cover - not installed here
    out = ocr_image(synth.typed_page(lines=4), languages=("eng", "deu"))
    assert out.languages == ["eng", "deu"]


# --------------------------------------------------------------------------
# words, boxes and lines
# --------------------------------------------------------------------------


def test_word_boxes_land_where_the_word_was_drawn(page: Image.Image, result: OcrResult) -> None:
    """``synth.typed_page`` draws line 0 at (140, 150) on a 1275x1650 page."""
    first = expected_line(0)[0]
    matches = [w for w in result.words if w.text.strip(".,") == first.strip(".,")]
    assert matches, f"never found {first!r} in {[w.text for w in result.words][:12]}"

    x_expected = _TEXT_START_XY[0] / page.width
    y_expected = _TEXT_START_XY[1] / page.height
    top_left = min(matches, key=lambda w: (w.box.y, w.box.x))
    assert abs(top_left.box.x - x_expected) < 0.01
    assert abs(top_left.box.y - y_expected) < 0.01
    assert 0.0 < top_left.box.w < 0.2
    assert 0.0 < top_left.box.h < 0.05


def test_every_box_is_inside_the_page(result: OcrResult) -> None:
    for word in result.words:
        assert 0.0 <= word.box.x < 1.0, word
        assert 0.0 <= word.box.y < 1.0, word
        assert 0.0 < word.box.x2 <= 1.0, word
        assert 0.0 < word.box.y2 <= 1.0, word


def test_the_page_is_actually_read(page: Image.Image, result: OcrResult) -> None:
    lexicon = {w.strip(".,").lower() for w in synth.LOREM}
    read = [w.text.strip(".,").lower() for w in result.words]
    assert len(read) == 48, "six lines of eight words"
    assert sum(1 for w in read if w in lexicon) / len(read) > 0.95


def test_lines_are_numbered_densely_and_match_the_line_strings(result: OcrResult) -> None:
    """``Word.line`` has to index ``lines`` - Tesseract's own triple does not.

    Its block/paragraph/line numbers are sparse and restart per block, so they
    are renumbered from zero in reading order.
    """
    assert result.lines
    assert max(w.line for w in result.words) == len(result.lines) - 1
    assert sorted({w.line for w in result.words}) == list(range(len(result.lines)))
    rebuilt = collections.defaultdict(list)
    for word in result.words:
        rebuilt[word.line].append(word.text)
    for index, text in enumerate(result.lines):
        assert " ".join(rebuilt[index]) == text


def test_reading_order_is_top_to_bottom(result: OcrResult) -> None:
    """Guarantee 3 ties this order to the token order in the page HTML."""
    tops = [result.words[0].box.y]
    for word in result.words[1:]:
        if word.box.y > tops[-1] + 0.005:
            tops.append(word.box.y)
    assert tops == sorted(tops)


# --------------------------------------------------------------------------
# confidence
# --------------------------------------------------------------------------


def test_only_level_five_rows_carry_a_confidence(page: Image.Image, result: OcrResult) -> None:
    """The bug this module exists to avoid.

    Levels 1-4 report ``conf == -1``. Averaging the raw table instead of the
    word rows drags the mean down by twenty-odd points on a page that was read
    perfectly, and every quality verdict downstream inherits the damage.
    """
    prepared, dpi = ocr._prepare(page, None)
    data = pytesseract.image_to_data(
        prepared, lang="eng", config=f"--psm 3 -c user_defined_dpi={dpi}", output_type=Output.DICT
    )

    non_word_confs = {
        int(c) for level, c in zip(data["level"], data["conf"], strict=True) if int(level) != 5
    }
    assert non_word_confs == {-1}, "the premise of the filter has changed"
    assert len(non_word_confs) < len(data["level"]), "there were no structural rows at all"

    raw_mean = statistics.mean(float(c) for c in data["conf"])
    word_mean = statistics.mean(w.conf for w in result.words)
    assert word_mean - raw_mean > 10, "the sentinel rows are supposed to poison the mean"

    assert all(w.conf >= 0 for w in result.words)
    assert len(result.words) == sum(1 for level in data["level"] if int(level) == 5)


def test_blank_word_rows_are_dropped(page: Image.Image) -> None:
    out = ocr_image(page, auto_rotate=False)
    assert all(w.text.strip() for w in out.words)


def test_min_conf_filters_words_and_keeps_the_lines_dense(page: Image.Image) -> None:
    everything = ocr_image(page, auto_rotate=False)
    threshold = int(statistics.median(w.conf for w in everything.words)) + 1
    filtered = ocr_image(page, auto_rotate=False, min_conf=threshold)

    assert 0 < len(filtered.words) < len(everything.words)
    assert all(w.conf >= threshold for w in filtered.words)
    assert max(w.line for w in filtered.words) == len(filtered.lines) - 1


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------


def marker_page(width: int = 1200, height: int = 1600) -> Image.Image:
    """A page with one unmistakable word in the top-left quadrant."""
    from PIL import ImageDraw, ImageFont

    img = Image.new("L", (width, height), 246)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 44)
    except OSError:  # pragma: no cover - depends on the host
        font = ImageFont.load_default()
    draw.text((110, 110), "ZEBRAFISH", fill=20, font=font)
    draw.text((110, 700), "middle of the page", fill=20, font=font)
    draw.text((110, 1400), "bottom anchor line", fill=20, font=font)
    return img


def find_marker(out: OcrResult) -> Box:
    matches = [w for w in out.words if "ZEBRA" in w.text.upper()]
    assert matches, f"marker word not read; got {[w.text for w in out.words]}"
    return matches[0].box


def test_the_marker_starts_in_the_top_left() -> None:
    box = find_marker(ocr_image(marker_page(), auto_rotate=False))
    assert box.x < 0.5 and box.y < 0.5
    assert box.w > box.h, "upright text is wider than it is tall"


@pytest.mark.parametrize(
    ("turned_clockwise", "expected_rotate", "quadrant"),
    [
        (90, 270, "top-right"),
        (180, 180, "bottom-right"),
        (270, 90, "bottom-left"),
    ],
)
def test_rotation_round_trip(turned_clockwise: int, expected_rotate: int, quadrant: str) -> None:
    """Rotate a page, OCR it, and check the box comes back in the right corner.

    The site displays the scan as it was filed, so boxes must be reported in
    the frame of the image that was handed in - not the frame Tesseract read.
    A word in the original's top-left corner ends up in the top-right of a page
    turned 90 degrees clockwise, and that is where its box has to be.
    """
    # PIL rotates counter-clockwise, so -deg turns the page clockwise.
    turned = marker_page().rotate(-turned_clockwise, expand=True)
    out = ocr_image(turned)

    assert out.rotated_by == expected_rotate
    box = find_marker(out)
    right = box.x > 0.5
    bottom = box.y > 0.5
    assert (right, bottom) == {
        "top-right": (True, False),
        "bottom-right": (True, True),
        "bottom-left": (False, True),
    }[quadrant], f"box {box} is not in the {quadrant}"

    if turned_clockwise in (90, 270):
        assert box.h > box.w, "a sideways word should be taller than it is wide"


def test_boxes_are_untouched_when_nothing_is_rotated() -> None:
    upright = marker_page()
    with_osd = ocr_image(upright, auto_rotate=True)
    without = ocr_image(upright, auto_rotate=False)
    assert with_osd.rotated_by == 0
    assert find_marker(with_osd) == find_marker(without)


def test_auto_rotate_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off means off: no OSD subprocess, no turn, boxes in the input frame.

    Tesseract's own layout analysis will often still read sideways text, so
    "did it find the word" proves nothing here. What matters is that we did not
    rotate anything behind the caller's back.
    """

    def forbidden(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("orientation detection ran with auto_rotate=False")

    monkeypatch.setattr(pytesseract, "image_to_osd", forbidden)
    turned = marker_page().rotate(-90, expand=True)
    out = ocr_image(turned, auto_rotate=False)
    assert out.rotated_by == 0
    for word in out.words:
        assert 0.0 <= word.box.x <= 1.0 and 0.0 <= word.box.y <= 1.0


def test_unrotate_maps_the_corners_exactly() -> None:
    """A unit check on the arithmetic the round-trip test exercises."""
    corner = Box(0.0, 0.0, 0.2, 0.05)
    assert ocr._unrotate_box(corner, 0) == corner
    assert ocr._unrotate_box(corner, 90) == Box(0.0, 0.8, 0.05, 0.2)
    assert ocr._unrotate_box(corner, 180) == Box(0.8, 0.95, 0.2, 0.05)
    assert ocr._unrotate_box(corner, 270) == Box(0.95, 0.0, 0.05, 0.2)
    for angle in (90, 180, 270):
        there = ocr._unrotate_box(corner, angle)
        back = ocr._unrotate_box(there, (360 - angle) % 360)
        assert back.x == pytest.approx(corner.x)
        assert back.y == pytest.approx(corner.y)
        assert back.w == pytest.approx(corner.w)
        assert back.h == pytest.approx(corner.h)


def test_orientation_detection_failing_is_not_a_page_failure() -> None:
    """OSD raises ``Too few characters`` on sparse pages, constantly."""
    out = ocr_image(synth.blank_page())
    assert out.rotated_by == 0
    assert out.ok


# --------------------------------------------------------------------------
# pages that say nothing
# --------------------------------------------------------------------------


def test_a_blank_page_reads_as_empty_not_as_broken() -> None:
    out = ocr_image(synth.blank_page())
    assert out.words == []
    assert out.lines == []
    assert out.reason is None, "blank is not a failure"


def test_a_page_of_noise_does_not_crash() -> None:
    out = ocr_image(synth.noise_page(600, 800))
    assert out.ok
    assert all(0.0 <= w.box.x <= 1.0 for w in out.words)


# --------------------------------------------------------------------------
# preprocessing
# --------------------------------------------------------------------------


def test_a_low_resolution_page_is_upscaled_towards_300_dpi() -> None:
    """Tesseract degrades badly below about 200 dpi; this is the fix."""
    small = synth.typed_page(lines=6).resize((637, 825), Image.Resampling.LANCZOS)
    prepared, dpi = ocr._prepare(small, None)
    assert prepared.width > small.width
    assert dpi == pytest.approx(ocr.TARGET_DPI, abs=2)
    assert prepared.mode == "L"


def test_an_already_high_resolution_page_is_left_alone() -> None:
    big = synth.typed_page(lines=6).resize((2550, 3300), Image.Resampling.LANCZOS)
    prepared, dpi = ocr._prepare(big, None)
    assert prepared.size == big.size
    assert dpi == 300


def test_a_caller_that_knows_the_dpi_is_believed() -> None:
    page = synth.typed_page(lines=4)
    prepared, dpi = ocr._prepare(page, source_dpi=600)
    assert prepared.size == page.size, "600 dpi input needs no upscale"
    assert dpi == 600


def test_the_upscale_is_bounded() -> None:
    """A mis-detected dpi must not turn one page into a gigabyte of pixels."""
    tiny = synth.typed_page(lines=2).resize((80, 104), Image.Resampling.LANCZOS)
    prepared, _ = ocr._prepare(tiny, None)
    assert prepared.width <= tiny.width * ocr.MAX_UPSCALE


def test_a_washed_out_page_is_binarised_and_still_reads() -> None:
    faint = Image.eval(synth.typed_page(lines=4), lambda v: int(150 + (v - 140) * 0.12))
    prepared, _ = ocr._prepare(faint, None)
    assert set(np_unique(prepared)) <= {0, 255}, "low contrast should trigger Otsu"

    out = ocr_image(faint, auto_rotate=False)
    lexicon = {w.strip(".,").lower() for w in synth.LOREM}
    read = [w.text.strip(".,").lower() for w in out.words]
    assert read and sum(1 for w in read if w in lexicon) / len(read) > 0.9


def test_a_blank_page_is_not_binarised_into_noise() -> None:
    """Otsu on a one-valued histogram is 0/0; the guard is not decorative."""
    prepared, _ = ocr._prepare(synth.blank_page(200, 260), None)
    assert len(np_unique(prepared)) == 1


def np_unique(img: Image.Image) -> list[int]:
    import numpy as np

    return sorted(int(v) for v in np.unique(np.asarray(img)))


# --------------------------------------------------------------------------
# failure handling and concurrency
# --------------------------------------------------------------------------


def test_a_timeout_returns_an_empty_result_with_a_reason(page: Image.Image) -> None:
    """A 5,000-page build must not hang on one pathological page."""
    out = ocr_image(page, auto_rotate=False, timeout=0.05)
    assert out.words == []
    assert out.reason and "timeout" in out.reason.lower()
    assert not out.ok
    # The settings are still reported, so the failure is diagnosable.
    assert out.languages == ["eng"] and out.psm == 3


def test_a_tesseract_error_is_reported_not_raised(
    page: Image.Image, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> dict[str, list[object]]:
        raise pytesseract.TesseractError(1, "Segmentation fault")

    monkeypatch.setattr(pytesseract, "image_to_data", explode)
    out = ocr_image(page, auto_rotate=False)
    assert out.words == []
    assert out.reason and "Segmentation fault" in out.reason


def test_an_uninstalled_tesseract_is_raised_not_swallowed(
    page: Image.Image, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken install is a mistake in the run, not a property of the page."""

    def missing(*args: object, **kwargs: object) -> dict[str, list[object]]:
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_data", missing)
    with pytest.raises(pytesseract.TesseractNotFoundError):
        ocr_image(page, auto_rotate=False)


def _ocr_text(img: Image.Image) -> list[str]:
    return [w.text for w in ocr_image(img, auto_rotate=False).words]


def test_ocr_image_is_safe_in_a_process_pool() -> None:
    """The concurrency contract, actually exercised.

    Processes rather than threads because Tesseract runs its own OpenMP pool
    inside a page: stacking Python threads on top oversubscribes the machine
    (measured six times slower here) and a segfault in the C++ recogniser takes
    the interpreter with it.
    """
    pages = [synth.typed_page(lines=3), synth.typed_page(lines=4)]
    serial = [_ocr_text(p) for p in pages]
    with ProcessPoolExecutor(max_workers=2) as pool:
        parallel = list(pool.map(_ocr_text, pages))
    assert parallel == serial
