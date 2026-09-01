"""Tests for control-number detection.

Two kinds of case. The first builds a real PDF, extracts its words with
pdfplumber and asks what the module makes of them - the end-to-end path, and
the one that catches a coordinate convention getting flipped somewhere. The
rest hand-place stamps on synthetic pages, because the interesting failures
(page numbers wearing a stamp's clothes, two productions interleaved, a scanner
that reads 5 as S) are easier to build than to find.
"""

from __future__ import annotations

import pathlib

import pytest

from stackroom.ingest.bates import detect
from stackroom.model import Box, Page, Word

synth = pytest.importorskip("synth")
pdfplumber = pytest.importorskip("pdfplumber")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

BODY = [
    ("MEMORANDUM", 0.10, 0.09),
    ("correspondence", 0.10, 0.40),
    ("00012345", 0.30, 0.55),  # a long number in the body: not a stamp
]


def make_page(number: int, *stamps: tuple[str, float, float], body: bool = True) -> Page:
    """A page carrying nothing but the words we care about.

    Stamps are ``(text, x, y)`` in page-relative coordinates, origin top-left,
    exactly as ``model.Box`` defines them.
    """
    words = []
    if body:
        words += [Word(text=t, box=Box(x, y, 0.14, 0.012)) for t, x, y in BODY]
    words += [Word(text=t, box=Box(x, y, 0.14, 0.012)) for t, x, y in stamps]
    return Page(number=number, words=words)


def stamped(prefix: str, start: int, count: int, *, width: int = 6, x: float = 0.83,
            y: float = 0.946) -> list[Page]:
    return [
        make_page(i, (f"{prefix}{start + i - 1:0{width}d}", x, y))
        for i in range(1, count + 1)
    ]


def words_from_pdf(path: pathlib.Path) -> list[Page]:
    """Extract pages the way the real pipeline will: pdfplumber, page-relative
    boxes, word order preserved."""
    pages: list[Page] = []
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            pw, ph = page.width, page.height
            words = [
                Word(
                    text=w["text"],
                    box=Box.from_pdf_rect(w["x0"], w["top"], w["x1"], w["bottom"], pw, ph),
                )
                for w in page.extract_words()
            ]
            pages.append(Page(number=number, width_pt=pw, height_pt=ph, words=words))
    return pages


# --------------------------------------------------------------------------
# the whole path, on a real file
# --------------------------------------------------------------------------


def test_gap_in_a_real_production_is_reported(tmp_path: pathlib.Path) -> None:
    """The payload: three pages were withheld in full, and nothing else says so."""
    pdf = synth.born_digital_pdf(
        tmp_path / "release.pdf", pages=10, bates_prefix="ACME", bates_skip={7, 8, 9}
    )
    (series,) = detect(words_from_pdf(pdf))

    assert series.prefix == "ACME"
    assert series.width == 6
    assert series.confirmed
    assert series.coverage == 1.0
    assert series.first == "ACME000001"
    assert series.last == "ACME000013"
    assert series.gaps == [("ACME000007", "ACME000009")]
    assert series.missing_pages == 3
    assert series.page_map[1] == "ACME000001"
    assert series.page_map[7] == "ACME000010"
    assert len(series.page_map) == 10
    assert series.confidence > 0.8


def test_an_unstamped_production_yields_nothing(tmp_path: pathlib.Path) -> None:
    pdf = synth.born_digital_pdf(tmp_path / "plain.pdf", pages=5)
    assert detect(words_from_pdf(pdf)) == []


# --------------------------------------------------------------------------
# page numbers wearing a stamp's clothes
# --------------------------------------------------------------------------


def test_numbers_running_one_to_page_count_are_page_numbers() -> None:
    pages = [make_page(i, (f"{i:04d}", 0.48, 0.95)) for i in range(1, 6)]
    assert detect(pages) == []


def test_a_bare_series_that_outruns_the_page_count_is_a_stamp() -> None:
    pages = [make_page(i, (f"{851 + i:04d}", 0.83, 0.95)) for i in range(1, 8)]
    (series,) = detect(pages)
    assert series.prefix == ""
    assert series.first == "0852"
    assert series.last == "0858"


def test_a_prefixed_short_production_is_not_a_page_count() -> None:
    """ACME000001-ACME000012 over twelve pages is a small release, not furniture."""
    (series,) = detect(stamped("ACME", 1, 12))
    assert series.confirmed
    assert series.last == "ACME000012"


def test_padding_that_wanders_is_a_page_counter() -> None:
    """9997, 9998, 9999, 10000 ... a counter crossing a power of ten. A stamp
    is padded to a fixed width precisely so that it never does this."""
    pages = [make_page(i, (f"{9996 + i:d}", 0.48, 0.95)) for i in range(1, 9)]
    assert {len(p.words[-1].text) for p in pages} == {4, 5}
    assert detect(pages) == []


# --------------------------------------------------------------------------
# where the stamp sits
# --------------------------------------------------------------------------


def test_body_text_is_not_searched() -> None:
    """A long number in a sentence is a docket, an amount or a phone number."""
    pages = [make_page(i, (f"ACME{i:06d}", 0.30, 0.55)) for i in range(1, 6)]
    assert detect(pages) == []


def test_a_top_margin_stamp_is_found() -> None:
    (series,) = detect(stamped("TOP", 400, 5, y=0.04))
    assert series.first == "TOP000400"


def test_a_stamp_that_wanders_across_the_page_is_not_a_stamp() -> None:
    """Positional stability is the strongest signal there is."""
    xs = [0.05, 0.40, 0.80, 0.20, 0.62, 0.10]
    pages = [make_page(i, (f"ACME{i:06d}", xs[i - 1], 0.95)) for i in range(1, 7)]
    assert detect(pages) == []


def test_a_stamp_may_jitter_a_little() -> None:
    jitter = [0.0, 0.004, -0.003, 0.002, -0.004, 0.001, 0.003, -0.002]
    pages = [
        make_page(i, (f"ACME{i:06d}", 0.83 + jitter[i - 1], 0.946))
        for i in range(1, 9)
    ]
    (series,) = detect(pages)
    assert series.coverage == 1.0


def test_coverage_floor() -> None:
    """A number on a third of the pages is an exhibit label, not a production."""
    pages = [make_page(i) for i in range(1, 11)]
    for i in (1, 2, 3):
        pages[i - 1].words.append(
            Word(text=f"ACME{i:06d}", box=Box(0.83, 0.946, 0.14, 0.012))
        )
    assert detect(pages) == []


# --------------------------------------------------------------------------
# more than one production
# --------------------------------------------------------------------------


def test_two_up_scan_yields_two_series_and_no_phantom_gaps() -> None:
    """One image, two pages of the original: odd numbers down the left, even
    down the right. Merged they would zigzag; read alone the left-hand column
    looks like every second page is missing. Neither is true, and the second
    mistake is the dangerous one - it reports withholdings that never
    happened."""
    pages = [
        make_page(
            i,
            (f"ACME{2 * i - 1:06d}", 0.18, 0.946),
            (f"ACME{2 * i:06d}", 0.78, 0.946),
        )
        for i in range(1, 7)
    ]
    got = detect(pages)
    assert len(got) == 2
    assert {s.first for s in got} == {"ACME000001", "ACME000002"}
    assert all(s.gaps == [] for s in got), [s.gaps for s in got]
    assert all(s.coverage == 1.0 for s in got)


def test_interleaved_productions_stay_apart() -> None:
    pages = []
    for i in range(1, 9):
        pages.append(make_page(i, (f"AAA{100 + i:06d}", 0.83, 0.946),
                               (f"BBB{900 + i:06d}", 0.15, 0.946)))
    got = detect(pages)
    assert sorted(s.prefix for s in got) == ["AAA", "BBB"]
    assert all(s.gaps == [] for s in got)


def test_a_step_backwards_splits_the_run_rather_than_failing() -> None:
    """A second production stapled to the first, or a re-ordered scan. Not an
    error, and emphatically not a gap of minus five hundred."""
    values = [1000, 1001, 1002, 1003, 500, 501, 502, 503]
    pages = [make_page(i, (f"ACME{v:06d}", 0.83, 0.946)) for i, v in enumerate(values, 1)]
    got = detect(pages)
    assert len(got) == 2
    assert {s.first for s in got} == {"ACME001000", "ACME000500"}
    assert all(s.gaps == [] for s in got)
    assert all("backwards" in " ".join(s.notes) for s in got)


# --------------------------------------------------------------------------
# damage
# --------------------------------------------------------------------------


def test_a_digit_read_as_a_letter_does_not_become_a_phantom_gap() -> None:
    """``ACME000005`` came back as ``ACME00000S``. The wrong repair here does
    not just lose a page - it claims one was withheld."""
    pages = stamped("ACME", 1, 8)
    pages[4].words[-1] = Word(text="ACME00000S", box=Box(0.83, 0.946, 0.14, 0.012))
    (series,) = detect(pages)
    assert series.gaps == []
    assert series.page_map[5] == "ACME000005"
    assert series.coverage == 1.0


def test_damage_inside_the_digits_is_folded_back() -> None:
    pages = stamped("ACME", 1, 8)
    pages[2].words[-1] = Word(text="ACME0000O3", box=Box(0.83, 0.946, 0.14, 0.012))
    (series,) = detect(pages)
    assert series.page_map[3] == "ACME000003"
    assert series.gaps == []


def test_a_misread_digit_is_repaired_only_to_close_an_impossible_step() -> None:
    pages = stamped("ACME", 1, 8)
    pages[3].words[-1] = Word(text="ACME900004", box=Box(0.83, 0.946, 0.14, 0.012))
    (series,) = detect(pages)
    assert series.page_map[4] == "ACME000004"
    assert series.gaps == []
    assert any("repaired" in n for n in series.notes)


def test_a_real_gap_is_never_repaired_away() -> None:
    """``000010`` is one character from ``000019``, and the difference between
    them is nine pages nobody was given."""
    values = [1, 2, 10, 19, 20, 21]
    pages = [make_page(i, (f"ACME{v:06d}", 0.83, 0.946)) for i, v in enumerate(values, 1)]
    (series,) = detect(pages)
    assert series.gaps == [
        ("ACME000003", "ACME000009"),
        ("ACME000011", "ACME000018"),
    ]
    assert series.missing_pages == 15


def test_a_genuine_trailing_letter_is_not_treated_as_damage() -> None:
    pages = [make_page(i, (f"ACME{i:06d}A", 0.83, 0.946)) for i in range(1, 7)]
    (series,) = detect(pages)
    assert series.suffix == "A"
    assert series.first == "ACME000001A"
    assert series.last == "ACME000006A"


def test_a_number_that_never_changes_is_not_a_stamp() -> None:
    """A form number in the footer is stable, in the margin, on every page and
    perfectly monotonic. It is also the same number every time."""
    pages = [make_page(i, ("FORM-0042", 0.15, 0.95)) for i in range(1, 7)]
    assert detect(pages) == []


def test_the_form_number_does_not_hide_the_real_stamp() -> None:
    pages = [
        make_page(i, ("FORM-0042", 0.15, 0.95), (f"ACME{i:06d}", 0.83, 0.946))
        for i in range(1, 7)
    ]
    (series,) = detect(pages)
    assert series.prefix == "ACME"


def test_a_word_in_the_footer_is_not_a_stamp() -> None:
    pages = [
        make_page(i, ("CONFIDENTIAL", 0.40, 0.95), (f"ACME{i:06d}", 0.83, 0.946))
        for i in range(1, 6)
    ]
    (series,) = detect(pages)
    assert series.prefix == "ACME"


# --------------------------------------------------------------------------
# formats and shapes
# --------------------------------------------------------------------------


def test_separator_is_kept_so_the_stamp_round_trips() -> None:
    pages = [make_page(i, (f"DOJ-OGR-{12344 + i:08d}", 0.80, 0.95)) for i in range(1, 6)]
    (series,) = detect(pages)
    assert series.prefix == "DOJ-OGR-"
    assert series.width == 8
    assert series.first == "DOJ-OGR-00012345"


def test_lowercase_prefix() -> None:
    pages = [make_page(i, (f"d123-{i:03d}", 0.80, 0.95)) for i in range(1, 6)]
    (series,) = detect(pages)
    assert series.first == "D123-001"
    assert series.width == 3


# --------------------------------------------------------------------------
# too little to go on
# --------------------------------------------------------------------------


def test_one_page_is_a_candidate_and_never_a_confirmed_series() -> None:
    (series,) = detect([make_page(1, ("ACME000001", 0.83, 0.946))])
    assert series.confirmed is False
    assert series.confidence <= 0.35
    assert "unverified" in " ".join(series.notes)


def test_two_pages_are_still_not_enough() -> None:
    got = detect(stamped("ACME", 1, 2))
    assert got and all(not s.confirmed for s in got)


def test_three_pages_are() -> None:
    got = detect(stamped("ACME", 1, 3))
    assert got and all(s.confirmed for s in got)


def test_no_pages_no_answer() -> None:
    assert detect([]) == []
    assert detect([make_page(1, body=False)]) == []
