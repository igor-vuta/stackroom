"""Tests for :mod:`stackroom.ingest.pdf`.

Everything here runs against a PDF the test built a moment ago - `tests/synth.py`
for the ordinary shapes, a few bytes of hand-written PDF for the pathological
ones that no generator produces on purpose.

The load-bearing assertions are the ones about *draw order* and *coordinates*.
Draw order is the only evidence that separates a failed redaction from a
highlight, and a box in the wrong place is worse than no box: it tells a reader
that the archive knows where something is when it does not.
"""

from __future__ import annotations

import sys
from itertools import pairwise
from pathlib import Path

import pytest

import synth
from stackroom.ingest import pdf as pdfmod
from stackroom.ingest.pdf import (
    MIN_WORDS_FOR_STOPWORDS,
    NO_ZORDER,
    PdfDamagedError,
    PdfEncryptedError,
    RawChar,
    document_meta,
    embedded_text_verdict,
    open_pdf,
    page_count,
    read_page,
)
from stackroom.model import Box, Word
from synth import RedactionSpec

LETTER_W = 612.0
LETTER_H = 792.0

# `synth.born_digital_pdf` draws its heading with `drawString(72, height - 72)`,
# so the one thing we always know about a synthetic page is where "MEMORANDUM"
# starts: 72pt in from the left, on a baseline 72pt down from the top.
HEADING_LEFT_PT = 72.0
HEADING_BASELINE_FROM_TOP_PT = 72.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def rotated(source: Path, target: Path, degrees: int) -> Path:
    """Copy *source* with ``/Rotate`` set, without re-drawing anything."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page in reader.pages:
        page.rotate(degrees)
        writer.add_page(page)
    with target.open("wb") as fh:
        writer.write(fh)
    return target


def encrypted(source: Path, target: Path, user_password: str) -> Path:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=user_password, owner_password="owner")
    with target.open("wb") as fh:
        writer.write(fh)
    return target


def hand_written_pdf(path: Path, font: bytes, content: bytes) -> Path:
    """A one-page PDF assembled byte by byte.

    reportlab will not emit a font whose ``/Differences`` map every letter into
    the private-use area, and that is exactly the file we need to test against:
    it renders perfectly and copies as nonsense, which is the commonest broken
    text layer in a real release.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        font,
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    startxref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        startxref,
    )
    path.write_bytes(bytes(out))
    return path


def private_use_pdf(path: Path) -> Path:
    """A page whose every glyph decodes into U+E0xx."""
    differences = b" ".join(b"/uni%04X" % (0xE000 + i) for i in range(26))
    font = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding "
        b"<< /Type /Encoding /Differences [65 " + differences + b"] >> >>"
    )
    line = b"(ABCDEFGH IJKLMNOP QRSTUV WXYZAB CDEFGH IJKLMN) Tj"
    content = b"BT /F1 12 Tf 72 700 Td " + line + b" 0 -18 Td " + line + b" ET"
    return hand_written_pdf(path, font, content)


def gibberish_pdf(path: Path) -> Path:
    """Real Latin letters, no language: a text layer with no function words."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    tokens = [
        "".join("qxzjkvwbgpf"[(i * 7 + j * 3) % 11] for j in range(6))
        for i in range(240)
    ]
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Helvetica", 11)
    y = LETTER[1] - 72
    for start in range(0, len(tokens), 8):
        c.drawString(72, y, " ".join(tokens[start : start + 8]))
        y -= 14
    c.showPage()
    c.save()
    return path


def word(text: str) -> Word:
    return Word(text=text, box=Box(0.0, 0.0, 0.01, 0.01))


def char(text: str) -> RawChar:
    return RawChar(text=text, box=Box(0.0, 0.0, 0.01, 0.01), seq=0, color=None,
                   fontname="Helvetica", size=10.0)


@pytest.fixture
def memo(tmp_path: Path) -> Path:
    return synth.born_digital_pdf(tmp_path / "memo.pdf", pages=3)


# --------------------------------------------------------------------------
# licensing - the one constraint that is not negotiable
# --------------------------------------------------------------------------


def test_no_agpl_pdf_library_is_reachable() -> None:
    """PyMuPDF is AGPL. Importing it would relicense the whole project."""
    assert "fitz" not in sys.modules
    assert "pymupdf" not in sys.modules
    source = Path(pdfmod.__file__).read_text().lower()
    for banned in ("import fitz", "from fitz", "import pymupdf", "from pymupdf"):
        assert banned not in source


# --------------------------------------------------------------------------
# the ordinary case
# --------------------------------------------------------------------------


def test_reads_pages_dimensions_and_text(memo: Path) -> None:
    with open_pdf(memo) as handle:
        assert handle.page_count == 3
        page = read_page(handle, 0)

    assert page.number == 1
    assert page.rotation == 0
    assert (page.width_pt, page.height_pt) == (LETTER_W, LETTER_H)
    assert page.chars and page.rects and page.words
    assert page.words[0].text == "MEMORANDUM"
    assert "Commission" in " ".join(w.text for w in page.words)


def test_page_count_agrees_with_the_handle(memo: Path) -> None:
    assert page_count(memo) == 3


def test_words_are_in_reading_order(memo: Path) -> None:
    """Guarantee 3: this order is the order the page HTML and the index use."""
    with open_pdf(memo) as handle:
        page = read_page(handle, 0)

    lines = [w.line for w in page.words]
    assert lines == sorted(lines)
    assert lines[0] == 0
    assert max(lines) > 10  # a full page of a synthetic memo is ~31 lines

    for previous, current in pairwise(page.words):
        if previous.line == current.line:
            assert previous.box.x <= current.box.x


def test_lines_group_words_that_share_a_baseline(memo: Path) -> None:
    with open_pdf(memo) as handle:
        page = read_page(handle, 0)

    by_line: dict[int, list[float]] = {}
    for w in page.words:
        by_line.setdefault(w.line, []).append(w.box.y)
    for tops in by_line.values():
        assert max(tops) - min(tops) < 0.005  # half a percent of the page height


def test_a_known_word_lands_where_it_was_drawn(memo: Path) -> None:
    with open_pdf(memo) as handle:
        page = read_page(handle, 0)

    heading = page.words[0]
    assert heading.text == "MEMORANDUM"

    left_pt = heading.box.x * LETTER_W
    top_pt = heading.box.y * LETTER_H
    bottom_pt = heading.box.y2 * LETTER_H
    width_pt = heading.box.w * LETTER_W

    assert abs(left_pt - HEADING_LEFT_PT) < 1.0
    # The box must straddle the baseline and rise no more than one font size
    # above it. 13pt Helvetica-Bold, so a cap height of about 9pt.
    assert top_pt < HEADING_BASELINE_FROM_TOP_PT < bottom_pt
    assert 0 < HEADING_BASELINE_FROM_TOP_PT - top_pt <= 14.0
    assert 60.0 < width_pt < 140.0


# --------------------------------------------------------------------------
# draw order
# --------------------------------------------------------------------------


def covering_rect(page: pdfmod.RawPage) -> pdfmod.RawRect:
    """The tallest filled rectangle: the redaction, not the table rule."""
    filled = [r for r in page.rects if r.fill]
    assert filled, "the synthetic page always draws at least one filled shape"
    return max(filled, key=lambda r: r.box.h)


def test_zorder_shows_text_hidden_under_a_later_rectangle(tmp_path: Path) -> None:
    """A failed redaction: characters painted, then a box painted over them."""
    path = synth.born_digital_pdf(
        tmp_path / "failed.pdf",
        pages=1,
        redactions={1: [RedactionSpec(100, 500, 180, 14, hidden_text="SECRET NAME")]},
    )
    with open_pdf(path) as handle:
        page = read_page(handle, 0)

    assert page.has_zorder
    rect = covering_rect(page)
    assert rect.seq > 0

    under = [
        c for c in page.chars
        if c.seq < rect.seq and c.box.overlap_ratio(rect.box) > 0.8
    ]
    assert "SECRET NAME" in "".join(c.text for c in under)


def test_zorder_shows_text_painted_on_top_of_a_rectangle(tmp_path: Path) -> None:
    """Not a failed redaction: the box is painted first, the text is visible."""
    path = synth.born_digital_pdf(
        tmp_path / "visible.pdf",
        pages=1,
        redactions={
            1: [
                RedactionSpec(
                    100, 500, 180, 14, hidden_text="ON TOP", draw_text_after=True
                )
            ]
        },
    )
    with open_pdf(path) as handle:
        page = read_page(handle, 0)

    rect = covering_rect(page)
    after = [
        c for c in page.chars
        if c.seq > rect.seq and c.box.overlap_ratio(rect.box) > 0.8
    ]
    assert "ON TOP" in "".join(c.text for c in after)


def test_sequence_numbers_are_a_single_run_over_chars_and_rects(memo: Path) -> None:
    with open_pdf(memo) as handle:
        page = read_page(handle, 0)
    all_seqs = sorted(item.seq for item in (*page.chars, *page.rects))
    assert all_seqs == sorted(set(all_seqs)), "draw order must be a strict ordering"
    assert min(all_seqs) > 0


def test_fill_colour_is_normalised_to_rgb(tmp_path: Path) -> None:
    path = synth.born_digital_pdf(
        tmp_path / "black.pdf",
        pages=1,
        redactions={1: [RedactionSpec(100, 500, 180, 14)]},
    )
    with open_pdf(path) as handle:
        page = read_page(handle, 0)
    rect = covering_rect(page)
    assert rect.fill_color == (0.0, 0.0, 0.0)
    assert rect.fill is True
    assert rect.stroke is False


def test_a_redaction_box_is_where_reportlab_drew_it(tmp_path: Path) -> None:
    path = synth.born_digital_pdf(
        tmp_path / "boxed.pdf",
        pages=1,
        redactions={1: [RedactionSpec(100, 500, 180, 14)]},
    )
    with open_pdf(path) as handle:
        page = read_page(handle, 0)
    box = covering_rect(page).box

    # reportlab measures y upwards from the bottom; Box measures it downwards
    # from the top, so a rect at y=500 of height 14 has its top at 792-514.
    assert box.x * LETTER_W == pytest.approx(100.0, abs=0.5)
    assert box.y * LETTER_H == pytest.approx(LETTER_H - 514.0, abs=0.5)
    assert box.w * LETTER_W == pytest.approx(180.0, abs=0.5)
    assert box.h * LETTER_H == pytest.approx(14.0, abs=0.5)


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("degrees", "size"),
    [(0, (LETTER_W, LETTER_H)), (90, (LETTER_H, LETTER_W)),
     (180, (LETTER_W, LETTER_H)), (270, (LETTER_H, LETTER_W))],
)
def test_rotation_is_reported_and_swaps_the_page_size(
    tmp_path: Path, degrees: int, size: tuple[float, float]
) -> None:
    base = synth.born_digital_pdf(tmp_path / "base.pdf", pages=1)
    path = rotated(base, tmp_path / f"r{degrees}.pdf", degrees)
    with open_pdf(path) as handle:
        page = read_page(handle, 0)
    assert page.rotation == degrees
    assert (page.width_pt, page.height_pt) == pytest.approx(size)


@pytest.mark.parametrize(
    ("degrees", "expected_left", "expected_top"),
    [
        # A point drawn at (72, 720) in unrotated PDF space, expressed in the
        # frame the reader sees once the viewer has applied /Rotate. 90 takes
        # the top-left corner to the top-right, 180 to the bottom-right, 270 to
        # the bottom-left.
        (0, 72.0, LETTER_H - 720.0),
        (90, 720.0, 72.0),
        (180, LETTER_W - 72.0, 720.0),
        (270, LETTER_H - 720.0, LETTER_W - 72.0),
    ],
)
def test_rotated_boxes_land_where_the_reader_sees_them(
    tmp_path: Path, degrees: int, expected_left: float, expected_top: float
) -> None:
    base = synth.born_digital_pdf(tmp_path / "base.pdf", pages=1)
    path = rotated(base, tmp_path / f"r{degrees}.pdf", degrees)
    with open_pdf(path) as handle:
        page = read_page(handle, 0)

    # The heading is the first thing the generator draws, so the first glyph in
    # draw order is the "M" of "MEMORANDUM" whichever way round the page is.
    heading = page.chars[0]
    assert heading.text == "M"
    left = heading.box.x * page.width_pt
    top = heading.box.y * page.height_pt

    # 15pt of slack: the expectations above are the mapped *baseline origin*,
    # and the measured box is the glyph's full em box around it.
    assert left == pytest.approx(expected_left, abs=15.0)
    assert top == pytest.approx(expected_top, abs=15.0)


def test_rotated_pages_still_produce_words(tmp_path: Path) -> None:
    base = synth.born_digital_pdf(tmp_path / "base.pdf", pages=1)
    path = rotated(base, tmp_path / "r90.pdf", 90)
    with open_pdf(path) as handle:
        page = read_page(handle, 0)
    assert len(page.words) > 100
    assert all(0.0 <= w.box.x <= 1.0 for w in page.words)


# --------------------------------------------------------------------------
# is the text layer worth believing
# --------------------------------------------------------------------------


def test_a_real_text_layer_is_trusted(memo: Path) -> None:
    with open_pdf(memo) as handle:
        page = read_page(handle, 0)
    assert page.embedded_text_ok
    assert page.embedded_text_reasons == []


def test_private_use_glyphs_are_caught(tmp_path: Path) -> None:
    path = private_use_pdf(tmp_path / "pua.pdf")
    with open_pdf(path) as handle:
        page = read_page(handle, 0)

    assert page.chars, "the page does have glyphs - that is what makes it dangerous"
    assert not page.embedded_text_ok
    assert any("private-use" in r for r in page.embedded_text_reasons)


def test_text_with_no_function_words_is_caught(tmp_path: Path) -> None:
    path = gibberish_pdf(tmp_path / "gibberish.pdf")
    with open_pdf(path) as handle:
        page = read_page(handle, 0)

    assert len(page.words) > 100
    assert not page.embedded_text_ok
    assert any("stopword ratio" in r for r in page.embedded_text_reasons)


def test_a_page_with_no_text_layer_says_so(tmp_path: Path) -> None:
    path = synth.image_only_pdf(tmp_path / "scan.pdf", [synth.typed_page()])
    with open_pdf(path) as handle:
        page = read_page(handle, 0)
    assert page.chars == []
    assert not page.embedded_text_ok
    assert page.embedded_text_reasons == ["no embedded text layer on this page"]


def test_verdict_flags_control_characters() -> None:
    chars = [char("\x01") for _ in range(30)] + [char("a") for _ in range(70)]
    ok, reasons = embedded_text_verdict(chars, [word("the")])
    assert not ok
    assert any("control codes" in r for r in reasons)


def test_verdict_flags_glyphs_that_decode_to_whitespace() -> None:
    chars = [char(" ") for _ in range(50)]
    ok, reasons = embedded_text_verdict(chars, [])
    assert not ok
    assert any("whitespace" in r for r in reasons)


def test_verdict_ignores_the_stopword_signal_on_a_short_page() -> None:
    """A cover sheet has no function words and is not broken."""
    heading = "EXHIBIT 14 DEPARTMENT OF TRANSPORTATION"
    words = [word(w) for w in heading.split()]
    chars = [char(c) for c in heading]
    ok, reasons = embedded_text_verdict(chars, words)
    assert ok, reasons


# --------------------------------------------------------------------------
# the stopword rule is `stackroom.lang`'s, not this module's
#
# This file used to keep a private union of five European languages and reject
# anything scoring under 0.05 against it "in any language we check". It checked
# five of the eleven lists the project holds, and it had no opinion whatever
# about a script none of them is written in - so it condemned Devanagari, Thai,
# Japanese and Korean text layers as garbage and sent perfectly readable pages
# to a recogniser that had never been asked to read them.
# --------------------------------------------------------------------------

HINDI_TEXT_LAYER = (
    "आयोग ने निदेशक के कार्यालय और ठेकेदार प्राधिकरण के बीच मार्च से सितंबर तक की अवधि "
    "के सभी पत्राचार की मांग की थी उत्तर ग्यारह महीने बाद आया और उसमें चार सौ पृष्ठ थे "
    "जिनमें से एक बड़ा हिस्सा रोक लिया गया था"
)

POLISH_TEXT_LAYER = (
    "w odpowiedzi na wniosek o udostępnienie informacji publicznej urząd przekazał "
    "komplet korespondencji między dyrektorem a wykonawcą z okresu od marca do "
    "września oraz wskazał że część dokumentów została wyłączona z udostępnienia"
)


def _layer(text: str) -> tuple[list[RawChar], list[Word]]:
    """A page whose glyphs decode cleanly to *text*. Nothing wrong with it."""
    return [char(c) for c in text if not c.isspace()], [word(w) for w in text.split()]


def test_a_devanagari_text_layer_is_not_condemned_for_matching_no_word_list() -> None:
    """A zero here means "we have no words for this", not "this is garbage".

    Both answers score zero and only one of them is a reason to throw the
    layer away and re-read the page in languages nobody asked for.
    """
    chars, words = _layer(HINDI_TEXT_LAYER)
    assert len(words) >= MIN_WORDS_FOR_STOPWORDS
    ok, reasons = embedded_text_verdict(chars, words)
    assert ok, reasons


def test_a_polish_text_layer_is_read_against_all_eleven_lists() -> None:
    """"Any language we check" now means the eleven the project ships.

    Polish is one of them and was not one of the five this file used to hold:
    against those it scored 0.03 and was rejected, against these it scores 0.36.
    """
    chars, words = _layer(POLISH_TEXT_LAYER)
    assert len(words) >= MIN_WORDS_FOR_STOPWORDS
    ok, reasons = embedded_text_verdict(chars, words)
    assert ok, reasons


# --------------------------------------------------------------------------
# damaged, empty and locked files
# --------------------------------------------------------------------------


def test_a_truncated_pdf_gives_a_diagnosable_error(tmp_path: Path) -> None:
    good = synth.born_digital_pdf(tmp_path / "good.pdf", pages=2)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(good.read_bytes()[:400])

    with pytest.raises(PdfDamagedError) as caught:
        open_pdf(broken)
    message = str(caught.value)
    assert "broken.pdf" in message
    assert message.rstrip().endswith(("EOF", "EOF'")) or ":" in message.split("broken.pdf")[1]


def test_an_empty_file_gives_a_diagnosable_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(PdfDamagedError, match="0 bytes"):
        open_pdf(empty)


def test_a_missing_file_gives_a_diagnosable_error(tmp_path: Path) -> None:
    with pytest.raises(PdfDamagedError):
        open_pdf(tmp_path / "nope.pdf")


def test_a_damaged_xref_table_is_recovered(tmp_path: Path) -> None:
    """pdfminer rebuilds the table by scanning; a bad offset is not fatal."""
    good = synth.born_digital_pdf(tmp_path / "good.pdf", pages=2)
    data = good.read_bytes()
    marker = data.rfind(b"startxref")
    line_start = data.index(b"\n", marker) + 1
    line_end = data.index(b"\n", line_start)
    damaged = tmp_path / "damaged.pdf"
    damaged.write_bytes(data[:line_start] + b"999999" + data[line_end:])

    with open_pdf(damaged) as handle:
        assert handle.page_count == 2
        assert read_page(handle, 0).words


def test_an_owner_locked_pdf_opens_with_the_empty_password(tmp_path: Path) -> None:
    good = synth.born_digital_pdf(tmp_path / "good.pdf", pages=1)
    locked = encrypted(good, tmp_path / "owner.pdf", user_password="")
    with open_pdf(locked) as handle:
        assert handle.page_count == 1
        assert read_page(handle, 0).words


def test_a_password_protected_pdf_says_what_to_do(tmp_path: Path) -> None:
    good = synth.born_digital_pdf(tmp_path / "good.pdf", pages=1)
    locked = encrypted(good, tmp_path / "locked.pdf", user_password="hunter2")
    with pytest.raises(PdfEncryptedError) as caught:
        open_pdf(locked)
    message = str(caught.value)
    assert "locked.pdf" in message
    assert "encrypted" in message
    assert "qpdf" in message


def test_a_pdf_with_no_pages_is_not_an_error(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    path = tmp_path / "nopages.pdf"
    with path.open("wb") as fh:
        PdfWriter().write(fh)

    with open_pdf(path) as handle:
        assert handle.page_count == 0
        with pytest.raises(IndexError, match="out of range"):
            read_page(handle, 0)


def test_an_out_of_range_index_is_a_programming_error(memo: Path) -> None:
    with open_pdf(memo) as handle:
        with pytest.raises(IndexError):
            read_page(handle, 3)
        with pytest.raises(IndexError):
            read_page(handle, -1)


# --------------------------------------------------------------------------
# a page that blows up mid-parse
# --------------------------------------------------------------------------


def test_a_failed_zorder_pass_degrades_instead_of_dying(
    memo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pdfminer changes; a broken instrumented device must not lose the page."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise TypeError("render_char() takes 7 positional arguments but 9 were given")

    monkeypatch.setattr(pdfmod, "_paint", explode)
    with open_pdf(memo) as handle:
        page = read_page(handle, 1)

    assert page.chars, "text must still come through without draw order"
    assert page.words
    assert not page.has_zorder
    assert all(c.seq == NO_ZORDER for c in page.chars)
    assert any("draw order unavailable" in r for r in page.embedded_text_reasons)


def test_a_page_that_cannot_be_parsed_at_all_comes_back_empty(
    memo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise ValueError("content stream is garbage")

    monkeypatch.setattr(pdfmod, "_paint", explode)
    monkeypatch.setattr(pdfmod, "_paint_without_order", explode)
    with open_pdf(memo) as handle:
        page = read_page(handle, 2)
        sibling = read_page(handle, 0)

    assert page.number == 3
    assert page.chars == [] and page.words == [] and page.rects == []
    assert (page.width_pt, page.height_pt) == (LETTER_W, LETTER_H)
    assert not page.embedded_text_ok
    assert any("could not be parsed" in r for r in page.embedded_text_reasons)
    # And the handle survives: a second read returns a page rather than raising.
    assert sibling.number == 1 and sibling.chars == []


# --------------------------------------------------------------------------
# metadata and cost
# --------------------------------------------------------------------------


def test_document_meta_reports_what_the_file_claims(tmp_path: Path) -> None:
    path = synth.born_digital_pdf(tmp_path / "meta.pdf", pages=1, title="Release 14")
    meta = document_meta(path)
    assert meta["title"] == "Release 14"
    assert meta["author"] == "Office of Synthetic Records"
    assert meta["created"].startswith("2")
    assert "T" in meta["created"] and meta["created"].count("-") >= 2
    assert all(isinstance(v, str) for v in meta.values())


def test_document_meta_refuses_a_file_that_is_not_a_pdf(tmp_path: Path) -> None:
    path = tmp_path / "not.pdf"
    path.write_bytes(b"just some words\n")
    with pytest.raises(PdfDamagedError):
        document_meta(path)


def test_reading_one_page_parses_exactly_one_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the handle: page 12 must not cost twelve pages."""
    path = synth.born_digital_pdf(tmp_path / "long.pdf", pages=12)
    calls: list[object] = []

    real = pdfmod.PDFPageInterpreter

    class Counting(real):  # type: ignore[misc, valid-type]
        def process_page(self, page: object) -> None:
            calls.append(page)
            super().process_page(page)

    monkeypatch.setattr(pdfmod, "PDFPageInterpreter", Counting)
    with open_pdf(path) as handle:
        assert calls == []
        page = read_page(handle, 11)
        assert len(calls) == 1
        assert page.words
        read_page(handle, 0)
        assert len(calls) == 2
