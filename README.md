# Stackroom

**Turn a folder of documents into a public, searchable archive.**

You have been sent 2,000 pages as a stack of PDFs. Somebody needs to be able to
read them, search them, cite a specific page, and check that what you published
is what you were given.

```
stackroom build ./release -o site
```

That is the whole thing. Out comes a static website — no server, no database,
no account — that you can host on GitHub Pages, put on a memory stick, or hand
to somebody as a zip file.

- **Search that lands on the page.** Type a phrase; the result opens the scan
  with that phrase boxed on the image where it was actually printed.
- **A ledger of what was withheld.** How many pages are redacted, what share of
  each page is gone, and — where the agency printed it — which exemption they
  relied on. Nobody else does this for free, and it is frequently the story.
- **It reads two releases of the same documents side by side.** When an agency
  produces a file again, `stackroom compare` finds the passages that were
  blacked out before and are readable now — and the ones that went the other
  way — with both sheets printed beside the finding, so a reader can check it.
- **It refuses to publish a failed redaction.** If a black box was drawn over
  text that is still recoverable from the file, the build stops and tells you
  which page. This has burned real people.
- **It admits what it cannot read.** Pages where recognition failed are marked
  as such, on the page and in the search results, because a search that
  silently skips 200 pages tells the reader a phrase is not there when it may
  be.

<!-- SCREENSHOT -->
<!--
  These are shots of the demo collection in `demo/`, which is entirely
  invented and regenerates from `demo/build-demo.py`. Every figure visible in
  them is a figure that build actually produced; a mocked-up number here would
  be the same bug this project exists to prevent. Retake them after changing
  the demo, and check each against `demo/site/manifest.json`.
-->

**A search result lands on the page, boxed where the words were printed.**

![A scanned inspection log beside its transcription. The searched phrase
"twelve weeks" is boxed on the scan itself, in the line "The answer was twelve
weeks from order", and marked again at the matching word in the transcription
beside it. Three black bars stand where passages were removed, each stamped
(b)(4) or (b)(6) beside it; the line under the page title reads "3 redactions,
22% of this page withheld".](docs/images/search-hit-on-scan.png)

**How much was withheld, and under which law — counted from the documents.**

![The redaction ledger for the demo release. Fifteen of its 21 pages carry
redactions, or 71%; 43 separate black boxes were counted; 50.5% of the content
on those 15 pages is withheld, and 44.8% of everything in the release.
Underneath, the exemptions the agency printed: b(4) on 6 pages, b(5) on 6,
b(6) on 6, b(7)(C) on 3, b(7)(E) on 2, and the Privacy Act code k(2) on 2,
each with a plain-English gloss of what it allows to be
withheld.](docs/images/withheld-ledger.png)

**Every redaction in the release, drawn at the size it took up on its page.**

![The negative, grouped by the exemption cited. b(5) is 14 rectangles adding up
to 69% of one page, among them a single near-full-page block; b(4) is ten thin
bars and b(6) nine; b(7)(C), b(7)(E) and k(2) are two or three bars apiece; two
rectangles carry no code at all. Below the picture a panel headed "What
this picture cannot show" states that one break in the control-number sequence
means three pages were withheld whole and can have no rectangle, that one page
came back from the recogniser as nothing legible, and that text deleted rather
than covered leaves no shape to draw.](docs/images/negative-by-exemption.png)

**It refuses to publish a document whose black box hides text that is still
there.**

![A terminal running stackroom check over a copy of the demo with a leak
planted in it. In red: "Stopped: this collection would publish a failed
redaction." Then "5 passage(s) on 2 page(s)", and a table giving the document,
the page, the length of the recovered text and its shape — the text itself
shown only as rows of hashes. It closes by saying the recovered text was never
written to disk, and that the fix is to get a corrected release or to remove
the words rather than cover them, because a black rectangle drawn over words in
a PDF hides nothing.](docs/images/check-refuses-a-failed-redaction.png)

**What one release blacked out and the next one did not.**

![Two productions of the same page, compared. A passage the 2019 release
covered with a black box — "Halcyon has invoiced us for cleaning unit 9 in
every month since the storm, at the full monthly rate, and every one of those
invoices has been passed for payment by this office." — is printed in the clear
in the 2024 release. Both halves are labelled corroborated, at 19 and 14 words,
and were withheld under b(4). Beside them, a map of the page's boxes — one
still covered, two uncovered since 2019 — and thumbnails of both sheets: 23%
withheld then, 4% now.](docs/images/compare-newly-disclosed.png)

**And it says what it could not read, rather than letting an empty result
stand for an empty page.**

![A search for "heliostat" returning four pages, each with a thumbnail of the
sheet and the sentence the word was found in, highlighted. Above the results,
in its own panel: "Search covers 20 of 21 pages. No text could be indexed from
the other one — it may be blank, it may be a picture, or its ink may not have
been recognised — so nothing on it can be found by
searching."](docs/images/search-says-what-it-cannot-read.png)

The archive in these pictures is `demo/`: an invented city's invented Bureau of
Sunlight, twenty-one pages of it, which
[`demo/build-demo.py`](demo/build-demo.py) will rebuild for you.

---

## Install

```
pipx install "stackroom[search]"        # or: pip install "stackroom[search]"
```

The `[search]` extra brings the `pagefind` binary, which builds the search
index. Plain `pipx install stackroom` works and produces a readable, citable
archive with **no search box at all** — every page is still a real HTML file —
so install the extra unless you have a reason not to.

You also need two things Stackroom does not bundle:

| | |
|---|---|
| **poppler** — renders the pages | `apt install poppler-utils` · `brew install poppler` |
| **tesseract** — reads scanned text | `apt install tesseract-ocr` · `brew install tesseract` |

For a language other than English, install its Tesseract data too —
`tesseract-ocr-rus`, `tesseract-ocr-deu`, and so on.

```
stackroom doctor
```
tells you what is missing and what to type to get it.

## Use

```
stackroom init ./release       # writes stackroom.toml and about.md
stackroom build ./release      # reads everything, writes ./site
stackroom serve site           # look at it before anyone else does
```

Then push `site/` to a `gh-pages` branch, or anywhere that serves files.
[`docs/PUBLISHING.md`](docs/PUBLISHING.md) covers the hosts, their limits, and
what to do when the archive is bigger than the place you wanted to put it.

### Before you publish anything

```
stackroom check ./release
```

reads the documents and reports any text that a black box covers but does not
remove. It builds no site, but it has to rasterise every page to look at the
pixels: those images go to a temporary folder, whose path it prints and which it
deletes on the way out. Pass `--scratch` to put that folder on a ramdisk. Run it
on documents you are about to *send*, not only on documents you are about to
publish.

### While you are still fixing things

```
stackroom build ./release --watch
```

rebuilds whenever the documents change. It rests on a page cache — the pages
that did not change are not read again — so the second build of a collection
takes seconds rather than hours. The cache holds rendered images of every page
you have built on this machine; `stackroom cache` says where it is and how big
it is, and `stackroom cache clear` empties it.
[`docs/CACHING.md`](docs/CACHING.md) is the long version, including what is
deliberately never cached. If you want to know where a first build's time goes
before you commit a weekend to one, [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)
is the measured breakdown — it is a dated lab notebook and says so.

### When the agency sends the same documents again

```
stackroom compare ./release-2019 ./release-2024 -o site
```

publishes the new release as an ordinary archive plus a `compare/` section
saying what changed: passages that were under a black box in 2019 and are
readable now, passages that went the other way, and pages that moved. It
publishes text from **both** folders, so only point it at documents you are
willing to publish. [`docs/COMPARING.md`](docs/COMPARING.md) is the long
version, and its section 6 — how this can be wrong — is the part to read before
quoting a finding in print.

### Configuration

Everything is optional. `stackroom.toml`, beside the documents:

```toml
title = "Contracting Authority correspondence, 2019"
jurisdiction = "us"          # which statute's exemption codes to look for
language = "en"              # the language of the interface: en, pl, ru, uk

[ocr]
languages = ["eng", "rus"]   # Tesseract codes
mode = "auto"                # auto | always | never

[render]
dpi = 150

[safety]
hidden_text = "stop"         # leave this alone
```

`stackroom init` writes a commented file with the settings most people change.
The rest — `widths`, `formats`, `publish_originals`, `strip_metadata`,
`search.language`, `base_url` — are in
[`src/stackroom/config.py`](src/stackroom/config.py), which is the list, and
each one is documented where it is defined.

`about.md`, beside it, is the part readers use to decide whether to trust the
archive: who released these documents, under what request, and what is missing.
Stackroom will not invent it for you, and it says so on the front page when it
is absent.

## What it produces

```
site/
  index.html                  what this is, and what is missing from it
  browse/                     every document
  search/                     instant full-text search
  withheld/                   the redaction ledger
  withheld/negative/          every redaction in the release, drawn at true size
  about/                      provenance, method, checksums
  d/<doc>/p/<n>/index.html    one real HTML page per page, with its text on it
  files/<doc>.pdf             the original, byte for byte
  manifest.json               SHA-256 of every source file, and a build stamp
  sw.js                       a service worker, so the archive reads offline
```

The full layout, and what each file is for, is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Every page of every document is an ordinary web page containing that page's
text. It is readable, citable and crawlable with JavaScript switched off.
Search is an accelerant on top of that; it is never a prerequisite for reading.
So is everything else the scripts add — the full-size scan viewer, the citation
panel, passage permalinks, the command palette, the reading settings (theme and
text size, kept in the reader's own browser and sent nowhere), and the button
that takes the whole archive offline.

## Honest limits

Read these before you commit to it.

- **About 20,000 pages** is the ceiling for search that Stackroom stands
  behind. Readers download about 202 KB before they can type at that size, and
  122 KB at 5,000 pages. Past 20,000 the build says what it will cost; past
  50,000 it refuses to write the site unless you pass `--i-know`. The exact
  figures are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#honest-limits-stated-in-the-docs-and-enforced-in-the-cli).
- **Query time tracks the number of hits, not the size of the archive.** A
  two-letter query that matches everything is slow no matter how small the
  collection, which is why very short queries are refused.
- **Redaction detection is not certain.** A uniform dark band left by a scanner
  is genuinely indistinguishable from a black box; those are counted and
  flagged rather than silently included. A redaction applied as an image, a
  clipping path, or a non-rectangular shape will be missed by the hidden-text
  check. **`stackroom check` passing is evidence, not a guarantee** —
  [`SECURITY.md`](SECURITY.md) lists what gets past it, and
  [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md) is the long argument.
- **Exemption codes are attributed by where they were printed, not by what they
  say.** A code goes to the black box on its own line, however far across the
  page it sits, because that is where a reviewer's stamp goes; failing that, to
  a box right beside it. Where a line holds several boxes and no code is near
  any of them, the ledger counts the code for the page rather than guessing
  which rectangle it covered. All of that is an inference from layout and it
  can be wrong. The site says which boxes carry a code of their own, which
  inherit one printed for the whole page, and which carry none.
- **The comparison is evidence, not a verdict.** `stackroom compare` calls a
  passage newly disclosed only when a black box went away *and* the text now
  standing in its place was not on the earlier page — a rule that exists
  because a machine reading the same sheet twice produces different words both
  times. Documents can still be paired wrongly, and a release that was re-typed
  or re-scanned produces few findings with no warning that there were more.
  [`docs/COMPARING.md`](docs/COMPARING.md) §6 is every way it can be wrong, and
  the site prints the same caveats beside the findings themselves.
- **Skewed scans defeat the visible-redaction pass.** At about 0.2° of rotation
  the boxes stop being detected as solid rectangles. Deskew first. This costs
  the ledger, not the leak check: hidden text is found in the content stream,
  which knows nothing about how straight the page was scanned.
- **Recognition quality is what it is.** Tesseract on a fax of a photocopy is
  poor, and Stackroom's job is to tell you *where* it was poor rather than to
  pretend otherwise.
- **The build is reproducible, if you pin the date.** Two builds of the same
  folder are byte-identical apart from `built_at` in `manifest.json` and the
  date in every page footer. Set `SOURCE_DATE_EPOCH` to a Unix timestamp and
  even those match, so a reader can rebuild your archive from your documents and
  diff it against yours. Publish the value you used.
- **PDFs and page images only**, for now. Not `.pst`, not audio, not video.
- **The interface ships in four languages** — English, Polish, Russian,
  Ukrainian. The statutory exemption glosses stay in English deliberately, and a
  short list of other things does not yet: see
  [`docs/TRANSLATING.md`](docs/TRANSLATING.md), which keeps that list and says
  how to add a language. No right-to-left catalogue ships, because the
  stylesheet is not finished for it.
- **No collaborative annotation, no accounts, no LLM summaries.** A static
  archive's whole advantage is that it does not guess.

## Why not DocumentCloud?

DocumentCloud is good and you should use it if it fits. Three things it cannot
do:

1. **Let you in.** Uploading requires verification as a journalism organisation.
   A FOIA requester, a court-watch volunteer, a graduate student and a community
   archivist are all locked out. They can run this by lunchtime.
2. **Let you keep it.** These are your files, in a folder, that anyone can
   mirror, fork or re-host without asking anyone. An archive that depends on one
   organisation's funding lasts as long as that funding.
3. **Count the redactions.** Nobody ships this. It converts the tool from
   somewhere to put documents into the thing that produced the number in your
   first paragraph.

## How it works

`stackroom/ingest/` reads: it walks and hashes the files, pulls the text layer
out of each PDF or recognises the page from its pixels, judges whether what came
back is trustworthy, finds the redactions, reads the exemption codes and the
production numbering. `stackroom/build/` writes: static HTML, one page per page,
plus a search index, the negative, and the offline service worker. Between them
is `model.py`, plain dataclasses, which is what lets either half be tested
without the other. `cache.py` sits underneath both and keeps a build from
redoing work it has already done; `compare.py` runs the whole ingest twice and
lines the two releases up.

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the contract between them,
including the six guarantees the output must keep.

Notable: the search index reports matches as *token positions*, the page's HTML
numbers its tokens with the same positions, and the word boxes are stored under
those positions too — which is how a search result knows where on the scan to
draw the box.

## Contributing

Very welcome, particularly:

- **Exemption vocabularies for other jurisdictions.** US FOIA and the Privacy
  Act, UK FOIA 2000, Canadian ATIA and EU 1049/2001 are in; adding one is
  mostly writing down what the statute says.
- **Real documents that break it.** A public release that Stackroom mis-reads is
  the most useful bug report there is.
- **Deskewing**, which would fix the biggest gap in redaction detection.
- **A language.** Four catalogues ship; every other language is missing, and a
  catalogue is an afternoon's work with a checker that tells you what is left.

`CONTRIBUTING.md` has the details. Tests: `pytest`. Lint: `ruff check`.

## Licence

MIT. The bundled fonts are subsets of Source Sans 3, Source Serif 4 and IBM Plex
Mono, all SIL OFL 1.1 — see
[`src/stackroom/assets/fonts/LICENSE-FONTS.md`](src/stackroom/assets/fonts/LICENSE-FONTS.md).
