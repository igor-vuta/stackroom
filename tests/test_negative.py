"""The negative, tested against the data model rather than the pipeline.

Every collection here is built by hand from ``model.py``, which is the seam
``docs/ARCHITECTURE.md`` describes: ingest never renders, build never parses.
It is what makes it possible to test the field a ten-thousand-redaction release
produces without owning ten thousand redactions.

The load-bearing tests are the three about geometry. This page makes one claim
that everything else on it rests on - *a rectangle's area on screen is its
share of the page it came from* - and a picture that quietly normalises sizes
to tidy the grid would be a lie told in a medium a reader cannot check. If
:func:`test_a_redaction_twice_the_size_is_drawn_twice_the_size` and its two
neighbours pass, the picture is honest; if they fail, everything above them on
the page is decoration.

As elsewhere in this suite, nothing is asserted about wording or layout: those
belong to the template and change. What is asserted is structure and number.
"""

from __future__ import annotations

import gzip
import random
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from stackroom.build import negative
from stackroom.build.negative import (
    CELL_LIMIT,
    DETAIL_LIMIT,
    PACKED_LIMIT,
    RECT_LIMIT,
    page_context,
)
from stackroom.build.site import build_site
from stackroom.config import Config
from stackroom.model import (
    Box,
    Collection,
    CollectionStats,
    Document,
    OcrQuality,
    Page,
    PageVerdict,
    Redaction,
    RedactionKind,
)

# --------------------------------------------------------------------------
# building collections by hand
# --------------------------------------------------------------------------


def redaction(x: float, y: float, w: float, h: float, *codes: str) -> Redaction:
    return Redaction(box=Box(x, y, w, h), kind=RedactionKind.VECTOR, codes=list(codes))


def page(
    number: int,
    *redactions: Redaction,
    exemptions: list[str] | None = None,
    ratio: float = 0.1,
    **kwargs,
) -> Page:
    return Page(
        number=number,
        redactions=list(redactions),
        redaction_ratio=ratio,
        exemptions=list(exemptions or []),
        **kwargs,
    )


def document(*pages: Page, id: str = "memo-2019", title: str = "Memorandum") -> Document:
    return Document(
        id=id,
        title=title,
        filename=f"{id}.pdf",
        sha256="a" * 64,
        size_bytes=204_800,
        pages=list(pages),
    )


def collection_of(*documents: Document, **kwargs) -> Collection:
    kwargs.setdefault("title", "Papers of the Commission")
    stats = CollectionStats(
        documents=len(documents),
        pages=sum(d.page_count for d in documents),
        pages_with_redactions=sum(
            1 for d in documents for p in d.pages if p.redactions
        ),
        redaction_boxes=sum(len(p.redactions) for d in documents for p in d.pages),
    )
    return Collection(documents=list(documents), stats=stats, **kwargs)


def many(boxes: int, *, per_page: int = 4, code: str = "b(5)") -> Collection:
    """A release with *boxes* redactions spread evenly over its pages."""
    pages: list[Page] = []
    for index in range(0, boxes, per_page):
        count = min(per_page, boxes - index)
        pages.append(
            page(
                len(pages) + 1,
                *[
                    redaction(0.1, 0.1 + row * 0.05, 0.6, 0.02, code)
                    for row in range(count)
                ],
                exemptions=[code],
            )
        )
    return collection_of(document(*pages))


# --------------------------------------------------------------------------
# reading the field back
# --------------------------------------------------------------------------


def field_of(context: dict, field_id: str = "page"):
    for field in context["fields"]:
        if field.id == field_id:
            return field
    return None


def parsed(field) -> ET.Element:
    """The field as an XML tree, which is also the proof that it is one."""
    return ET.fromstring(str(field.svg))


def rects(tree: ET.Element) -> list[ET.Element]:
    return [
        el
        for el in tree.iter("rect")
        if "negative__paper" not in (el.get("class") or "")
    ]


def geometry(el: ET.Element) -> tuple[float, float, float, float]:
    return (
        float(el.get("x")),
        float(el.get("y")),
        float(el.get("width")),
        float(el.get("height")),
    )


# ==========================================================================
# 1. geometry - the claim the page rests on
# ==========================================================================


def test_a_redaction_twice_the_size_is_drawn_twice_the_size():
    """Areas are proportional, and nothing is normalised to tidy the grid.

    This is the whole argument of the page. A field that scaled a withheld name
    and a withheld chapter to comparable rectangles would be easier to look at
    and would be telling a reader something false about the release.
    """
    collection = collection_of(
        document(
            page(1, redaction(0.1, 0.1, 0.2, 0.1)),
            page(2, redaction(0.1, 0.1, 0.4, 0.1)),
        )
    )
    small, large = rects(parsed(field_of(page_context(collection))))
    small_area = geometry(small)[2] * geometry(small)[3]
    large_area = geometry(large)[2] * geometry(large)[3]
    assert large_area == pytest.approx(small_area * 2, rel=1e-6)


def test_the_ratio_between_the_smallest_and_the_largest_survives_the_drawing():
    """A name and eleven pages are 500 times apart, and stay 500 times apart."""
    collection = collection_of(
        document(
            page(1, redaction(0.1, 0.1, 0.9, 0.9)),
            page(2, redaction(0.1, 0.1, 0.09, 0.018)),
        )
    )
    drawn = [
        geometry(r)[2] * geometry(r)[3]
        for r in rects(parsed(field_of(page_context(collection))))
    ]
    true = [0.9 * 0.9, 0.09 * 0.018]
    # Coordinates are written to two decimals of a thousand-unit field, which
    # is a hundredth of a pixel at any width anybody reads at. That rounding is
    # the only thing standing between these two numbers.
    assert max(drawn) / min(drawn) == pytest.approx(max(true) / min(true), rel=2e-3)


def test_a_box_is_drawn_where_it_sat_on_its_page():
    """Position is true in the page arrangement, or the arrangement is pointless."""
    collection = collection_of(document(page(1, redaction(0.25, 0.5, 0.5, 0.1))))
    tree = parsed(field_of(page_context(collection)))
    paper = next(el for el in tree.iter("path") if "negative__paper" in (el.get("class") or ""))
    # The mount is written as M x y h w v h h -w z, which is the cell.
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", paper.get("d"))]
    cell_x, cell_y, cell_w, cell_h = numbers[0], numbers[1], numbers[2], numbers[3]
    x, y, w, h = geometry(rects(tree)[0])
    assert (x - cell_x) / cell_w == pytest.approx(0.25, abs=1e-3)
    assert (y - cell_y) / cell_h == pytest.approx(0.50, abs=1e-3)
    assert w / cell_w == pytest.approx(0.50, abs=1e-3)
    assert h / cell_h == pytest.approx(0.10, abs=1e-3)


def test_two_pages_of_different_shapes_are_still_drawn_at_the_same_area():
    """A4 and Letter and a fold-out all get the same amount of screen.

    Otherwise "a third of the page" means one thing on one page and another
    thing on the next, and the field cannot be compared with itself.
    """
    collection = collection_of(
        document(
            page(1, redaction(0.0, 0.0, 0.5, 0.5), width_pt=612, height_pt=792),
            page(2, redaction(0.0, 0.0, 0.5, 0.5), width_pt=595, height_pt=842),
        )
    )
    areas = [
        geometry(r)[2] * geometry(r)[3]
        for r in rects(parsed(field_of(page_context(collection))))
    ]
    # Within the two decimals the coordinates are written to.
    assert areas[0] == pytest.approx(areas[1], rel=1e-3)


def test_a_box_that_runs_off_the_page_is_trimmed_to_it():
    """Detection can overshoot the edge; a rectangle must not land on the
    neighbouring page and read as a redaction that is not there."""
    collection = collection_of(document(page(1, redaction(0.8, 0.9, 0.5, 0.4))))
    tree = parsed(field_of(page_context(collection)))
    paper = next(el for el in tree.iter("path") if "negative__paper" in (el.get("class") or ""))
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", paper.get("d"))]
    cell_x, cell_y, cell_w, cell_h = numbers[0], numbers[1], numbers[2], numbers[3]
    x, y, w, h = geometry(rects(tree)[0])
    assert x + w <= cell_x + cell_w + 1e-6
    assert y + h <= cell_y + cell_h + 1e-6


def test_a_landscape_page_does_not_push_the_grid_off_the_edge_of_the_field():
    """Columns are divided out of a fixed width, so a cell can never be wider
    than one. A fold-out among letter pages used to walk the last column of
    every row past the edge of the viewBox, where it is simply not drawn."""
    pages = [page(n, redaction(0.1, 0.1, 0.3, 0.05)) for n in range(1, 13)]
    pages.append(page(13, redaction(0.1, 0.1, 0.3, 0.05), width_pt=1224, height_pt=612))
    tree = parsed(field_of(page_context(collection_of(document(*pages)))))
    paper = next(el for el in tree.iter("path") if "negative__paper" in (el.get("class") or ""))
    # M x y h w v h h -w z: five numbers to a cell.
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", paper.get("d"))]
    assert len(numbers) == 13 * 5
    for i in range(0, len(numbers), 5):
        x, w = numbers[i], numbers[i + 2]
        assert x + w <= negative.FIELD_WIDTH + 0.01
    for el in rects(tree):
        x, _y, w, _h = geometry(el)
        assert x + w <= negative.FIELD_WIDTH + 0.01


def test_a_page_drawn_smaller_to_fit_the_grid_is_admitted_to():
    pages = [page(n, redaction(0.1, 0.1, 0.3, 0.05)) for n in range(1, 13)]
    pages.append(page(13, redaction(0.1, 0.1, 0.3, 0.05), width_pt=1224, height_pt=612))
    spots = page_context(collection_of(document(*pages)))["blind_spots"]
    assert any("unusual size" in s["heading"] for s in spots)


def test_a_box_with_no_area_is_not_drawn_at_all():
    collection = collection_of(document(page(1, redaction(0.1, 0.1, 0.0, 0.2))))
    context = page_context(collection)
    assert context["fields"] == []


# ==========================================================================
# 2. every rectangle leads back
# ==========================================================================


def test_every_rectangle_is_inside_a_link_to_the_page_it_came_from():
    """The picture is a finding aid, not an illustration."""
    collection = collection_of(
        document(
            page(1, redaction(0.1, 0.1, 0.2, 0.02)),
            page(3, redaction(0.1, 0.2, 0.2, 0.02), redaction(0.1, 0.3, 0.2, 0.02)),
            id="release", title="Release",
        )
    )
    for field in page_context(collection)["fields"]:
        tree = parsed(field)
        found = 0
        for link in tree.iter("a"):
            href = link.get("href")
            assert re.fullmatch(r"\.\./\.\./d/release/p/[13]/index\.html", href), href
            found += len(rects(link))
        assert found == 3, f"{field.id} drew {found} rectangles inside links"


def test_a_rectangle_is_never_a_tab_stop():
    """Four thousand rectangles must not be four thousand tab stops. The links
    are reachable by pointer, by the roving tab stop the script adds, and in
    full in the index list under the field."""
    collection = many(200)
    for field in page_context(collection)["fields"]:
        for link in parsed(field).iter("a"):
            assert link.get("tabindex") == "-1"


def test_the_field_says_what_it_is_to_a_screen_reader():
    collection = many(12)
    field = field_of(page_context(collection))
    tree = parsed(field)
    assert tree.get("role") == "img"
    assert "12 redactions" in tree.get("aria-label")


def test_every_page_in_the_field_is_also_in_the_list_under_it():
    """The list is the keyboard and screen-reader route into the picture, so
    the two have to agree about what is in it."""
    collection = many(60, per_page=3)
    context = page_context(collection)
    drawn = {link.get("href") for link in parsed(field_of(context)).iter("a")}
    listed = {cell.url for cell in context["cells"]}
    assert drawn == listed


# ==========================================================================
# 3. the ends of the range
# ==========================================================================


def test_a_release_with_nothing_withheld_draws_no_field_at_all():
    """Not an empty picture: no picture, and a sentence saying so."""
    collection = collection_of(document(page(1), page(2)))
    context = page_context(collection)
    assert context["fields"] == []
    assert context["cells"] == []
    assert context["code_rows"] == []
    assert context["selection"].total_marks == 0


def test_one_redaction_is_a_field_of_one_rectangle_on_one_page():
    collection = collection_of(document(page(1, redaction(0.2, 0.3, 0.4, 0.05))))
    context = page_context(collection)
    field = field_of(context)
    assert len(rects(parsed(field))) == 1
    assert field.rects == 1
    assert len(context["cells"]) == 1


def test_a_single_page_is_not_drawn_a_metre_high():
    """One redacted page must not become one cell as wide as the field."""
    collection = collection_of(document(page(1, redaction(0.2, 0.3, 0.4, 0.05))))
    tree = parsed(field_of(page_context(collection)))
    paper = next(el for el in tree.iter("path") if "negative__paper" in (el.get("class") or ""))
    width = float(re.findall(r"h(-?\d+(?:\.\d+)?)", paper.get("d"))[0])
    assert width <= negative.MAX_CELL_W


def test_thousands_of_pages_still_leave_a_cell_wide_enough_to_see():
    collection = many(6_000, per_page=1)
    tree = parsed(field_of(page_context(collection)))
    paper = next(el for el in tree.iter("path") if "negative__paper" in (el.get("class") or ""))
    width = float(re.findall(r"h(-?\d+(?:\.\d+)?)", paper.get("d"))[0])
    assert width >= negative.MIN_CELL_W * 0.9


def test_past_the_ceiling_the_field_says_how_much_it_left_out():
    """A picture that quietly omits four fifths of its subject is worse than
    one that admits it, which is the whole argument of this page applied to
    the page itself."""
    collection = many(CELL_LIMIT * 3, per_page=1)
    selection = page_context(collection)["selection"]
    assert not selection.complete
    assert selection.dropped_cells > 0
    assert len(selection.cells) == CELL_LIMIT
    assert selection.total_cells == CELL_LIMIT * 3
    assert 0 < selection.area_share <= 1


def test_what_survives_the_ceiling_is_the_pages_with_the_most_taken_out():
    small = [page(n, redaction(0.1, 0.1, 0.05, 0.01)) for n in range(1, CELL_LIMIT + 1)]
    big = [
        page(n, redaction(0.05, 0.05, 0.9, 0.9))
        for n in range(CELL_LIMIT + 1, CELL_LIMIT + 51)
    ]
    context = page_context(collection_of(document(*small, *big)))
    kept = {cell.number for cell in context["selection"].cells}
    assert all(p.number in kept for p in big)


def test_a_release_over_the_rectangle_ceiling_keeps_the_largest_boxes():
    pages = []
    for n in range(1, 41):
        boxes = [redaction(0.1, 0.1 + i * 0.002, 0.02, 0.002) for i in range(400)]
        boxes.append(redaction(0.1, 0.5, 0.8, 0.3))
        pages.append(page(n, *boxes))
    context = page_context(collection_of(document(*pages)))
    selection = context["selection"]
    assert selection.total_marks == 40 * 401
    assert selection.drawn_marks == RECT_LIMIT
    assert selection.area_share > 0.9


def test_a_big_release_is_drawn_as_paths_rather_than_rectangles():
    """Past the detail limit each page's boxes are merged per code: half the
    bytes for the same picture, and the links survive because they are per
    page rather than per box."""
    context = page_context(many(DETAIL_LIMIT + 400, per_page=8))
    field = field_of(context)
    tree = parsed(field)
    assert rects(tree) == []
    drawn = [el for el in tree.iter("path") if "negative__paper" not in (el.get("class") or "")]
    assert drawn, "the field drew nothing"
    assert sum(el.get("d").count("z") for el in drawn) == field.rects
    assert list(tree.iter("a")), "a merged field still links back to its pages"


def test_the_regrouped_arrangements_stop_before_the_page_costs_more_than_it_is_worth():
    small = page_context(many(PACKED_LIMIT - 20))
    large = page_context(many(PACKED_LIMIT + 20))
    assert [f.id for f in small["fields"]] == ["page", "code", "size"]
    assert [f.id for f in large["fields"]] == ["page"]
    assert large["packed"] is False


# ==========================================================================
# 4. exemptions
# ==========================================================================


def test_a_code_printed_beside_a_box_belongs_to_that_box():
    collection = collection_of(
        document(page(1, redaction(0.1, 0.1, 0.2, 0.02, "b(5)"), exemptions=["b(5)", "b(6)"]))
    )
    rows = page_context(collection)["code_rows"]
    assert [row["code"] for row in rows] == ["b(5)"]
    assert rows[0]["inherited"] == 0


def test_one_code_for_a_whole_page_is_carried_by_everything_on_it():
    """A weaker claim than a code stamped beside the box, so it is counted
    separately and the page says which is which."""
    collection = collection_of(
        document(page(1, redaction(0.1, 0.1, 0.2, 0.02), exemptions=["b(5)"]))
    )
    rows = page_context(collection)["code_rows"]
    assert rows[0]["code"] == "b(5)"
    assert rows[0]["inherited"] == 1


def test_several_page_codes_and_no_box_code_attributes_nothing():
    """Choosing whichever code is nearest would be inventing a fact, which is
    what ingest/exemptions.py refuses to do and this page has no business
    undoing."""
    collection = collection_of(
        document(page(1, redaction(0.1, 0.1, 0.2, 0.02), exemptions=["b(5)", "b(6)"]))
    )
    rows = page_context(collection)["code_rows"]
    assert [row["code"] for row in rows] == [""]


def test_the_law_is_spelled_out_beside_every_code_it_counts():
    collection = collection_of(
        document(page(1, redaction(0.1, 0.1, 0.2, 0.02, "b(5)"), exemptions=["b(5)"]))
    )
    row = page_context(collection)["code_rows"][0]
    assert "deliberative-process" in row["label"]


def test_codes_are_ordered_by_how_much_they_took():
    collection = collection_of(
        document(
            page(1, redaction(0.1, 0.1, 0.05, 0.01, "b(6)"), exemptions=["b(6)"]),
            page(2, redaction(0.1, 0.1, 0.8, 0.6, "b(5)"), exemptions=["b(5)"]),
        )
    )
    rows = page_context(collection)["code_rows"]
    assert [row["code"] for row in rows] == ["b(5)", "b(6)"]
    assert rows[0]["total"] > rows[1]["total"]


def test_every_rectangle_carries_the_class_of_the_code_it_was_withheld_under():
    """The class is what the filter switches on, so a rectangle with no class
    is a rectangle no filter can ever find."""
    collection = collection_of(
        document(
            page(1, redaction(0.1, 0.1, 0.2, 0.02, "b(5)")),
            page(2, redaction(0.1, 0.1, 0.2, 0.02)),
        )
    )
    context = page_context(collection)
    classes = {r.get("class") for r in rects(parsed(field_of(context)))}
    assert all(c and re.fullmatch(r"c\d+", c) for c in classes)
    assert len(classes) == 2, "a coded box and an uncited one are not the same class"


# ==========================================================================
# 5. what the picture cannot show
# ==========================================================================


def test_pages_withheld_in_full_are_named_even_though_they_have_no_rectangle():
    doc = document(page(1, redaction(0.1, 0.1, 0.2, 0.02)))
    doc.bates_gaps = [("OCA000005", "OCA000008")]
    headings = [s["heading"] for s in page_context(collection_of(doc))["blind_spots"]]
    assert any("withheld in full" in h for h in headings)


def test_a_gap_counts_both_of_its_ends():
    """The gap is the inclusive run of numbers nobody delivered, so a gap from
    5 to 8 is four missing pages and not two."""
    doc = document(page(1, redaction(0.1, 0.1, 0.2, 0.02)))
    doc.bates_gaps = [("OCA000005", "OCA000008")]
    spot = next(
        s for s in page_context(collection_of(doc))["blind_spots"]
        if "withheld in full" in s["heading"]
    )
    assert "4 pages" in spot["body"]


def test_pages_search_cannot_read_are_named():
    collection = collection_of(
        document(
            page(1, redaction(0.1, 0.1, 0.2, 0.02)),
            page(2, quality=OcrQuality(verdict=PageVerdict.UNREADABLE)),
        )
    )
    headings = [s["heading"] for s in page_context(collection)["blind_spots"]]
    assert any("could not read" in h for h in headings)


def test_the_limits_of_detection_are_stated_even_when_nothing_else_is_wrong():
    """A redaction made by deleting the text leaves no shape to find. That is
    true of every release, so it is said on every one of these pages."""
    collection = collection_of(document(page(1, redaction(0.1, 0.1, 0.2, 0.02))))
    headings = [s["heading"] for s in page_context(collection)["blind_spots"]]
    assert any("leave no mark" in h for h in headings)


def test_boxes_with_no_code_beside_them_are_counted_out_loud():
    collection = collection_of(document(page(1, redaction(0.1, 0.1, 0.2, 0.02))))
    spots = page_context(collection)["blind_spots"]
    assert any("no law printed" in s["heading"] for s in spots)


# ==========================================================================
# 6. it is a document, and it is the same document every time
# ==========================================================================


@pytest.mark.parametrize("boxes", [1, 10, 400, 5_000])
def test_every_field_is_well_formed_xml(boxes):
    for field in page_context(many(boxes))["fields"]:
        ET.fromstring(str(field.svg))


def test_a_document_title_with_markup_in_it_cannot_break_out_of_the_picture():
    collection = collection_of(
        document(page(1, redaction(0.1, 0.1, 0.2, 0.02)), title='Memo </text><script>x')
    )
    field = field_of(page_context(collection))
    assert "<script>" not in str(field.svg)
    ET.fromstring(str(field.svg))


def test_the_same_release_produces_the_same_bytes_twice():
    """Guarantee 6 in docs/ARCHITECTURE.md, applied here: two people must be
    able to check they published the same picture."""
    collection = many(300)
    first = [str(f.svg) for f in page_context(collection)["fields"]]
    second = [str(f.svg) for f in page_context(many(300))["fields"]]
    assert first == second


def test_no_link_in_the_field_is_absolute(tmp_path):
    """The same rule the rest of the archive keeps: this folder has to work at
    a domain root, in a subdirectory, on a memory stick and inside a zip."""
    collection = many(40)
    for field in page_context(collection)["fields"]:
        for link in parsed(field).iter("a"):
            href = link.get("href")
            assert not href.startswith(("/", "http:", "https:", "//")), href


# ==========================================================================
# 7. byte cost
# ==========================================================================


def scattered(boxes: int) -> Collection:
    """A release whose boxes are all in different places.

    The byte cost has to be measured against this rather than against
    :func:`many`, whose identical rectangles gzip away to nothing and would
    make the page look four times cheaper than it is.
    """
    rng = random.Random(11)
    pages: list[Page] = []
    left = boxes
    while left > 0:
        count = min(left, rng.choice([1, 1, 2, 3, 5]))
        left -= count
        marks = []
        for _ in range(count):
            w = rng.uniform(0.05, 0.8)
            h = rng.uniform(0.012, 0.4)
            marks.append(
                redaction(
                    rng.uniform(0.05, max(0.06, 0.93 - w)),
                    rng.uniform(0.05, max(0.06, 0.93 - h)),
                    w,
                    h,
                    rng.choice(["b(5)", "b(6)", "b(7)(C)"]),
                )
            )
        pages.append(page(len(pages) + 1, *marks))
    return collection_of(document(*pages))


def measure(boxes: int) -> tuple[int, int]:
    """Raw and gzipped bytes of every field a release of *boxes* produces."""
    text = "".join(str(f.svg) for f in page_context(scattered(boxes))["fields"])
    return len(text.encode()), len(gzip.compress(text.encode(), 9))


@pytest.mark.parametrize(
    "boxes,raw_ceiling,gz_ceiling",
    [
        (10, 8_000, 2_000),
        (100, 48_000, 8_000),
        (1_000, 128_000, 24_000),
        (10_000, 448_000, 128_000),
    ],
)
def test_the_field_costs_what_it_is_documented_to_cost(boxes, raw_ceiling, gz_ceiling):
    """One rect per redaction is fine for thousands and not for tens of
    thousands, which is why the renderer changes shape on the way up and stops
    drawing the regrouped arrangements at all.

    Both numbers are here on purpose. The gzipped one is what a reader pays for
    over the wire, and it is small because this markup is the same forty
    characters over and over. The raw one is what their browser has to parse,
    and it is the one that gets away from you. These are the promise
    ``docs/ARCHITECTURE.md`` makes about this page; if one fails, the table
    there is wrong and wants rewriting rather than this number nudging up.
    """
    raw, packed = measure(boxes)
    assert raw < raw_ceiling, f"{boxes} redactions cost {raw:,} bytes"
    assert packed < gz_ceiling, f"{boxes} redactions cost {packed:,} bytes gzipped"


def test_the_merged_renderer_is_cheaper_per_rectangle_than_the_detailed_one():
    detail_raw, _ = measure(DETAIL_LIMIT - 100)
    merged_raw, _ = measure(DETAIL_LIMIT + 100)
    assert merged_raw / (DETAIL_LIMIT + 100) < detail_raw / (DETAIL_LIMIT - 100)


# ==========================================================================
# 8. the page, built
# ==========================================================================


def build(tmp_path: Path, collection: Collection) -> Path:
    """Write the site, with the negative wired in the way site.py wires it."""
    cfg = Config()
    cfg.search.enabled = False
    out = tmp_path / "site"

    from stackroom.build.site import SiteBuilder

    original = SiteBuilder.run

    def run(self):
        report = original(self)
        negative.build(self)
        return report

    SiteBuilder.run = run
    try:
        build_site(collection, cfg, out)
    finally:
        SiteBuilder.run = original
    return out


def negative_html(out: Path) -> str:
    return (out / "withheld" / "negative" / "index.html").read_text(encoding="utf-8")


def test_the_page_is_written_where_the_withheld_page_points(tmp_path):
    out = build(tmp_path, many(20))
    assert (out / "withheld" / "negative" / "index.html").is_file()
    withheld = (out / "withheld" / "index.html").read_text(encoding="utf-8")
    assert "negative/index.html" in withheld


def test_the_built_page_carries_the_field_and_the_list_of_the_same_pages(tmp_path):
    out = build(tmp_path, many(24, per_page=3))
    html = negative_html(out)
    assert html.count('data-negative="page"') == 1
    pages = set(re.findall(r'href="(\.\./\.\./d/memo-2019/p/\d+/index\.html)"', html))
    assert len(pages) == 8


def test_the_page_still_builds_when_there_is_nothing_to_draw(tmp_path):
    out = build(tmp_path, collection_of(document(page(1), page(2))))
    html = negative_html(out)
    assert "<svg" not in html
    assert "nothing" in html.lower()


def test_the_withheld_page_can_be_filtered_to_one_code_without_a_script(tmp_path):
    """The codes are links, the rows are pre-rendered and one `:target` rule
    per code does the filtering. Nothing here needs JavaScript, and with the
    rules missing entirely the reader still lands on the code they asked for
    with every page listed under it."""
    collection = collection_of(
        document(
            page(1, redaction(0.1, 0.1, 0.2, 0.02, "b(5)"), exemptions=["b(5)"], ratio=0.4),
            page(2, redaction(0.1, 0.1, 0.2, 0.02, "b(6)"), exemptions=["b(6)"], ratio=0.2),
        )
    )
    collection.stats.exemption_counts = {"b(5)": 1, "b(6)": 1}
    out = build(tmp_path, collection)
    html = (out / "withheld" / "index.html").read_text(encoding="utf-8")

    assert 'href="#f-b5"' in html
    assert 'id="f-b5"' in html
    assert "#f-b5:target" in html
    assert html.count("cf-b5") >= 2       # the rule, and the row it hides on

    # The mechanism is a link, a pre-rendered row and a rule. The shell around
    # it loads the archive's own enhancements on every page; the filter itself
    # reaches for nothing.
    section = html[html.index('class="codefilter"'):html.index("</section>")]
    assert "<script" not in section
    assert "data-" not in section
    assert 'href="#w-pages"' in section    # and a way back out of the filter


def test_a_page_with_no_code_on_it_is_still_in_the_withheld_list(tmp_path):
    collection = collection_of(
        document(page(1, redaction(0.1, 0.1, 0.2, 0.02), ratio=0.3), id="rel", title="Release")
    )
    out = build(tmp_path, collection)
    html = (out / "withheld" / "index.html").read_text(encoding="utf-8")
    assert 'd/rel/p/1/index.html' in html
