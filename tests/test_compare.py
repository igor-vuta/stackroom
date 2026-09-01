"""Comparing two releases: does it align the right pages, and does it lie?

The second question is the important one. A comparison that misses a disclosure
is a tool that is not very good; a comparison that *invents* one is a false
claim about what an agency did, published under an archive's name, and no
amount of the first is worth any of the second. So the suite is weighted that
way: the cases that must produce **nothing** - a page re-scanned at a worse
resolution, two documents that are not the same document, a passage that only
recognition disagrees about - get as much attention as the cases that must
produce a finding.

Three layers, cheapest first.

**The algorithms**, on ``Page`` objects built in this file. The dynamic
programme is checked against a plain three-way-max implementation written out
below, because the fast one is a numpy identity and identities are exactly the
sort of thing that is right for a year and then quietly is not.

**The accuracy**, measured rather than asserted at. ``test_measured_accuracy``
builds thirty-odd perturbations of a document whose correct alignment is known
by construction, runs the real aligner over all of them, and prints precision
and recall. The assertions are floors well under what it currently scores; the
numbers it prints are the ones quoted in ``docs/COMPARING.md``.

**End to end**, on real PDFs from ``tests/synth.py`` - poppler renders them,
Tesseract reads the scanned ones - because a synthetic ``Page`` cannot tell you
whether the redaction detector finds the same box twice, and that is the whole
foundation the geometry-first rule stands on.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import numpy as np
import pytest

import synth
from stackroom import compare
from stackroom.config import Config
from stackroom.model import (
    Box,
    Collection,
    Document,
    OcrQuality,
    Page,
    PageVerdict,
    Redaction,
    RedactionKind,
    Word,
)
from stackroom.pipeline import SafetyStop
from synth import RedactionSpec

# --------------------------------------------------------------------------
# fixtures built in memory
# --------------------------------------------------------------------------

VOCAB = str.split(
    "Commission requested correspondence between office director contracting authority period "
    "beginning March ending September terms agreed schedule appendix memorandum counsel deputy "
    "secretary transmit briefing summary attachment enclosure reference subject action officer "
    "regional bureau programme allocation expenditure procurement contractor invoice remittance "
    "settlement negotiation clause provision paragraph exhibit affidavit deposition testimony "
    "custodian retention disposition determination coordination supplemental jurisdiction"
)
"""Fifty-odd words of institutional prose. Wide enough that two pages drawn
from it are lexically distinguishable, which real pages of a memo are; the
``interchangeable pages`` test below deliberately narrows it to one line to
show what happens on a production of forms, where they are not."""

LINES = 24
PER_LINE = 9


def body(seed: int) -> list[list[str]]:
    """One page's worth of words, reproducibly."""
    rnd = random.Random(seed)
    return [[rnd.choice(VOCAB) for _ in range(PER_LINE)] for _ in range(LINES)]


def page_from(
    number: int,
    text: list[list[str]],
    *,
    boxes: tuple[Box, ...] = (),
    codes: tuple[str, ...] = ("b(5)",),
    noise: float = 0.0,
    noise_seed: int = 0,
    bates: str | None = None,
    unreadable: bool = False,
) -> Page:
    """A page with word boxes, laid out like a typed sheet.

    Words falling under one of *boxes* are simply not there, which is what the
    pipeline hands the rest of the program: a properly redacted page has no
    token under the rectangle, and a badly redacted one has had its tokens
    dropped before anything downstream sees the page.
    """
    rnd = random.Random(noise_seed + 5000)
    words: list[Word] = []
    for line_no, line in enumerate(text):
        y, x = 0.10 + line_no * 0.032, 0.10
        for token in line:
            width = 0.012 * len(token)
            box = Box(x, y, width, 0.018)
            if not any(box.overlap_ratio(b) > 0.5 for b in boxes):
                shown = token
                if noise and rnd.random() < noise:
                    shown = (token[:-1] + rnd.choice("aeioumn")) if len(token) > 3 else token
                words.append(Word(text=shown, box=box, conf=90, line=line_no))
            x += width + 0.008
    page = Page(
        number=number,
        words=words,
        bates=bates,
        redactions=[Redaction(box=b, kind=RedactionKind.VECTOR, codes=list(codes)) for b in boxes],
    )
    page.quality = OcrQuality(
        verdict=PageVerdict.UNREADABLE if unreadable else PageVerdict.GOOD,
        word_count=len(words),
    )
    return page


def prints(pages: list[Page]) -> list[compare.PageFingerprint]:
    return [compare.fingerprint_page(p) for p in pages]


def matched(alignment: compare.Alignment) -> list[tuple[int, int]]:
    return [(p.old, p.new) for p in alignment.pairs if p.both]


FULL = Box(0.05, 0.05, 0.90, 0.90)
BAND = Box(0.20, 0.20, 0.35, 0.06)


# ==========================================================================
# the dynamic programme
# ==========================================================================


def reference_alignment(scores: np.ndarray, gap: float = compare.GAP) -> float:
    """A plain Needleman-Wunsch, written for clarity and nothing else.

    The shipped one folds each row's left-extension into a ``maximum.accumulate``
    so a 2,000-page document is a few thousand numpy calls instead of four
    million Python ones. That is an algebraic identity, and an identity that
    holds until somebody changes the gap penalty into something that is not a
    constant is exactly the kind of bug that produces a plausible, wrong
    alignment. This is the thing it is checked against.
    """
    n, m = scores.shape
    grid = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        grid[i][0] = i * gap
    for j in range(1, m + 1):
        grid[0][j] = j * gap
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            grid[i][j] = max(
                grid[i - 1][j - 1] + scores[i - 1][j - 1] - compare.MATCH_FLOOR,
                grid[i - 1][j] + gap,
                grid[i][j - 1] + gap,
            )
    return grid[n][m]


def path_score(scores: np.ndarray, pairs: list[tuple[int | None, int | None]]) -> float:
    total = 0.0
    for i, j in pairs:
        if i is None or j is None:
            total += compare.GAP
        else:
            total += float(scores[i][j]) - compare.MATCH_FLOOR
    return total


def test_the_fast_alignment_is_the_same_alignment():
    rng = np.random.default_rng(11)
    for _ in range(60):
        n, m = int(rng.integers(1, 12)), int(rng.integers(1, 12))
        scores = rng.random((n, m))
        pairs = compare._needleman_wunsch(scores)
        assert path_score(scores, pairs) == pytest.approx(reference_alignment(scores), abs=1e-9)


def test_the_alignment_visits_every_page_exactly_once():
    rng = np.random.default_rng(5)
    for _ in range(30):
        n, m = int(rng.integers(1, 9)), int(rng.integers(1, 9))
        pairs = compare._needleman_wunsch(rng.random((n, m)))
        assert sorted(i for i, _ in pairs if i is not None) == list(range(n))
        assert sorted(j for _, j in pairs if j is not None) == list(range(m))


def test_the_alignment_is_monotone():
    rng = np.random.default_rng(7)
    for _ in range(30):
        pairs = compare._needleman_wunsch(rng.random((9, 11)))
        both = [(i, j) for i, j in pairs if i is not None and j is not None]
        assert both == sorted(both)
        assert [j for _, j in both] == sorted(j for _, j in both)


def test_the_same_two_releases_align_the_same_way_twice():
    """Determinism, and it is not a nicety: a claim about an agency that moves
    between two runs of the same command cannot be cited by anybody."""
    old = [page_from(i + 1, body(300 + i)) for i in range(7)]
    new = [page_from(i + 1, body(300 + i)) for i in range(7)]
    new.insert(3, page_from(99, body(999)))
    first = compare.align_pages(prints(old), prints(new))
    second = compare.align_pages(prints(old), prints(new))
    assert [(p.old, p.new, p.score, p.confidence) for p in first.pairs] == [
        (p.old, p.new, p.score, p.confidence) for p in second.pairs
    ]


# ==========================================================================
# the pieces
# ==========================================================================


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Commission", "Cornmission"),
        ("model", "rn0del"),
        ("SEPTEMBER", "september"),
        ("Bureau,", "bureau"),
        ("l000", "1OOO"),
        ("naïve", "naive"),
    ],
)
def test_ocr_confusions_fold_onto_one_skeleton(a, b):
    assert compare.skeleton(a) == compare.skeleton(b)


def test_different_words_keep_different_skeletons():
    assert compare.skeleton("director") != compare.skeleton("directive")
    assert compare.skeleton("2018") != compare.skeleton("2019")


def test_the_sketch_estimates_jaccard():
    text = " ".join(random.Random(4).choices(VOCAB, k=400))
    other = " ".join(random.Random(5).choices(VOCAB, k=400))
    assert compare.jaccard(compare.shingle_sketch(text), compare.shingle_sketch(text)) == 1.0
    assert compare.jaccard(compare.shingle_sketch("aaaa bbbb"), compare.shingle_sketch("zzzz yyyy")) == 0.0
    mixed = compare.jaccard(compare.shingle_sketch(text), compare.shingle_sketch(other))
    assert 0.0 < mixed < 1.0


def test_the_bottom_k_estimate_tracks_the_exact_one():
    """The sketch is a sample, so it has to be checked against the population."""
    rnd = random.Random(9)
    for _ in range(12):
        a = " ".join(rnd.choices(VOCAB, k=600))
        b = " ".join([*a.split()[:400], *rnd.choices(VOCAB, k=200)])
        big = compare.SKETCH_K * 40
        exact = compare.jaccard(
            compare.shingle_sketch(a, k=big), compare.shingle_sketch(b, k=big)
        )
        estimate = compare.jaccard(compare.shingle_sketch(a), compare.shingle_sketch(b))
        assert estimate == pytest.approx(exact, abs=0.12)


def test_an_empty_page_has_no_layout_opinion():
    blank = Page(number=1)
    profile = compare.layout_profile(blank)
    assert compare.layout_similarity(profile, profile) is None


def test_a_redacted_page_still_looks_like_itself():
    """Ruzicka alone scores a page against its own blacked-out copy at about a
    third, which would throw away the most valuable page in any comparison."""
    open_page = page_from(1, body(21))
    shut_page = page_from(1, body(21), boxes=(FULL,))
    similarity = compare.layout_similarity(
        compare.layout_profile(open_page), compare.layout_profile(shut_page)
    )
    assert similarity is not None and similarity > 0.5


def test_rectangle_subtraction():
    whole = Box(0.0, 0.0, 1.0, 1.0)
    assert compare.subtract(whole, Box(2.0, 2.0, 1.0, 1.0)) == [whole]
    assert compare.subtract(whole, whole) == []
    pieces = compare.subtract(whole, Box(0.25, 0.25, 0.5, 0.5))
    assert sum(p.area for p in pieces) == pytest.approx(0.75)
    for piece in pieces:
        assert piece.intersection(Box(0.25, 0.25, 0.5, 0.5)) is None


@pytest.mark.parametrize(
    ("old_box", "new_box", "kind"),
    [
        (Box(0.2, 0.2, 0.3, 0.05), Box(0.2, 0.2, 0.3, 0.05), "unchanged"),
        (Box(0.2, 0.2, 0.3, 0.05), Box(0.2, 0.2, 0.3005, 0.05), "unchanged"),
        (Box(0.2, 0.2, 0.3, 0.05), Box(0.2, 0.2, 0.15, 0.05), "shrunk"),
        (Box(0.2, 0.2, 0.15, 0.05), Box(0.2, 0.2, 0.3, 0.05), "grown"),
        (Box(0.2, 0.2, 0.3, 0.05), Box(0.32, 0.2, 0.3, 0.05), "moved"),
    ],
)
def test_box_changes_are_classified(old_box, new_box, kind):
    old = [Redaction(box=old_box, kind=RedactionKind.VECTOR)]
    new = [Redaction(box=new_box, kind=RedactionKind.VECTOR)]
    changes = compare.match_boxes(old, new)
    assert [c.kind for c in changes] == [kind]


def test_a_box_with_no_partner_is_removed_or_added():
    box = Redaction(box=Box(0.2, 0.2, 0.3, 0.05), kind=RedactionKind.VECTOR)
    assert [c.kind for c in compare.match_boxes([box], [])] == ["removed"]
    assert [c.kind for c in compare.match_boxes([], [box])] == ["added"]


def test_specks_are_not_compared():
    speck = Redaction(box=Box(0.2, 0.2, 0.002, 0.002), kind=RedactionKind.VECTOR)
    assert compare.match_boxes([speck], []) == []


def test_the_numbering_regime_is_decided_before_the_numbers_are_used():
    a = prints([page_from(i + 1, body(600 + i), bates=f"OCA-{i + 1:06d}") for i in range(5)])
    same = prints([page_from(i + 1, body(600 + i), bates=f"OCA {i + 1:06d}") for i in range(5)])
    renumbered = prints(
        [page_from(i + 1, body(600 + i), bates=f"DOJ-2024-{900 + i:06d}") for i in range(5)]
    )
    unstamped = prints([page_from(i + 1, body(600 + i)) for i in range(5)])
    assert compare.bates_regime(a, same) == "shared"
    assert compare.bates_regime(a, renumbered) == "disjoint"
    assert compare.bates_regime(a, unstamped) == "absent"


def test_a_renumbered_production_ignores_its_stamps():
    """Two productions stamped under different schemes have to be matched on
    content. Believing the numbers there would pair page 1 with page 1 by
    coincidence and page 40 with nothing at all."""
    old = [page_from(i + 1, body(700 + i), bates=f"OCA-{i + 1:06d}") for i in range(5)]
    new = [page_from(i + 1, body(700 + i), bates=f"DOJ-{500 + i:06d}") for i in range(5)]
    alignment = compare.align_pages(prints(old), prints(new))
    assert alignment.aligned
    assert matched(alignment) == [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]


# ==========================================================================
# alignment, case by case
# ==========================================================================


def test_an_inserted_page_does_not_desynchronise_the_rest():
    old = [page_from(i + 1, body(100 + i)) for i in range(6)]
    new = [page_from(i + 1, body(100 + i)) for i in range(3)]
    new.append(page_from(4, body(9999)))
    new += [page_from(i + 5, body(103 + i)) for i in range(3)]
    alignment = compare.align_pages(prints(old), prints(new))
    assert alignment.aligned
    assert matched(alignment) == [(0, 0), (1, 1), (2, 2), (3, 4), (4, 5), (5, 6)]
    assert alignment.added == 1


def test_a_page_dropped_from_the_release_is_reported_as_dropped():
    old = [page_from(i + 1, body(110 + i)) for i in range(6)]
    new = [p for i, p in enumerate(old) if i != 2]
    alignment = compare.align_pages(prints(old), prints(new))
    assert alignment.aligned
    assert alignment.removed == 1
    assert [p.old for p in alignment.pairs if p.new is None] == [2]


def test_two_pages_that_swapped_places_are_found_and_marked():
    """A monotone alignment cannot represent a swap, so it is caught afterwards
    and labelled - not by letting the alignment break its own monotonicity,
    which lets one coincidence desynchronise everything downstream."""
    old = [page_from(i + 1, body(120 + i)) for i in range(6)]
    order = [0, 4, 2, 3, 1, 5]
    new = [page_from(n + 1, body(120 + order[n])) for n in range(6)]
    alignment = compare.align_pages(prints(old), prints(new))
    assert alignment.aligned
    assert {(p.old, p.new) for p in alignment.pairs if p.moved} == {(1, 4), (4, 1)}
    assert matched(alignment) == sorted([(0, 0), (1, 4), (2, 2), (3, 3), (4, 1), (5, 5)])


@pytest.mark.parametrize("noise", [0.15, 0.30, 0.45])
def test_a_worse_scan_of_the_same_pages_claims_nothing(noise):
    """The case the whole design is arranged around. Recognition disagreeing
    with itself changes hundreds of words and no black boxes, and the geometry
    is what is allowed to speak."""
    old = [page_from(i + 1, body(130 + i), noise_seed=i) for i in range(4)]
    new = [page_from(i + 1, body(130 + i), noise=noise, noise_seed=900 + i) for i in range(4)]
    alignment = compare.align_pages(prints(old), prints(new))
    assert alignment.aligned
    assert matched(alignment) == [(0, 0), (1, 1), (2, 2), (3, 3)]

    claims = 0
    read_differently = 0
    for pair in alignment.pairs:
        diff = compare.diff_pages(old[pair.old], new[pair.new], pair=pair)
        claims += len(diff.disclosed) + len(diff.withheld)
        read_differently += diff.noise_tokens
    assert claims == 0
    assert read_differently > 0, "the fixture is not actually noisy"


def test_two_unrelated_documents_are_refused_rather_than_aligned():
    old = [page_from(i + 1, body(140 + i)) for i in range(5)]
    other = ["zebra", "quixotic", "vermilion", "phosphor", "kaleidoscope", "tributary", "axolotl", "bergamot"]
    new = [
        page_from(i + 1, [[other[(i * 3 + k + line) % len(other)] for k in range(PER_LINE)] for line in range(LINES)])
        for i in range(5)
    ]
    alignment = compare.align_pages(prints(old), prints(new))
    assert not alignment.aligned
    assert alignment.refusal
    assert "same document" in alignment.refusal or "too few" in alignment.refusal


def test_a_page_withheld_in_full_and_then_released_is_still_recognised():
    """No text in common, no control numbers: what identifies it is that it is
    the page between two pages we are sure about. That is an inference and it
    is recorded as one."""
    old = [page_from(i + 1, body(150 + i)) for i in range(5)]
    old[2] = page_from(3, body(152), boxes=(FULL,))
    new = [page_from(i + 1, body(150 + i)) for i in range(5)]
    alignment = compare.align_pages(prints(old), prints(new))
    assert alignment.aligned
    assert matched(alignment) == [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]

    pair = alignment.pairs[2]
    assert pair.confidence == "medium", "never certain about a page matched on shape alone"
    assert "text" not in pair.evidence

    diff = compare.diff_pages(old[2], new[2], pair=pair)
    assert diff.matched_on_position
    assert any("matched by their position" in note for note in diff.notes)
    assert sum(f.tokens for f in diff.disclosed) > 100


def test_a_control_number_settles_what_content_cannot():
    old = [page_from(i + 1, body(160 + i), bates=f"OCA-{i + 1:06d}") for i in range(5)]
    old[2] = page_from(3, body(162), boxes=(FULL,), bates="OCA-000003")
    new = [page_from(i + 1, body(160 + i), bates=f"OCA {i + 1:06d}") for i in range(5)]
    alignment = compare.align_pages(prints(old), prints(new))
    assert matched(alignment) == [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]
    assert alignment.pairs[2].confidence == "certain"
    assert "control number" in alignment.pairs[2].evidence


def test_interchangeable_pages_say_that_position_chose_them():
    """A production of near-identical forms is genuinely ambiguous, and the
    honest answer is to pair them by position and say so."""
    form = [["FORM", "SF", "SECTION", "APPLICANT", "NAME", "DATE", "SIGNATURE", "PAGE", "OF"]] * LINES
    old = [page_from(i + 1, form) for i in range(5)]
    new = [page_from(i + 1, form) for i in range(5)]
    alignment = compare.align_pages(prints(old), prints(new))
    assert alignment.aligned
    assert {p.confidence for p in alignment.pairs} == {"medium"}
    assert all("position" in p.evidence for p in alignment.pairs)


def test_a_document_too_long_to_align_says_so_instead_of_running_for_an_hour():
    huge = [
        compare.PageFingerprint(
            number=i, bates=None, bates_folded=None,
            sketch=compare.shingle_sketch(""), order=compare.token_sketch([]),
            chars=0, tokens=0, layout=(), aspect=1.29, readable=True,
        )
        for i in range(3000)
    ]
    alignment = compare.align_pages(huge, huge)
    assert not alignment.aligned
    assert "past what this alignment stands behind" in alignment.refusal


# ==========================================================================
# the diff
# ==========================================================================


def test_a_lifted_box_discloses_the_text_that_is_now_where_it_was():
    old = page_from(1, body(170), boxes=(BAND,))
    new = page_from(1, body(170))
    diff = compare.diff_pages(old, new)
    assert [c.kind for c in diff.boxes] == ["removed"]
    assert diff.disclosed and not diff.withheld
    assert all(f.confidence == "corroborated" for f in diff.disclosed)
    for finding in diff.disclosed:
        # Every word quoted has to sit inside the rectangle the earlier release
        # covered, and has to be absent from the tokens that release published.
        assert finding.box is not None and finding.box.overlap_ratio(BAND) > 0.5
        assert finding.unmatched >= compare.UNMATCHED_RUN
        assert finding.text not in old.token_text


def test_a_new_box_reports_the_text_the_earlier_release_published():
    old = page_from(1, body(171))
    new = page_from(1, body(171), boxes=(BAND,))
    diff = compare.diff_pages(old, new)
    assert [c.kind for c in diff.boxes] == ["added"]
    assert diff.withheld and not [f for f in diff.disclosed if f.confidence != "geometry"]
    for finding in diff.withheld:
        # Quoted from the release that published it, in its own words.
        assert finding.text in old.token_text
        assert finding.box is not None and finding.box.overlap_ratio(BAND) > 0.5


def test_a_shrunken_box_discloses_only_the_part_it_stopped_covering():
    wide = Box(0.20, 0.20, 0.40, 0.06)
    narrow = Box(0.20, 0.20, 0.18, 0.06)
    old = page_from(1, body(172), boxes=(wide,))
    new = page_from(1, body(172), boxes=(narrow,))
    diff = compare.diff_pages(old, new)
    assert [c.kind for c in diff.boxes] == ["shrunk"]
    assert diff.disclosed
    for finding in diff.disclosed:
        assert finding.box is not None
        assert finding.box.overlap_ratio(narrow) < 0.5, "still covered, must not be quoted"


def test_a_box_that_did_not_move_produces_nothing():
    old = page_from(1, body(173), boxes=(BAND,))
    new = page_from(1, body(173), boxes=(BAND,))
    diff = compare.diff_pages(old, new)
    assert [c.kind for c in diff.boxes] == ["unchanged"]
    assert not diff.disclosed and not diff.withheld
    assert not diff.changed


def test_an_exemption_can_change_without_the_box_changing():
    old = page_from(1, body(174), boxes=(BAND,), codes=("b(5)",))
    new = page_from(1, body(174), boxes=(BAND,), codes=("b(6)",))
    old.exemptions = ["b(5)"]
    new.exemptions = ["b(6)"]
    diff = compare.diff_pages(old, new)
    assert [c.kind for c in diff.boxes] == ["unchanged"]
    assert [(c.old_codes, c.new_codes) for c in diff.code_changes] == [(("b(5)",), ("b(6)",))]
    assert diff.exemption_added == ("b(6)",)
    assert diff.exemption_removed == ("b(5)",)
    assert diff.changed


def test_a_lifted_box_with_nothing_readable_under_it_says_so():
    old = page_from(1, body(175), boxes=(BAND,))
    new = page_from(1, body(175), boxes=(BAND,))
    new.redactions = []
    new.words = [w for w in new.words if w.box.overlap_ratio(BAND) < 0.2]
    diff = compare.diff_pages(old, new)
    assert [f.confidence for f in diff.disclosed] == ["geometry"]
    assert diff.disclosed[0].text == ""


def test_scans_that_do_not_register_are_measured_and_said_so():
    shifted = Box(BAND.x + 0.03, BAND.y + 0.03, BAND.w, BAND.h)
    old = page_from(1, body(176), boxes=(BAND, Box(0.2, 0.5, 0.3, 0.05)))
    new = page_from(1, body(176), boxes=(shifted,))
    diff = compare.diff_pages(old, new)
    assert diff.offset > 0
    assert any("registered" in note for note in diff.notes)


# ==========================================================================
# measured accuracy
# ==========================================================================


def perturbations() -> list[tuple[str, list[Page], list[Page], set[tuple[int, int]]]]:
    """Documents whose correct alignment is known because we built it.

    Each case returns the two page lists and the set of correspondences a
    perfect aligner would find. Cases where a page has genuinely no counterpart
    contribute no correspondence for it, so recall is not punished for
    correctly refusing to pair one.
    """
    cases: list[tuple[str, list[Page], list[Page], set[tuple[int, int]]]] = []
    base = [body(200 + i) for i in range(10)]

    def pages(texts, **kw):
        return [page_from(i + 1, t, **kw) for i, t in enumerate(texts)]

    identity = {(i, i) for i in range(10)}
    cases.append(("identical", pages(base), pages(base), identity))

    for at in (0, 4, 9):
        new = [*base[:at], body(4242), *base[at:]]
        truth = {(i, i if i < at else i + 1) for i in range(10)}
        cases.append((f"one page inserted at {at}", pages(base), pages(new), truth))

    for at in (0, 5, 9):
        new = [t for i, t in enumerate(base) if i != at]
        truth = {(i, i if i < at else i - 1) for i in range(10) if i != at}
        cases.append((f"one page dropped at {at}", pages(base), pages(new), truth))

    new = [*[body(7000 + k) for k in range(3)], *base]
    truth = {(i, i + 3) for i in range(10)}
    cases.append(("three pages prepended", pages(base), pages(new), truth))

    order = [0, 1, 7, 3, 4, 5, 6, 2, 8, 9]
    truth = {(order[j], j) for j in range(10)}
    cases.append(("two pages swapped", pages(base), pages([base[k] for k in order]), truth))

    for noise in (0.15, 0.30, 0.45):
        new = [page_from(i + 1, t, noise=noise, noise_seed=900 + i) for i, t in enumerate(base)]
        cases.append((f"re-read at {noise:.0%} noise", pages(base), new, identity))

    withheld = pages(base)
    withheld[4] = page_from(5, base[4], boxes=(FULL,))
    cases.append(("one page withheld in full before", withheld, pages(base), identity))
    cases.append(("one page withheld in full now", pages(base), withheld, identity))

    partly = [page_from(i + 1, t, boxes=(BAND,) if i % 3 == 0 else ()) for i, t in enumerate(base)]
    cases.append(("a third of the pages redacted", partly, pages(base), identity))

    stamped_old = [page_from(i + 1, t, bates=f"OCA-{i + 1:06d}") for i, t in enumerate(base)]
    stamped_new = [page_from(i + 1, t, bates=f"OCA {i + 1:06d}") for i, t in enumerate(base)]
    cases.append(("stamped, same scheme", stamped_old, stamped_new, identity))
    renumbered = [page_from(i + 1, t, bates=f"DOJ-{700 + i:06d}") for i, t in enumerate(base)]
    cases.append(("stamped, re-numbered", stamped_old, renumbered, identity))

    dark = pages(base)
    dark[6] = page_from(7, base[6], unreadable=True)
    cases.append(("one page recognition failed on", dark, pages(base), identity))

    # The hard case, kept in the measurement on purpose: a production of forms.
    # Every page is lexically and structurally the same page, so nothing but
    # position can tell them apart - and an insertion in the middle of one is
    # the shape of document this method is weakest on.
    form = [[["FORM", "SF", "SECTION", "APPLICANT", "NAME", "DATE", "SIGN", "PAGE", "OF"]] * LINES
            for _ in range(8)]
    cases.append(("eight identical forms", pages(form), pages(form), {(i, i) for i in range(8)}))
    inserted_form = [*form[:4], [["EXTRA"] * PER_LINE] * LINES, *form[4:]]
    cases.append(
        (
            "identical forms, one inserted",
            pages(form),
            pages(inserted_form),
            {(i, i if i < 4 else i + 1) for i in range(8)},
        )
    )

    both = [*base[:3], body(8001), *base[3:6], *base[7:]]
    truth = {(i, i) for i in range(3)} | {(i, i + 1) for i in range(3, 6)} | {
        (i, i) for i in range(7, 10)
    }
    cases.append(("one inserted and one dropped", pages(base), pages(both), truth))
    return cases


def test_measured_accuracy(capsys):
    """Precision and recall of the page alignment, over every perturbation.

    Precision is what matters: a pair the aligner gets wrong is a page whose
    findings are about two different sheets. Recall costs a reader a finding,
    which is bad; precision costs them a false one, which is worse. The floors
    below are set under what this currently scores, and the printed table is
    what ``docs/COMPARING.md`` quotes - run with ``-s`` to see it.
    """
    rows = []
    correct = claimed = expected = 0
    for name, old, new, truth in perturbations():
        alignment = compare.align_pages(prints(old), prints(new))
        got = set(matched(alignment))
        hits = len(got & truth)
        rows.append((name, len(truth), len(got), hits, alignment.mean_score))
        correct += hits
        claimed += len(got)
        expected += len(truth)

    precision = correct / claimed if claimed else 0.0
    recall = correct / expected if expected else 0.0

    lines = [f"{'case':34} {'true':>5} {'made':>5} {'right':>6} {'mean':>6}"]
    for name, want, got, hits, mean in rows:
        lines.append(f"{name:34} {want:5} {got:5} {hits:6} {mean:6.2f}")
    lines.append(f"\nprecision {precision:.4f}   recall {recall:.4f}   "
                 f"({correct} correct of {claimed} made, {expected} to find)")
    with capsys.disabled():
        print("\n" + "\n".join(lines))

    assert precision >= 0.98, "a wrong pair is a finding about two different sheets"
    assert recall >= 0.95


def test_a_refusal_is_measured_too(capsys):
    """How often two documents that are *not* two releases of one thing are
    correctly refused. A tool that aligns anything you give it is worse than no
    tool, because it will align the wrong things confidently."""
    refused = 0
    trials = 12
    for k in range(trials):
        old = [page_from(i + 1, body(3000 + k * 20 + i)) for i in range(6)]
        new = [page_from(i + 1, body(6000 + k * 20 + i)) for i in range(6)]
        alignment = compare.align_pages(prints(old), prints(new))
        if not alignment.aligned:
            refused += 1
    with capsys.disabled():
        print(f"\nunrelated documents refused: {refused}/{trials}")
    assert refused == trials


# ==========================================================================
# whole collections
# ==========================================================================


def collection_of(*documents: Document) -> Collection:
    return Collection(title="test", documents=list(documents))


def document_of(doc_id: str, pages: list[Page], *, filename: str = "", sha: str = "") -> Document:
    return Document(
        id=doc_id,
        title=doc_id,
        filename=filename or f"{doc_id}.pdf",
        sha256=sha or doc_id,
        size_bytes=1,
        pages=pages,
    )


def test_documents_are_paired_across_a_rename():
    old = collection_of(document_of("2019-part-one", [page_from(i + 1, body(400 + i)) for i in range(4)]))
    new = collection_of(document_of("appeal-response", [page_from(i + 1, body(400 + i)) for i in range(4)]))
    pairs, lonely_old, lonely_new = compare.pair_documents(old, new)
    assert not lonely_old and not lonely_new
    assert [(a.id, b.id) for a, b, _, _ in pairs] == [("2019-part-one", "appeal-response")]


def test_an_unrelated_document_is_left_unpaired():
    old = collection_of(document_of("memo", [page_from(i + 1, body(410 + i)) for i in range(4)]))
    other = ["zebra", "quixotic", "vermilion", "phosphor", "kaleidoscope", "tributary"]
    new = collection_of(
        document_of(
            "invoice",
            [page_from(i + 1, [[other[(k + i) % len(other)]] * PER_LINE for k in range(LINES)]) for i in range(4)],
        )
    )
    pairs, lonely_old, lonely_new = compare.pair_documents(old, new)
    assert not pairs
    assert [d.id for d in lonely_old] == ["memo"]
    assert [d.id for d in lonely_new] == ["invoice"]


def test_an_identical_file_is_named_as_identical_and_not_aligned():
    pages = [page_from(i + 1, body(420 + i)) for i in range(3)]
    old = collection_of(document_of("memo", pages, sha="deadbeef"))
    new = collection_of(document_of("memo", pages, sha="deadbeef"))
    comparison = compare.compare_collections(old, new)
    assert comparison.documents[0].identical
    assert not comparison.documents[0].diffs
    assert not comparison.anything


# ==========================================================================
# the safety guarantee
# ==========================================================================


def test_no_line_of_this_module_reads_hidden_text():
    """The guarantee, checked as a fact about the source.

    Everything the comparison can render comes from ``Page.words``, which the
    pipeline has already stripped. The way that would stop being true is
    somebody reaching for ``page.hidden`` in good faith - to show what an
    earlier release had covered, which is exactly the feature this must never
    grow - so the reach itself is what is tested for.
    """
    source = Path(compare.__file__).read_text(encoding="utf-8")
    body_only = _strip_docstrings(source)
    for banned in (".hidden", "HiddenText", "redacted_repr"):
        assert banned not in body_only, f"compare.py reaches for {banned}"


def _evidence_literals(tree) -> set[str]:
    """Every string literal this module puts into an ``evidence`` collection.

    Three shapes, because the module uses three: ``evidence.append("text")``,
    ``pair.evidence = (*pair.evidence, "position")``, and an ``evidence=(...)``
    argument to ``PagePair``.
    """
    import ast

    out: set[str] = set()

    def strings(node) -> set[str]:
        return {
            n.value
            for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("append", "insert")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "evidence"
        ):
            out |= {a.value for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)}
        if isinstance(node, ast.keyword) and node.arg == "evidence":
            out |= strings(node.value)
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
        named = {
            t.id if isinstance(t, ast.Name) else t.attr
            for t in targets if isinstance(t, (ast.Name, ast.Attribute))
        }
        if "evidence" in named and node.value is not None:
            out |= strings(node.value)
    return out


def test_every_evidence_and_confidence_identifier_has_a_word_for_it():
    """The two lookup tables are lists, and a list can fall out of step.

    ``evidence`` and ``confidence`` are identifiers in the data - the aligner
    tests them (``"control number" not in evidence``) and the built page keys
    CSS classes off them - so they are turned into words at the edge, by
    ``EVIDENCE_KEYS`` and ``CONFIDENCE_KEYS``. Adding a kind of evidence and
    forgetting the entry publishes the identifier itself to a reader, in
    English, on a page in another language, and nothing else would catch it.

    Both directions, and a third check that the whole thing is not vacuous.
    """
    import ast

    from stackroom import i18n

    source = Path(compare.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    produced = _evidence_literals(tree)
    assert len(produced) >= 10, "the scan found almost nothing; it has stopped working"
    missing = sorted(produced - set(compare.EVIDENCE_KEYS))
    assert not missing, f"evidence identifiers with no word for them: {missing}"

    # The other direction. A key for an identifier the module cannot produce is
    # a key nobody will ever notice is wrong. Two of them - "identical file" and
    # "position" - are built inside tuple literals the scan above cannot follow,
    # so this is the check that keeps them honest.
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    stale = sorted(k for k in compare.EVIDENCE_KEYS if k not in literals)
    assert not stale, f"EVIDENCE_KEYS names identifiers compare.py never makes: {stale}"

    english = i18n.load("en")
    for table in ("EVIDENCE_KEYS", "CONFIDENCE_KEYS"):
        for identifier, key in getattr(compare, table).items():
            assert key in english.messages, f"{table}[{identifier!r}] names no message: {key}"

    # And the four levels `_confidence` can return, which are a closed set.
    assert set(compare.CONFIDENCE_KEYS) == {"certain", "high", "medium", "low"}


def _strip_docstrings(source: str) -> str:
    """Everything in the file that is not a docstring or a comment."""
    import ast
    import io
    import tokenize

    out: list[str] = []
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.string.strip('"\'' ) in docstrings:
            continue
        out.append(token.string)
    return " ".join(out)


# ==========================================================================
# end to end, on real PDFs
# ==========================================================================


def fast_config(**overrides) -> Config:
    cfg = Config()
    cfg.render.dpi = 100
    cfg.render.widths = [600]
    cfg.render.thumb_width = 120
    cfg.render.formats = ["webp"]
    cfg.search.enabled = False
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


A = RedactionSpec(x=150, y=560, w=200, h=14, code="(b)(5)")
B = RedactionSpec(x=150, y=500, w=250, h=14, code="(b)(6)")
C = RedactionSpec(x=150, y=440, w=180, h=14, code="(b)(5)")
D = RedactionSpec(x=150, y=380, w=220, h=14, code="(b)(7)(C)")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    return tmp_path


def run(workspace: Path, **kwargs):
    return compare.run_comparison(
        workspace / "old",
        workspace / "new",
        workspace / "site",
        fast_config(),
        workers=1,
        old_label="the 2019 release",
        new_label="the 2024 release",
        **kwargs,
    )


pytest.importorskip("reportlab", reason="synthetic PDFs need reportlab")


def test_end_to_end_a_lifted_redaction_is_reported(workspace):
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=3, redactions={2: [A, B, C]},
                           bates_prefix="OCA-", title="Contracting memo")
    synth.born_digital_pdf(workspace / "new" / "memo-2024.pdf", pages=3, redactions={2: [A, C]},
                           bates_prefix="OCA-", title="Contracting memo")
    comparison, _report = run(workspace)

    assert len(comparison.documents) == 1
    entry = comparison.documents[0]
    assert entry.alignment.aligned
    assert comparison.lifted == 1
    assert comparison.imposed == 0
    assert comparison.disclosed >= 1
    assert all(f.confidence == "corroborated" for f in entry.disclosed)

    page = workspace / "site" / "compare" / entry.doc_id / "index.html"
    assert page.is_file()
    html = page.read_text(encoding="utf-8")
    assert "Newly disclosed" in html
    for finding in entry.disclosed:
        assert finding.text in html


def test_end_to_end_a_new_redaction_is_reported(workspace):
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=3, redactions={2: [A]},
                           bates_prefix="OCA-")
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=3, redactions={2: [A, D]},
                           bates_prefix="OCA-")
    comparison, _ = run(workspace)
    assert comparison.imposed == 1
    assert comparison.withheld >= 1
    html = (workspace / "site" / "compare" / comparison.documents[0].doc_id / "index.html").read_text()
    assert "Newly withheld" in html


def test_end_to_end_an_inserted_page(workspace):
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=3, bates_prefix="OCA-")
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=4, bates_prefix="OCA-")
    comparison, _ = run(workspace)
    entry = comparison.documents[0]
    assert entry.alignment.aligned
    assert entry.new_only_pages == [4]
    assert not entry.old_only_pages
    assert comparison.disclosed == 0


def test_end_to_end_a_re_scan_claims_nothing(workspace):
    """The same sheets, photographed worse. Poppler renders both and Tesseract
    reads both, so the token streams genuinely differ - and the black boxes do
    not, which is the only thing allowed to make a claim."""
    clean = [synth.typed_page(width=1000, height=1294, lines=18, redactions=[(150, 420, 520, 452)])
             for _ in range(2)]
    rough = [synth.typed_page(width=850, height=1100, lines=18, redactions=[(128, 357, 442, 384)],
                              grain=0.22) for _ in range(2)]
    synth.image_only_pdf(workspace / "old" / "scan.pdf", clean)
    synth.image_only_pdf(workspace / "new" / "scan.pdf", rough)
    comparison, _ = run(workspace)
    assert comparison.disclosed == 0, "a worse scan is not a disclosure"
    assert comparison.withheld == 0
    assert comparison.pages_added == 0
    assert comparison.pages_removed == 0


def test_end_to_end_stamped_against_unstamped(workspace):
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=4, redactions={2: [A, B]},
                           bates_prefix="OCA-")
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=4, redactions={2: [A]})
    comparison, _ = run(workspace)
    entry = comparison.documents[0]
    assert entry.alignment.aligned
    assert entry.alignment.regime == "absent"
    assert comparison.regime == "absent"
    assert any("no control numbers" in note for note in comparison.notes)
    assert comparison.lifted == 1


def test_end_to_end_two_unrelated_releases_are_refused(workspace):
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=3, title="Contracting memo")
    blank = [synth.blank_page(900, 1160) for _ in range(3)]
    synth.image_only_pdf(workspace / "new" / "photos.pdf", blank)
    comparison, _ = run(workspace)
    assert comparison.disclosed == 0
    assert comparison.withheld == 0
    unaligned = [d for d in comparison.documents if not d.alignment.aligned]
    assert comparison.unpaired_old or comparison.unpaired_new or unaligned
    html = (workspace / "site" / "compare" / "index.html").read_text(encoding="utf-8")
    assert "no counterpart" in html or "not compared" in html


def test_the_page_ceiling_is_checked_on_both_releases(workspace):
    """A comparison reads two collections, so it can be twice the work.

    ``_page_ceiling`` was reachable only from ``stackroom build``; ``compare``
    carried a ``--i-know`` flag that did nothing. The hook has to reach both
    reads, and it has to reach the *earlier* one first - the point of counting
    at discovery is refusing a collection before a page of it is rasterised.
    """
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=2)
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=3)

    counted: list[int] = []

    class Refused(RuntimeError):
        pass

    def on_counted(pages: int) -> None:
        counted.append(pages)
        raise Refused(f"{pages} pages")

    with pytest.raises(Refused):
        run(workspace, on_counted=on_counted)
    assert counted == [2], "the earlier release is counted, and refused, first"
    assert not (workspace / "site").exists() or not list(
        (workspace / "site").rglob("*.webp")
    ), "nothing may be rendered after the hook refuses"


def test_the_page_ceiling_sees_the_second_release_too(workspace):
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=2)
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=3)
    counted: list[int] = []
    run(workspace, on_counted=counted.append)
    assert counted == [2, 3]


def test_end_to_end_a_leak_in_the_earlier_release_stops_the_build(workspace):
    leaky = RedactionSpec(x=150, y=200, w=260, h=16, hidden_text="SOURCE IS ANATOLY KRAVCHENKO")
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=2, redactions={1: [leaky]})
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=2)
    with pytest.raises(SafetyStop):
        run(workspace)


def test_end_to_end_a_leak_is_never_published_even_when_permitted(workspace):
    """``--unsafe-publish-leaks`` lets the build finish. It must not let one
    character of the recovered text into the comparison, in either direction:
    text under a box in the earlier release is not something that release
    disclosed, so it cannot be quoted as newly withheld either."""
    secret = "SOURCE IS ANATOLY KRAVCHENKO"
    leaky = RedactionSpec(x=150, y=200, w=300, h=16, hidden_text=secret)
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=2, redactions={1: [leaky]})
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=2)

    cfg = fast_config()
    cfg.safety.hidden_text = "warn"
    comparison, _ = compare.run_comparison(
        workspace / "old", workspace / "new", workspace / "site", cfg, workers=1
    )
    assert compare.hidden_text_is_unreachable(comparison)
    for path in (workspace / "site").rglob("*"):
        if path.is_file() and path.suffix in (".html", ".json", ".js", ".css", ".txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for fragment in ("KRAVCHENKO", "ANATOLY", secret):
                assert fragment not in text, f"{fragment} leaked into {path.name}"


def _reachable(root, want):
    """Every object of type *want* reachable from *root*, with the path to it.

    Follows dataclass fields (the models use ``slots=True``, so ``vars()``
    alone would see nothing), sequences, mappings and ordinary attributes.
    """
    import dataclasses

    seen: set[int] = set()
    hits: list[str] = []
    stack: list[tuple[object, str]] = [(root, "context")]
    while stack:
        obj, path = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, want):
            hits.append(path)
        if isinstance(obj, (str, bytes, int, float, bool, type(None))):
            continue
        if isinstance(obj, dict):
            stack.extend((v, f"{path}[{k!r}]") for k, v in obj.items())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend((v, f"{path}[{i}]") for i, v in enumerate(obj))
        elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            for f in dataclasses.fields(obj):
                if hasattr(obj, f.name):
                    stack.append((getattr(obj, f.name), f"{path}.{f.name}"))
        elif hasattr(obj, "__dict__"):
            stack.extend((v, f"{path}.{k}") for k, v in vars(obj).items())
    return hits


def test_no_sentence_on_a_comparison_page_is_written_outside_the_catalogue(workspace):
    """Build the section with a catalogue of markers and see what survives.

    Every message becomes ``@@key@@``, so anything on the built page that is
    still an English sentence was written somewhere the translator cannot
    reach - a literal in a template, or one in ``compare.py``. That is exactly
    how this feature was English in the first place, and a grep for a
    particular phrase would only catch the phrases somebody thought of.

    The English the test looks for is ``en.json``'s own: every ``compare.*``
    message with no placeholders in it, which is the set of sentences these
    pages can say. Finding one means the page said it without asking.
    """
    from stackroom import i18n
    from stackroom.build import site as site_mod

    english = i18n.load("en")

    def marker(key: str, message) -> str:
        """``@@key@@`` plus every slot the English source has.

        The slots matter. Half of what ``compare.py`` writes reaches a reader
        only *through* a frame - the refusal inside ``compare.not_compared_html``,
        the page's state inside ``compare.sheet_caption`` - so a marker that
        dropped its parameters would hide exactly the sentences this test is
        looking for. `@@` and not `<<`, because Jinja escapes a plain message
        on the way out and angle brackets would come back as `&lt;`.
        """
        slots = "".join("{" + name + "}" for name in sorted(i18n.message_placeholders(message)))
        return "@@" + key + "@@" + slots

    marked = i18n.Catalog(
        locale="zz", name="Marker", english_name="Marker", direction="ltr", plural="other",
        messages={
            k: (marker(k, v) if isinstance(v, str) else {"other": marker(k, v)})
            for k, v in english.messages.items()
        },
    )
    translator = i18n.Translator(marked, english)

    # Page 4 changes from no boxes at all to one, so the caption under the
    # earlier sheet takes the branch of `_page_state_words` that says "nothing
    # blacked out" - a sentence `compare.py` writes rather than a template, and
    # therefore the one this test would otherwise never reach.
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=4,
                           redactions={2: [A, B, C], 3: [A]}, bates_prefix="OCA-")
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=5,
                           redactions={2: [A, C], 3: [A, D], 4: [B]}, bates_prefix="OCA-")
    # Same filename on both sides and nothing else in common, so the two are
    # paired and the alignment then refuses them - which is how a refusal, the
    # highest-stakes prose on the page, gets onto the page at all.
    synth.born_digital_pdf(workspace / "old" / "notes.pdf", pages=3)
    synth.image_only_pdf(
        workspace / "new" / "notes.pdf", [synth.blank_page(900, 1160) for _ in range(3)]
    )
    # And one document with no counterpart, for the section that lists those.
    # Blank rather than born-digital: every born-digital fixture here is the
    # same lorem, so a text-bearing one would out-score `notes.pdf`'s filename
    # and steal its pair, and the refusal above would never happen.
    synth.image_only_pdf(workspace / "new" / "extra.pdf", [synth.blank_page(900, 1160)])

    cfg = fast_config()
    from stackroom.pipeline import build_collection

    old_collection, _ = build_collection(workspace / "old", cfg, workspace / "oldout", workers=1)
    new_collection, _ = build_collection(workspace / "new", cfg, workspace / "site", workers=1)
    comparison = compare.compare_collections(
        old_collection, new_collection,
        old_label="R1", new_label="R2",
        old_media_root=workspace / "oldout", new_media_root=workspace / "site",
        t=translator,
    )

    site_mod.attach_about(new_collection, cfg)
    builder = site_mod.SiteBuilder(new_collection, cfg, workspace / "site")
    builder.t = translator
    i18n.install(builder.env, translator)
    builder.comparison = comparison
    builder.compare_old_documents = old_collection.documents
    compare.build(builder, t=translator)

    pages = sorted((workspace / "site" / "compare").rglob("index.html"))
    assert len(pages) >= 2, "the fixture produced no per-document comparison page"
    html = "\n".join(p.read_text(encoding="utf-8") for p in pages)

    # It really did render through the marker catalogue.
    assert html.count("@@compare.") > 60, "the marker catalogue was not used"

    # And nothing said an English sentence on its own account. Only messages
    # with no placeholders, because one with a slot cannot appear verbatim -
    # and only phrases the documents themselves do not contain, so that a
    # message which happens to quote the archive's own prose ("not compared")
    # is judged on where it appears rather than on whether it appears.
    prose = " ".join(
        [doc.title for c in (old_collection, new_collection) for doc in c.documents]
        + [w.text for c in (old_collection, new_collection) for doc in c.documents
           for page in doc.pages for w in page.words]
    )
    said = []
    for key, message in english.messages.items():
        if not key.startswith("compare.") or i18n.message_placeholders(message):
            continue
        for form in ([message] if isinstance(message, str) else message.values()):
            if len(form.split()) >= 2 and form not in prose and form in html:
                said.append((key, form))
    assert not said, f"written outside the catalogue: {said}"


def test_the_mastheads_link_to_the_comparison_is_translated(tmp_path):
    """The last English word on a translated comparison page.

    ``base.html.jinja`` hard-coded ``Compared``, marked ``lang="en"``, with a
    comment saying the compare templates had no catalogue entries. They have
    155, and ``nav.compare`` is in all four catalogues. Every page of a Russian
    archive built with ``stackroom compare`` carried the English word in its
    navigation, on every page, not only in ``compare/``.
    """
    from stackroom import i18n
    from stackroom.build import site as site_mod
    from stackroom.model import Collection, CollectionStats

    collection = Collection(
        title="Сличение", documents=[], stats=CollectionStats(documents=0, pages=0)
    )
    for language, expected in (("ru", "Сличение"), ("uk", "Звірка"), ("pl", "Porównanie")):
        cfg = fast_config()
        cfg.language = language
        out = tmp_path / language
        builder = site_mod.SiteBuilder(collection, cfg, out)
        builder.env.globals["compare_enabled"] = True
        builder.render("index.html.jinja", "index.html", nav="browse")
        html = (out / "index.html").read_text(encoding="utf-8")
        assert 'href="compare/index.html"' in html
        assert expected in html, f"{language}: the masthead did not say {expected}"
        assert ">Compared<" not in html
        assert i18n.load(language).messages["nav.compare"] == expected


def test_the_template_context_carries_no_page_and_so_no_hidden_text(workspace):
    """The stronger form of the guarantee, checked on the objects rather than
    on the source.

    ``test_no_line_of_this_module_reads_hidden_text`` reads the file, and a
    substring test cannot see ``getattr(page, name)``, ``dataclasses.asdict``
    or an f-string that interpolates a whole ``Page`` - a dataclass ``repr``
    carries every field it has, ``hidden`` included. So this asks the other
    question: of everything ``compare.py`` hands a template, is a
    :class:`HiddenText` reachable at all?

    The answer is stronger than "no": no ``Page`` object reaches a comparison
    template either. Every page-shaped fact on those pages - a number, a
    control number, a share withheld - is copied out into a plain dict or a
    string by ``_document_context``, so there is no object there for a future
    template to walk into.
    """
    from stackroom.build import site as site_mod
    from stackroom.model import HiddenText, Page

    secret = "SOURCE IS ANATOLY KRAVCHENKO"
    # A leak on both sides, and the boxes differ, so there is a changed page
    # and `compare.html.jinja` is rendered as well as the index.
    leaky_old = RedactionSpec(x=150, y=200, w=300, h=16, hidden_text=secret)
    leaky_new = RedactionSpec(x=150, y=560, w=300, h=16, hidden_text=secret)
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=2, redactions={1: [leaky_old]})
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=2, redactions={1: [leaky_new]})

    cfg = fast_config()
    cfg.safety.hidden_text = "warn"

    contexts: list[tuple[str, dict]] = []
    collections: list[object] = []
    original = site_mod.SiteBuilder.render

    def spy(self, template, relative, **context):
        if template.startswith("compare"):
            contexts.append((template, dict(context)))
            collections.append(self.collection)
        return original(self, template, relative, **context)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(site_mod.SiteBuilder, "render", spy)
    try:
        compare.run_comparison(
            workspace / "old", workspace / "new", workspace / "site", cfg, workers=1
        )
    finally:
        monkey.undo()

    assert {t for t, _ in contexts} == {"compare.html.jinja", "compare_index.html.jinja"}
    for template, context in contexts:
        assert _reachable(context, HiddenText) == [], f"{template} can reach recovered text"
        assert _reachable(context, Page) == [], f"{template} can reach a Page object"

    # And the one thing this module does not control: `SiteBuilder.render`
    # gives every template the whole collection. It carries no recovered text
    # either, because the pipeline puts findings on the *outcome* and never on
    # the page - `cache.encode_page` refuses a page that has any, for the same
    # reason. If that ever changes, this is the assertion that fails first.
    for collection in collections:
        assert not any(doc.has_hidden_text for doc in collection.documents)


def test_the_built_section_is_static_relative_and_offline(workspace):
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=3, redactions={2: [A, B]},
                           bates_prefix="OCA-")
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=3, redactions={2: [A]},
                           bates_prefix="OCA-")
    comparison, _ = run(workspace)
    site = workspace / "site"
    index = (site / "compare" / "index.html").read_text(encoding="utf-8")
    doc_page = (site / "compare" / comparison.documents[0].doc_id / "index.html").read_text("utf-8")

    for html in (index, doc_page):
        assert 'href="/' not in html and 'src="/' not in html
        assert "http://" not in html.replace("http://www.w3.org", "")
        assert str(workspace) not in html
    assert 'href="../../d/' in doc_page
    assert (site / "assets" / "stackroom.css").read_text(encoding="utf-8").count(".cmp-diagram") >= 1
    assert (site / "assets" / "js" / "compare.js").is_file()


def test_rebuilding_produces_the_same_bytes(workspace):
    synth.born_digital_pdf(workspace / "old" / "memo.pdf", pages=3, redactions={2: [A, B]},
                           bates_prefix="OCA-")
    synth.born_digital_pdf(workspace / "new" / "memo.pdf", pages=3, redactions={2: [A]},
                           bates_prefix="OCA-")
    run(workspace)
    first = (workspace / "site" / "compare" / "index.html").read_bytes()
    shutil.rmtree(workspace / "site")
    run(workspace)
    second = (workspace / "site" / "compare" / "index.html").read_bytes()
    # The colophon carries a build timestamp; everything above it must not move.
    assert first.split(b"<footer")[0] == second.split(b"<footer")[0]


def test_a_build_with_no_comparison_writes_no_comparison(tmp_path):
    """The wiring is one unconditional call in ``SiteBuilder.run``, so an
    ordinary build has to be untouched by it."""
    from stackroom.build import site as site_mod
    from stackroom.pipeline import build_collection

    source = tmp_path / "release"
    source.mkdir()
    synth.born_digital_pdf(source / "memo.pdf", pages=2)
    collection, _ = build_collection(source, fast_config(), tmp_path / "out", workers=1)
    site_mod.build_site(collection, fast_config(), tmp_path / "out")
    assert not (tmp_path / "out" / "compare").exists()


# ==========================================================================
# the filter, in a real browser
# ==========================================================================

FILTER_FIXTURE = """
<main>
  <section class="cmp-page" id="a">
    <ul class="cmp-findings">
      <li class="cmp-finding" data-confidence="corroborated"><p>kept</p></li>
      <li class="cmp-finding" data-confidence="suspected"><p>doubted</p></li>
    </ul>
  </section>
  <section class="cmp-page" id="b">
    <h4 class="cmp-finding-title">Only doubts here</h4>
    <ul class="cmp-findings">
      <li class="cmp-finding" data-confidence="suspected"><p>also doubted</p></li>
    </ul>
    <details class="cmp-details"><summary>more</summary></details>
  </section>
</main>
"""


@pytest.mark.browser
def test_the_filter_narrows_to_what_is_claimed_and_says_what_it_hid(browser):
    """The one thing the script adds, driven for real.

    Everything else on a comparison page is already correct before any script
    runs, which is why there is one browser test here and not twelve. What has
    to be checked in a browser is that narrowing the page never narrows it
    silently: a filter that quietly removes findings from a page about
    disclosure is the same failure as a search that quietly skips 200 pages.
    """
    from stackroom import i18n

    assets = Path(i18n.__file__).resolve().parent / "assets"
    script = (assets / "js" / "compare.js").read_text(encoding="utf-8")
    context = browser.new_context()
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.set_content(FILTER_FIXTURE)
        # The head of every page shell, in the order the shell loads it: the
        # build's own message catalogue, then prefs.js, which reads it once and
        # publishes t() for every script that follows. compare.js carries no
        # English of its own - the sentences it says are `js.compare.*` keys in
        # the catalogue - so a fixture without these two is a fixture of a page
        # that could not exist, and the script correctly reports `[key]`.
        # English here because the assertions below quote English; the same two
        # lines with another code would give this test another language.
        page.add_script_tag(content=i18n.browser_script(i18n.translator_for("en")))
        page.add_script_tag(path=str(assets / "js" / "prefs.js"))
        page.add_script_tag(content=script)

        assert page.locator(".cmp-filter").count() == 1
        assert page.locator('.cmp-finding[data-confidence="suspected"]:not([hidden])').count() == 2

        page.locator('.cmp-filter input[value="corroborated"]').check()
        assert page.locator('.cmp-finding[data-confidence="suspected"]:not([hidden])').count() == 0
        assert page.locator('.cmp-finding[data-confidence="corroborated"]:not([hidden])').count() == 1
        # The page with nothing left to show is folded away, and the one with a
        # surviving finding is not.
        assert page.locator("#b").get_attribute("hidden") is not None
        assert page.locator("#a").get_attribute("hidden") is None
        assert page.locator(".cmp-details").get_attribute("hidden") is not None

        said = page.locator('[role="status"]').inner_text()
        assert "1 page" in said and "2 passages" in said

        page.locator('.cmp-filter input[value="all"]').check()
        assert page.locator(".cmp-finding:not([hidden])").count() == 3
        assert page.locator("#b").get_attribute("hidden") is None
        assert "Showing everything" in page.locator('[role="status"]').inner_text()
        assert not errors
    finally:
        context.close()


def test_the_alignment_does_not_depend_on_the_interpreter_s_hash_seed():
    """Python salts ``hash()`` per process, so a module that reached for it
    would produce a different comparison on every run of the same command. This
    runs the aligner in two subprocesses with different salts and compares the
    bytes."""
    import subprocess
    import sys as _sys

    program = (
        "import sys, hashlib;"
        f"sys.path[:0] = [{str(Path(compare.__file__).parent.parent)!r}, {str(Path(__file__).parent)!r}];"
        "from stackroom import compare as C;"
        "from test_compare import page_from, body, prints;"
        "old = [page_from(i + 1, body(700 + i)) for i in range(6)];"
        "new = [page_from(i + 1, body(700 + i)) for i in range(6)];"
        "new.insert(3, page_from(99, body(4242)));"
        "a = C.align_pages(prints(old), prints(new));"
        "blob = repr([(p.old, p.new, round(p.score, 12), p.confidence, p.evidence) for p in a.pairs]);"
        "blob += repr(sorted(C.fingerprint_page(old[0]).sketch.values)[:8]);"
        "print(hashlib.sha256(blob.encode()).hexdigest())"
    )
    digests = set()
    for seed in ("0", "1", "424242"):
        result = subprocess.run(
            [_sys.executable, "-c", program],
            capture_output=True, text=True, env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            check=True,
        )
        digests.add(result.stdout.strip())
    assert len(digests) == 1, f"the alignment moved with the hash seed: {digests}"


# ==========================================================================
# the paths a large document takes
# ==========================================================================


def test_a_long_document_is_compared_near_the_diagonal_and_says_so(monkeypatch):
    """Above a size, only pages within a window of each other are compared.

    The window is generous and the out-of-order pass covers what falls outside
    it, but a reader is told the shortcut was taken. Exercised by lowering the
    threshold rather than by building a 500-page fixture, so the test costs a
    second instead of a minute.
    """
    monkeypatch.setattr(compare, "WINDOW_TRIGGER", 4)
    old = [page_from(i + 1, body(800 + i)) for i in range(8)]
    new = [page_from(i + 1, body(800 + i)) for i in range(8)]
    new.insert(2, page_from(99, body(4243)))
    alignment = compare.align_pages(prints(old), prints(new))
    assert alignment.windowed > 0
    assert any("positions of each other" in note for note in alignment.notes)
    assert matched(alignment) == [(0, 0), (1, 1), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8)]


def test_too_many_leftovers_to_test_for_reordering_is_said_out_loud(monkeypatch):
    monkeypatch.setattr(compare, "MOVE_BUDGET", 1)
    old = [page_from(i + 1, body(810 + i)) for i in range(4)]
    other = ["zebra", "quixotic", "vermilion", "phosphor", "kaleidoscope", "tributary"]
    new = [
        page_from(i + 1, [[other[(i + k + line) % len(other)] for k in range(PER_LINE)]
                          for line in range(LINES)])
        for i in range(4)
    ]
    alignment = compare.align_pages(prints(old), prints(new))
    assert any("too many to test for reordering" in note for note in alignment.notes)


def test_a_missing_page_image_does_not_take_the_comparison_down(tmp_path):
    """A fingerprint channel is an optimisation. Losing one is not a failure."""
    assert compare.image_dhash(tmp_path / "not-here.webp") is None
    broken = tmp_path / "broken.webp"
    broken.write_bytes(b"not an image")
    assert compare.image_dhash(broken) is None


def test_the_image_channel_survives_a_change_of_resolution():
    pillow = pytest.importorskip("PIL.Image")
    big = synth.typed_page(width=1200, height=1550, lines=20)
    small = big.resize((600, 775), pillow.Resampling.LANCZOS)
    other = synth.noise_page(1200, 1550)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for name, image in (("big", big), ("small", small), ("other", other)):
            path = Path(tmp) / f"{name}.png"
            image.save(path)
            paths[name] = compare.image_dhash(path)
        same = compare.hamming_similarity(paths["big"], paths["small"])
        different = compare.hamming_similarity(paths["big"], paths["other"])
    assert same > 0.85, "the same sheet at half the size must still look like itself"
    assert same - different > 0.2


def test_two_files_with_one_name_are_paired_and_then_refused(tmp_path):
    """The most useful thing to say about two files called `part1.pdf` that
    cannot be lined up is that they have the same name and could not be lined
    up. Leaving them in two "no counterpart" lists says they are unrelated,
    which is a different and probably false claim."""
    readable = [page_from(i + 1, body(900 + i)) for i in range(3)]
    dark = [Page(number=i + 1) for i in range(3)]
    for page in dark:
        page.quality = OcrQuality(verdict=PageVerdict.UNREADABLE)
    old = collection_of(document_of("part1", readable, filename="part1.pdf", sha="a"))
    new = collection_of(document_of("part1", dark, filename="part1.pdf", sha="b"))
    comparison = compare.compare_collections(old, new)
    assert not comparison.unpaired_old and not comparison.unpaired_new
    entry = comparison.documents[0]
    assert "same filename" in entry.pair_evidence
    assert not entry.alignment.aligned
    assert entry.changed, "a document that could not be compared has to be visible"


def test_an_identical_file_still_counts_its_pages_as_matched():
    pages = [page_from(i + 1, body(910 + i)) for i in range(3)]
    old = collection_of(document_of("annex", pages, sha="same"))
    new = collection_of(document_of("annex", pages, sha="same"))
    comparison = compare.compare_collections(old, new)
    assert comparison.documents[0].identical
    assert comparison.matched_pages == 3


def test_document_pairing_reads_word_order_as_well_as_vocabulary():
    """Two files of forms from one agency share every word and no order."""
    form = [["FORM", "SF", "SECTION", "APPLICANT", "NAME", "DATE", "SIGN", "PAGE", "OF"]] * LINES
    shuffled = [list(reversed(line)) for line in form]
    old = collection_of(
        document_of("a", [page_from(i + 1, form) for i in range(4)], filename="a.pdf", sha="a")
    )
    new = collection_of(
        document_of("b", [page_from(i + 1, shuffled) for i in range(4)], filename="b.pdf", sha="b")
    )
    pairs, lonely_old, lonely_new = compare.pair_documents(old, new)
    assert not pairs and lonely_old and lonely_new
