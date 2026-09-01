"""The site builder, tested against the data model rather than the pipeline.

Everything here builds ``Collection``/``Document``/``Page`` objects by hand and
hands them to the builder. That is the seam ``docs/ARCHITECTURE.md`` describes -
ingest never renders, build never parses - and it is what makes it possible to
test a 2,000-page document's ribbon without owning 2,000 pages.

The important test in this file is :func:`test_the_indexed_body_is_exactly_the
_page_words_in_order` and its two neighbours. Guarantee 3 says word order in
``Page.words`` is identical to token order in the page HTML, because the search
index reports matches as indices into that sequence and the viewer turns those
indices into boxes drawn on the scan. One divergence and every highlight in
every published archive lands on the wrong word - silently, and only for the
readers, who have no way to know.

The tests deliberately avoid asserting on CSS class names, wording or layout:
those belong to the templates and change. What is asserted is structure - which
tokens, in which order, at which relative depth.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path

import pytest

from stackroom.build.search import IndexInfo
from stackroom.build.site import (
    ASSETS,
    SiteBuilder,
    _ribbon_label,
    _root,
    attach_about,
    build_site,
    display_lines,
    human_bytes,
    page_payload,
    page_state,
    ribbon,
)
from stackroom.config import Config
from stackroom.ingest import exemptions as exemptions_mod
from stackroom.model import (
    Box,
    Collection,
    CollectionStats,
    Document,
    ImageVariant,
    OcrQuality,
    Page,
    PageVerdict,
    Redaction,
    RedactionKind,
    Word,
)

# --------------------------------------------------------------------------
# building pages by hand
# --------------------------------------------------------------------------

LINE_HEIGHT = 0.02


def word(text: str, x: float, y: float, *, w: float = 0.05, line: int = 0, conf: int = -1) -> Word:
    return Word(text=text, box=Box(x, y, w, LINE_HEIGHT), line=line, conf=conf)


def text_page(number: int, lines: list[str], **kwargs) -> Page:
    """A page whose words are laid out left to right, one row per line."""
    words: list[Word] = []
    for row, line in enumerate(lines):
        x = 0.1
        for token in line.split():
            words.append(word(token, x, 0.1 + row * 0.04, w=0.012 * len(token), line=row))
            x += 0.012 * len(token) + 0.01
    return Page(number=number, words=words, lines=list(lines), **kwargs)


def blank_pages(count: int, verdict: PageVerdict = PageVerdict.GOOD) -> list[Page]:
    return [Page(number=n, quality=OcrQuality(verdict=verdict)) for n in range(1, count + 1)]


def one_document(pages: list[Page], **kwargs) -> Document:
    kwargs.setdefault("id", "memo-2019")
    kwargs.setdefault("title", "Memorandum, March 2019")
    kwargs.setdefault("filename", "memo-2019.pdf")
    kwargs.setdefault("sha256", "a" * 64)
    kwargs.setdefault("size_bytes", 204_800)
    return Document(pages=pages, **kwargs)


def collection_of(*documents: Document, **kwargs) -> Collection:
    kwargs.setdefault("title", "Papers of the Commission")
    stats = CollectionStats(
        documents=len(documents),
        pages=sum(d.page_count for d in documents),
        words=sum(len(p.words) for d in documents for p in d.pages),
    )
    return Collection(documents=list(documents), stats=stats, **kwargs)


def build(tmp_path: Path, collection: Collection, cfg: Config | None = None) -> Path:
    """Write the site. Search is off: pagefind is a separate binary and a
    separate module's tests, and every property here is about the HTML."""
    cfg = cfg or Config()
    cfg.search.enabled = False
    out = tmp_path / "site"
    build_site(collection, cfg, out)
    return out


# --------------------------------------------------------------------------
# reading the site back
# --------------------------------------------------------------------------


class PagefindBody(HTMLParser):
    """Extracts the text of the element carrying ``data-pagefind-body``.

    This is what Pagefind itself does: strip the tags, keep the text, split on
    whitespace. Doing the same thing here is the only way to test the contract
    rather than the template.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag: str | None = None
        self.depth = 0
        self.parts: list[str] = []
        self.elements = 0

    def handle_starttag(self, tag, attrs):
        names = [name for name, _ in attrs]
        if self.depth == 0 and "data-pagefind-body" in names:
            self.elements += 1
            self.tag, self.depth = tag, 1
        elif self.depth and tag == self.tag:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth and tag == self.tag:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def indexed_tokens(html: str) -> list[str]:
    parser = PagefindBody()
    parser.feed(html)
    parser.close()
    assert parser.elements == 1, f"expected exactly one indexed body, found {parser.elements}"
    return "".join(parser.parts).split()


class Links(HTMLParser):
    """Every ``href``/``src``/``srcset`` target in a generated page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if not value:
                continue
            if name in ("href", "src"):
                self.targets.append(value)
            elif name == "srcset":
                self.targets.extend(part.split()[0] for part in value.split(",") if part.strip())


def links_in(path: Path) -> list[str]:
    parser = Links()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser.targets


def page_html(out: Path, doc_id: str, number: int) -> str:
    return (out / "d" / doc_id / "p" / str(number) / "index.html").read_text(encoding="utf-8")


# ==========================================================================
# 1. the ribbon
# ==========================================================================


def test_a_document_in_one_state_is_one_rectangle_not_two_thousand():
    """Run-length encoding is the whole reason this is built in Python.

    Every document on the front page carries a ribbon. Without the merge a
    2,000-page release is 2,000 rectangles, which is a 120 KB graphic in place
    of a 300-byte one, repeated once per document.
    """
    svg = str(ribbon(blank_pages(100)))
    assert svg.count("<rect") == 1
    assert 'data-pages="100"' in svg
    assert len(svg) < 400, f"one run should be a small graphic, got {len(svg)} bytes"


def test_runs_merge_only_while_the_state_is_the_same():
    """Two hundred plain pages with one withheld page in the middle: three runs."""
    pages = blank_pages(200)
    pages[99].redactions = [Redaction(box=Box(0, 0, 0.5, 0.1), kind=RedactionKind.VECTOR)]
    svg = str(ribbon(pages))
    assert svg.count("<rect") == 3
    assert svg.count('class="r-plain"') == 2
    assert svg.count('class="r-part"') == 1


def test_a_single_page_among_thousands_is_still_wide_enough_to_see():
    """A tick a thousandth of the width wide is a tick nobody can click.

    The floor is what makes one withheld page in a 2,000-page production
    visible on the front page, which is usually the point of the ribbon.
    """
    pages = blank_pages(2000)
    pages[1500].quality = OcrQuality(verdict=PageVerdict.UNREADABLE)
    widths = [float(w) for w in re.findall(r'width="([0-9.]+)"', str(ribbon(pages)))]
    assert min(widths) >= 0.6


def test_the_runs_cover_the_whole_strip_with_no_gaps():
    """A gap in the ribbon reads as a page in no state at all."""
    pages = blank_pages(7)
    for index, verdict in enumerate(
        [PageVerdict.GOOD, PageVerdict.BLANK, PageVerdict.BLANK, PageVerdict.SUSPECT]
    ):
        pages[index].quality = OcrQuality(verdict=verdict)
    svg = str(ribbon(pages))
    rects = [
        (float(x), float(w))
        for x, w in re.findall(r'x="([0-9.]+)" y="0" width="([0-9.]+)"', svg)
    ]
    assert rects[0][0] == 0.0
    assert rects[-1][0] + rects[-1][1] == pytest.approx(1000.0, abs=0.01)
    for (x, w), (next_x, _) in pairwise(rects):
        assert x + w == pytest.approx(next_x, abs=0.01)


def test_a_document_with_no_pages_produces_no_ribbon_rather_than_an_empty_svg():
    """A zero-page document is a broken PDF we still have to list somewhere."""
    assert str(ribbon([])) == ""
    assert not ribbon([])


def test_the_ribbon_carries_the_link_template_the_viewer_needs():
    """The strip is clickable; the base URL is how a tick becomes a page."""
    svg = str(ribbon(blank_pages(3), base="p/{n}/index.html"))
    assert 'data-base="p/{n}/index.html"' in svg


def test_the_ribbon_label_counts_what_a_reader_would_want_counted():
    """The accessible name of a picture of 2,000 pages has to say something.

    "2000 pages" alone is not what the graphic conveys; the counts of withheld
    and unreadable pages are the reason it is on the page at all.
    """
    pages = blank_pages(10)
    pages[0].redactions = [Redaction(box=Box(0, 0, 0.2, 0.1), kind=RedactionKind.VECTOR)]
    pages[1].redaction_ratio = 1.0
    pages[2].quality = OcrQuality(verdict=PageVerdict.UNREADABLE)
    pages[3].quality = OcrQuality(verdict=PageVerdict.BLANK)
    label = _ribbon_label(pages)
    assert label.startswith("10 pages")
    assert "1 partly withheld" in label
    assert "1 withheld in full" in label
    assert "search cannot read" in label
    assert "1 blank" in label
    assert label.endswith(".")


def test_a_label_says_nothing_about_categories_with_nothing_in_them():
    assert _ribbon_label(blank_pages(4)) == "4 pages."


def test_the_ribbon_is_well_formed_xml():
    """It is inlined into every page; a browser has to parse it as markup.

    An SVG that only accidentally survives an HTML parser's error recovery is
    not something to inline a few thousand times.

    The regression this guards: ``ribbon()`` used to build its attribute string
    by concatenating a plain ``str`` onto a ``Markup``, which escapes the
    *string* - so the quotes around the label came out as ``&#34;`` and the
    element was emitted as ``aria-label=&#34;3 pages.&#34;``: an unquoted
    attribute value followed by a handful of invented boolean attributes, and
    not parseable as XML at all. Every attribute is now escaped as its own
    fragment and only then joined.
    """
    ET.fromstring(str(ribbon(blank_pages(3))))


def test_the_ribbon_announces_itself_to_a_screen_reader():
    """``role="img"`` with no usable name is a picture that says nothing.

    The regression this guards: the same ``str``-onto-``Markup`` concatenation
    turned the aria-label's own quotes into ``&#34;`` entities, which left the
    graphic with an accessible name of ``"3`` and scattered the rest of the
    sentence across attributes nobody wrote.
    """
    element = ET.fromstring(str(ribbon(blank_pages(3))))
    assert element.get("role") == "img"
    assert element.get("aria-label") == "3 pages."


def test_a_label_with_an_ampersand_in_it_cannot_break_out_of_the_attribute():
    """Collection titles contain ampersands and quotation marks."""
    svg = str(ribbon(blank_pages(2), label='Smith & Jones "the papers"'))
    assert "&amp;" in svg
    assert 'Jones "the' not in svg


# ==========================================================================
# 2. page_state
# ==========================================================================


@pytest.mark.parametrize(
    ("verdict", "state"),
    [
        (PageVerdict.GOOD, "plain"),
        (PageVerdict.PICTORIAL, "plain"),
        (PageVerdict.BLANK, "void"),
        (PageVerdict.SUSPECT, "dark"),
        (PageVerdict.UNREADABLE, "dark"),
    ],
)
def test_every_verdict_has_a_state(verdict, state):
    """A verdict with no case here would fall through to "plain", which claims
    a page is readable when we have just decided it is not."""
    assert page_state(Page(number=1, quality=OcrQuality(verdict=verdict))) == state


@pytest.mark.parametrize(
    ("ratio", "state"), [(0.0, "part"), (0.5, "part"), (0.8999, "part"), (0.9, "full"), (1.0, "full")]
)
def test_nine_tenths_withheld_is_where_a_page_becomes_a_withheld_page(ratio, state):
    """The boundary is inclusive. A page at exactly 0.9 reads as withheld."""
    page = Page(
        number=1,
        redaction_ratio=ratio,
        redactions=[Redaction(box=Box(0, 0, 1, ratio or 0.1), kind=RedactionKind.VECTOR)],
    )
    assert page_state(page) == state


def test_a_page_can_be_withheld_in_full_without_a_box_being_listed():
    """A raster-only page withheld in full may carry a ratio and no boxes."""
    assert page_state(Page(number=1, redaction_ratio=0.95)) == "full"


def test_a_page_search_cannot_read_says_so_before_it_says_anything_else():
    """Unreadable beats withheld: the reader's problem is that search is blind
    here, and that is true whether or not the page is also redacted."""
    page = Page(
        number=1,
        redaction_ratio=1.0,
        quality=OcrQuality(verdict=PageVerdict.SUSPECT),
        redactions=[Redaction(box=Box(0, 0, 1, 1), kind=RedactionKind.VECTOR)],
    )
    assert page_state(page) == "dark"


def test_a_page_with_nothing_wrong_with_it_is_plain():
    assert page_state(Page(number=1)) == "plain"


# ==========================================================================
# 3. display_lines
# ==========================================================================


def test_words_come_back_in_reading_order_whatever_order_they_arrived_in():
    """OCR does not always emit left to right, and the transcription must.

    The scan sits beside this text; a reader checking one against the other
    needs the same words in the same order.
    """
    page = Page(
        number=1,
        words=[
            word("second", 0.30, 0.10, line=0),
            word("first", 0.10, 0.10, line=0),
            word("later", 0.10, 0.20, line=1),
            word("third", 0.50, 0.10, line=0),
        ],
    )
    lines = display_lines(page)
    assert [[item.text for item in line] for line in lines] == [
        ["first", "second", "third"],
        ["later"],
    ]
    assert [item.index for item in lines[0]] == [1, 0, 3], "indices must survive the reordering"


def test_a_redaction_lands_on_the_line_its_centre_falls_in():
    """The bar goes where the words went, in the sentence it interrupts.

    Putting it at the bottom of the page, or on the image only, loses the one
    thing a reader came for: which sentence has the hole in it.
    """
    page = Page(
        number=1,
        words=[
            word("the", 0.10, 0.10, line=0),
            word("director", 0.20, 0.10, line=0),
            word("wrote", 0.10, 0.30, line=1),
        ],
        redactions=[Redaction(box=Box(0.40, 0.105, 0.2, 0.01), kind=RedactionKind.VECTOR)],
    )
    lines = display_lines(page)
    assert [item.kind for item in lines[0]] == ["word", "word", "gap"]
    assert [item.kind for item in lines[1]] == ["word"]


def test_a_redaction_between_two_words_is_placed_between_them():
    page = Page(
        number=1,
        words=[word("before", 0.10, 0.10, line=0), word("after", 0.60, 0.10, line=0)],
        redactions=[Redaction(box=Box(0.30, 0.105, 0.2, 0.01), kind=RedactionKind.VECTOR)],
    )
    assert [item.kind for item in display_lines(page)[0]] == ["word", "gap", "word"]


def test_a_redaction_on_no_line_at_all_becomes_a_line_of_its_own():
    """A page that is one black box has no line to hang it on.

    Dropping it would make the transcription say the page was empty when it was
    withheld, which is the difference between "nothing here" and "something
    here you may not see".
    """
    page = Page(
        number=1,
        words=[word("heading", 0.10, 0.05, line=0)],
        redactions=[Redaction(box=Box(0.10, 0.50, 0.7, 0.2), kind=RedactionKind.VECTOR)],
    )
    lines = display_lines(page)
    assert len(lines) == 2
    assert [item.kind for item in lines[-1]] == ["gap"]


def test_a_page_of_nothing_but_redactions_still_has_something_to_show():
    page = Page(
        number=1,
        redactions=[
            Redaction(box=Box(0.1, 0.1, 0.8, 0.1), kind=RedactionKind.VECTOR),
            Redaction(box=Box(0.1, 0.3, 0.8, 0.1), kind=RedactionKind.VECTOR),
        ],
    )
    lines = display_lines(page)
    assert [item.kind for item in lines[0]] == ["gap", "gap"]


@pytest.mark.parametrize(
    ("box_width", "expected"),
    [(1.0, 100), (0.5, 50), (0.335, 34), (0.001, 3), (0.0, 3), (2.0, 100)],
)
def test_the_bar_is_as_wide_a_share_of_the_column_as_the_box_is_of_the_page(
    box_width, expected
):
    """A share of the width, not a count of characters.

    A character estimate drifts as soon as the reader changes the text size,
    and then the bar no longer matches the box on the scan beside it.
    """
    page = Page(
        number=1,
        redactions=[Redaction(box=Box(0.0, 0.5, box_width, 0.02), kind=RedactionKind.VECTOR)],
    )
    assert display_lines(page)[0][0].width == expected


def test_an_exemption_code_reaches_the_bar_with_the_words_that_explain_it():
    """``(b)(6)`` means nothing to a reader; the gloss beside it does.

    The label has to come from the same vocabulary the page legend uses, or the
    bar and the legend disagree about what was withheld.
    """
    page = Page(
        number=1,
        words=[word("text", 0.1, 0.1, line=0)],
        redactions=[
            Redaction(box=Box(0.4, 0.105, 0.2, 0.01), kind=RedactionKind.VECTOR, codes=["b(6)"])
        ],
    )
    gap = display_lines(page)[0][-1]
    assert gap.kind == "gap"
    assert gap.code == "b(6)"
    assert gap.code_label == dict(exemptions_mod.legend(["b(6)"], jurisdiction="us"))["b(6)"]
    assert gap.code_label and gap.code_label != gap.code


def test_a_redaction_with_no_code_carries_no_label():
    page = Page(
        number=1,
        redactions=[Redaction(box=Box(0.1, 0.1, 0.2, 0.02), kind=RedactionKind.VECTOR)],
    )
    gap = display_lines(page)[0][0]
    assert gap.code == "" and gap.code_label == ""


@pytest.mark.parametrize(("conf", "doubtful"), [(-1, False), (0, True), (59, True), (60, False), (96, False)])
def test_a_word_the_recogniser_was_unsure_of_is_marked_as_doubtful(conf, doubtful):
    """Below 60 the word is a guess, and a reader checking against the scan
    should be told which words to check first. Text from a PDF's own layer has
    no confidence at all and must not be marked."""
    page = Page(number=1, words=[word("maybe", 0.1, 0.1, conf=conf)])
    assert display_lines(page)[0][0].doubtful is doubtful


def test_a_page_with_no_words_and_no_redactions_has_no_lines():
    """The template checks this for truth to decide whether to explain itself."""
    assert display_lines(Page(number=1)) == []


# ==========================================================================
# 4. page_payload
# ==========================================================================


def test_the_payload_is_four_integers_a_word_in_the_order_the_words_are_in():
    """The viewer reads box ``i`` for match ``i``. Order is the whole contract."""
    page = text_page(1, ["alpha beta", "gamma"])
    payload = json.loads(page_payload(page))
    assert payload["n"] == len(page.words) == 3
    assert len(payload["b"]) == 4 * len(page.words)
    assert all(isinstance(value, int) for value in payload["b"])


def test_every_box_survives_the_round_trip_through_the_payload():
    """Fixed point at 1/10,000 of the page: finer than a pixel on a 5,000px scan."""
    page = text_page(1, ["the director wrote", "to the authority"])
    boxes = json.loads(page_payload(page))["b"]
    for index, original in enumerate(page.words):
        restored = Box.from_ints(tuple(boxes[index * 4 : index * 4 + 4]))
        assert restored.x == pytest.approx(original.box.x, abs=1e-4)
        assert restored.y == pytest.approx(original.box.y, abs=1e-4)
        assert restored.w == pytest.approx(original.box.w, abs=1e-4)
        assert restored.h == pytest.approx(original.box.h, abs=1e-4)


def test_a_page_with_no_words_still_has_a_payload_the_viewer_can_parse():
    """The viewer fetches this file for every page it opens."""
    assert json.loads(page_payload(Page(number=1))) == {"b": [], "n": 0}


def test_the_payload_is_written_beside_the_page_it_belongs_to(tmp_path):
    out = build(tmp_path, collection_of(one_document([text_page(1, ["one two"])])))
    written = json.loads((out / "data" / "memo-2019" / "1.json").read_text())
    assert written["n"] == 2


# ==========================================================================
# 5. human_bytes
# ==========================================================================


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024 - 1, "1024 KB"),
        (1024 * 1024, "1 MB"),
        (5 * 1024 * 1024 + 512 * 1024, "5.5 MB"),
        (1024**3, "1 GB"),
        (1024**4, "1024 GB"),
    ],
)
def test_a_file_size_reads_the_way_a_person_would_say_it(size, text):
    """This number sits beside a download link. A round one loses the ".0"."""
    assert human_bytes(size) == text


# ==========================================================================
# 6. the search contract
# ==========================================================================


def test_the_indexed_body_is_exactly_the_page_words_in_order(tmp_path):
    """Guarantee 3, checked on the file Pagefind actually reads.

    Pagefind strips the tags inside ``data-pagefind-body`` and splits what is
    left on whitespace, so ``result.words[i]`` is an index into ``Page.words``.
    The viewer looks up box ``i`` and draws it on the scan. If this test fails,
    every highlight in every archive built with this version is off by however
    many tokens the page furniture added.
    """
    page = text_page(1, ["The Commission requested all correspondence", "between the office"])
    out = build(tmp_path, collection_of(one_document([page])))
    assert indexed_tokens(page_html(out, "memo-2019", 1)) == [w.text for w in page.words]


def test_a_redaction_bar_contributes_no_tokens_to_the_index(tmp_path):
    """The bars are in the reading order, and they are not words.

    A bar that indexes as one token shifts every highlight after it on the page
    by one - and the pages with bars are the pages people search hardest.
    """
    page = text_page(1, ["the director wrote to", "the contracting authority"])
    page.redactions = [
        Redaction(box=Box(0.55, 0.105, 0.2, 0.01), kind=RedactionKind.VECTOR, codes=["b(6)"]),
        Redaction(box=Box(0.55, 0.145, 0.2, 0.01), kind=RedactionKind.RASTER),
        Redaction(box=Box(0.10, 0.80, 0.6, 0.05), kind=RedactionKind.RASTER),
    ]
    page.exemptions = ["b(6)"]
    out = build(tmp_path, collection_of(one_document([page])))
    html = page_html(out, "memo-2019", 1)

    assert indexed_tokens(html) == [w.text for w in page.words]
    assert "withheld" in html, "the bars should still be in the page for a reader"


def test_punctuation_in_a_word_stays_attached_to_that_word(tmp_path):
    """The index splits on whitespace only, so "authority," is one token.

    The tokens must match ``Page.words`` character for character: if the
    template inserted a space before a comma, or stripped it, the two sequences
    would still be the same length and the highlights would still be wrong.
    """
    page = text_page(
        1, ['Re: "the request", dated 3.4.2019 (b)(6)', "para. 12-14 - see also §7"]
    )
    out = build(tmp_path, collection_of(one_document([page])))
    tokens = indexed_tokens(page_html(out, "memo-2019", 1))
    assert tokens == [w.text for w in page.words]
    assert '"the' in tokens and "(b)(6)" in tokens


def test_a_word_containing_markup_characters_is_indexed_as_written(tmp_path):
    """OCR reads ``<`` and ``&`` off real pages; the token must survive them."""
    page = text_page(1, ["a<b AT&T <script>"])
    out = build(tmp_path, collection_of(one_document([page])))
    html = page_html(out, "memo-2019", 1)
    assert indexed_tokens(html) == ["a<b", "AT&T", "<script>"]
    assert "<script>" not in html, "a token was written into the page as markup"


def test_the_page_with_no_text_has_no_indexed_body_at_all(tmp_path):
    """A blank page in the index is a result that answers nothing."""
    page = Page(number=1, quality=OcrQuality(verdict=PageVerdict.BLANK))
    out = build(tmp_path, collection_of(one_document([page])))
    assert "data-pagefind-body" not in page_html(out, "memo-2019", 1)


def test_every_page_of_every_document_gets_its_own_file(tmp_path):
    """Guarantee 1: one real HTML file per page, readable without JavaScript."""
    docs = [
        one_document([text_page(n, [f"page {n}"]) for n in (1, 2, 3)]),
        one_document(
            [text_page(1, ["second document"])],
            id="letter",
            title="A letter",
            filename="letter.pdf",
            sha256="b" * 64,
        ),
    ]
    out = build(tmp_path, collection_of(*docs))
    for number in (1, 2, 3):
        assert (out / "d" / "memo-2019" / "p" / str(number) / "index.html").is_file()
    assert (out / "d" / "letter" / "p" / "1" / "index.html").is_file()
    assert indexed_tokens(page_html(out, "letter", 1)) == ["second", "document"]


# ==========================================================================
# 7. relative links
# ==========================================================================


def test_the_root_prefix_climbs_one_level_per_path_segment():
    assert _root(0) == ""
    assert _root(4) == "../../../../"


def test_a_page_four_levels_deep_reaches_the_root_with_four_steps(tmp_path):
    """``d/<doc>/p/<n>/index.html`` is the deepest file in the archive.

    Getting this wrong does not break the build, only every stylesheet, image
    and link on the page a reader is most likely to be sent.
    """
    page = text_page(1, ["one"])
    page.images = [ImageVariant("media/memo-2019/p1@1600.webp", "webp", 1600, 2071, 54_000)]
    out = build(tmp_path, collection_of(one_document([page])))
    targets = links_in(out / "d" / "memo-2019" / "p" / "1" / "index.html")
    to_root = [t for t in targets if t.startswith("..")]
    assert to_root, "nothing on the page points back at the site root"
    for target in to_root:
        assert target.startswith("../../../../"), target
        assert not target.startswith("../../../../../"), target


def test_no_link_anywhere_in_the_archive_is_absolute(tmp_path):
    """The same folder has to work at a domain root, in a subdirectory, on a
    USB stick and inside a zip somebody downloaded. A leading slash breaks
    three of those, and only for the reader."""
    page = text_page(1, ["one"])
    page.images = [ImageVariant("media/memo-2019/p1@1600.webp", "webp", 1600, 2071, 54_000)]
    page.thumbs = [ImageVariant("media/memo-2019/p1@240.webp", "webp", 240, 310, 5_000)]
    out = build(tmp_path, collection_of(one_document([page])))
    for html in sorted(out.rglob("*.html")):
        for target in links_in(html):
            assert not target.startswith("/"), f"{html.name}: {target}"
            assert not target.startswith("//"), f"{html.name}: {target}"


def test_no_relative_link_climbs_out_of_the_site(tmp_path):
    """One ``../`` too many and the archive reads files from its own host."""
    out = build(tmp_path, collection_of(one_document([text_page(1, ["one"])])))
    root = out.resolve()
    for html in sorted(out.rglob("*.html")):
        for target in links_in(html):
            if target.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            resolved = (html.parent / target.split("#")[0]).resolve()
            assert root in resolved.parents or resolved == root, f"{html.name}: {target}"


# ==========================================================================
# 8. the manifest
# ==========================================================================


def test_the_manifest_is_valid_json_and_names_every_document(tmp_path):
    """An archive earns trust by being checkable, and this is the file that
    lets somebody check it: one digest per document, plus what built it."""
    docs = [
        one_document([text_page(1, ["one"])]),
        one_document(
            [text_page(1, ["two"])],
            id="letter",
            title="A letter",
            filename="letter.pdf",
            sha256="b" * 64,
            size_bytes=1024,
        ),
    ]
    out = build(tmp_path, collection_of(*docs))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["title"] == "Papers of the Commission"
    assert manifest["stackroom"]
    assert manifest["built_at"]
    listed = {entry["id"]: entry for entry in manifest["documents"]}
    assert set(listed) == {"memo-2019", "letter"}
    for doc in docs:
        entry = listed[doc.id]
        assert entry["sha256"] == doc.sha256
        assert len(entry["sha256"]) == 64
        assert entry["pages"] == doc.page_count
        assert entry["filename"] == doc.filename
        assert entry["bytes"] == doc.size_bytes


def test_the_manifest_carries_what_was_withheld_and_what_is_missing(tmp_path):
    """A gap in the control numbers is a page withheld in full, and it belongs
    in the machine-readable record as much as on the withheld page."""
    page = text_page(1, ["one"])
    page.redactions = [Redaction(box=Box(0.1, 0.1, 0.2, 0.02), kind=RedactionKind.VECTOR)]
    doc = one_document([page], bates_prefix="ABC-", bates_gaps=[("ABC-000002", "ABC-000004")])
    out = build(tmp_path, collection_of(doc))
    entry = json.loads((out / "manifest.json").read_text())["documents"][0]
    assert entry["bates_prefix"] == "ABC-"
    assert entry["bates_gaps"] == [["ABC-000002", "ABC-000004"]]
    assert entry["redacted_pages"] == 1


def test_the_collection_index_is_valid_json(tmp_path):
    """``data/docs.json`` is what the client uses to name a document by id."""
    out = build(tmp_path, collection_of(one_document([text_page(1, ["one"])])))
    docs = json.loads((out / "data" / "docs.json").read_text(encoding="utf-8"))
    assert docs == {"memo-2019": {"t": "Memorandum, March 2019", "p": 1}}


SEARCH_CONFIG = re.compile(
    r'<script type="application/json" id="search-config">(.*?)</script>', re.S
)


def search_config(out: Path) -> dict:
    """The JSON block ``assets/search.js`` reads out of the search page."""
    html = (out / "search" / "index.html").read_text(encoding="utf-8")
    match = SEARCH_CONFIG.search(html)
    assert match, "the search page carries no config for search.js to read"
    return json.loads(match.group(1))


def test_the_search_caveat_counts_the_pages_the_index_actually_holds(tmp_path):
    """"Search covers N of M" has to be the index's N, not a guess at it.

    ``search.js`` renders ``{pages - unreadablePages} of {pages}``, and this used
    to pass ``stats.unreadable_pages`` - pages with ink on them that OCR could
    not read. That is not the set of pages missing from the index: a blank page
    and a page that is a photograph are perfectly readable and neither is in it,
    because neither has any text to find. The error was always in the same
    direction, and it was in the one sentence on the site whose entire job is to
    stop a reader concluding a phrase is not in the archive when it may be.
    """
    pages = [text_page(n, [f"page {n}"]) for n in range(1, 15)]
    pages.append(Page(number=15, quality=OcrQuality(verdict=PageVerdict.UNREADABLE)))
    pages.append(Page(number=16, quality=OcrQuality(verdict=PageVerdict.BLANK)))
    collection = collection_of(one_document(pages))
    collection.stats.unreadable_pages = 1
    collection.stats.blank_pages = 1

    cfg = Config()
    out = tmp_path / "site"
    builder = SiteBuilder(collection, cfg, out)
    builder.copy_assets()
    builder.build_search(IndexInfo(pages_indexed=14))

    config = search_config(out)
    assert config["pages"] == 16
    assert config["indexedPages"] == 14
    assert config["pages"] - config["unreadablePages"] == 14, (
        "the caveat would read 'covers 15 of 16' with the unreadable count"
    )


def test_the_search_caveat_is_written_after_the_index_exists(tmp_path):
    """The number is only knowable once pagefind has run, so the page waits.

    An end-to-end check that the ordering inside ``SiteBuilder.run`` actually
    holds: the caveat on disk agrees with what the build reports it indexed.
    """
    pytest.importorskip("stackroom.build.search")
    from stackroom.build import search as search_mod

    usable, detail = search_mod.pagefind_available()
    if not usable:
        pytest.skip(f"pagefind unavailable: {detail}")

    pages = [text_page(n, [f"unmistakable token{n}"]) for n in range(1, 4)]
    pages.append(Page(number=4, quality=OcrQuality(verdict=PageVerdict.BLANK)))
    cfg = Config()
    out = tmp_path / "site"
    report = build_site(collection_of(one_document(pages)), cfg, out)

    assert report.search is not None
    config = search_config(out)
    assert config["indexedPages"] == report.search.pages_indexed
    assert config["pages"] - config["unreadablePages"] == report.search.pages_indexed


def test_the_two_digest_paragraph_comes_from_the_catalogue(tmp_path):
    """The About page's explanation of ``sha256`` vs ``published_sha256``.

    It shipped as English marked ``lang="en"``, in an archive whose interface
    may not be. It is two keys now - one that agrees with a count and one that
    frames three literal names in markup - so a Russian build says it in
    Russian and a reader is not shown two hashes of the same document with no
    account of why they differ.
    """
    from stackroom.ingest.pdf import PublishedFile

    pages = [text_page(1, ["one"])]
    for language, fragment in (("en", "Two digests are shown"), ("ru", "два отпечатка")):
        collection = collection_of(one_document(pages))
        doc = collection.documents[0]
        doc.sha256 = "a" * 64
        cfg = Config()
        cfg.language = language
        out = tmp_path / language
        builder = SiteBuilder(collection, cfg, out)
        builder.published[doc.id] = PublishedFile(sha256="b" * 64, stripped=True)
        builder.build_about()

        html = (out / "about" / "index.html").read_text(encoding="utf-8")
        assert fragment in html, f"{language}: the paragraph was not translated"
        assert "published_sha256" in html, "the field names are literal and must survive"
        assert "shasum -a 256" in html
        assert '<span class="mono">sha256</span>' in html, "the _html message lost its markup"
        assert "manifest.json" in html
        assert "[about.two_digests" not in html


def test_an_ordinary_build_says_nothing_about_two_digests(tmp_path):
    """Nothing was stripped, so there is no second digest and no paragraph."""
    collection = collection_of(one_document([text_page(1, ["one"])]))
    out = tmp_path / "site"
    SiteBuilder(collection, Config(), out).build_about()
    html = (out / "about" / "index.html").read_text(encoding="utf-8")
    assert "published_sha256" not in html


def test_the_front_page_names_both_denominators_of_the_withheld_share(tmp_path):
    """Either figure alone misleads, in opposite directions.

    One page withheld in full out of a thousand is 100% of that page and 0.1%
    of the release. ``redaction_ratio_collection`` was computed, written into
    ``manifest.json``, printed by the command line - and printed nowhere a
    reader would ever see it.
    """
    collection = collection_of(one_document([text_page(n, [f"page {n}"]) for n in range(1, 11)]))
    collection.stats.pages_with_redactions = 1
    collection.stats.redaction_ratio = 1.0
    collection.stats.redaction_ratio_collection = 0.1
    out = tmp_path / "site"
    builder = SiteBuilder(collection, Config(), out)
    builder.build_index()

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "100.0%" in html and "of the content on 1 redacted page" in html
    assert "10.0%" in html and "of everything in this release" in html


def test_the_withheld_page_names_both_denominators_too(tmp_path):
    """The page whose whole subject is this number must not print half of it.

    The front page carries both shares; ``withheld/index.html`` carried only
    the first, under the same catalogue key, so a reader who followed the link
    to read more about the figure saw less about it than the page they came
    from. Same two numbers, same two sentences, same order.
    """
    page = text_page(1, ["one"])
    page.redactions = [Redaction(box=Box(0.1, 0.1, 0.5, 0.2), kind=RedactionKind.VECTOR)]
    collection = collection_of(one_document([page] + [text_page(n, [f"page {n}"]) for n in range(2, 11)]))
    collection.stats.pages_with_redactions = 1
    collection.stats.redaction_ratio = 1.0
    collection.stats.redaction_ratio_collection = 0.1
    out = tmp_path / "site"
    SiteBuilder(collection, Config(), out).build_withheld()

    html = (out / "withheld" / "index.html").read_text(encoding="utf-8")
    assert "100.0%" in html and "of the content on 1 redacted page" in html
    assert "10.0%" in html and "of everything in this release" in html


def test_search_js_is_given_the_covered_count_rather_than_deriving_it():
    """One subtraction, in one place, done by the side that has the number.

    ``build_search`` writes ``indexedPages`` - what pagefind reported it took -
    and ``unreadablePages`` beside it. ``search.js`` used to render
    ``pages - unreadablePages`` and arrive at the same figure the long way
    round; the sentence it renders is the one on the site whose whole job is
    not to flatter the archive, so it should be handed the number.
    """
    source = (ASSETS / "search.js").read_text(encoding="utf-8")
    assert "cfg.indexedPages" in source
    assert "cfg.pages - u" not in source
    assert "cfg.pages - cfg.unreadablePages" not in source


def test_a_build_with_no_search_index_says_it_covers_nothing(tmp_path):
    """Zero indexed is a true "covers 0 of 16", not a silent full-coverage claim."""
    pages = [text_page(n, [f"page {n}"]) for n in range(1, 5)]
    collection = collection_of(one_document(pages))
    out = tmp_path / "site"
    builder = SiteBuilder(collection, Config(), out)
    builder.copy_assets()
    builder.build_search(None)

    config = search_config(out)
    assert config["indexedPages"] == 0
    assert config["unreadablePages"] == config["pages"] == 4


def test_two_builds_with_source_date_epoch_are_byte_identical(tmp_path, monkeypatch):
    """Guarantee 6, made true. ``built_at`` was the one thing standing in its way.

    It reads the clock, it is written into ``manifest.json``, and it is printed
    into the footer of *every* page - so two people building the same folder on
    different days differed in every file in the site, not in one line of one
    file. With ``SOURCE_DATE_EPOCH`` set, the whole tree is a function of the
    input bytes and can be compared byte for byte.
    """
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1717200000")

    def build_into(name: str) -> Path:
        # A fresh Collection each time: BuildInfo is stamped when one is made,
        # so reusing an object would prove nothing about the clock.
        pages = [text_page(1, ["a first page"]), text_page(2, ["a second page"])]
        out = tmp_path / name
        build_site(collection_of(one_document(pages)), _no_search(), out)
        return out

    first, second = build_into("one"), build_into("two")

    left = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    right = sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    assert left == right, "the two builds do not even hold the same files"
    differing = [
        str(rel)
        for rel in left
        if (first / rel).read_bytes() != (second / rel).read_bytes()
    ]
    assert differing == []
    # And the stamp really is the pinned one, so two builds a second apart
    # cannot pass this by accident.
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["built_at"] == "2024-06-01T00:00:00+00:00"


def test_without_source_date_epoch_the_build_stamp_is_still_the_clock(tmp_path, monkeypatch):
    """The default has to be unchanged: a build with no environment set still
    records when it ran."""
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    out = build(tmp_path, collection_of(one_document([text_page(1, ["one"])])))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["built_at"].endswith("+00:00")
    assert manifest["built_at"][:4].isdigit()


def _no_search() -> Config:
    cfg = Config()
    cfg.search.enabled = False
    return cfg


def test_github_pages_is_stopped_from_deleting_the_search_index(tmp_path):
    """Jekyll silently removes directories beginning with an underscore."""
    out = build(tmp_path, collection_of(one_document([text_page(1, ["one"])])))
    assert (out / ".nojekyll").is_file()


# ==========================================================================
# 9. the operator's own words
# ==========================================================================


def test_the_about_file_is_rendered_as_html_and_not_shown_as_source(tmp_path):
    """``about.md`` is the provenance narrative, and it is inserted unescaped.

    ``attach_about`` is the only place in the builder that puts a string into a
    template without the autoescape catching it, which is exactly why the
    renderer it goes through has its own test file.
    """
    cfg = Config()
    cfg.search.enabled = False
    cfg.about_path = tmp_path / "about.md"
    cfg.about_path.write_text(
        "Released to **the Commission** in March 2019.\n\n"
        "- 412 pages\n- 61 withheld in full\n",
        encoding="utf-8",
    )
    collection = collection_of(one_document([text_page(1, ["one"])]))
    attach_about(collection, cfg)

    assert "<strong>the Commission</strong>" in collection.about_html
    out = build(tmp_path, collection, cfg)
    about = (out / "about" / "index.html").read_text(encoding="utf-8")
    assert "<strong>the Commission</strong>" in about
    assert "&lt;strong&gt;" not in about, "the rendered HTML was escaped a second time"
    assert "61 withheld in full" in about


def test_a_collection_with_no_about_file_gets_no_empty_prose_block(tmp_path):
    """The template checks this value for truth; an empty div is furniture."""
    cfg = Config()
    cfg.search.enabled = False
    collection = collection_of(one_document([text_page(1, ["one"])]))
    attach_about(collection, cfg)
    assert collection.about_html == ""


def test_markup_pasted_into_the_about_file_does_not_reach_the_page(tmp_path):
    """The operator is usually pasting from an agency's cover letter.

    ``textblock`` is what makes this safe; this test is the assertion that the
    builder actually goes through it.
    """
    cfg = Config()
    cfg.search.enabled = False
    cfg.about_path = tmp_path / "about.md"
    cfg.about_path.write_text("<script>alert(1)</script>\n", encoding="utf-8")
    collection = collection_of(one_document([text_page(1, ["one"])]))
    attach_about(collection, cfg)
    about = (build(tmp_path, collection, cfg) / "about" / "index.html").read_text()
    assert "<script>alert(1)</script>" not in about
    assert "&lt;script&gt;" in about
