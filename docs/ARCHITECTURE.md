# Architecture

Stackroom turns a folder of documents into a static website that anyone can
host, mirror, fork or read from a USB stick. There is no server, no database
and no account. This document is the contract between the modules.

## The shape of the thing

```
  a folder of PDFs
        │
        ▼
  ┌───────────┐   discover: walk, hash, dedupe, order
  │  ingest   │   read: text layer or OCR, word boxes, confidence
  │           │   analyse: redactions, exemptions, control numbers, quality
  └───────────┘
        │  Collection  (src/stackroom/model.py — plain dataclasses)
        ▼
  ┌───────────┐   render: one real HTML page per document page
  │   build   │   index: Pagefind over those pages
  │           │   assets: images, fonts, css, js, manifest
  └───────────┘
        │
        ▼
  a directory of static files
```

Everything the pipeline learns lives in `model.py`. A module that needs to know
something a dataclass does not carry should add a field, not reach sideways.

**Ingest never renders HTML, and build parses a PDF for exactly one reason.**
`build/site.py` imports `ingest/pdf.py` to publish the originals: with
`safety.strip_metadata` set, `pdf.publish_pdf()` rewrites the file from its page
tree on the way into `files/` rather than copying it, which is what drops the
earlier revisions an incremental save left behind. That is the whole exception,
it is one call, and it is there because the rewrite has to happen at the moment
the file is written. Anything else in `build/` that wants to open a PDF is
reaching sideways and should add a field to `model.py` instead.

## Guarantees the output must keep

These are not preferences. Breaking one of them is a bug.

1. **Every page of every document has its own URL, and that URL is a real HTML
   file containing that page's text.** No JavaScript required to read it, cite
   it, or crawl it. Search is an accelerant, never a prerequisite.
2. **The original file is downloadable from every page that came from it.** An
   archive whose derived renderings cannot be checked against the source is not
   evidence, it is a claim. (With `safety.strip_metadata` the published copy is
   a rewrite rather than a byte-for-byte copy, and `manifest.json` then carries
   both digests — the file that arrived and the file that was published.)
3. **Word order in `Page.words` is identical to token order in the page HTML.**
   The search index returns match positions as indices into that sequence, and
   the viewer turns those indices into boxes drawn on the scan. One divergence
   and every highlight in the archive lands on the wrong word.
4. **Text that a redaction failed to remove is never published.** Not in the
   HTML, not in the JSON, not in the search index. The build stops instead.
5. **Nothing loads from a third-party host.** No CDN, no font service, no
   analytics. An archive that phones home is a log of who read what.
6. **The build is deterministic.** Same input bytes, same output bytes, so two
   people can verify they published the same thing.

   One field reads the clock: `BuildInfo.built_at`, which goes into
   `manifest.json` and whose date is printed in every page footer. Set
   `SOURCE_DATE_EPOCH` to a Unix timestamp and it comes from there instead, and
   the whole tree is then a function of the input bytes — verified by building
   the demo twice with it set and comparing every one of the 204 files. A value
   that is not a timestamp is refused rather than silently ignored, because
   somebody who exported it asked for a build whose date does not move.

   Without it, two builds of the same folder differ in that one line of
   `manifest.json`, and in every footer if they straddle midnight; everything
   else — the HTML, the JSON, the encoded images, the service worker and its
   inventory — is byte-identical, cached or not.
   `tests/test_cache.py::test_a_warm_build_is_byte_identical_to_a_cold_one`
   pins the warm-versus-cold half of that.

   So: **quote `SOURCE_DATE_EPOCH` when you publish**, and a reader can rebuild
   your archive and diff it against yours with no differences at all.

## Modules

### `ingest/discover.py`
Walks the input, filters to supported types, computes SHA-256, drops exact
duplicates (same digest), assigns stable slugs, and orders documents naturally
(`doc2` before `doc10`). Refuses to follow a symlink out of the source folder,
and reports the ones it skipped.

### `ingest/pdf.py`
Per page: dimensions, embedded words with boxes, filled rectangles, and a
verdict on whether the embedded text layer is trustworthy. Also
`publish_pdf()`, the `strip_metadata` rewrite described above. Uses
`pdfplumber`, `pdfminer.six` and `pypdf`, and nothing else — `pypdf` for the
rewrite, for reading document metadata, and to turn "this file will not parse"
into a message that names why.

> `PyMuPDF` is AGPL-licensed and must not be imported anywhere in this project.

### `ingest/raster.py`
Renders pages to images by invoking `pdftoppm` as a subprocess, then encodes
WebP (and AVIF where available) at two widths plus a thumbnail, and a 24 px
placeholder inlined into the page. Denoises before encoding — scan grain costs
WebP 71% of its size and AVIF only 12%. Every encoder setting is pinned, and
`encoder_threads` is 1, because guarantee 6 is worth more than the wall clock
multi-threaded encoding would save.

`pdftoppm` is invoked with `-cropbox`, and `ingest/pdf.py` measures the page
from the same box. The two frames have to be the same rectangle or every pixel
check lands on the wrong pixels; `tests/test_security.py::test_the_rendered_frame_and_the_content_stream_frame_are_the_same`
is the assertion.

### `ingest/ocr.py`
Runs Tesseract with TSV output, keeping per-word boxes and confidence. Only
`level == 5` rows carry a real confidence; every other level reports `-1` and
will silently poison any average computed over it.

### `ingest/quality.py`
Decides a `PageVerdict` from the evidence. The stopword ratio is the primary
signal; mean confidence is the least trustworthy number in the file and is
reported but not decisive. Distinguishes *blank* from *pictorial* from *failed*
using ink coverage against word count, because all three produce no text and
only one of them is a problem.

### `ingest/redaction.py`
Two jobs, in order of importance:

1. **Hidden text.** Filled shape, opaque, drawn after the characters it covers
   or in the same colour, characters ≥80% inside it, confirmed by rendering the
   box and asking **each character whether its own cell is uniform** — a glyph
   reversed out of a black box is the most textured thing on the page, while
   the box around it is flat by any statistic taken over the whole of it. This
   is a safety feature; a false negative can burn a source. Every box that
   obliterated text is recorded separately from the findings worth stopping a
   build over, because *suppressed* never means *safe to publish*: the
   transcription drops every token any such box touches, whether or not the
   finding was reported. Whitespace is not text for this purpose — the gaps
   between the words of a heading printed *on* a box are not something the box
   removed, and counting them withheld the transcription of every page an
   agency had stamped `PAGE WITHHELD IN FULL`.
2. **Visible redactions.** Connected components on the binarised page, filtered
   by solidity, size, aspect and interior variance, then a redaction ratio
   measured against the inked region rather than the page.

### `ingest/exemptions.py`
Extracts statutory withholding codes, tolerating OCR damage, and associates each
code with the box it annotates. Not the nearest box: a stamp sits on the
*baseline* of the passage it explains and can be right across the page from it,
out in whichever margin the reviewer used, so the line is asked first and
distance only settles what the line leaves open.

| What is on the code's line | Which box gets it |
|---|---|
| exactly one box | that box, at any distance across the page |
| several boxes, one of them near | the nearest of them |
| several boxes, none near | nobody: ambiguous, and counted at page level |
| no box at all | the nearest box within the near field, if any |

"Near" is 0.05 of the page height, about 40pt on Letter — the width of a stamp
printed *beside* a box, and the whole of the near field. Footer and header
legends are unchanged by any of this: inside those bands the reach along the
line is switched off, so a code there needs a box right beside it or it is read
as a legend for the page, and a band holding three or more codes is a legend
whatever else is true. Attaching six footer codes to whichever box happens to
be lowest on the page would be a false statement about a law.

Four vocabularies, one per jurisdiction, selected by `jurisdiction` in the
config:

| Key | Vocabulary | Codes |
|---|---|---:|
| `us` | US FOIA `(b)(1)`–`(b)(9)` and subparts, **plus the Privacy Act** (5 U.S.C. 552a) `(j)` and `(k)` codes, which travel beside the `(b)` codes in FBI and other law-enforcement releases | 23 |
| `uk` | UK Freedom of Information Act 2000, by section | 16 |
| `ca` | Access to Information Act (Canada), by section | 12 |
| `eu` | Regulation (EC) 1049/2001, by article | 6 |

`VOCABULARIES` is the only list of jurisdictions: `config.py` derives the set of
valid `jurisdiction` values from it, so a new vocabulary is reachable the moment
it exists.

The vocabulary is also the last filter: a code the vocabulary does not list is
not a code, whatever it looks like. `legend()` turns the codes a release cites
into an ordered, deduplicated key with a plain-English gloss for each — written
for a reader who has never seen the statute, because the gloss is what ends up
under a black box on the site.

### `ingest/bates.py`
Finds the production control number stamped in the page margins, verifies it by
requiring positional stability and monotonicity across pages, and reports
**gaps**, which are pages withheld in full.

### `build/site.py`
Orchestration, output layout, JSON payloads, manifest, and the published
originals.

### `build/search.py`
Emits the Pagefind index by invoking the `pagefind` binary over the generated
pages. Never the Python API: it is about 60× slower for identical output.

### `build/negative.py`
Draws `withheld/negative/index.html`: every redaction in the release as a
rectangle, at the size it took up on the page it came from. Builds the SVG in
Python so the page is correct before any script runs, and regroups it with
radio buttons and sibling selectors rather than JavaScript. Its ceilings are in
the limits section below.

### `build/offline.py`
Generates the service worker and its inventory. Runs **last** in
`SiteBuilder.run()`, and has to: it takes an inventory of what is on disk, and
anything written after it — the search index most of all — would be missing
from what a reader can store.

### `compare.py`
Reads a second folder, works out which page of the new release is which page of
the old one, and writes the `compare/` section. `compare.build()` is called
unconditionally from `SiteBuilder.run()` and is a no-op on an ordinary build.
It is at the top level rather than under `build/` because it drives a whole
ingest of its own. [`COMPARING.md`](COMPARING.md) is the long version.

### `cache.py`
A content-addressed page cache, and the `--watch` loop that sits on it. Keyed on
the source file's digest plus every part of the environment that could change
what a page comes out as. Never stores a page that leaked, or one the build
could not vouch for. [`CACHING.md`](CACHING.md) is the long version.

### `i18n.py`
The message catalogues, the plural rules, the number and date formats, and the
`t()` / `n()` / `pct()` globals the templates use. Also emits `assets/i18n.js`,
which is the same catalogue for the strings the reader's scripts write.
Translation happens at build time; nothing is translated in the browser.
[`TRANSLATING.md`](TRANSLATING.md) is the long version.

### `lang.py`, `imaging.py`, `textblock.py`, `serve.py`
Support. `lang.py` answers *does this look like language?* from stopword lists
held as plain literals. `imaging.py` holds the pixel primitives three modules
would otherwise each reimplement with three different thresholds.
`textblock.py` renders the small Markdown subset `about.md` is allowed to use,
escaping first and re-introducing only a fixed list of constructs. `serve.py` is
the preview server, which states every MIME type this project emits rather than
trusting the machine's own table.

## Output layout

```
site/
  index.html                     collection overview
  about/index.html               provenance, method, checksums
  browse/index.html              every document
  search/index.html              search
  withheld/index.html            the redaction ledger
  withheld/negative/index.html   every redaction drawn at true size
  compare/index.html             what changed since an earlier release
  compare/<doc>/index.html       …per document, for the documents that changed
  compare/earlier/<doc>/…        thumbnails of the earlier release's changed pages
  d/<doc>/index.html             document: page grid, metadata, notes
  d/<doc>/p/<n>/index.html       page: scan + text + boxes
  files/<doc>.pdf                the original; see guarantee 2
  media/<doc>/p<nnnn>@<size>.<fmt>   page images; see below
  data/<doc>/<n>.json            word boxes for the page
  data/docs.json                 titles, page counts, control numbers, legend
  manifest.json                  digests and build stamp
  sw.js                          the offline service worker, generated
  offline.json                   its inventory: every file, with sizes
  assets/stackroom.css           fonts, base and parts, concatenated
  assets/i18n.js                 the interface strings the scripts write
  assets/viewer.js               page templates only
  assets/search.js               the search page only
  assets/js/*.js                 one file per enhancement, every page
  assets/fonts/*.woff2           self-hosted, subsetted
  assets/favicon.svg
  _pagefind/…                    search index
  .nojekyll                      or GitHub Pages deletes _pagefind/
  .stackroom                     a note saying this folder is rewritten
```

**The published extension comes from the file's own bytes, never from its
name.** A PDF that arrives called `report.html` is published as `report.pdf`,
and a file whose type cannot be established is published as `.bin`. A name is
all it takes to have a source file served as active content from the archive's
own origin.

**Image filenames.** The page number is zero-padded to four digits, the
thumbnail is named `@thumb` and not by its width, and *every* configured format
is written at *every* configured width — the `<picture>` element picks one.
With the default `render.widths = [1600, 900]` and
`render.formats = ["avif", "webp"]` that is `p0007@1600.avif`,
`p0007@1600.webp`, `p0007@900.avif`, `p0007@900.webp`, `p0007@thumb.avif`,
`p0007@thumb.webp`: **six files per page**, and more if a third format is
configured. Together with the page's HTML and its word-box JSON that is eight
files per page in the published site.

**`data/<doc>/<n>.json` is not what the viewer reads.** The same boxes are
inlined in the page's HTML, in a `<script type="application/json">` block, and
that is where `viewer.js` gets them; nothing in the site fetches the JSON files.
They are published as a machine-readable side-channel for anyone building on
the archive, and they cost about 4.7 KB a page. `docs/PERFORMANCE.md` §8.4 is
the argument about whether to keep them.

**Scripts.** Everything in `assets/js/` is loaded on every page, deferred,
except the ones a template takes in the head instead — `HEAD_SCRIPTS` in
`build/site.py` names them and the templates do not. A script belongs there
only if something it does has to happen before the browser's first rendering
opportunity: `prefs.js` puts the reader's theme on `<html>` before the first
paint, and `scan.js` registers the `pagereveal` listener that gives a page turn
its direction, on page templates alone. `assets/i18n.js` is loaded ahead of
both, because `prefs.js` reads it.

## The search contract

Pagefind indexes only `d/*/p/*/index.html`. Each of those files contains
exactly one element carrying `data-pagefind-body`, and that element contains
**only** the page's tokens, each in its own `<span>`, separated by whitespace.
Pagefind strips tags and splits on `/\s+/`, so `result.words[i]` is an index
into `Page.words`. The viewer reads the box for that index out of the inline
`page-data` block and draws it over the scan.

Consequences to respect:

- No punctuation, page furniture or chrome inside the body element.
- For CJK, emit one character per token; multi-character tokens get
  re-segmented by Pagefind and the indices silently desynchronise.
- `--force-language` must be set, or Pagefind builds a separate index per
  `<html lang>` and the page only ever loads one of them.

**The index's language is not the interface's language.** `SiteBuilder.index_language()`
takes `search.language` if set, otherwise the language most of the pages were
actually read as, otherwise the interface language, otherwise English. A
Russian-language archive of English documents gets a Russian interface and an
English stemmer; see [`TRANSLATING.md`](TRANSLATING.md).

## What the withheld percentage means

One definition, used everywhere the number appears. All of it is an **area**:
square points of content, not a count of pages and not a mean of percentages.

- **A page's content area** is the union of its surviving word boxes and its
  redaction boxes, measured as an area union on a grid — the part of the sheet
  that carries content, not the sheet. Not the page, because a letter page
  blacked out from margin to margin would score 0.63 when the honest answer is
  1.0, an inch of margin on every side being 37% of the paper. Not the bounding
  box of the surviving text either, because that shrinks as more is withheld,
  so the ratio would *fall* the more an agency removed. On a scan there is no
  text layer to take the content region from, so the ink is measured from the
  pixels of the render instead: the same fraction, at the resolution of the
  image.
- **`Page.redaction_ratio`** is that page's withheld area over that page's
  content area. It is the number printed beside the page on
  `withheld/index.html`.
- **`CollectionStats.redaction_ratio`** is the total withheld area over the
  total content area **of the pages that carry redactions**. It is the figure on
  the front page and at the head of the ledger, and both name that denominator
  in the sentence beside the number — on the demo release, *"35.3% of the
  content on 5 redacted pages"*.
- **`CollectionStats.redaction_ratio_collection`** is the same division over
  **every** page. A different fact, published in `manifest.json` and printed by
  the CLI, and a reader has to be told which one they are looking at: one page
  withheld in full out of a thousand is 100% of that page and 0.1% of the
  release.

Because both collection figures are areas rather than means, they are the
content-weighted mean of the per-page shares the site prints, and the two can
never disagree. A page with neither words nor boxes has no measurable content:
it goes into neither side of either division and is counted in
`unmeasured_pages`, which the CLI states out loud, because a denominator that
quietly excludes pages is the same bug in a different place.

`manifest.json` publishes `withheld_area_pt`, `redacted_pages_area_pt` and
`collection_area_pt` beside the two ratios, so anyone can check the division.

## Honest limits, stated in the docs and enforced in the CLI

Cold start is what a reader downloads before they can type: the pagefind
runtime, which is fixed, plus about 5.5 bytes of metadata per page.
`build/search.py` is the authority — `RUNTIME_BYTES` is 97,151 and
`estimate_cold_start()` is the arithmetic.

| Pages | Cold start | Behaviour |
|---|---:|---|
| ≤ 5,000 | ≈ 122 KB | Comfortable. |
| ≤ 20,000 | ≈ 202 KB | The ceiling Stackroom stands behind. |
| ≤ 50,000 | ≈ 363 KB | Works, degrades. The build report says what it will cost. |
| > 50,000 | — | The build refuses to write the site without `--i-know`. |

Two details worth having right. The warning above 20,000 comes from the search
index, so `--no-search` silences it; the refusal above 50,000 is in the CLI and
fires after the ingest, before the site is written. And a build stopped by that
refusal has still read every page, so the operator has paid for the ingest
either way.

Query latency tracks the *number of hits*, not the corpus size — 3 ms at 59
hits, 3.2 s at 20,000 — so the client enforces a minimum query length
(`search.min_query`, default 2) and debounces. A tool that gets slower the more
useful the query is should say so.

The negative has its own ceilings, for the same reason: it draws one shape per
redaction, and a release can hold more of them than a picture can carry. The
constants are `PACKED_LIMIT`, `DETAIL_LIMIT`, `RECT_LIMIT`, `CELL_LIMIT` and
`LIST_LIMIT` in `build/negative.py`.

| Redactions drawn | Behaviour |
|---|---|
| ≤ 500 | All three arrangements: page order, by exemption, by size. |
| ≤ 4,000 | One arrangement, one `<rect>` per redaction, each with its own tooltip. |
| ≤ 8,000 | One arrangement, redactions merged into one path per exemption code — half the bytes, the same picture, no per-rectangle tooltip. |
| > 8,000 | Only the largest are drawn. |

Pages are capped separately at 1,200, and the index under the field at 500
rows. At both ceilings the field is roughly 370 KB of markup and 100 KB over
the wire — the same order as one of the page scans this archive already serves
without apology. Above them the field keeps the pages with the most withheld on
them and the largest rectangles within those pages. That is a real bias, so the
page states it, along with how many rectangles it left out and what share of
the withheld area is on screen: a picture that quietly omits half its subject
is worse than one that admits it.
