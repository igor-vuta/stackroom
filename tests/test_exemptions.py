"""Tests for the statutory-code reader.

The bench below is the point of this file. Exemption parsing is the kind of
problem where a regex that looks right passes every example its author thought
of and fails on the first real release, so the cases are written as data:
strings that must be read, strings that must be left alone, and the reason each
one is in the list. Adding a case is one line.
"""

from __future__ import annotations

import random

import pytest

from stackroom.ingest.exemptions import (
    ExemptionHit,
    associate,
    legend,
    scan_document,
    scan_text,
)
from stackroom.model import Box, Redaction, RedactionKind

# --------------------------------------------------------------------------
# the bench
# --------------------------------------------------------------------------

# (text, expected codes, needs the OCR-tolerant reading)
MATCHES: list[tuple[str, list[str], bool]] = [
    # --- the ten verified against real and OCR-damaged releases -----------
    ("(b)(7)(C)", ["b(7)(C)"], False),
    ("b(7)(c)", ["b(7)(C)"], False),
    ("(b) (6)", ["b(6)"], False),
    ("(b](6)", ["b(6)"], False),
    ("(6)(6)", ["b(6)"], True),
    ("[b][7][C]", ["b(7)(C)"], False),
    ("(b)(l)", ["b(1)"], False),
    ("( b ) ( 7 ) ( C )", ["b(7)(C)"], False),
    ("5 U.S.C. 552(b)(5)", ["b(5)"], False),
    ("(b) - (7)(A)", ["b(7)(A)"], False),
    # --- damage of our own, from the same failure modes -------------------
    ("(b)(I)", ["b(1)"], False),
    ("{b}{7}{E}", ["b(7)(E)"], False),
    ("(b)(7)(c) and (b)(6)", ["b(7)(C)", "b(6)"], False),
    ("5 U.S.C. § 552 (b)(7)(D)", ["b(7)(D)"], False),
    ("5 USC 552(b)(4)", ["b(4)"], False),
    ("Exemption (b)(3)", ["b(3)"], False),
    ("Ex. (b)(9)", ["b(9)"], False),
    ("b - (8)", ["b(8)"], False),
    ("(&)(5)", ["b(5)"], False),
    ("(b)\u2013(2)", ["b(2)"], False),  # an en dash, as a real letter uses
    ("552(b)(6)", ["b(6)"], False),
    ("((b)(6))", ["b(6)"], False),
    # --- prose: the two the bracket pattern cannot reach ------------------
    ("withheld under Exemption 5", ["b(5)"], False),
    ("FOIA Exemption No. 7(C)", ["b(7)(C)"], False),
]

REJECTS: list[str] = [
    "(a)(1)",
    "(c)(2)",
    "paragraph (b)",
    "(b)(10)",
    "Figure 5",
    "Exhibit 5",
    # ours: shapes that live in ordinary documents
    "Table 6 (6 rows)",
    "see item (4) below",
    "the (b) column",
    "Exhibit B, page 7",
    "(b)(0)",
    "clause (d)(2)",
    # every statute anyone cites has subsections shaped exactly like a code
    "Rule 26(b)(1)",
    "42 U.S.C. 1983(b)(2)",
    "Fed. R. Civ. P. 26(b)(1)",
    "45 C.F.R. 164.512(b)(1)",
    "Article 8(b)(2) of the agreement",
]


@pytest.mark.parametrize(("text", "expected", "ocr"), MATCHES, ids=[m[0] for m in MATCHES])
def test_bench_matches(text: str, expected: list[str], ocr: bool) -> None:
    hits = scan_text(text, allow_ocr_variants=True)
    assert [h.code for h in hits] == expected
    assert all(h.label for h in hits), "every hit must carry a gloss a reader can use"
    for hit in hits:
        assert text[hit.span[0] : hit.span[1]] == hit.raw


@pytest.mark.parametrize("text", REJECTS, ids=REJECTS)
def test_bench_rejects(text: str) -> None:
    assert scan_text(text, allow_ocr_variants=True) == []


def test_the_bracket_pattern_carries_the_bench_and_prose_is_the_backstop() -> None:
    """Which pattern found what. Two of the bench are out of reach of the
    bracket form entirely - they never write the ``(b)`` - and everything else
    must come from it, because prose is the looser of the two."""
    by_source: dict[str, list[str]] = {}
    for text, _expected, _ocr in MATCHES:
        hits = scan_text(text, allow_ocr_variants=True)
        by_source.setdefault(hits[0].source, []).append(text)
    assert by_source["prose"] == ["withheld under Exemption 5", "FOIA Exemption No. 7(C)"]
    assert by_source["ocr-code"] == ["(6)(6)"]
    assert len(by_source["code"]) == len(MATCHES) - 3


def test_a_statute_cited_by_its_subsection_is_not_a_withholding() -> None:
    """The single largest source of phantom exemptions in litigation records:
    every statute in the world has a subsection (b), and half of them have a
    paragraph (1) under it."""
    for text in ("Rule 26(b)(1)", "42 U.S.C. 1983(b)(2)", "164.512(b)(1)"):
        assert scan_text(text, allow_ocr_variants=True) == [], text
    # but 552 is the statute this module exists for
    assert [h.code for h in scan_text("552(b)(6)")] == ["b(6)"]
    assert [h.code for h in scan_text("5 U.S.C. \u00a7 552(b)(6)")] == ["b(6)"]


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [("(b)(l)", "b(1)"), ("(b)(I)", "b(1)"), ("(b)(i)", "b(1)"), ("(B)(7)(c)", "b(7)(C)")],
)
def test_ocr_digits_are_normalised(text: str, code: str) -> None:
    assert [h.code for h in scan_text(text)] == [code]


def test_o_normalises_to_zero_and_is_then_dropped() -> None:
    """There is no exemption zero, so the vocabulary refuses the hit."""
    assert scan_text("(b)(O)") == []
    assert scan_text("(b)(o)") == []


def test_one_withholding_counted_once() -> None:
    """The cite and the word are the same refusal written twice."""
    assert [h.code for h in scan_text("Exemption 5 U.S.C. 552(b)(5)")] == ["b(5)"]


# --------------------------------------------------------------------------
# the OCR gate
# --------------------------------------------------------------------------


def test_six_for_b_is_gated_off_by_default() -> None:
    assert scan_text("(6)(6)") == []


def test_document_with_a_canonical_code_unlocks_the_damaged_one() -> None:
    pages = ["Withheld under (b)(6).", "See (6)(6) on the attached."]
    got = scan_document(pages)
    assert [h.code for h in got[0]] == ["b(6)"]
    assert [h.code for h in got[1]] == ["b(6)"]
    assert got[1][0].source == "ocr-code"


def test_document_without_one_leaves_numeric_pairs_alone() -> None:
    pages = ["Quarterly totals", "Column (6)(6) of the appendix, and (5)(2)."]
    assert scan_document(pages) == [[], []]


def test_the_gate_is_the_document_not_the_page() -> None:
    """The page with the damaged code is exactly the page with no clean one."""
    pages = ["cover letter citing (b)(7)(C)", "", "(6)(7)"]
    got = scan_document(pages)
    assert [h.code for h in got[2]] == ["b(7)"]


def test_prose_alone_does_not_unlock_the_gate() -> None:
    """"Exemption 5" in a letter is not proof that (6)(6) is a code."""
    got = scan_document(["Withheld under Exemption 5.", "rows (6)(6) and (6)(7)"])
    assert [h.code for h in got[0]] == ["b(5)"]
    assert got[1] == []


# --------------------------------------------------------------------------
# enumerations
# --------------------------------------------------------------------------


def test_and_split_finds_the_second_code() -> None:
    assert [h.code for h in scan_text("Exemptions 6 and 7(C)")] == ["b(6)", "b(7)(C)"]


def test_comma_list() -> None:
    codes = [h.code for h in scan_text("Exemptions 6, 7(C), and 5.")]
    assert codes == ["b(6)", "b(7)(C)", "b(5)"]


def test_enumeration_does_not_swallow_ordinary_numbers() -> None:
    assert [h.code for h in scan_text("Exemption 5 and 200 pages were withheld")] == ["b(5)"]
    assert [h.code for h in scan_text("Exemption 6, dated 3 March")] == ["b(6)"]


def test_a_continuation_must_be_the_whole_span() -> None:
    """The price of that safety, stated: a trailing item with words attached
    is missed rather than guessed. Under-reading a list is a smaller lie than
    reporting a withholding the document never claimed."""
    assert [h.code for h in scan_text("Exemptions 6 and 7 were applied")] == ["b(6)"]


def test_a_footer_legend_of_bare_codes_is_read_whole() -> None:
    """The layout that breaks a naive pattern: ``b`` is a letter in A-F, so the
    subpart group of one code will happily eat the ``(b)`` of the next and take
    the code after it with it."""
    codes = [h.code for h in scan_text("(b)(6) (b)(7)(C) (b)(7)(E)")]
    assert codes == ["b(6)", "b(7)(C)", "b(7)(E)"]
    assert [h.code for h in scan_text("(b)(6)(b)(7)(D)")] == ["b(6)", "b(7)(D)"]


def test_a_following_group_that_is_not_a_code_is_left_alone() -> None:
    assert [h.code for h in scan_text("(b)(6) (a)(1)")] == ["b(6)"]


def test_bracketed_codes_need_no_help() -> None:
    codes = [h.code for h in scan_text("(b)(6), (b)(7)(C) and (b)(7)(E)")]
    assert codes == ["b(6)", "b(7)(C)", "b(7)(E)"]


# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------


def test_every_us_code_has_a_gloss() -> None:
    for n in range(1, 10):
        (hit,) = scan_text(f"(b)({n})")
        assert hit.label and hit.label[0].islower()
    for sub in "ABCDEF":
        (hit,) = scan_text(f"(b)(7)({sub})")
        assert "law-enforcement" in hit.label


def test_privacy_act_companions() -> None:
    codes = [h.code for h in scan_text("(j)(2) and (k)(1) and (k)(7)")]
    assert codes == ["j(2)", "k(1)", "k(7)"]
    assert all("Privacy Act" in h.label for h in scan_text("(j)(2)"))


def test_privacy_act_needs_its_brackets() -> None:
    assert scan_text("k2 mountain") == []


def test_unknown_subpart_falls_back_to_the_parent() -> None:
    (hit,) = scan_text("(b)(7)(Z)")
    assert hit.code == "b(7)"


def test_legend_is_ordered_deduplicated_and_explained() -> None:
    entries = legend(["(b)(7)(C)", "b(6)", "B (6)", "(b)(1)"])
    assert [c for c, _ in entries] == ["b(1)", "b(6)", "b(7)(C)"]
    assert all(gloss for _, gloss in entries)


def test_legend_keeps_codes_it_cannot_explain() -> None:
    (_code, gloss) = legend(["(b)(99)"])[0]
    assert "recognise" in gloss


# --------------------------------------------------------------------------
# other jurisdictions
# --------------------------------------------------------------------------


def test_uk_sections() -> None:
    text = "Information is withheld under section 40(2) and sections 31 and 43."
    codes = [h.code for h in scan_text(text, jurisdiction="uk")]
    assert codes == ["s.40(2)", "s.31", "s.43"]


def test_uk_sections_are_never_scanned_in_us_mode() -> None:
    """Bare section numbers are far too generic to look for by default."""
    text = "See section 40 of the lease and section 21 of the schedule."
    assert scan_text(text) == []
    assert scan_text(text, jurisdiction="uk")


def test_uk_ignores_sections_that_are_not_exemptions() -> None:
    assert scan_text("under section 3 of the contract", jurisdiction="uk") == []


def test_uk_possessive_is_not_a_section() -> None:
    assert scan_text("the agency's 21 employees", jurisdiction="uk") == []


def test_canada_atia() -> None:
    hits = scan_text("Exempted under s.19 and s.23", jurisdiction="ca")
    assert [h.code for h in hits] == ["s.19", "s.23"]
    assert "solicitor-client" in hits[1].label


def test_the_same_number_means_different_things_in_different_acts() -> None:
    (uk,) = scan_text("s.21", jurisdiction="uk")
    (ca,) = scan_text("s.21", jurisdiction="ca")
    assert uk.label != ca.label


def test_eu_articles() -> None:
    hits = scan_text("Refused under Article 4(1)(a) and 4(3)", jurisdiction="eu")
    assert [h.code for h in hits] == ["art.4(1)(a)", "art.4(3)"]


def test_unknown_jurisdiction_is_an_error_not_an_empty_list() -> None:
    with pytest.raises(KeyError):
        scan_text("(b)(6)", jurisdiction="atlantis")


# --------------------------------------------------------------------------
# association
# --------------------------------------------------------------------------


def _box(x: float, y: float, w: float = 0.06, h: float = 0.02) -> Box:
    return Box(x, y, w, h)


def test_code_attaches_to_the_nearest_box() -> None:
    redactions = [
        Redaction(box=Box(0.10, 0.20, 0.30, 0.03), kind=RedactionKind.VECTOR),
        Redaction(box=Box(0.10, 0.60, 0.30, 0.03), kind=RedactionKind.VECTOR),
    ]
    hits = scan_text("(b)(6)")
    associate(hits, [_box(0.42, 0.20)], redactions)
    assert redactions[0].codes == ["b(6)"]
    assert redactions[1].codes == []
    assert hits[0].document_level is False


def test_a_code_too_far_from_any_box_is_document_level() -> None:
    redactions = [Redaction(box=Box(0.1, 0.1, 0.2, 0.03), kind=RedactionKind.VECTOR)]
    hits = scan_text("(b)(5)")
    associate(hits, [_box(0.4, 0.75)], redactions)
    assert redactions[0].codes == []
    assert hits[0].document_level is True


def test_a_footer_legend_is_not_blamed_on_the_lowest_box() -> None:
    """The commonest way to invent a fact about a redaction."""
    redactions = [Redaction(box=Box(0.1, 0.86, 0.3, 0.03), kind=RedactionKind.VECTOR)]
    hits = scan_text("(b)(6) (b)(7)(C) (b)(7)(E)")
    boxes = [_box(0.10, 0.94), _box(0.25, 0.94), _box(0.40, 0.94)]
    associate(hits, boxes, redactions)
    assert redactions[0].codes == []
    assert all(h.document_level for h in hits)


def test_a_box_in_the_footer_still_gets_its_code() -> None:
    redactions = [Redaction(box=Box(0.1, 0.93, 0.2, 0.02), kind=RedactionKind.VECTOR)]
    hits = scan_text("(b)(6)")
    associate(hits, [_box(0.32, 0.93)], redactions)
    assert redactions[0].codes == ["b(6)"]


def test_association_never_duplicates_a_code() -> None:
    redactions = [Redaction(box=Box(0.1, 0.2, 0.3, 0.03), kind=RedactionKind.VECTOR)]
    hits = scan_text("(b)(6) and (b)(6)")
    associate(hits, [_box(0.42, 0.20), _box(0.42, 0.21)], redactions)
    assert redactions[0].codes == ["b(6)"]


def test_hits_without_boxes_are_document_level() -> None:
    hits = [ExemptionHit("b(5)", "x", (0, 6), "(b)(5)")]
    associate(hits, [None], [Redaction(box=Box(0.1, 0.1, 0.2, 0.02), kind=RedactionKind.VECTOR)])
    assert hits[0].document_level is True


# --------------------------------------------------------------------------
# association: the margin stamp
#
# Agencies print the exemption in the left or right margin, level with the
# passage it covers. That is several hundred points from a box in the middle of
# a line, so a single euclidean radius reads none of them: measured on the demo
# collection, 35 of its 41 boxes came back with no code at all. The geometry
# below is transcribed from that collection - a code box 0.0095 of the page
# high at x 0.0425, redaction boxes 0.0162 high starting at x 0.1157 - so these
# tests fail if the rule stops reading the layout it was written for.
# --------------------------------------------------------------------------


def _stamp(y: float, x: float = 0.0425, w: float = 0.030) -> Box:
    """A code printed at *x*, on the line whose glyphs start at *y*."""
    return Box(x, y, w, 0.0095)


def _redaction(y: float, x: float, x2: float) -> Redaction:
    return Redaction(box=Box(x, y, x2 - x, 0.0162), kind=RedactionKind.VECTOR)


def test_a_margin_stamp_is_read_onto_the_box_on_its_line() -> None:
    """The fix: a code in the left margin annotates the box level with it.

    428pt away across the page, which is nine times the near field. Nothing but
    the line says these two belong together, and the line is enough - it is
    what a person reads instantly and the only thing the layout offers.
    """
    box = _redaction(0.3361, 0.7712, 0.8318)
    hits = scan_text("(b)(4)")
    associate(hits, [_stamp(0.3415)], [box])
    assert box.codes == ["b(4)"]
    assert hits[0].document_level is False
    assert hits[0].ambiguous is False


def test_a_right_margin_stamp_is_read_the_same_way() -> None:
    """Which margin it is in is not information. Some agencies use the other."""
    box = _redaction(0.3361, 0.1157, 0.2400)
    hits = scan_text("(b)(6)")
    associate(hits, [_stamp(0.3415, x=0.9100)], [box])
    assert box.codes == ["b(6)"]


def test_the_line_beats_a_box_that_is_merely_close() -> None:
    """Transcribed from demo page 4 of the correspondence, where it went wrong.

    The stamp is 12pt below the box on the line above it and 281pt to the left
    of the box on its own line. A nearest-box rule takes the one above, and
    then the ledger says that passage was withheld under a statute that was
    never cited for it - a false statement about which law was invoked, which
    is the failure this module cares about most.
    """
    above = _redaction(0.2437, 0.1157, 0.6608)
    own_line = _redaction(0.2702, 0.5492, 0.8430)
    hits = scan_text("(b)(7)(C)")
    associate(hits, [_stamp(0.2756, w=0.0469)], [above, own_line])
    assert own_line.codes == ["b(7)(C)"]
    assert above.codes == []


def test_a_code_between_two_boxes_on_its_line_goes_to_the_nearer_one() -> None:
    """Two boxes on a line, a stamp printed between them: proximity decides.

    This is the one case where the near field is the right answer even though
    the line is crowded - the stamp is beside one of them and 0.4 inch from the
    other, and a reviewer who wanted it read the other way would have put it
    somewhere else.
    """
    left = _redaction(0.30, 0.10, 0.30)
    right = _redaction(0.30, 0.36, 0.60)
    hits = scan_text("(b)(5)")
    associate(hits, [_stamp(0.3054, x=0.31)], [left, right])
    assert left.codes == ["b(5)"]
    assert right.codes == []


def test_several_boxes_on_one_line_leave_a_far_code_ambiguous() -> None:
    """Ambiguity stays ambiguous. It does not get resolved by guessing.

    One margin stamp and two boxes on the line it is level with: the page says
    which law, and does not say which of the two rectangles it covered. Naming
    one of them would be inventing a fact; the code is counted for the page and
    attached to nothing, and says so.
    """
    first = _redaction(0.30, 0.20, 0.35)
    second = _redaction(0.30, 0.55, 0.80)
    hits = scan_text("(b)(6)")
    associate(hits, [_stamp(0.3054)], [first, second])
    assert first.codes == []
    assert second.codes == []
    assert hits[0].document_level is True
    assert hits[0].ambiguous is True


def test_a_footer_legend_cannot_reach_along_its_line_either() -> None:
    """The reach along the line stops at the footer band, on purpose.

    A single ``(b)(6)`` at the foot of the page and a box that happens to be
    low enough to share its line. ``crowded`` cannot help here - there is only
    one code - so this is the band rule itself doing the work, and it is the
    guard that keeps a legend from acquiring a rectangle now that a code can
    reach across the page.
    """
    box = _redaction(0.9380, 0.50, 0.80)
    hits = scan_text("(b)(6)")
    associate(hits, [_stamp(0.9400)], [box])
    assert box.codes == []
    assert hits[0].document_level is True


def test_a_code_on_a_line_with_no_box_is_still_document_level() -> None:
    """Reaching along a line does not mean reaching to another one.

    A code written in the prose of a paragraph nowhere near a redaction is a
    statement about the release. Attaching it to the box two lines down would
    be exactly the fabrication the footer rule exists to prevent, in a place
    the footer rule does not look.
    """
    box = _redaction(0.5000, 0.1157, 0.6000)
    hits = scan_text("withheld under Exemption 5")
    associate(hits, [_stamp(0.3000, x=0.4000)], [box])
    assert box.codes == []
    assert hits[0].document_level is True
    assert hits[0].ambiguous is False


def test_a_stamp_above_a_page_withheld_in_full_still_attaches() -> None:
    """The near field still carries the cases the line rule cannot see.

    A page withheld end to end is stamped above the rectangle, not level with
    it, so no line joins them; 30pt of clear space does. Demoting the near
    field to second place must not cost this, because it is what puts a code on
    every fully withheld page in a production.
    """
    box = Redaction(box=Box(0.0817, 0.1616, 0.8366, 0.7298), kind=RedactionKind.VECTOR)
    hits = scan_text("(b)(5)")
    associate(hits, [Box(0.3333, 0.1132, 0.0320, 0.0101)], [box])
    assert box.codes == ["b(5)"]


# --------------------------------------------------------------------------
# false positives on ordinary prose
# --------------------------------------------------------------------------

_PROSE_SOURCE = (
    "the report said that a decision was taken in committee after the review "
    "board met and considered whether the contract should be renewed for a "
    "further period which officials described as necessary given the volume of "
    "correspondence and the number of outstanding requests from members of the "
    "public who had written to the department during the previous financial "
    "year about the handling of complaints and the cost of the programme"
)
_WORDS = _PROSE_SOURCE.split()

_PARENTHETICALS = [
    "(1)", "(2)", "(3)", "(4)", "(5)", "(6)", "(7)", "(8)", "(9)", "(10)",
    "(a)", "(b)", "(c)", "(d)", "(i)", "(ii)", "(iii)", "(iv)",
    "(a)(1)", "(c)(2)", "(d)(4)", "(see below)", "(2019)", "(1998)",
    "Figure 5", "Exhibit 5", "Table 7", "Annex 3", "paragraph (b)",
    "item 6", "page 7", "clause 21", "section 30", "chapter 4",
]


def _ordinary_prose(words: int = 4000, seed: int = 3) -> str:
    """A few thousand words of plain English, littered with the punctuation
    that makes this problem hard: numbered lists, sub-clauses, figure
    references and bare parentheses."""
    rnd = random.Random(seed)
    out: list[str] = []
    while len(out) < words:
        for _ in range(rnd.randint(8, 20)):
            out.append(rnd.choice(_WORDS))
        if rnd.random() < 0.5:
            out.append(rnd.choice(_PARENTHETICALS))
            # never let a "(b)" sit directly against a number: that really is
            # an exemption, and generating one would be testing the wrong thing
            out.append(rnd.choice(_WORDS))
        if rnd.random() < 0.3:
            out.append(rnd.choice([".", ",", ";"]).join(["", ""]) + rnd.choice(_WORDS))
    return " ".join(out)


def test_no_false_positives_on_ordinary_prose() -> None:
    text = _ordinary_prose()
    assert len(text.split()) >= 4000
    hits = scan_text(text)
    assert hits == [], f"false positives: {[(h.raw, h.code) for h in hits][:10]}"


def test_no_false_positives_even_with_the_gate_open() -> None:
    """The gate is the difference between a few and none, so measure both."""
    hits = scan_text(_ordinary_prose(), allow_ocr_variants=True)
    assert [h.code for h in hits] == []


def test_prose_with_one_real_code_finds_exactly_that_code() -> None:
    text = _ordinary_prose(1200) + " The remainder is withheld under (b)(5). " + _ordinary_prose(
        1200, seed=9
    )
    assert [h.code for h in scan_text(text)] == ["b(5)"]
