"""Does the quality module say what a person looking at the page would say?

Every assertion here is a human judgement written down: *this* page is readable,
*that* one is a photograph, *that* one is a failure the archive must admit to.
The fixtures come from ``tests/synth.py`` and are OCR'd with the real Tesseract,
because the thing under test is a decision about real OCR output and mocking it
would only test our imagination.

Running this file directly prints the whole measured metric table::

    cd stackroom && PYTHONPATH=src python tests/test_quality.py

That table is where the thresholds in ``ingest/quality.py`` came from. If a
future contributor changes a threshold, this is the output to check it against.
"""

# ruff: noqa: RUF001
# The samples below are deliberately written in four alphabets; the
# ambiguous-character rule has nothing to catch here.

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

from stackroom.ingest.quality import (
    LOW_STOPWORD_RATIO,
    ComponentProfile,
    component_profile,
    embedded_layer_broken,
    ink_coverage,
    score_page,
)
from stackroom.lang import (
    SCRIPTS_WITH_STOPWORDS,
    detect_language,
    is_garbage_token,
    normalize_token,
    script_of,
    stopword_ratio,
    stopwords_apply,
)
from stackroom.model import Box, PageVerdict, Word

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth  # the fixture generator lives beside this file, not on the path

pytesseract = pytest.importorskip("pytesseract")


def tokens(text: str) -> list[str]:
    """Whitespace-split a sample. Kept out of the literals so the samples read
    as sentences rather than as sixty quoted strings."""
    return text.split()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def pictorial_page(width: int = 1275, height: int = 1650) -> Image.Image:
    """A page that is one photograph: smooth, large-scale, no text.

    Built here rather than in ``synth.py`` because it must be deterministic -
    ``Image.effect_noise`` is unseeded, and the build is required to be
    reproducible. Upsampling coarse noise and blurring it gives the property
    that matters: a few large connected regions rather than a fog of specks.
    """
    rng = np.random.default_rng(20_240_831)
    coarse = rng.integers(0, 256, size=(height // 8, width // 8), dtype=np.uint8)
    return (
        Image.fromarray(coarse)
        .resize((width, height), Image.BICUBIC)
        .filter(ImageFilter.GaussianBlur(8))
        .convert("L")
    )


_OCR_CACHE: dict[int, list[Word]] = {}


def ocr(image: Image.Image) -> list[Word]:
    """OCR a page into ``Word``s, exactly as ``ingest/ocr.py`` will.

    Only ``level == 5`` rows carry a real confidence; every other level reports
    -1 and would poison any statistic computed over it.
    """
    key = id(image)
    if key in _OCR_CACHE:
        return _OCR_CACHE[key]
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    width, height = image.size
    words: list[Word] = []
    for i in range(len(data["text"])):
        if data["level"][i] != 5:
            continue
        text = data["text"][i]
        if not text.strip():
            continue
        words.append(
            Word(
                text=text,
                box=Box(
                    data["left"][i] / width,
                    data["top"][i] / height,
                    data["width"][i] / width,
                    data["height"][i] / height,
                ),
                conf=int(float(data["conf"][i])),
                line=int(data["line_num"][i]),
            )
        )
    _OCR_CACHE[key] = words
    return words


@pytest.fixture(scope="module")
def pages() -> dict[str, Image.Image]:
    """Every page image the suite judges, generated once."""
    return {
        "clean": synth.typed_page(),
        "grain": synth.typed_page(grain=0.9),
        "rotated": synth.typed_page(rotate=90),
        "blank": synth.blank_page(),
        "noise": synth.noise_page(),
        "photo_block": synth.typed_page(photo_block=True),
        "inverted": synth.typed_page(invert=True),
        "pictorial": pictorial_page(),
    }


# --------------------------------------------------------------------------
# the verdicts a human would give
# --------------------------------------------------------------------------


def test_clean_page_is_good(pages: dict[str, Image.Image]) -> None:
    quality = score_page(ocr(pages["clean"]), pages["clean"])
    assert quality.verdict is PageVerdict.GOOD
    assert quality.word_count > 200
    assert quality.stopword_ratio > 0.30
    assert quality.median_conf > 90
    assert quality.garbage_ratio < 0.08
    # A good page has nothing to complain about.
    assert quality.reasons == []


def test_heavy_grain_is_unreadable(pages: dict[str, Image.Image]) -> None:
    """Scan grain that defeats OCR entirely. There is ink; we read nothing.

    This is the page the archive must never publish silently: a reader who
    searches for a phrase on it gets no hit and concludes the phrase is not
    there.
    """
    quality = score_page(ocr(pages["grain"]), pages["grain"])
    assert quality.verdict is PageVerdict.UNREADABLE
    assert quality.verdict.is_failure
    assert quality.word_count == 0
    assert quality.ink_coverage > 0.05
    assert any("0 words" in r for r in quality.reasons)


def test_rotated_page_is_suspect(pages: dict[str, Image.Image]) -> None:
    """A page turned on its side: plenty of "words", none of them language."""
    quality = score_page(ocr(pages["rotated"]), pages["rotated"])
    assert quality.verdict is PageVerdict.SUSPECT
    assert quality.word_count > 100  # OCR is confident it read something
    assert quality.stopword_ratio < 0.10  # and it is not language
    assert len(quality.reasons) >= 2  # at least two independent signals


def test_blank_page_is_blank_not_a_failure(pages: dict[str, Image.Image]) -> None:
    quality = score_page(ocr(pages["blank"]), pages["blank"])
    assert quality.verdict is PageVerdict.BLANK
    assert not quality.verdict.is_failure
    assert quality.ink_coverage < 0.005


def test_noise_page_is_unreadable_not_pictorial(pages: dict[str, Image.Image]) -> None:
    """50% ink and no words - but it is not a photograph.

    The suggested rule was "more than 40% ink with high local variance means
    pictorial". Pure noise has the highest local variance a page can have, so
    that rule gets this page exactly backwards. The component profile settles
    it: a photograph concentrates its ink in a few large components (measured
    0.45 in the largest), noise has no large structure at all (0.002).
    """
    quality = score_page(ocr(pages["noise"]), pages["noise"])
    assert quality.verdict is PageVerdict.UNREADABLE
    assert quality.ink_coverage > 0.40
    profile = component_profile(pages["noise"])
    assert profile.largest_share < 0.05
    assert profile.count > 1000


def test_pictorial_page_is_not_reported_as_a_failure() -> None:
    page = pictorial_page()
    quality = score_page(ocr(page), page)
    assert quality.verdict is PageVerdict.PICTORIAL
    assert not quality.verdict.is_failure
    profile = component_profile(page)
    assert profile.largest_share > 0.25
    assert profile.count < 1000
    assert any("picture" in r for r in quality.reasons)


def test_photo_block_page_is_good(pages: dict[str, Image.Image]) -> None:
    """A typed page with a photograph pasted into it is still a typed page."""
    quality = score_page(ocr(pages["photo_block"]), pages["photo_block"])
    assert quality.verdict is PageVerdict.GOOD
    # The photo dominates the ink, and that must not outvote 250 good words.
    assert component_profile(pages["photo_block"]).largest_share > 0.25


# --------------------------------------------------------------------------
# inversion
# --------------------------------------------------------------------------


def test_inverted_page_is_detected_and_not_reported_as_98_percent_ink(
    pages: dict[str, Image.Image],
) -> None:
    """White text on black: the ink metric inverts unless we catch it.

    The measured page is 95.8% dark. Reporting that as ink would make a
    perfectly readable page look like a solid black rectangle.
    """
    coverage, was_inverted = ink_coverage(pages["inverted"])
    assert was_inverted is True
    assert coverage < 0.10, "ink must be measured on the text, not the background"
    upright, _ = ink_coverage(pages["clean"])
    assert coverage == pytest.approx(upright, abs=0.005)

    quality = score_page(ocr(pages["inverted"]), pages["inverted"])
    assert quality.verdict is PageVerdict.GOOD
    assert quality.ink_coverage < 0.10
    assert any("dark page" in r for r in quality.reasons)


def test_solid_dark_page_is_not_mistaken_for_inverted_text() -> None:
    """A fully blacked-out page is dark for the opposite reason.

    Inverting it would leave 0% ink and the page would be reported as blank -
    the most misleading answer available for a page that was withheld in full.
    """
    solid = Image.new("L", (600, 800), 8)
    coverage, was_inverted = ink_coverage(solid)
    assert was_inverted is False
    assert coverage > 0.99


def test_upright_page_is_never_inverted(pages: dict[str, Image.Image]) -> None:
    for name in ("clean", "blank", "noise", "pictorial", "photo_block"):
        _, was_inverted = ink_coverage(pages[name])
        assert was_inverted is False, name


# --------------------------------------------------------------------------
# score_page contract
# --------------------------------------------------------------------------


def test_every_field_is_populated(pages: dict[str, Image.Image]) -> None:
    quality = score_page(ocr(pages["rotated"]), pages["rotated"])
    for field in (
        "verdict",
        "word_count",
        "median_conf",
        "low_conf_fraction",
        "stopword_ratio",
        "garbage_ratio",
        "mean_word_length",
        "ink_coverage",
        "reasons",
    ):
        assert getattr(quality, field) is not None
    assert quality.median_conf > 0
    assert quality.mean_word_length > 0


def test_reasons_are_written_for_a_person(pages: dict[str, Image.Image]) -> None:
    """These strings reach the operator's console and the reader's page."""
    seen = 0
    for name in ("grain", "rotated", "blank", "noise", "pictorial", "inverted"):
        for reason in score_page(ocr(pages[name]), pages[name]).reasons:
            seen += 1
            assert "_" not in reason, reason  # not ERR_OCR_LOW_CONF
            assert reason[0].islower() or reason[0].isdigit(), reason  # a phrase
            assert len(reason.split()) >= 4, reason  # a sentence, not a code
            assert len(reason) < 120, reason
            assert reason == reason.rstrip("."), reason  # no full stops; these
            # strings are placed in lists and headings, not in paragraphs
    assert seen >= 6


def test_embedded_layer_confidence_is_not_averaged_in() -> None:
    """``CONF_UNKNOWN`` is -1 and must never reach a statistic.

    An embedded text layer has no confidences. Averaging the -1 sentinels would
    score a perfect page at -1 and condemn every born-digital PDF in the
    collection.
    """
    words = [Word(text=t, box=Box(0, 0, 0.1, 0.02)) for t in synth.LOREM * 3]
    quality = score_page(words)
    assert quality.median_conf == 0.0
    assert quality.low_conf_fraction == 0.0
    assert quality.verdict is PageVerdict.GOOD


def test_page_of_numbers_is_not_condemned() -> None:
    """A budget table has no function words and is perfectly readable.

    Digits are kept out of the denominator of the stopword ratio for exactly
    this reason; the guard is that so few *alphabetic* tokens remain that the
    stopword signal never fires.
    """
    rows = [f"{n:,}" for n in range(1_000, 1_120)]
    words = [Word(text=t, box=Box(0, 0, 0.05, 0.01), conf=91) for t in rows]
    quality = score_page(words)
    assert quality.verdict is PageVerdict.GOOD


def test_broken_embedded_layer_is_flagged_when_glyphs_were_present() -> None:
    """A PDF that draws characters and yields gibberish must be re-OCR'd."""
    gibberish = tokens("xdfg wJoJ aq} YONS pue ~qq zzxk lkjh mnbv qwrt plkm zxcv bnmq wert")
    words = [Word(text=t, box=Box(0, 0, 0.05, 0.01), conf=95) for t in gibberish * 3]
    with_glyphs = score_page(words, had_glyphs=True)
    assert with_glyphs.verdict is PageVerdict.SUSPECT
    assert any("re-ocr" in r.lower() for r in with_glyphs.reasons)


def test_no_image_and_no_words_says_so() -> None:
    """Without a picture of the page we cannot tell empty from failed."""
    empty = score_page([])
    assert empty.verdict is PageVerdict.BLANK
    assert any("no page image" in r for r in empty.reasons)
    had_text = score_page([], had_glyphs=True)
    assert had_text.verdict is PageVerdict.UNREADABLE


def test_languages_argument_is_honoured() -> None:
    german = tokens(
        "Der Ausschuss hat die Unterlagen des Ministeriums für den Zeitraum von "
        "März bis September angefordert und nach elf Monaten eine Antwort erhalten "
        "die aus vierhundert Seiten bestand von denen ein Teil geschwärzt war"
    )
    words = [Word(text=t, box=Box(0, 0, 0.05, 0.01), conf=88) for t in german]
    assert score_page(words, languages=["de"]).verdict is PageVerdict.GOOD
    # Asked about the wrong language, the page is still judged as the language
    # it is. The declared list is a prior and may only ever help.
    wrong = score_page(words, languages=["ru"])
    assert wrong.stopword_ratio > 0.30
    assert wrong.verdict is PageVerdict.GOOD
    assert wrong.stopword_ratio == score_page(words, languages=["de"]).stopword_ratio


def test_a_declared_language_can_never_make_a_page_look_worse() -> None:
    """The defect this rule exists to stop, at the page level.

    A born-digital Russian page in a collection declared ``["eng"]`` used to
    score a stopword ratio of exactly zero - not because there was anything
    wrong with it, but because it was scored against a list of English words.
    With ``had_glyphs`` that was a single-signal condemnation: *the PDF's own
    text layer does not read as language; re-OCR this page*. The page was then
    read again from the pixels by a recogniser that had been told to expect
    English, and the archive published the worse of the two readings - or told
    its readers that search could not find anything on a page whose text was
    perfect.
    """
    russian = tokens(
        "В соответствии с требованиями закона все документы были переданы в "
        "комиссию для рассмотрения и для последующего опубликования на сайте "
        "ведомства а ответ поступил только через одиннадцать месяцев"
    )
    words = [Word(text=t, box=Box(0, 0, 0.05, 0.01)) for t in russian * 3]
    for declared in (None, ["eng"], ["eng", "fra"], ["deu"]):
        quality = score_page(words, languages=declared, had_glyphs=True)
        assert quality.verdict is PageVerdict.GOOD, declared
        assert quality.stopword_ratio > 0.25, declared
        assert not any("re-ocr" in r.lower() for r in quality.reasons), declared


def test_declaring_languages_is_a_prior_and_not_a_filter() -> None:
    """It may raise a page's score. It may never lower it."""
    bilingual = GERMAN + tokens("the office of the director and the contracting authority")
    alone = stopword_ratio(bilingual)
    # Two declared languages let a genuinely bilingual page score for both
    # halves at once, which is the whole of what a declared list buys.
    assert stopword_ratio(bilingual, ["de", "en"]) > alone
    # And no declaration, right or wrong, can drag any page below the
    # language-agnostic answer.
    for declared in (["de"], ["en"], ["ru"], ["zh"], ["ara"], []):
        assert stopword_ratio(bilingual, declared) >= alone, declared


# --------------------------------------------------------------------------
# connected components
# --------------------------------------------------------------------------


def test_component_labelling_is_correct_on_a_known_shape() -> None:
    """Three separate blobs and one L-shape that must stay a single component."""
    canvas = np.full((60, 60), 255, dtype=np.uint8)
    canvas[5:10, 5:10] = 0  # a square
    canvas[5:10, 40:45] = 0  # another square
    canvas[40:50, 5:8] = 0  # the vertical arm of the L
    canvas[47:50, 5:20] = 0  # the horizontal arm, touching it
    canvas[30:34, 30:34] = 0  # a fourth blob
    profile = component_profile(canvas)
    assert profile.count == 4
    # The L is the biggest: 30 + 45 - 9 overlapping = 66 of the 132 ink pixels.
    assert profile.largest_share == pytest.approx(66 / 132, abs=0.01)
    assert profile.median_height == pytest.approx(5.5, abs=0.5)


def test_component_profile_separates_text_from_picture(
    pages: dict[str, Image.Image],
) -> None:
    text = component_profile(pages["clean"])
    picture = component_profile(pages["pictorial"])
    assert text.count > 1_000  # many small marks
    assert picture.count < 1_000  # a few large ones
    assert text.median_height < picture.median_height
    assert text.largest_share < 0.01 < picture.largest_share
    # Typed text is one size, so its heights barely spread.
    assert text.height_spread < 0.5 < picture.height_spread


def test_component_profile_is_fast_enough(pages: dict[str, Image.Image]) -> None:
    """The build runs this on every page, so the worst case has a budget.

    Pure noise is the worst input this can be given: 526,000 ink runs on a
    letter page. Measured here at 67 ms; the budget is 150 ms.

    Best of three, because this is a wall clock on a machine that may be doing
    something else: the fastest run is the one that measures the code rather
    than the contention, and this assertion exists to catch an algorithmic
    regression, not a busy afternoon.
    """
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        component_profile(pages["noise"])
        timings.append((time.perf_counter() - start) * 1000)
    assert min(timings) < 150, f"connected components took {min(timings):.0f} ms"


def test_component_profile_of_an_empty_page_is_empty(
    pages: dict[str, Image.Image],
) -> None:
    profile = component_profile(pages["blank"])
    assert profile == ComponentProfile(scale=profile.scale)


# --------------------------------------------------------------------------
# the embedded text layer
# --------------------------------------------------------------------------


def test_embedded_layer_broken_detects_a_dead_tounicode_map() -> None:
    broken, reasons = embedded_layer_broken(" " * 8, 80, None)
    assert broken
    assert any("unmapped" in r for r in reasons)


def test_embedded_layer_broken_detects_dropped_glyphs() -> None:
    broken, reasons = embedded_layer_broken("The Commission", 900, None)
    assert broken
    assert any("could be decoded" in r for r in reasons)


def test_embedded_layer_broken_detects_missing_spaces() -> None:
    broken, reasons = embedded_layer_broken("TheCommissionRequestedAllCorrespondence" * 3, 0, None)
    assert broken
    assert any("spacing" in r for r in reasons)


def test_embedded_layer_broken_believes_ocr_over_the_layer() -> None:
    layer = " ".join(tokens("qwrt zxcv plkm bnmq yuio wert lkjh mnbv") * 4)
    ocr_words = [
        Word(text=t, box=Box(0, 0, 0.05, 0.01), conf=94)
        for t in (synth.LOREM * 2)
    ]
    broken, reasons = embedded_layer_broken(layer, len(layer), ocr_words)
    assert broken
    assert any("OCR reads this page as language" in r for r in reasons)


def test_healthy_embedded_layer_is_left_alone() -> None:
    text = " ".join(synth.LOREM * 3)
    broken, reasons = embedded_layer_broken(text, len(text), None)
    assert not broken
    assert reasons == []


def test_empty_page_has_no_broken_layer() -> None:
    assert embedded_layer_broken("", 0, None) == (False, [])
    broken, _ = embedded_layer_broken("", 200, None)
    assert broken  # 200 glyphs drawn and nothing extracted


# --------------------------------------------------------------------------
# lang.py
# --------------------------------------------------------------------------

RUSSIAN = tokens(
    "В соответствии с требованиями закона все документы были переданы в "
    "комиссию для рассмотрения и для последующего опубликования на сайте"
)

GERMAN = tokens(
    "Der Ausschuss hat die Unterlagen des Ministeriums für den Zeitraum von "
    "März bis September angefordert und nach elf Monaten eine Antwort erhalten"
)

# One character per token, as ARCHITECTURE.md requires for CJK so that the
# search index's word offsets stay aligned with the page HTML.
CHINESE = list("委员会要求提供主任办公室与承包机关之间在三月至九月期间的所有往来函件")

GARBAGE = tokens("pue Jo ay} Se YONS wJoJ aq} ll1111 xdfghjklmnpqrstvwxz ~~~ eeee")


def test_stopword_ratio_separates_prose_from_garbage() -> None:
    assert stopword_ratio(GERMAN) > 0.30
    assert stopword_ratio(RUSSIAN) > 0.20
    assert stopword_ratio(GARBAGE) < 0.20


def test_detect_language_finds_the_right_language() -> None:
    assert detect_language(GERMAN)[0] == "de"
    assert detect_language(RUSSIAN)[0] == "ru"
    assert detect_language(CHINESE)[0] == "zh"
    assert detect_language(tokens("the of and to a in that is was it for on"))[0] == "en"


def test_detect_language_is_unconfident_about_garbage() -> None:
    """Low confidence is itself the signal: this is probably not language."""
    code, confidence = detect_language(GARBAGE)
    assert confidence < 0.15, (code, confidence)
    assert detect_language([])[0] == "und"


def test_detect_language_confidence_rises_with_evidence() -> None:
    short = detect_language(tokens("der die und in den"))[1]
    long = detect_language(GERMAN * 4)[1]
    assert long > short


def test_chinese_is_not_garbage() -> None:
    """The rule that matters most.

    The vowel/consonant heuristic is Latin-specific. Applied blindly to Han it
    reports 100% garbage, and the tool then declares an entire Chinese corpus
    unreadable - worse than having no quality check at all.
    """
    assert script_of("".join(CHINESE)) == "han"
    for character in CHINESE:
        assert not is_garbage_token(character, "han"), character
    garbage_rate = sum(is_garbage_token(c, "han") for c in CHINESE) / len(CHINESE)
    assert garbage_rate == 0.0

    # And through the full page path, which is where it would actually bite.
    words = [Word(text=c, box=Box(0, 0, 0.01, 0.01), conf=88) for c in CHINESE * 3]
    quality = score_page(words)
    assert quality.garbage_ratio == 0.0
    assert quality.verdict is PageVerdict.GOOD


def test_japanese_and_korean_are_not_garbage_either() -> None:
    for text, script in (("ひらがなカタカナ", "han"), ("한글문서", "han")):
        assert script_of(text) == script
        for ch in text:
            assert not is_garbage_token(ch, script), ch


def test_arabic_and_greek_skip_the_vowel_rule() -> None:
    """Arabic is an abjad: short vowels are not written, so the rule cannot
    apply. Greek has vowels of its own and keeps the rule."""
    assert script_of("العربية") == "arabic"
    assert not is_garbage_token("العربية", "arabic")
    assert script_of("Ελλάδα") == "greek"
    assert not is_garbage_token("Ελλάδα", "greek")
    assert is_garbage_token("κνστρβ", "greek")  # no vowels at all


HINDI = tokens(
    "आयोग ने निदेशक के कार्यालय और ठेकेदार प्राधिकरण के बीच मार्च से सितंबर तक की "
    "अवधि के सभी पत्राचार की मांग की थी और उत्तर ग्यारह महीने बाद आया"
)


def test_stopwords_apply_knows_which_pages_it_can_judge() -> None:
    """The test that decides whether a zero means anything.

    A zero stopword ratio has two possible meanings - *this is garbage* and
    *we have no words for this alphabet* - and they are not interchangeable:
    one of them re-OCRs a page and the other must not.
    """
    for judgeable in ("hello world of the", "Привет мир и не на", "委员会要求提供", "中文abcdef"):
        assert stopwords_apply(judgeable), judgeable
    for unjudgeable in ("नमस्ते दुनिया", "สวัสดีชาวโลก", "שלום עולם", "العربية", "Ελλάδα",
                        "ひらがなカタカナ", "한글문서", "გამარჯობა", ""):
        assert not stopwords_apply(unjudgeable), unjudgeable


def test_stopwords_apply_is_not_the_dominant_script_test() -> None:
    """``script_of`` is the wrong vocabulary for this question, both ways.

    Kana and Hangul are folded into "han" because the vowel rule treats all
    three alike, and the "zh" list is a hundred Han *characters* that no
    Japanese page contains. Devanagari and Thai have no name at all and come
    back "mixed" - the same answer a genuinely bilingual Latin/Cyrillic page
    gives, and that page must still be judged, because OCR spraying Cyrillic
    across a Latin page is the damage this whole module is looking for.
    """
    assert script_of("ひらがなカタカナ") in SCRIPTS_WITH_STOPWORDS
    assert not stopwords_apply("ひらがなカタカナ")
    assert script_of("नमस्ते दुनिया") == "mixed"
    assert not stopwords_apply("नमस्ते दुनिया")
    assert script_of("Привет hello мир world") == "mixed"
    assert stopwords_apply("Привет hello мир world")


def test_a_page_in_an_unjudgeable_script_is_reported_as_unjudged() -> None:
    """Not garbage. Unjudged. It is a different and honest answer.

    With ``had_glyphs`` this is the exact shape of the reported defect: a
    born-digital page whose text layer is perfect, condemned by a word list
    that has no words for its alphabet.
    """
    words = [Word(text=t, box=Box(0, 0, 0.05, 0.01)) for t in HINDI * 3]
    quality = score_page(words, had_glyphs=True)
    assert quality.verdict is PageVerdict.GOOD
    assert quality.garbage_ratio == 0.0
    assert any("no stopword list" in r for r in quality.reasons)
    # And it says so instead of counting the zero against the page.
    assert not any("common words" in r for r in quality.reasons)


def test_japanese_and_korean_pages_are_unjudged_rather_than_condemned() -> None:
    """The case the coarse script test got wrong: kana and Hangul are "han"."""
    for sample in ("ひらがな カタカナ の 文書 です から また ため こと もの", "한글 문서 그리고 또한 이것 저것 무엇"):
        page = tokens(sample) * 4
        words = [Word(text=t, box=Box(0, 0, 0.05, 0.01)) for t in page]
        quality = score_page(words, had_glyphs=True)
        assert quality.verdict is PageVerdict.GOOD, sample
        assert any("no stopword list" in r for r in quality.reasons), sample


def test_a_broken_text_layer_needs_more_than_a_low_stopword_ratio() -> None:
    """One signal condemned a page here, and one signal was not enough.

    A page of proper nouns has no function words and is perfectly readable.
    Before, ``had_glyphs`` turned that single fact into *re-OCR this page*; now
    it has to be corroborated, exactly like every other verdict in this module.
    """
    names = tokens(
        "Ridgeway Brennan Okonkwo Vasquez Nakamura Achebe Mbeki Anand Silva Rossi "
        "Costa Moreau Dubois Weber Bauer Novak Horvat Kovacs Nagy Ahmed Khan Malik "
        "Reyes Ortiz Nunez Vega Haddad Farah Osei Mensah"
    )
    words = [Word(text=t, box=Box(0, 0, 0.05, 0.01), conf=93) for t in names]
    quality = score_page(words, had_glyphs=True)
    assert quality.stopword_ratio < LOW_STOPWORD_RATIO
    assert quality.garbage_ratio == 0.0
    assert quality.verdict is PageVerdict.GOOD
    assert not any("re-ocr" in r.lower() for r in quality.reasons)

    # The layer that really is broken still gets the specific diagnosis, and it
    # gets it because a second signal agrees rather than on the ratio alone.
    gibberish = tokens("xdfg wJoJ aq} YONS pue ~qq zzxk lkjh mnbv qwrt plkm zxcv bnmq wert")
    broken = [Word(text=t, box=Box(0, 0, 0.05, 0.01), conf=95) for t in gibberish * 3]
    verdict = score_page(broken, had_glyphs=True)
    assert verdict.verdict is PageVerdict.SUSPECT
    assert any("re-ocr" in r.lower() for r in verdict.reasons)
    assert len(verdict.reasons) >= 3  # the diagnosis plus the signals behind it


def test_no_writing_system_is_condemned_wholesale() -> None:
    """The failure this module most needs to avoid, across every script.

    A quality check that cannot read a script must say "no opinion", never
    "garbage". Hebrew has no name in this vocabulary and is grouped with the
    other abjad; Devanagari, Thai and Georgian have no name at all and come
    back as "mixed", which switches the Latin-shaped rules off.
    """
    for sample in ("שלום עולם", "नमस्ते दुनिया", "สวัสดีชาวโลก", "გამარჯობა"):
        for token in sample.split():
            assert not is_garbage_token(token, script_of(token)), token


def test_cyrillic_vowel_rule_uses_cyrillic_vowels() -> None:
    assert not is_garbage_token("документы", "cyrillic")
    assert not is_garbage_token("комиссию", "cyrillic")
    assert is_garbage_token("бвгджзкл", "cyrillic")


def test_garbage_rules_from_the_literature() -> None:
    assert is_garbage_token("a" * 21)  # length > 20
    assert not is_garbage_token("correspondence")  # length 14, fine
    assert is_garbage_token("balllloon")  # four identical in a row
    assert is_garbage_token("xdfghjklmn")  # no vowels
    assert is_garbage_token("aeiouae")  # all vowels
    assert is_garbage_token("a-b-c-d")  # more than two punctuation marks
    for word in ("the", "strengths", "rhythms", "September", "withheld"):
        assert not is_garbage_token(word), word
    # Short forms must survive: the vowel rule does not apply below four letters.
    for abbreviation in ("Mr", "St", "vs", "pp", "II", "b7"):
        assert not is_garbage_token(abbreviation), abbreviation
    # Numbers are not garbage.
    for number in ("1985", "4,500", "12.5"):
        assert not is_garbage_token(number), number


def test_normalize_token() -> None:
    assert normalize_token("“Hello,”") == "hello"
    assert normalize_token("l’homme") == "l'homme"  # apostrophes fold to one
    assert normalize_token("ﬁle") == "file"  # NFKC splits the ligature
    assert normalize_token("STRASSE") == "strasse"
    assert normalize_token("--") == ""
    assert normalize_token("") == ""


def test_script_of() -> None:
    assert script_of("hello world") == "latin"
    assert script_of("Привет") == "cyrillic"
    assert script_of("Ελλάδα") == "greek"
    assert script_of("العربية") == "arabic"
    assert script_of("中文字") == "han"
    assert script_of("中文abcdef") == "mixed"
    assert script_of("(b)(7)(C)") == "latin"  # digits and punctuation do not vote
    assert script_of("") == "latin"


def test_stopword_ratio_ignores_numbers() -> None:
    """A page of figures should not be dragged to zero by its figures."""
    prose = tokens("the office of the director and the contracting authority")
    with_numbers = prose + [f"{n:,}" for n in range(2_000, 2_060)]
    assert stopword_ratio(with_numbers) == pytest.approx(stopword_ratio(prose))


def test_stopword_ratio_with_declared_languages() -> None:
    mixed = GERMAN + tokens("the office of the director and the contracting authority")
    assert stopword_ratio(mixed, ["de", "en"]) > stopword_ratio(mixed, ["de"])
    # German declared as Russian is still German. This used to be 0.0, and that
    # zero is what condemned every page in an undeclared language.
    assert stopword_ratio(GERMAN, ["ru"]) == stopword_ratio(GERMAN)
    assert stopword_ratio(GERMAN, ["ru"]) > 0.30
    # A code with no word list drops out rather than emptying the answer.
    assert stopword_ratio(GERMAN, ["ara", "hin"]) == stopword_ratio(GERMAN)


# --------------------------------------------------------------------------
# calibration table
# --------------------------------------------------------------------------


def _metrics_row(name: str, image: Image.Image) -> str:
    words = ocr(image)
    started = time.perf_counter()
    quality = score_page(words, image)
    elapsed = (time.perf_counter() - started) * 1000
    profile = component_profile(image)
    _, inverted = ink_coverage(image)
    lengths = [len(w.text) for w in words] or [0]
    return (
        f"{name:12s} {quality.verdict.value:11s} {quality.word_count:5d} "
        f"{quality.ink_coverage * 100:6.2f}% {'yes' if inverted else ' no':>4s} "
        f"{quality.median_conf:5.1f} {quality.low_conf_fraction:5.2f} "
        f"{quality.stopword_ratio:5.2f} {quality.garbage_ratio:5.2f} "
        f"{quality.mean_word_length:5.2f} {statistics.median(lengths):5.1f} "
        f"{profile.count:6d} {profile.median_height:5.1f} {profile.height_spread:5.2f} "
        f"{profile.largest_share:6.3f} {profile.top10_share:6.3f} {elapsed:5.0f}"
    )


def calibration_table() -> list[str]:
    """The measured metrics behind every threshold in ``ingest/quality.py``."""
    header = (
        f"{'fixture':12s} {'verdict':11s} {'words':>5s} {'ink':>7s} {'inv':>4s} "
        f"{'medc':>5s} {'lowc':>5s} {'stop':>5s} {'garb':>5s} {'mlen':>5s} "
        f"{'mdln':>5s} {'comps':>6s} {'medH':>5s} {'sprd':>5s} {'top1':>6s} "
        f"{'top10':>6s} {'ms':>5s}"
    )
    rows = [header, "-" * len(header)]
    cases: Sequence[tuple[str, Image.Image]] = [
        ("clean", synth.typed_page()),
        ("grain 0.35", synth.typed_page(grain=0.35)),
        ("grain 0.5", synth.typed_page(grain=0.5)),
        ("grain 0.9", synth.typed_page(grain=0.9)),
        ("rotate 3", synth.typed_page(rotate=3)),
        ("rotate 90", synth.typed_page(rotate=90)),
        ("blank", synth.blank_page()),
        ("noise", synth.noise_page()),
        ("photo block", synth.typed_page(photo_block=True)),
        ("pictorial", pictorial_page()),
        ("inverted", synth.typed_page(invert=True)),
        ("redacted", synth.typed_page(redactions=[(140, 300, 1100, 700)])),
        ("scan border", synth.typed_page(scan_border=True)),
    ]
    rows.extend(_metrics_row(name, image) for name, image in cases)
    return rows


def test_calibration_table_runs(capsys: pytest.CaptureFixture[str]) -> None:
    """Keeps the calibration path working; run with ``-s`` to read it."""
    rows = calibration_table()
    assert len(rows) == 15
    with capsys.disabled():
        print()
        print("\n".join(rows))


if __name__ == "__main__":  # pragma: no cover - the contributor's entry point
    print("\n".join(calibration_table()))
