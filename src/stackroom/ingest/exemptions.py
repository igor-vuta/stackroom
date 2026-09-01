"""Statutory withholding codes.

A government that withholds part of a document has to say which law let it, so
the black boxes come with citations printed beside them: ``(b)(6)``,
``(b)(7)(C)``, ``s.40(2)``. One at a time they are noise. Counted across a
release they are the finding - *exemption 5, the deliberative-process
privilege, accounts for 41% of everything withheld here* - and that sentence is
the reason this module exists.

Four jobs, in order of how often they go wrong:

1. **Find** the codes in a page's text, through whatever the scanner did to
   them. Real releases arrive with ``(b](6)``, ``[b][7][C]``, ``( b ) ( 7 )``
   and ``(b)(l)``; a parser that only knows ``(b)(6)`` finds a third of them.
2. **Normalise** every spelling to one canonical form, because a ledger that
   lists ``b(7)(C)`` and ``B (7) (c)`` as different codes has counted the same
   withholding twice.
3. **Explain**, in words a reader with no legal training can act on. "Exemption
   5" means nothing; "the agency talking to itself, withheld as deliberative"
   tells a reader whether to be annoyed.
4. **Attach** a code to the box it belongs to - by reading the layout, and
   conservatively. A stamp sits on the *line* of the passage it explains and
   can be right across the page from it, out in whichever margin the reviewer
   used; a code in a page footer is a legend for the whole page; a code with
   three boxes on its line names none of them. So the rule is the line first,
   then proximity, then silence - see :func:`associate` - and blaming a code on
   whichever box happens to be nearest invents a fact.

Every loosening in the patterns below is there because a real release needed
it, and loosening has a price: ``(6)(6)`` is also an ordinary pair of numbers
in an ordinary table. So the loosest alternative - a ``6`` standing in for the
``b`` the scanner could not read - is gated behind evidence that the document
really is a FOIA release. That gate is :func:`scan_document`, and it is the
reason this module has a document-level API at all.

Jurisdictions live in :data:`VOCABULARIES`, plain dicts of plain strings.
Adding Australia or India means adding an entry there and naming the scanners
it uses; no function in this file has to change.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from ..model import Box, Redaction

__all__ = [
    "EXEMPTION_RE",
    "MAX_SCAN_CHARS",
    "PROSE_RE",
    "VOCABULARIES",
    "ExemptionHit",
    "associate",
    "legend",
    "scan_document",
    "scan_text",
]


MAX_SCAN_CHARS = 200_000
"""Longest page of text this module will read.

Sixty times the length of a dense page. Past it the codes are not the point:
whatever produced that page is not a document, and a bound on the input is the
only defence against a pattern blow-up that survives a change of word grouper.
"""


# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------

# The workhorse. Reading it from the outside in:
#
#   - a left edge that is not alphanumeric, so ``sub(b)(6)`` in a variable name
#     and ``1552(b)`` inside a longer number cannot start a match;
#   - and a second left edge, ``(?<!\d[\(\[\{])``, without which every statute
#     anyone cites becomes an exemption. ``Rule 26(b)(1)``, ``1983(b)(2)`` and
#     ``164.512(b)(1)`` are the same shape as a withholding code and are not
#     one; a stamp beside a black box is never preceded by a section number.
#     ``552`` is exempted from that rule by being part of the lead-in, because
#     ``552(b)(6)`` really is the citation this module is looking for;
#   - an optional lead-in: the statutory cite (of which only the ``552`` is
#     required), the word "Exemption", or "Ex.". The cite has to be part of
#     this pattern rather than ignored, because without it the left edge sees
#     the ``2`` of ``552`` and refuses the match that follows it;
#   - the ``b``, with brackets that may be missing, mismatched or the wrong
#     shape entirely, because OCR reads ``)`` as ``]`` all day long. ``6`` is
#     in that class as an OCR reading of ``b``; see ``allow_ocr_variants``;
#   - an optional dash, for ``(b) - (7)(A)``. The en and em dashes are
#     written as escapes: on screen they are indistinguishable from the
#     hyphen beside them, and a character class nobody can read is a
#     character class nobody can maintain;
#   - the exemption number, where ``l``, ``I``, ``i``, ``O`` and ``o`` are the
#     usual misreadings of ``1`` and ``0``;
#   - an optional subpart letter, which only exemption 7 really has - and a
#     lookahead. ``b`` is a letter in A-F, so in a footer legend
#     reading "(b)(6) (b)(7)(C) (b)(7)(E)" the subpart group swallows the
#     ``(b)`` of the *next* citation, and the code after it disappears
#     entirely. If what follows the letter is another exemption number, the
#     letter belonged to that citation and not to this one.
#
#   - and every whitespace run is bounded rather than open. Three ``\s*`` runs
#     separated by optional single characters is a cubic backtracker: "(b"
#     followed by 800 spaces took 2.2 s here and 3,200 took over two minutes,
#     because the engine tries every way of splitting the run between them.
#     Nothing reachable through a PDF can carry a whitespace run at all today -
#     both word sources strip them, which
#     ``test_the_word_extractor_drops_every_character_re_calls_whitespace``
#     pins - but that is an accident of two upstream libraries rather than a
#     property of this module, and a bound costs nothing. ``{0,8}`` is more
#     whitespace than any real stamp contains; possessive quantifiers would be
#     tidier and need Python 3.11, and this project supports 3.10.
EXEMPTION_RE = re.compile(r"""
    (?<![A-Za-z0-9])
    (?<!\d[\(\[\{])
    (?: (?:5\s{0,8}[Uu]\.?\s{0,8}[Ss]\.?\s{0,8}[Cc]\.?\s{0,8}(?:§+\s{0,8})?)?55\s{0,8}2\s{0,8}
      | [Ee]xemption[s]?\s{0,8} | [Ee]x\.?\s{0,8} )?
    [\(\[\{]?\s{0,8}[bB6&]\s{0,8}[\)\]\}]?
    \s{0,8}[-\u2013\u2014]?\s{0,8}
    [\(\[\{]\s{0,8}([1-9lIiOo])\s{0,8}[\)\]\}]
    (?:\s{0,8}[\(\[\{]\s{0,8}([A-Fa-f])\s{0,8}[\)\]\}]
       (?!\s{0,8}[\(\[\{]\s{0,8}[1-9lIiOo]\s{0,8}[\)\]\}]) )?
""", re.VERBOSE)

# The prose form, for letters that never write the ``(b)`` at all: "withheld
# under Exemption 5", "FOIA Exemption No. 7(C)". Kept separate from the pattern
# above because it needs the keyword - a bare ``(5)`` is a list item.
PROSE_RE = re.compile(r"(?<![A-Za-z])(?:FOIA\s+)?(?:[Ee]xemptions?|[Ee]x\.)\s*"
                      r"(?:[Nn]o\.?\s*)?\(?\s*([1-9])\s*\)?"
                      r"(?:\s*\(\s*([A-Fa-f])\s*\))?(?![0-9])")

# Privacy Act companions. They sit next to the (b) codes in FBI and other
# law-enforcement releases and mean something different, so they get their own
# pattern. Brackets are mandatory here: a bare ``k2`` is a mountain.
PRIVACY_ACT_RE = re.compile(
    # Whitespace runs bounded for the same reason as EXEMPTION_RE above.
    r"(?<![A-Za-z0-9])[\(\[\{]\s{0,8}([jkJK])\s{0,8}[\)\]\}]\s{0,8}[-\u2013\u2014]?\s{0,8}"
    r"[\(\[\{]\s{0,8}([1-9lIi])\s{0,8}[\)\]\}]"
)

# UK FOIA 2000 and Canadian ATIA both cite bare section numbers. That is a
# terrible thing to search for in free text - "section 30 of the contract" is
# not a withholding - which is why these are NEVER scanned in ``us`` mode, and
# why the vocabulary itself does the filtering: a section number that is not an
# exemption in that Act produces no hit at all.
#
# ``(?<!')`` keeps the possessive out: "the agency's 21 employees" would
# otherwise read as section 21.
SECTION_RE = re.compile(
    r"(?<![A-Za-z0-9])(?<!')(?:sections?|s\.?)\s*(\d{1,2})"
    r"(?:\s*\(\s*(\d)\s*\))?(?!\d)(?!\.\d)",
    re.IGNORECASE,
)

# EU Regulation 1049/2001: "Article 4(1)(a)".
ARTICLE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:Articles?|Arts?\.?)\s*4\s*\(\s*([1-9])\s*\)"
    r"(?:\s*\(\s*([a-cA-C])\s*\))?"
)

# A span that is nothing but a continuation of the list before it: the ``7(C)``
# in "Exemptions 6 and 7(C)".
_BARE_US_RE = re.compile(r"^\(?\s*([1-9])\s*\)?(?:\s*\(\s*([A-Fa-f])\s*\))?$")
_BARE_SECTION_RE = re.compile(r"^\(?\s*(\d{1,2})\s*\)?(?:\s*\(\s*(\d)\s*\))?$")
_BARE_ARTICLE_RE = re.compile(r"^(?:4\s*)?\(\s*([1-9])\s*\)(?:\s*\(\s*([a-cA-C])\s*\))?$")

# Enumerations are why the scanners run over split spans instead of straight
# down the text: "Exemptions 6 and 7(C)" yields only the 6 to a single pass,
# and the 7(C) is the one that tells you the record is a criminal file.
# Whitespace runs bounded, as in EXEMPTION_RE: an unanchored `\s*` in front of
# an alternation is quadratic over a run of spaces, which is the last of the
# three backtrackers this module used to carry.
_SPLIT_RE = re.compile(r"\s{0,8}(?:[,;/]|&|\band\b|\bor\b)\s{0,8}", re.IGNORECASE)

OCR_DIGITS = {"l": "1", "I": "1", "i": "1", "O": "0", "o": "0"}
"""Characters a scanner returns instead of the digit it was looking at."""

_OCR_B = frozenset("6")
"""Characters accepted in place of ``b`` only under ``allow_ocr_variants``.

``&`` is in the pattern's class too and is deliberately *not* gated: it is
never a digit, so it cannot turn an ordinary numeric pair into a false code.
``6`` can, and does.
"""

FOOTER_BAND = 0.90
"""Below this fraction of the page height, a code is presumed to be legend."""

HEADER_BAND = 0.06
"""And above this, likewise: some agencies print the legend at the top."""

LETTER_ASPECT = 612.0 / 792.0
"""Width ÷ height of US Letter. Boxes are normalised per axis, so horizontal
distances have to be scaled by this before they can be compared with vertical
ones or with a threshold expressed in page heights.

Only :func:`_gap` uses it, and only for the near field. The rule that reads a
margin stamp does not convert one axis into the other at all - see
:func:`_shares_a_line` - which is deliberate: a page-relative height and a
page-relative width are different units, and the one number that claims to
convert between them is the easiest thing in this module for a caller to hand
over upside down."""

LINE_OVERLAP = 0.5
"""How much of the shorter span two boxes must share vertically to be *on the
same line of the page*.

Half. A code stamped beside the redaction it explains sits on the same baseline
as it, so its box - which is shorter, being smaller type - falls wholly inside
the box's vertical span: measured over every stamp in the demo collection, the
overlap is 1.00 of the shorter span for the right box and negative for every
other box on the page, because consecutive lines do not touch. Half is
therefore nowhere near either population, which is where a threshold belongs;
it is loose enough for a scan whose baselines wander and far too tight to join
two different lines."""


# --------------------------------------------------------------------------
# vocabularies
# --------------------------------------------------------------------------

US_LABELS: dict[str, str] = {
    "b(1)": "properly classified national-defense or foreign-policy information",
    "b(2)": "internal agency personnel rules and practices",
    "b(3)": "withheld under another statute",
    "b(4)": "trade secrets and confidential commercial or financial information",
    "b(5)": (
        "privileged inter- or intra-agency memoranda, including the "
        "deliberative-process, attorney-client and work-product privileges"
    ),
    "b(6)": (
        "personnel and medical files whose release would be a clearly "
        "unwarranted invasion of privacy"
    ),
    "b(7)": "law-enforcement records",
    "b(7)(A)": "law-enforcement records: interferes with proceedings",
    "b(7)(B)": "law-enforcement records: deprives the right to a fair trial",
    "b(7)(C)": "law-enforcement records: unwarranted invasion of privacy",
    "b(7)(D)": "law-enforcement records: reveals a confidential source",
    "b(7)(E)": "law-enforcement records: reveals techniques or procedures",
    "b(7)(F)": "law-enforcement records: endangers life or safety",
    "b(8)": "financial-institution examination reports",
    "b(9)": "geological and geophysical well data",
    # Privacy Act, 5 U.S.C. 552a. These travel with the (b) codes in FBI
    # releases and are often the only marking on a page.
    "j(2)": (
        "Privacy Act: a criminal law-enforcement agency's own records, exempted "
        "from the Act's access rules as a class"
    ),
    "k(1)": "Privacy Act: classified in the interest of national defense or foreign policy",
    "k(2)": "Privacy Act: investigative material compiled for law-enforcement purposes",
    "k(3)": "Privacy Act: records kept to protect the President and other protectees",
    "k(4)": "Privacy Act: records used solely for statistics, such as census returns",
    "k(5)": (
        "Privacy Act: background-check material whose release would identify a "
        "confidential source"
    ),
    "k(6)": (
        "Privacy Act: testing or examination material whose release would "
        "compromise a hiring or promotion process"
    ),
    "k(7)": (
        "Privacy Act: armed-forces promotion evaluations that would identify a "
        "confidential source"
    ),
}

UK_LABELS: dict[str, str] = {
    "s.21": "already reasonably accessible to you by other means",
    "s.22": "held for publication at a later date",
    "s.23": "supplied by, or relating to, the security bodies",
    "s.24": "withheld to safeguard national security",
    "s.26": "would prejudice the defence of the realm",
    "s.27": "would prejudice international relations",
    "s.30": "held for a criminal investigation or prosecution",
    "s.31": "would prejudice law enforcement",
    "s.32": "held in a court, tribunal or inquiry record",
    "s.35": "concerns the formulation or development of government policy",
    "s.36": "would prejudice the effective conduct of public affairs",
    "s.38": "would endanger someone's health or safety",
    "s.40": "personal data about an identifiable person",
    "s.41": "provided to the authority in confidence",
    "s.42": "covered by legal professional privilege",
    "s.43": "a trade secret, or would prejudice commercial interests",
}

CA_LABELS: dict[str, str] = {
    "s.13": "obtained in confidence from another government",
    "s.14": "would harm federal-provincial affairs",
    "s.15": "would harm international affairs or the defence of Canada",
    "s.16": "law-enforcement and investigative records",
    "s.17": "would threaten the safety of an individual",
    "s.18": "would harm the economic interests of Canada",
    "s.19": "personal information about an identifiable person",
    "s.20": "a third party's trade secrets or confidential business information",
    "s.21": "advice, recommendations or accounts of consultations",
    "s.22": "testing procedures, tests and audits",
    "s.23": "subject to solicitor-client or litigation privilege",
    "s.24": "restricted from disclosure by another Act of Parliament",
}

EU_LABELS: dict[str, str] = {
    "art.4(1)": "would undermine the protection of the public interest",
    "art.4(1)(a)": (
        "would undermine public security, defence, international relations or "
        "financial and economic policy"
    ),
    "art.4(1)(b)": "would undermine the privacy and integrity of an individual",
    "art.4(2)": (
        "would undermine commercial interests, court proceedings and legal "
        "advice, or inspections, investigations and audits"
    ),
    "art.4(3)": (
        "an internal document about a decision not yet taken, or an opinion held "
        "for internal use"
    ),
    "art.4(5)": "withheld at the request of the member state that supplied it",
}

VOCABULARIES: dict[str, dict[str, object]] = {
    "us": {
        "name": "US Freedom of Information Act (5 U.S.C. 552) and Privacy Act",
        "scanners": ("us-foia", "us-prose", "privacy-act"),
        "labels": US_LABELS,
    },
    "uk": {
        "name": "UK Freedom of Information Act 2000",
        "scanners": ("section",),
        "labels": UK_LABELS,
    },
    "ca": {
        "name": "Access to Information Act (Canada)",
        "scanners": ("section",),
        "labels": CA_LABELS,
    },
    "eu": {
        "name": "Regulation (EC) 1049/2001",
        "scanners": ("article",),
        "labels": EU_LABELS,
    },
}
"""Every jurisdiction this build knows, as plain data.

To add one: give it a name, list the scanners it uses by the keys of
:data:`_SCANNERS`, and write the glosses. Write them for a reader who has never
seen the statute - the gloss is what ends up under a black box on the site.
"""

UNKNOWN_LABEL = "withheld under a code this build does not recognise"


# --------------------------------------------------------------------------
# the hit
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ExemptionHit:
    """One citation, found in one page's text."""

    code: str
    """Canonical spelling, e.g. ``b(7)(C)``, ``s.40(2)``, ``art.4(1)(a)``."""

    label: str
    """The plain-language gloss, ready to show a reader."""

    span: tuple[int, int]
    """Character offsets into the text that was scanned."""

    raw: str
    """Exactly what was on the page, damage included. Kept so an operator can
    see why a code was read the way it was."""

    jurisdiction: str = "us"
    source: str = "code"
    """Which pattern found it: ``code``, ``ocr-code``, ``prose``,
    ``enumeration``, ``privacy-act``, ``section`` or ``article``. ``code`` is
    the canonical bracketed form, and :func:`scan_document` uses its presence
    as the evidence that unlocks the OCR-tolerant reading."""

    box: Box | None = None
    """Where the code sits on the page, once :func:`associate` has been told."""

    document_level: bool = False
    """True when this code describes the page or the release rather than one
    box: a footer legend, or a code in prose with no box near it. Counting
    these as box annotations is how a ledger ends up claiming a redaction cites
    a code that was printed two inches away."""

    ambiguous: bool = False
    """True when the code plainly annotates *one of* several boxes and there is
    no way to say which.

    Always accompanied by ``document_level``, because that is what we do about
    it: the code is counted for the page and attached to no rectangle. The two
    are separate because they are different facts about the release, and an
    operator reading a page that reports no code for a box deserves to know
    which of them it was. ``document_level`` alone means the page really does
    not say; this means the page says it and we could not read which box it
    said it about."""


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def _vocabulary(jurisdiction: str) -> dict[str, object]:
    voc = VOCABULARIES.get(jurisdiction)
    if voc is None:
        raise KeyError(
            f"unknown jurisdiction {jurisdiction!r}; "
            f"known: {', '.join(sorted(VOCABULARIES))}"
        )
    return voc


def _labels(jurisdiction: str) -> dict[str, str]:
    return _vocabulary(jurisdiction)["labels"]  # type: ignore[return-value]


def _hit(
    code: str,
    labels: dict[str, str],
    span: tuple[int, int],
    raw: str,
    source: str,
    jurisdiction: str,
) -> ExemptionHit | None:
    """Build a hit, or nothing at all if the code is not in the vocabulary.

    The vocabulary is the last line of defence against a normalisation that
    produced nonsense: ``(b)(O)`` normalises to ``b(0)``, and there is no
    exemption zero, so it is dropped rather than published.

    A code whose *last* group is unknown falls back to the group above it, and
    what happens to the code then depends on what that group was. A numeric
    tail is a subsection - ``s.40(2)``, which the Act really does have and
    which is worth counting separately - so the code is kept and only the gloss
    comes from the parent. An alphabetic tail is a subpart, and the subparts
    are a closed set: a letter that is not in it is a misread, so ``b(7)(Z)``
    collapses to ``b(7)`` rather than inventing a category for the ledger.
    """
    label = labels.get(code)
    if label is None and code.endswith(")"):
        head, _, tail = code[:-1].rpartition("(")
        parent = head.rstrip("(")
        label = labels.get(parent)
        if label is not None and not tail.isdigit():
            code = parent
    if label is None:
        return None
    return ExemptionHit(
        code=code,
        label=label,
        span=span,
        raw=raw,
        jurisdiction=jurisdiction,
        source=source,
    )


def _split_spans(text: str) -> list[tuple[int, int]]:
    """Offsets of the comma-, ``and``- and ``or``-separated pieces of *text*."""
    out: list[tuple[int, int]] = []
    pos = 0
    for sep in _SPLIT_RE.finditer(text):
        out.append((pos, sep.start()))
        pos = sep.end()
    out.append((pos, len(text)))
    return out


def _scan_enumerated(
    text: str,
    head_re: re.Pattern[str],
    bare_re: re.Pattern[str],
    build: Callable[[str, str], str],
    labels: dict[str, str],
    source: str,
    jurisdiction: str,
) -> list[ExemptionHit]:
    """Run *head_re* over the split spans, letting bare items continue a list.

    A span that is *nothing but* a number - and that follows a span which named
    an exemption - is the rest of an enumeration. The "nothing but" is the
    whole safety argument: "Exemption 5 and 200 pages" does not turn 200 into a
    code, because that span has other words in it.
    """
    hits: list[ExemptionHit] = []
    continuing = False
    for start, end in _split_spans(text):
        chunk = text[start:end]
        found = False
        for m in head_re.finditer(chunk):
            hit = _hit(
                build(m.group(1), m.group(2) or ""),
                labels,
                (start + m.start(), start + m.end()),
                m.group(0),
                source,
                jurisdiction,
            )
            if hit is not None:
                hits.append(hit)
                # Only a hit the vocabulary accepted opens an enumeration.
                # "section 3, 40" is a contract, not a refusal, and section 3
                # is not an exemption - so the 40 must not inherit a list that
                # never started.
                found = True
        if found:
            continuing = True
            continue
        stripped = chunk.strip().rstrip(".;:")
        if not stripped:
            # "6, 7(C), and 5" splits on both the comma and the "and", leaving
            # an empty piece between them. It is punctuation, not a break in
            # the list, so it must not end the enumeration.
            continue
        if continuing:
            m = bare_re.match(stripped)
            if m is not None:
                off = start + chunk.index(stripped)
                hit = _hit(
                    build(m.group(1), m.group(2) or ""),
                    labels,
                    (off, off + len(stripped)),
                    stripped,
                    "enumeration",
                    jurisdiction,
                )
                if hit is not None:
                    hits.append(hit)
                continue
        continuing = False
    return hits


def _scan_us_codes(
    text: str, labels: dict[str, str], allow_ocr_variants: bool, jurisdiction: str
) -> list[ExemptionHit]:
    hits: list[ExemptionHit] = []
    for m in EXEMPTION_RE.finditer(text):
        # Everything before the exemption number is the lead-in and the ``b``.
        # If a gated OCR character is in there, this match only survives in a
        # document that has already proved itself elsewhere.
        head = m.group(0)[: m.start(1) - m.start(0)]
        ocr = bool(_OCR_B.intersection(head))
        if ocr and not allow_ocr_variants:
            continue
        num = OCR_DIGITS.get(m.group(1), m.group(1))
        sub = (m.group(2) or "").upper()
        code = f"b({num})" + (f"({sub})" if sub else "")
        hit = _hit(
            code, labels, m.span(), m.group(0), "ocr-code" if ocr else "code", jurisdiction
        )
        if hit is not None:
            hits.append(hit)
    return hits


def _scan_us_prose(
    text: str, labels: dict[str, str], allow_ocr_variants: bool, jurisdiction: str
) -> list[ExemptionHit]:
    return _scan_enumerated(
        text,
        PROSE_RE,
        _BARE_US_RE,
        lambda n, s: f"b({n})" + (f"({s.upper()})" if s else ""),
        labels,
        "prose",
        jurisdiction,
    )


def _scan_privacy_act(
    text: str, labels: dict[str, str], allow_ocr_variants: bool, jurisdiction: str
) -> list[ExemptionHit]:
    hits: list[ExemptionHit] = []
    for m in PRIVACY_ACT_RE.finditer(text):
        letter = m.group(1).lower()
        num = OCR_DIGITS.get(m.group(2), m.group(2))
        hit = _hit(f"{letter}({num})", labels, m.span(), m.group(0), "privacy-act", jurisdiction)
        if hit is not None:
            hits.append(hit)
    return hits


def _scan_sections(
    text: str, labels: dict[str, str], allow_ocr_variants: bool, jurisdiction: str
) -> list[ExemptionHit]:
    return _scan_enumerated(
        text,
        SECTION_RE,
        _BARE_SECTION_RE,
        lambda n, s: f"s.{n}" + (f"({s})" if s else ""),
        labels,
        "section",
        jurisdiction,
    )


def _scan_articles(
    text: str, labels: dict[str, str], allow_ocr_variants: bool, jurisdiction: str
) -> list[ExemptionHit]:
    return _scan_enumerated(
        text,
        ARTICLE_RE,
        _BARE_ARTICLE_RE,
        lambda n, s: f"art.4({n})" + (f"({s.lower()})" if s else ""),
        labels,
        "article",
        jurisdiction,
    )


_SCANNERS: dict[str, Callable[[str, dict[str, str], bool, str], list[ExemptionHit]]] = {
    "us-foia": _scan_us_codes,
    "us-prose": _scan_us_prose,
    "privacy-act": _scan_privacy_act,
    "section": _scan_sections,
    "article": _scan_articles,
}


def _dedupe(hits: list[ExemptionHit]) -> list[ExemptionHit]:
    """Drop hits that overlap a longer one already taken.

    ``Exemption 5 U.S.C. 552(b)(5)`` is one withholding written twice; two hits
    would double it in the ledger. Longest match at each position wins.
    """
    hits.sort(key=lambda h: (h.span[0], -(h.span[1] - h.span[0])))
    kept: list[ExemptionHit] = []
    end = -1
    for h in hits:
        if h.span[0] < end:
            continue
        kept.append(h)
        end = h.span[1]
    return kept


def scan_text(
    text: str, *, jurisdiction: str = "us", allow_ocr_variants: bool = False
) -> list[ExemptionHit]:
    """Find every withholding code in one page of text, in reading order.

    ``allow_ocr_variants`` unlocks the readings that are only safe once
    something else has established that this document is a release - today that
    means accepting a ``6`` where the ``b`` should be. Call
    :func:`scan_document` instead of setting it by hand; it works the evidence
    out for itself.
    """
    voc = _vocabulary(jurisdiction)
    labels: dict[str, str] = voc["labels"]  # type: ignore[assignment]
    # A bound on the input as well as on the patterns. Every scanner here runs
    # `finditer` over the whole page, so cost is linear in this length times the
    # number of scanners, and a page carrying a megabyte of text is a hostile
    # page rather than a document. An ordinary page is three kilobytes; the
    # spans stay valid because this truncates rather than reshapes.
    if len(text) > MAX_SCAN_CHARS:
        text = text[:MAX_SCAN_CHARS]
    hits: list[ExemptionHit] = []
    for name in voc["scanners"]:  # type: ignore[union-attr]
        hits.extend(_SCANNERS[name](text, labels, allow_ocr_variants, jurisdiction))
    return _dedupe(hits)


def scan_document(
    page_texts: Sequence[str], *, jurisdiction: str = "us"
) -> list[list[ExemptionHit]]:
    """Scan a whole document, in two passes, and return one list per page.

    The first pass is strict. If it finds a canonical ``(b)(N)`` anywhere in
    the document, we know we are looking at a US release, and the second pass
    re-reads every page with the OCR-tolerant alternatives switched on - so a
    page whose only marking came back from the scanner as ``(6)(6)`` is read
    correctly, while the same characters in a document that never once said
    ``(b)`` are left alone as what they almost certainly are: two numbers in a
    table.

    A document is the right unit for that decision. A page is too small - the
    page with the damaged code is exactly the page that has no clean one - and
    a collection is too large, because one FOIA release in the folder should
    not loosen the parser over an unrelated set of spreadsheets.
    """
    strict = [scan_text(t, jurisdiction=jurisdiction) for t in page_texts]
    if not any(h.source == "code" for page in strict for h in page):
        return strict
    return [
        scan_text(t, jurisdiction=jurisdiction, allow_ocr_variants=True) for t in page_texts
    ]


# --------------------------------------------------------------------------
# association
# --------------------------------------------------------------------------


def _gap(a: Box, b: Box, aspect: float) -> float:
    """Shortest distance between two boxes, in page heights. Zero if they touch."""
    dx = max(a.x - b.x2, b.x - a.x2, 0.0) * aspect
    dy = max(a.y - b.y2, b.y - a.y2, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _shares_a_line(code: Box, box: Box) -> bool:
    """Is *code* printed on the same line of the page as *box*?

    Vertical *overlap*, not vertical distance, and no horizontal term at all.
    That asymmetry is the whole point: a reviewer's stamp sits on the baseline
    of the passage it explains and can be anywhere across the page from it -
    out in the left margin, out in the right margin, tucked inside the box -
    while a stamp one line up is on somebody else's line however few points
    away it is. A rule built from one euclidean radius cannot express either
    half of that, and it is the reason a margin stamp went unread.

    Two boxes on consecutive lines of body text do not overlap at all, so this
    does not need a tie-break between neighbouring lines; see
    :data:`LINE_OVERLAP` for the measured separation. A tall box - a paragraph
    or a page withheld whole - overlaps every line it spans, and that is
    correct: a stamp printed beside such a box is beside it.
    """
    overlap = min(code.y2, box.y2) - max(code.y, box.y)
    shorter = min(code.h, box.h)
    if shorter <= 0:
        # A degenerate box has no span to take a share of, so ask whether the
        # code's line contains it rather than dropping it silently.
        return overlap >= 0
    return overlap >= LINE_OVERLAP * shorter


class _Candidate(NamedTuple):
    """One redaction weighed against one code's position."""

    gap: float
    """Clear space between the two, in page heights. Zero if they touch."""

    on_the_line: bool
    redaction: Redaction


def _annotated(
    code: Box,
    redactions: Sequence[Redaction],
    *,
    max_distance: float,
    aspect: float,
    reach_along_the_line: bool,
) -> tuple[Redaction | None, bool]:
    """``(the box this code annotates, was it ambiguous)``.

    ``(None, False)`` is "nothing here is annotated by this code" and
    ``(None, True)`` is "several boxes are, and the page does not say which".
    Both leave the code at page level; they are told apart because they are
    different things to report to a person.

    The order of the questions is the order a reader answers them in, and *the
    line comes first*. A stamp is printed level with the passage it explains,
    so being on a box's line is stronger evidence than being a few points from
    a box on some other line - and the two really do disagree. On page 4 of the
    demo correspondence a ``(b)(7)(C)`` in the left margin sits 12pt below the
    box on the line above it and 281pt to the left of the box on its own line;
    a nearest-box rule reads it onto the line above, which is a false statement
    about which law covered that passage.

    1. **Are any boxes on the code's line?**

       * exactly one - it annotates that box, at any distance across the page.
         This is the margin stamp, and it is the common layout in FOIA
         releases: the code goes in the one column body text never reaches,
         level with the passage it explains.
       * several, one of them within ``max_distance`` - the code is printed
         beside that one, and the nearest of them wins. A code between two
         boxes belongs to the nearer.
       * several, none of them near - **ambiguous**. One code and three boxes
         on a line is a fact about the line; guessing which box it cites would
         put a statute against a redaction withheld under a different one, and
         a ledger that says the wrong law is worse than a ledger that says the
         page printed none.

    2. **Otherwise, is something right beside it?** Within ``max_distance`` in
       any direction: a stamp printed under a box, or above one, or beside a
       box whose line the OCR could not agree with. Nearest wins. This is the
       old rule and it is what still carries a code printed above a page
       withheld in full.

    ``reach_along_the_line`` turns question 1 off, which is what the header and
    footer bands do: see :func:`associate`.
    """
    scored = [
        _Candidate(_gap(code, r.box, aspect), _shares_a_line(code, r.box), r)
        for r in redactions
    ]

    def nearest(candidates: Sequence[_Candidate]) -> Redaction | None:
        """The closest of *candidates* that is inside the near field, if any."""
        within = sorted((c for c in candidates if c.gap <= max_distance), key=lambda c: c.gap)
        return within[0].redaction if within else None

    if reach_along_the_line:
        on_the_line = [c for c in scored if c.on_the_line]
        if len(on_the_line) == 1:
            return on_the_line[0].redaction, False
        if len(on_the_line) > 1:
            beside = nearest(on_the_line)
            return (beside, False) if beside is not None else (None, True)

    return nearest(scored), False


def associate(
    hits: Sequence[ExemptionHit],
    hit_boxes: Sequence[Box | None],
    redactions: Sequence[Redaction],
    *,
    max_distance: float = 0.05,
    aspect: float = LETTER_ASPECT,
) -> None:
    """Attach each code to the redaction it annotates, in place.

    *hit_boxes* runs parallel to *hits*: where on the page each code was
    printed, or ``None`` if the caller could not work it out. Codes land in
    :attr:`~stackroom.model.Redaction.codes`; hits that belong to no box are
    marked :attr:`~ExemptionHit.document_level` and left for the page-level
    ledger.

    The rule is :func:`_annotated`: something right beside the code, else the
    one box on its line, else nothing. It is written down there rather than
    here because the *order* of those questions is the substance of it.

    ``max_distance`` is 0.05 of the page height, about 40pt on Letter - a
    little over half an inch. That is how far a stamp printed *beside* a box
    sits from it, and it is the whole of the near field. It is emphatically not
    how far a stamp can be from its box: the two commonest layouts in a real
    release put the code inside the rectangle or out in a margin, and a margin
    stamp for a redaction in the middle of a line is several hundred points
    away across the page. Reaching it is what :func:`_shares_a_line` is for,
    and measured on the demo collection built with ``--stamp margin`` it is
    the difference between 23 of 43 boxes carrying their code and 41 of 43.

    The footer is the case that matters, and it is why the reach along the line
    stops at the band. Plenty of releases print the full list of exemptions
    used at the bottom of every page; the nearest box to that list is whatever
    happens to be lowest on the page, and attaching six codes to it would be a
    fabrication. Inside the top or bottom band, therefore, a code is legend
    unless a box is *right there* with it - the old near-field rule, unchanged
    - and a band with three or more codes in it is legend regardless. A margin
    stamp for a box that is itself down in the footer band is the price, and it
    is the right way round: a legend mis-attributed is a false statement about
    a law, and a margin stamp missed is a box that reports no code.

    Nothing here is scaled by anything except ``aspect``, which only reaches
    the near field. Hand over height÷width instead of width÷height and that
    field is *stricter* horizontally - 24pt on Letter where this docstring
    promises 40 - which can only ever cost recall, never precision, and is why
    it went unnoticed for as long as it did. ``stackroom.pipeline`` passes
    width÷height; the function it passes it from,
    :func:`stackroom.pipeline._width_over_height`, exists to stop anyone
    handing over :attr:`~stackroom.model.Page.aspect` again.
    """
    if len(hits) != len(hit_boxes):
        raise ValueError(
            f"associate() needs one box per hit: got {len(hits)} hits and "
            f"{len(hit_boxes)} boxes. Pass None for a hit whose position is unknown."
        )
    band = [
        i
        for i, b in enumerate(hit_boxes)
        if b is not None and (b.y >= FOOTER_BAND or b.y2 <= HEADER_BAND)
    ]
    crowded = len(band) >= 3

    for hit, box in zip(hits, hit_boxes, strict=True):
        hit.box = box
        if box is None or not redactions:
            hit.document_level = True
            continue

        in_band = box.y >= FOOTER_BAND or box.y2 <= HEADER_BAND
        target, ambiguous = _annotated(
            box,
            redactions,
            max_distance=max_distance,
            aspect=aspect,
            reach_along_the_line=not in_band,
        )
        hit.ambiguous = ambiguous

        # A legend, or a stray marking in the furniture. Either way it
        # describes the page, not a rectangle on it.
        if target is None or (in_band and crowded):
            hit.document_level = True
            continue
        if hit.code not in target.codes:
            target.codes.append(hit.code)


# --------------------------------------------------------------------------
# legend
# --------------------------------------------------------------------------


def legend(codes: Iterable[str], *, jurisdiction: str = "us") -> list[tuple[str, str]]:
    """Turn the codes seen in a release into an ordered, deduplicated key.

    Accepts whatever spelling the caller has - ``(b)(7)(C)``, ``b(7)(c)``,
    ``B (6)`` - and returns canonical codes paired with their glosses, in the
    order the vocabulary lists them, which is the order a reader expects to
    find them in. Codes this build does not know are kept rather than dropped,
    with a gloss that says so: a legend that silently omits a code the page
    displays is worse than one that admits ignorance.
    """
    labels = _labels(jurisdiction)
    order = {code: i for i, code in enumerate(labels)}
    seen: dict[str, str] = {}
    for raw in codes:
        text = (raw or "").strip()
        if not text:
            continue
        canonical = text
        if text not in labels:
            found = scan_text(text, jurisdiction=jurisdiction, allow_ocr_variants=True)
            if found:
                canonical = found[0].code
        seen[canonical] = labels.get(canonical, UNKNOWN_LABEL)
    return sorted(seen.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0]))
