# Contributing

Stackroom is a tool for people who have been handed a stack of documents and
have to publish them. That is the test every change has to pass: does it help
the person with 2,000 pages and a deadline, and does it keep them from
publishing something they cannot take back.

Two kinds of contribution are worth more than the rest, and both of them are
open to you whether or not you write Python.

**A public document that Stackroom gets wrong.** A real release that it
mis-reads — a redaction it fails to see, an exemption code it cannot parse, a
page it declares unreadable that is perfectly legible — is the most useful bug
report this project can receive. There is an issue template for it. Everything
in `ingest/` was written against real damage, and it only stays correct if
people keep bringing more.

**An exemption vocabulary for another jurisdiction.** Four are in: US FOIA and
the Privacy Act, UK FOIA 2000, the Canadian ATIA, and EU Regulation 1049/2001.
Every other freedom-of-information regime in the world is missing, and adding
one is mostly writing down what the statute says. There is a step-by-step
section on it below.

**A language.** The interface is translated at build time and four catalogues
ship — English, Polish, Russian, Ukrainian. A catalogue is an afternoon: run
`python -m stackroom.i18n list` for the message count, and `check` tells you
what is still missing as you go.

[`docs/TRANSLATING.md`](docs/TRANSLATING.md) is the whole of it, including an
honest list of what is still English whatever you do.

If you have found a document where the failed-redaction check misses text that
is still recoverable, stop here and read [SECURITY.md](SECURITY.md) first. That
one does not go in a public issue.

---

## Setting up

You need Python 3.10 or newer, and two things Stackroom does not bundle:

```sh
# Debian, Ubuntu
sudo apt install poppler-utils tesseract-ocr

# macOS
brew install poppler tesseract
```

`poppler` renders the pages (`pdftoppm`, run as a subprocess). `tesseract`
reads the ones that are scans. For a language other than English, install its
Tesseract data too — `tesseract-ocr-deu`, `tesseract-ocr-rus`, and so on, or
`brew install tesseract-lang` for all of them. One test skips without
`tesseract-ocr-deu`; the rest do not care.

Then:

```sh
git clone https://github.com/igor-vuta/stackroom
cd stackroom
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev,search]"
```

`dev` brings pytest, ruff, mypy and reportlab (which the tests use to
manufacture PDFs). `search` brings the `pagefind` binary. Without `search` the
project still builds archives — they simply have no search index — and the
tests that need the binary skip rather than fail.

```sh
stackroom doctor
```

prints what it found and what to install for anything it did not.

## Tests and lint

```sh
pytest                                  # the whole suite: about 1,350 tests
pytest tests/test_redaction.py          # one file
pytest -k hidden_text                   # one subject
pytest -m 'not browser'                 # everything that needs no Chromium
ruff check .                            # the linter, as CI runs it
ruff check --fix .                      # the part of it that fixes itself
```

The whole suite takes about **nine minutes** on the two-core machine described
in [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) — 300 seconds of CPU, and the
rest is poppler and Tesseract working on pages the suite manufactures as it
goes. It is not a fast suite and it is not meant to be run after every
keystroke; run the file you are working in, and the whole thing before you push.

CI runs `ruff check` first and stops there if it fails, then runs `pytest` on
Python 3.10 through 3.13 on Linux and once on macOS. A pull request that is red
is not ignored, but it is also not reviewed until it is green.

Tests skip rather than fail when a system dependency is missing, which is why
running `stackroom doctor` before you start is worth the ten seconds. A suite
that reports forty skips is not a suite that passed; run `pytest -ra` and read
the reasons.

`mypy` is configured in `pyproject.toml` but the tree does not pass it cleanly
yet — `python -m mypy src/stackroom` reported **39 errors in 8 files** on
2026-09-01 — so it is not in CI. If you are already inside a module, leaving it no worse than
you found it is welcome. Making it clean is a contribution in itself; if you do,
say what the count was when you started, because that is the number the next
person will check against.

## The rules the code keeps

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) states six guarantees about the
output. They are not style preferences; breaking one is a bug, and a pull
request that breaks one will be asked to change. In the terms a contributor
runs into:

1. **Every page is a real HTML file with that page's text in it.** If you find
   yourself moving page text into JSON, a client-side template, or a bundle,
   stop. Search is an accelerant; it is never the only way to read a page.
2. **The original file is downloadable from every page that came from it.** An
   archive whose renderings cannot be checked against the source is a claim
   rather than evidence.
3. **`Page.words` order is identical to token order in the page HTML.** The
   search index returns match positions as indices into that sequence and the
   viewer turns them into boxes drawn on the scan. Touching either side means
   touching both, in the same commit, with a test. One divergence and every
   highlight in the archive lands on the wrong word.
4. **Text a redaction failed to remove is never published.** Not in the HTML,
   not in the JSON, not in the search index, and not written to any file on
   disk — `HiddenText.text` is held in memory so the CLI can tell the operator
   what leaked, and that is all. The build stops instead. Do not add an option
   that weakens this; `safety.hidden_text` already has the only two settings it
   is going to have.
5. **Nothing loads from a third-party host.** No CDN, no font service, no
   analytics, no telemetry, not in the generated site and not at build time. An
   archive that phones home is a log of who read what.
6. **The build is deterministic.** Same input bytes, same output bytes, so two
   people can verify they published the same thing. One field reads the clock —
   `BuildInfo.built_at`, in `manifest.json` and dated into every page footer —
   and `SOURCE_DATE_EPOCH` overrides it, which is what makes a byte-identical
   rebuild possible at all. Everything else that breaks this breaks it quietly:
   iteration over an unordered set, thread counts that reach an encoder, and
   anything seeded from the clock. Two tests are where a regression shows up:
   `tests/test_cache.py::test_a_warm_build_is_byte_identical_to_a_cold_one` and
   the `SOURCE_DATE_EPOCH` pair in `tests/test_site.py` and
   `tests/test_pipeline.py`.

Three more, from the module layout:

- **`ingest/` never renders, and `build/` parses a PDF for exactly one reason.**
  `build/site.py` calls `ingest.pdf.publish_pdf()` to write the originals,
  because `safety.strip_metadata` has to rewrite the file at the moment it is
  copied. That is the whole exception. Between the two halves is `model.py`,
  plain dataclasses; a module that needs to know something the dataclasses do
  not carry should add a field, not reach sideways.
- **`PyMuPDF` is AGPL-licensed and must not be imported anywhere.** This is why
  the hidden-text pass is a re-implementation of Free Law Project's `x-ray`
  rather than a call into it. `pdfplumber`, `pdfminer.six` and `pypdf` are what
  we have — `pypdf` for the `strip_metadata` rewrite, for document metadata,
  and for turning "this file will not parse" into a message that says why.
- **New runtime dependencies need a reason in the pull request.** Everything in
  `dependencies` has to install cleanly on three operating systems for a person
  who is not a programmer.

## Adding an exemption vocabulary for another jurisdiction

This is the contribution the project most needs, and the one most likely to be
made by somebody who knows the statute rather than the code. It is four small
edits, and the worked example below is Australia.

Everything lives in
[`src/stackroom/ingest/exemptions.py`](src/stackroom/ingest/exemptions.py).
Read its module docstring first; it explains why the patterns are as loose as
they are.

That file is divided by banner comments, and new code goes in the section it
belongs to: a regex under `# patterns`, a labels dict and the `VOCABULARIES`
entry under `# vocabularies`, a scanner function under `# scanning` beside the
other `_scan_*` functions and above `_SCANNERS`. Nothing needs adding to
`__all__` — the existing label dicts are not in it either.

### Step 0: get some real releases

Before writing a pattern, find five or six real documents released under the
Act and look at how the codes are actually printed. You are looking for the
spellings, not the law: `s 47F`, `s.47F`, `section 47F`, `ss. 47E(d) and 47F`,
`47F` alone in a footer legend. Write them down. They become the test cases,
and the test cases are the contribution — the regex is the easy part.

### Step 1: write the glosses

A vocabulary is a dict of canonical code to plain-language gloss. The gloss is
what a reader sees under a black box on the published site, so write it for
somebody who has never opened the Act. "Section 47F" tells a reader nothing;
"personal information about an identifiable person" tells them whether to be
annoyed.

Order matters: `legend()` sorts a page's codes by their position in this dict,
so list them in the order the Act does, which is the order a reader expects.

```python
AU_LABELS: dict[str, str] = {
    "s.33": "would damage national security, defence or international relations",
    "s.34": "a Cabinet document",
    "s.37": "would prejudice law enforcement or public safety",
    "s.38": "a secrecy provision in another Act forbids disclosure",
    "s.42": "covered by legal professional privilege",
    "s.45": "provided to the agency in confidence",
    "s.46": "disclosure would be contempt of Parliament or of a court",
    "s.47": "a trade secret, or information with commercial value",
    # The conditional exemptions: withheld only if disclosure would also be
    # contrary to the public interest, which is worth saying in the gloss
    # because it is the part a requester can argue with.
    "s.47B": "would damage Commonwealth-State relations (public-interest test)",
    "s.47C": "deliberative matter: opinion, advice or recommendation (public-interest test)",
    "s.47D": "would harm the Commonwealth's financial or property interests (public-interest test)",
    "s.47E": "would harm an agency's own operations (public-interest test)",
    # The subparagraphs of 47E are listed individually because agencies cite
    # them individually, and (d) in particular carries a great deal of traffic.
    # A subpart you do not list here is folded into its parent - see below.
    "s.47E(a)": "would prejudice an agency's audit, examination or review",
    "s.47E(b)": "would prejudice the purpose of a test, examination or audit",
    "s.47E(c)": "would substantially harm the management or assessment of staff",
    "s.47E(d)": "would substantially harm the proper and efficient conduct of the agency's operations",
    "s.47F": "personal information about an identifiable person (public-interest test)",
    "s.47G": "would harm a third party's business affairs (public-interest test)",
    "s.47H": "would harm an agency's research (public-interest test)",
    "s.47J": "would harm the economy (public-interest test)",
}
```

Two rules the vocabulary enforces for you, both worth knowing before you decide
how fine-grained to be:

- A code whose **last group is a letter** and which is not in the dict collapses
  to its parent. `s.47E(d)` is read as `s.47E` unless you list `s.47E(d)`
  yourself. Subparts are a closed set, so an unlisted letter is treated as a
  misreading rather than a new category for the ledger.
- A code whose **last group is a number** keeps its own code and borrows the
  parent's gloss. `s.47F(1)` stays `s.47F(1)` and is counted separately, because
  a numbered subsection is usually a real subdivision of the exemption.

Which is why the dict above lists `s.47E(a)` to `s.47E(d)` separately: in
Australian practice the subparagraph is what agencies cite, and a ledger that
folded all of them into `s.47E` would lose the distinction that matters. If you
leave a subpart out, expect to see its parent in the ledger instead — that is
the design, not a bug, but it is your decision to make deliberately.

### Step 2: decide whether an existing scanner can read your codes

`_SCANNERS` holds five: `us-foia`, `us-prose`, `privacy-act`, `section` and
`article`. The `section` scanner is the generic one, and it is what the UK and
Canadian vocabularies use. It reads `s.30`, `section 30`, `s.40(2)` — a section
number of one or two digits, with an optional numeric subsection.

It cannot read a letter suffix. Check yours before you assume:

```pycon
>>> from stackroom.ingest.exemptions import SECTION_RE
>>> [m.groups() for m in SECTION_RE.finditer("under section 47C")]
[('47', None)]
```

`47C` came back as `47`. For Australia that is fatal — `s.47` (trade secrets)
and `s.47C` (deliberative matter) are different exemptions and conflating them
would put the wrong sentence under a black box. So Australia needs its own
scanner. If your jurisdiction cites bare numbered sections, skip to step 4 and
use `"scanners": ("section",)`.

`SECTION_RE` used to drop a code that ended a sentence, so
`scan_text("Withheld under s.40.", jurisdiction="uk")` returned nothing. That is
fixed — both of these now read the way you would expect —

```pycon
>>> [h.code for h in scan_text("Withheld under s.40.", jurisdiction="uk")]
['s.40']
>>> [h.code for h in scan_text("s.30, s.31.", jurisdiction="uk")]
['s.30', 's.31']
```

— and it is worth knowing about only because it is the shape of mistake a new
pattern makes. Test yours against a full stop before you trust it.

### Step 3: write the scanner, if you need one

A scanner is a function with a fixed signature, registered in `_SCANNERS` under
a name. `_scan_enumerated` does the hard part: it splits the text on commas,
`and`, `or`, `&`, `;` and `/`, runs your pattern over each piece, and lets a
piece that is *nothing but* a bare code continue the list — so `ss. 47E(d) and
47F` yields both, and `Exemption 5 and 200 pages` does not turn 200 into a
code.

```python
# Australia cites lettered sections: 47C, 47E(d), 47F. The letter is part of
# the section number, not a subsection, so it is inside the first group.
#
# The two lookbehinds are the safety. Without ``(?<![A-Za-z0-9])`` the ``s`` of
# any word ending in one starts a match, and without ``(?<!')`` "the agency's
# 21 employees" reads as section 21.
#
# The tail is ``(?!\d)(?!\.\d)`` and not ``(?![\d.])``: both refuse a decimal
# citation like ``164.512``, but the stricter one also refuses a code at the
# end of a sentence. "Exempt under s.47F." backtracks off the ``F`` and comes
# back as ``s.47``, a different exemption, silently. Test your pattern against
# a full stop before you trust it.
AU_SECTION_RE = re.compile(
    r"(?<![A-Za-z0-9])(?<!')(?:sections?|ss?\.?)\s*"
    r"(\d{2}[A-J]?)"
    r"(?:\s*\(\s*([1-9a-z])\s*\))?"
    r"(?!\d)(?!\.\d)",
    re.IGNORECASE,
)

# A span that is nothing but a continuation of the list before it: the ``47F``
# in "under ss. 47E(d) and 47F".
_BARE_AU_RE = re.compile(
    r"^\(?\s*(\d{2}[A-J]?)\s*\)?(?:\s*\(\s*([1-9a-z])\s*\))?$", re.IGNORECASE
)


def _scan_au_sections(
    text: str, labels: dict[str, str], allow_ocr_variants: bool, jurisdiction: str
) -> list[ExemptionHit]:
    return _scan_enumerated(
        text,
        AU_SECTION_RE,
        _BARE_AU_RE,
        lambda n, s: f"s.{n.upper()}" + (f"({s.lower()})" if s else ""),
        labels,
        "section",
        jurisdiction,
    )
```

The `lambda` is the normaliser: it turns whatever was on the page into the one
canonical spelling you used as a key in `AU_LABELS`. A ledger that lists
`s.47f` and `S 47F` separately has counted the same withholding twice, and that
number is the reason this module exists. Note the `.upper()` and `.lower()`:
`re.IGNORECASE` means the page's own capitalisation reaches you unchanged.

Then register it:

```python
_SCANNERS: dict[str, Callable[[str, dict[str, str], bool, str], list[ExemptionHit]]] = {
    "us-foia": _scan_us_codes,
    # … the rest, unchanged …
    "au-section": _scan_au_sections,
}
```

### Step 4: add the vocabulary entry

```python
VOCABULARIES: dict[str, dict[str, object]] = {
    ...
    "au": {
        "name": "Freedom of Information Act 1982 (Australia)",
        "scanners": ("au-section",),
        "labels": AU_LABELS,
    },
}
```

`name` is shown to readers on the withheld ledger, so give the Act its real
title. `scanners` is a tuple of `_SCANNERS` keys; a jurisdiction can name
several, as `us` does.

### Step 5: the config comment, which is now the only other place

There used to be a second list of jurisdictions in `src/stackroom/config.py`,
and forgetting it was the step everybody missed: the vocabulary worked
perfectly and nobody could reach it, because the file refused the name. That
list is gone — `config._jurisdictions()` reads `VOCABULARIES` — so your entry is
valid in `stackroom.toml` the moment it exists. Check it:

```pycon
>>> from stackroom.config import _VALID
>>> sorted(_VALID["jurisdiction"])
['ca', 'eu', 'uk', 'us']
```

One thing in `config.py` is still hand-maintained: the comment in `TEMPLATE`
that lists the choices (`# Which statute's withholding codes to look for: us,
uk, ca, eu.`). That comment is what `stackroom init` writes into every new
collection, so add your code to it.

### Step 6: the tests, which are the actual contribution

`tests/test_exemptions.py` opens with a bench: strings that must be read, and
strings that must be left alone, written as data so that adding a case is one
line. The `MATCHES`/`REJECTS` lists at the top are US-specific; per-jurisdiction
tests live further down, next to the UK and Canadian ones.

Write at least as many rejects as matches. The rejects are what stop the
loosening you just added from finding exemption codes in a spreadsheet:

```python
def test_australian_lettered_sections() -> None:
    text = "Exempt under s.47F. Partly exempt under ss. 47E(d) and 47C."
    codes = [h.code for h in scan_text(text, jurisdiction="au")]
    assert codes == ["s.47F", "s.47E(d)", "s.47C"]


def test_australian_subparagraph_needs_its_own_gloss() -> None:
    """An unlisted letter collapses to the parent rather than inventing a
    category for the ledger."""
    (hit,) = scan_text("s.47E(z)", jurisdiction="au")
    assert hit.code == "s.47E"


@pytest.mark.parametrize(
    "text",
    [
        "section 3 of the contract",
        "the agency's 21 employees",
        "clause 47 of the deed",
        "page 47",
        "47F",                      # a bare number with no section marker
        "s.99",                     # not an exemption in this Act
    ],
)
def test_australia_leaves_ordinary_text_alone(text: str) -> None:
    assert scan_text(text, jurisdiction="au") == []
```

Run them:

```sh
pytest tests/test_exemptions.py -k australia
```

### Step 7: the documentation that goes with it

- `README.md` — the "Contributing" section names the jurisdictions that are in.
- `docs/ARCHITECTURE.md` — the `ingest/exemptions.py` entry lists them, with a
  count of the codes in each.
- `CHANGELOG.md` — a line under `Unreleased`.

The glosses stay in English even in a translated archive, deliberately; the
reasoning is in [`docs/TRANSLATING.md`](docs/TRANSLATING.md) under "Terms of
art", and it is worth reading before you write them.

A vocabulary that is in the code but in none of those is a vocabulary nobody
finds.

## Reporting a document that Stackroom mis-reads

Use the **"A document that breaks it"** issue template. What makes such a
report usable:

- **A link to the document, on a site that is not yours if possible** — an
  agency's own FOIA reading room, DocumentCloud, a court docket. Not an
  attachment. If the only copy is yours, say so and we will work out how to get
  it privately.
- **The page number**, and what is on that page.
- **What Stackroom said**, pasted from the terminal, and **what the correct
  answer is** — the reader's answer, not the developer's. "It says this page is
  35% withheld; I count two boxes, about 8% of the text" is a complete bug
  report.
- The output of `stackroom doctor` and your `stackroom.toml`, because the
  answer often depends on which Tesseract you have.

**Never attach a document that is not already public.** Not a draft, not a
release you have not published yet, not something a source gave you. There is
no bug worth that, and the fix can always be reproduced from a synthetic file
once we know what shape the problem is. `tests/synth.py` exists for exactly
this: manufacturing the damaged page rather than passing the real one around.

If the mis-reading is the failed-redaction check *missing* something —
Stackroom said a document was clear and it was not —
[SECURITY.md](SECURITY.md) says what to do instead, and it is not a public
issue.

## Sending a change

- One thing per pull request. A vocabulary and a refactor of the scanner
  machinery are two.
- Tests for the behaviour you changed. Every threshold in `ingest/` is a number
  somebody measured on a page; if you move one, say which page.
- Keep the comment style. The code explains *why* rather than *what*, and
  particularly why a threshold is the number it is. A pull request that removes
  that reasoning to make a function shorter is a net loss.
- `ruff check .` clean, line length 100.
- Update `CHANGELOG.md` under `Unreleased`, and `README.md`'s honest-limits list
  if you have changed what a limit is.
- Commits in plain language. There is no format to follow.

Small fixes do not need an issue first. Anything that changes the output layout,
the search contract, or a guarantee should start as an issue, because the answer
may be no and it is better to hear that before you write it.

## Things that will be turned down

Not because they are bad ideas — because they are not this tool:

- LLM summaries, entity extraction, or anything that guesses at what a document
  says. A static archive's whole advantage is that it does not guess.
- Accounts, comments, collaborative annotation, or anything that needs a server.
- Analytics, error reporting, update checks, or any other call home.
- A CDN for the fonts or the JavaScript.
- `PyMuPDF`, on licence grounds, however much easier it would make something.
- An option to publish a document with a known failed redaction more quietly
  than `--unsafe-publish-leaks` already does.

## Licence

MIT, the same as the project — see [LICENSE](LICENSE). By contributing you
agree your work is released under it. The bundled fonts are subsets of Source
Sans 3, Source Serif 4 and IBM Plex Mono under the SIL OFL 1.1; see
[`src/stackroom/assets/fonts/LICENSE-FONTS.md`](src/stackroom/assets/fonts/LICENSE-FONTS.md).

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
