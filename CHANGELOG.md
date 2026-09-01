# Changelog

All notable changes to this project are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two kinds of change get more detail than their size suggests, because people
depend on the exact behaviour: anything affecting the failed-redaction check,
and anything affecting the published output layout. Both are called out below
under their own headings when they occur.

## [Unreleased]

### Fixed

#### The failed-redaction check

**A black box no longer withholds a whole page because of the spaces between
the words printed on it.** The commonest single page in a FOIA release is a
sheet withheld end to end: a page-sized black rectangle with
`(b)(5) PAGE WITHHELD IN FULL` reversed out of it in white. Every *letter* of
that heading stands out of its own cell and is correctly not hidden — and the
*spaces* between the words do not, because a space has no glyph in it and its
cell really is solid box colour. `ingest/redaction.py` recorded "this box
obliterated text" from that list before it tested whether any of it was
whitespace, so the box went into `covered`, and `covered` is the list
`pipeline._drop_hidden` withholds every token of.

- Nothing was ever reported as leaking, which is what made it quiet. The page
  simply came out of the pipeline with no words on it: a blank transcription
  of a sheet whose entire content is the sentence saying it was withheld, and
  no exemption code, because the code an agency prints inside the rectangle
  went with it. On the demo collection that took the ledger from six pages
  withheld under b(5) to five.
- Every question in `_scan_hidden` that means *was there text here* is now
  asked of the characters that carry a glyph. The recovered runs keep their
  spaces — a fix that dropped them from the run rather than from the count
  would report `GregoryAldana`.
- Two more consequences of the same shape went with it. A box painted over
  nothing but a word gap no longer produces *N hidden-text finding(s) rest on
  the content stream alone* with no findings behind it; and the warning that
  asks a human to look at a box the check declined to clear — *the characters
  under it stand out of their own cells in the rendered page* — is no longer
  silenced by the highlighted phrase happening to contain a space.

**A page stamped `PAGE WITHHELD IN FULL` is no longer reported as a failed
redaction.** `_Crop.box_is_flat` measures `std` and a percentile span over the
whole rectangle, and a line of 10pt lettering across a page-sized box is 0.2%
of it: measured at 150 dpi that page read std 10.5 and spread 0, inside both
cuts, so the box was judged uniform and every character the file reported
inside it was called invisible — the loudest thing this program can say, fired
on a page that is exactly right. Each character is now asked about **its own
footprint**, where a white stem beside a black counter is the most textured
thing on the page. The whole-box test survives only to answer for cells too
small to measure, and only while no cell contradicts it.

**A glyph flush with the edge of its box is no longer read as legible.**
`render_page_crop` rounds its crop outwards, so a character's cell at the very
edge of a rectangle could reach the sliver of paper behind it. That did not
matter while the whole-box test was deciding; now that each character decides
for itself it decides real findings. Measured on a real 150 dpi rendering,
shifting a 40pt box by two thirds of a pixel took a buried glyph's cell from
std 0.0 to std 56.9 — from *hidden* to *legible*, with nothing about the
document changed. Per-character cells are now clamped off the crop's outermost
row and column, and the finding survives wherever the box lands between two
pixels.

#### Everything else

**The near field for a statutory code is measured in the units it is
documented in.** `exemptions.associate` scales horizontal gaps by width÷height
so they can be compared with a threshold expressed in page heights;
`Page.aspect` is height÷width, which is what the layout, the negative and the
compare diagrams want. The pipeline was handing over the second, which squares
the page up the wrong way and makes the near field 1.67× tighter horizontally —
about 24pt on Letter, where the docstring promises 40. It could only ever drop
a code, never invent one, which is why it survived so long.
`pipeline._width_over_height` is now the one place that ratio is computed, and
it exists to stop anyone handing over `Page.aspect` again.

**A page in a language the collection did not declare is no longer called
unreadable.** `ocr.languages` holds Tesseract's codes — what the recogniser
should expect on a *scan* — and it was also being handed to the quality check
as the list of languages whose stopwords to count. A born-digital Russian page
in a collection declared `["eng"]` therefore scored a stopword ratio of exactly
zero, was told *the PDF's own text layer does not read as language; re-OCR this
page*, and was then either re-read by an English-only recogniser or published
as unreadable — which tells a reader that search cannot find anything on a page
whose text is perfect. Reaching people who do not read English is a stated goal
of this project, and this was the tool refusing to.

- `lang.stopword_ratio` now takes the maximum over every word list it has,
  whatever it is told, so a page is judged as the language it is. A declared
  list is a **prior**: it may raise a page's score — the union of two lists is
  what a genuinely bilingual page needs — and it can never lower it.
- There is deliberately **no second configuration key** for "what the text is
  expected to be". With the maximum always taken, a declared list changes the
  answer only for a page that is two languages at once. A fourth language
  setting, in a tool that already has three people confuse — `ocr.languages`,
  `search.language` and the interface `language` — is a tool people
  misconfigure. `ocr.languages` keeps exactly one meaning: what to hand the
  recogniser.
- The default stays `["eng"]`, and that is now a measured decision rather than
  an assumption. On this project's synthetic English scans, `eng` against
  `eng+rus`: no difference on undamaged pages, and on a page scanned three
  degrees out of upright the word error rate doubled, 0.0076 to 0.0152, with
  `no` coming back as the Cyrillic `по` and `cape` as `саре`. Declaring the
  second language to work around the bug above was not free, which is why the
  bug had to be fixed rather than documented.

**A page in a script no word list covers is reported as unjudged, not as
garbage.** Arabic, Hebrew, Greek, Devanagari, Thai, Japanese and Korean score
zero against all eleven lists this project ships, and a zero was being read as
evidence against the page. `lang.stopwords_apply` is the new test, and it is
deliberately not "is the dominant script one we have a list for": `script_of`
folds kana and Hangul in with the Han ideographs, whose word list is a hundred
Chinese characters no Japanese page contains, and folds Devanagari, Thai and
Georgian in with genuinely bilingual pages, which must still be judged. Such a
page now carries the note *no stopword list covers the script here, so the text
is judged on other signals*, and its text layer is kept rather than replaced by
recognition in an alphabet nobody asked for.

**The verdict "the PDF's own text layer does not read as language; re-OCR this
page" now needs corroboration.** It was the one single-signal condemnation in
`ingest/quality.py`, on the argument that a text layer is either right or wrong
and not a matter of degree — true of the layer, false of the evidence, since a
low stopword ratio also means "a language we have no list for". A broken
`ToUnicode` map supplies the second signal easily (invented-looking tokens,
word lengths unlike prose, often replacement characters caught earlier still);
a page in Hindi supplies none, because there is nothing wrong with it.

### Added

- When a page reads as a language the collection did not declare, the build
  says so — once, grouped, with a count: *this page reads as Russian, which is
  not among the languages this collection declares (English); nothing is wrong
  with this page, but a scan in that language would be recognised without it*.
  Four hundred Russian pages in a collection somebody called English is a
  decision they need to make, not a detail to discover later.
- `stackroom cache path --entries`, which prints `<base>/pages/<layout>` — the
  path `stackroom cache path` used to print. See below.
- When a build gets nothing at all from a cache that is not empty, it now
  explains why instead of leaving "0 of 16 pages came from the cache" looking
  like a broken cache. Each layout directory carries an `environment.json`
  stamp of what its entries were last written with, so the message can usually
  name what moved — *tesseract moved from 5.3.3 to 5.3.4* — and falls back to
  printing this build's fingerprint when nothing in the environment changed or
  the cache predates the stamp.

### Changed

**A statutory code is attributed by the line it sits on, not by how close it
is.** A reviewer's stamp sits on the *baseline* of the passage it explains and
can be anywhere across the page from it — out in whichever margin the reviewer
used, or tucked inside the rectangle. The rule was a single radius, which
cannot express that, so a margin stamp for a redaction in the middle of a line
was several hundred points away and went unread. The order of the questions is
now:

| What is on the code's line | Which box gets it |
|---|---|
| exactly one box | that box, at any distance across the page |
| several boxes, one of them near | the nearest of them |
| several boxes, none near | nobody: ambiguous, and counted at page level |
| no box at all | the nearest box within the near field, if any |

- **Footer and header legends are unchanged**, and the reach along the line
  stops at those bands deliberately. Plenty of releases print the full list of
  exemptions used at the bottom of every page; the nearest box to that list is
  whatever happens to be lowest on the page, and attaching six codes to it
  would be a fabrication. Inside a band a code needs a box right there with it
  or it is read as a legend for the page, and a band holding three or more
  codes is a legend whatever else is true. The price is a margin stamp for a
  box that is itself down in the footer band, and it is the right way round: a
  legend mis-attributed is a false statement about a law, and a margin stamp
  missed is a box that reports no code.
- Measured on the demo collection stamped in the margin: 23 of 43 boxes
  carried their code before, 41 of 43 now. `demo/build-demo.py --stamp` builds
  the collection either way so the difference can be measured rather than
  argued about.

**`stackroom cache path` prints a different path, and scripts consume it.** It
now prints the cache's **base** directory — the one `--cache-dir` takes, and
the one `stackroom cache` shows beside "Where" — so
`stackroom build ./release --cache-dir "$(stackroom cache path)"` reopens the
cache you had. It used to print the entry directory inside it,
`<base>/pages/<layout>`, which is the one path here that cannot be fed back in:
`--cache-dir` pointed at it opens a second cache nested inside the first,
silently, and every page misses. A script that was feeding `cache path` to
`--cache-dir` was broken and is now right; a script that wanted the entry
directory needs `--entries` adding to it. `docs/CACHING.md` §6.

### Translation

- The masthead's link to `compare/` is translated. It was published as the
  English word `Compared`, marked `lang="en"`, while everything behind it was
  translated; `base.html.jinja` now calls `t('nav.compare')` and the key is
  filled in in all four catalogues.
- The paragraph that explains why a document has both a `sha256` and a
  `published_sha256`, shown on the about page when `safety.strip_metadata` is
  on, is translated. Two keys rather than one — `about.two_digests`, which
  agrees with a count and carries the plural forms, and
  `about.two_digests_which_html`, which is markup around three literal names no
  language translates — so the plural forms stay off the half with tags in it.
  The two field names themselves stay English, marked `lang="en"`, because they
  are what a reader will find beside those numbers in `manifest.json`.

### Removed

- `docs/i18n-templates.patch`. It was a delivery mechanism for the change that
  turned every literal string in `templates/` into a catalogue lookup, and that
  change is in the tree — the templates carry `t()` calls and
  `patch -p1 --dry-run` reports the patch as already applied. Left in place it
  was 53 KB of instructions that would half-apply for the next person who tried
  them. The argument it carried is in `docs/TRANSLATING.md`.

## [0.1.0] — unreleased

<!-- Set the date when the tag is cut: ## [0.1.0] - YYYY-MM-DD -->

The first release. Stackroom turns a folder of documents into a static,
searchable archive that anyone can host, mirror or read from a USB stick.

### Added

**The command line**

- `stackroom build <folder>` reads a folder of documents and writes a static
  site (default `./site`). `--out`/`-o`, `--config`/`-c`, `--title`,
  `--workers`/`-j`, `--no-search`, `--force`, `--unsafe-publish-leaks`,
  `--i-know`, `--watch`, `--watch-interval`, `--cache`/`--no-cache`,
  `--cache-dir`, `--cache-max`, `--debug`.
- `stackroom check <folder>` looks for failed redactions without building a
  site, for use on documents you are about to *send* as well as ones you are
  about to publish. It rasterises every page to look at the pixels; the images
  go to a temporary folder whose path it prints and which it deletes on the way
  out, and `--scratch` puts that folder wherever you say, such as a ramdisk. It
  never uses the page cache, in either direction. Exits 2 when it finds
  something, and also when it could not check a page.
- `--debug`, on `build`, `check` and `compare`: when something goes wrong, print
  the traceback and what is installed on this machine, in the form an issue
  needs.
- `stackroom compare <old> <new>` publishes the new release as an ordinary
  archive plus a `compare/` section saying what was disclosed, what was
  withheld, and what moved. `--old-label`, `--new-label`, `--no-old-scans`, and
  the build flags. It publishes text from both folders.
- `stackroom serve <site>` previews a built archive over loopback, with every
  MIME type this project emits stated explicitly rather than looked up in the
  machine's own table. `--port`, `--host`, `--open`.
- `stackroom init <folder>` writes a `stackroom.toml` and an `about.md` to fill
  in.
- `stackroom doctor` reports what is installed and what to type to get what is
  not: poppler, Tesseract and its language data, pagefind, AVIF encoding.
- `stackroom cache` shows what the page cache holds and where — both the base
  directory and the entry directory inside it, labelled, because only the first
  can be handed back to `--cache-dir`; `cache path`, `cache path --entries`,
  `cache prune --max`, `cache clear`.

**Reading documents**

- PDFs and page images (PNG, JPEG, TIFF, GIF, BMP, WebP), identified by magic
  number rather than by extension. Text, Markdown and CSV files beside the
  documents are treated as the operator's own notes and are not published as
  pages.
- SHA-256 of every source file, exact duplicates dropped, natural ordering
  (`doc2` before `doc10`), and stable URL slugs including a fixed
  transliteration table for Cyrillic filenames. Slugs are never Windows device
  names, and a filename whose bytes are not valid UTF-8 does not stop the
  build.
- Symlinks pointing out of the source folder are not followed, and are listed
  as skipped rather than dropped silently.
- Embedded text layers via `pdfplumber` and `pdfminer.six`, with per-word boxes
  and a verdict on whether the layer can be trusted.
- OCR via Tesseract with per-word boxes and confidence; `auto`, `always` and
  `never` modes; multiple languages; optional auto-rotation.
- Page quality verdicts that distinguish *blank* from *pictorial* from *failed*,
  using the stopword ratio as the primary signal rather than mean confidence.
  The ratio is the best a page can do over the eleven word lists that ship, so
  a page is judged as the language it is rather than as the language the
  operator declared, and a page in a script no list covers is reported as
  unjudged rather than as garbage. Pages whose recognition failed are marked as
  such on the page and in the search results, so a search that skipped them
  cannot silently imply a phrase is absent.
- The rendered frame and the content-stream frame are the same rectangle:
  `pdftoppm` is invoked with `-cropbox` and the page is measured from the same
  box. A `CropBox` that differs from the `MediaBox` is ordinary — Acrobat's
  "crop pages" and many scanners emit one — and used to point every pixel check
  at a corner of the page.
- `render.max_megapixels` is enforced on the path the build actually uses, and
  a page poppler refuses to allocate is an error rather than a one-pixel scan.

**Redactions**

- A failed-redaction check: text still present in the file underneath an opaque
  filled rectangle, confirmed by rendering the box and checking that the pixels
  really are uniform. The build **stops** rather than publish it. The recovered
  text is held in memory to show the operator and is never written to any file,
  including the page cache. `--unsafe-publish-leaks` overrides the stop for the
  case where the text underneath is already public; the original PDF still
  contains it.
- Every box that obliterated text withholds that text from the page and the
  index, **whether or not the finding was reported**. Suppression means *not
  worth stopping the build over*, never *safe to publish*.
- A token that any opaque shape touches is withheld whole. Half a redacted name
  is still a redacted name.
- Visible-redaction detection over both the content stream and the rendered
  page, with a per-page withheld share measured against the page's own content —
  the union of its surviving words and its black boxes — rather than the area of
  the sheet. The collection-level figures are areas summed over pages rather
  than means of percentages, so a dense page counts for more than a one-line
  one and a page blacked out end to end is counted rather than dropped for
  having no surviving words. Two of them are published: the share of the
  content on the redacted pages, which is what the front page and the ledger
  print, and the share of the whole release, which is in `manifest.json` and in
  the build report. The site names the denominator in the sentence beside each
  number, and `manifest.json` publishes the three areas so anyone can check the
  division. `docs/ARCHITECTURE.md` has the definition.
- A `withheld/` ledger: how many pages are redacted, how much of each page is
  gone, and which exemption the agency cited. Ambiguities that cannot be
  resolved — a dark scan band is genuinely indistinguishable from a black box —
  are counted and reported rather than hidden.
- `withheld/negative/`: every redaction in the release drawn as a rectangle at
  the size it took up on the page it came from, in three arrangements, with the
  drawing done in Python so the page is correct before any script runs. It
  states what it left out and why.
- Every per-page note the ingest produced — *this box has characters painted
  under it, but they stand out of their own cells in the render, so check it by
  hand*, *this page could not be rendered, so it was never checked*, *the
  redaction check failed here* — is printed to the operator, grouped by
  message, safety notes first.

**Statutory codes**

- Withholding codes read through OCR damage and normalised to one spelling per
  code, with a plain-language gloss for readers who have not read the statute:
  US FOIA `(b)(1)`–`(b)(9)` with subparts and the Privacy Act `(j)`/`(k)`
  companions, UK FOIA 2000 sections, Canadian ATIA sections, and EU Regulation
  1049/2001 articles. Selected with `jurisdiction` in `stackroom.toml`, whose
  valid values are derived from the vocabulary table itself, so a new
  vocabulary is reachable the moment it exists.
- Codes are attached to the box they annotate by reading the layout: a code on
  the same line as exactly one box belongs to that box however far across the
  page it is, several boxes on the line are settled by proximity or left
  ambiguous, and a code with no box on its line falls back to the near field.
  Footer and header legends are recorded against the page instead of being
  blamed on whichever box happens to be nearest.

**Production numbering**

- Bates and other control numbers found in the page margins, verified by
  positional stability and monotonicity rather than by pattern alone, and
  distinguished from ordinary page numbers. Reports **gaps**, which are pages
  withheld in full.

**The site**

- One real HTML page per document page, containing that page's text, readable
  and citable with JavaScript switched off.
- The original file, downloadable from every page that came from it, published
  under an extension derived from its own bytes rather than from its filename —
  a name is all it takes to have a source file served as active content from
  the archive's own origin.
- Page renderings in WebP and AVIF at two widths plus thumbnails, denoised
  before encoding, and a 24-pixel-wide inline placeholder so the frame holds a
  picture of the page while the scan is still arriving.
- Full-text search over the per-page files via Pagefind, with results that open
  the scan with the matched phrase boxed where it was printed. The index's
  stemmer is the language the documents were read as, which is not necessarily
  the language of the interface.
- `manifest.json` with the SHA-256 of every source file, the digest of the file
  as published, and a build stamp; `about.md` rendered into an about page, and
  a note on the front page when it is missing.
- A generated service worker, so the archive keeps reading with the network
  off: the shell and the fonts a page actually uses are stored on the first
  visit, pages and scans as the reader opens them, and everything else only on
  an explicit click that states the size first.
- Self-hosted fonts, subsetted by script, and no third-party requests of any
  kind. A strict Content-Security-Policy on every generated page.
- Reader enhancements, every one of them optional and none of them required to
  read, cite or crawl a page: the full-size scan viewer, the page-turn
  animation, the ribbon scrubber, a citation panel that carries the digest an
  editor will check, passage permalinks over a selected run of words, a command
  palette for finding a document or a control number, and a light/dark
  preference.
- Output is deterministic: same input bytes, same output bytes, cached or not.
  The build timestamp is the one thing that reads the clock, and
  `SOURCE_DATE_EPOCH` overrides it — with it set, two builds of the same folder
  are byte-identical in every file. A value that is not a Unix timestamp is
  refused rather than ignored. See `docs/ARCHITECTURE.md` guarantee 6.

**Translation**

- The interface is translated at build time, from `language` in
  `stackroom.toml`. Four catalogues ship — English, Polish, Russian, Ukrainian
  — with CLDR plural categories, per-language number and date formats, and
  `lang`/`dir` on `<html>`. `python -m stackroom.i18n list` prints the message
  count.
- The strings the reader's own scripts write are translated the same way, into
  a generated `assets/i18n.js`. Nothing is translated in the browser.
- `python -m stackroom.i18n` scaffolds, inspects and checks a catalogue,
  including the mistake everybody makes: a `one` form that covers more than the
  number one and does not print the count.
- What is *not* translated is listed in `docs/TRANSLATING.md`, and it is short:
  the statutory glosses, deliberately; one `aria-label` inside the
  search-indexed block, which is frozen byte-for-byte because the index's word
  positions are the boxes drawn on the scan; and the command line, which talks
  to the operator rather than to a reader.

**Caching and watch mode**

- A content-addressed page cache, on by default, keyed on the source file's
  digest and on everything in the environment that could change what a page
  comes out as — the versions of Tesseract, poppler and Pillow, the
  `.traineddata` files, the installed fonts, and Stackroom's own source when it
  is running from a working tree. A warm rebuild of an unchanged collection is
  seconds rather than the whole build.
- It refuses to store a page that leaked, a page the build could not check, a
  page whose warnings suggest the environment rather than the document, and a
  page that has already been annotated. Adding a field to `PageJob` that nobody
  has classified disables the cache with a message naming the field.
- `stackroom build --watch` rebuilds on change, debounced, polling, ignoring
  the output directory and the cache, and surviving a build that stops.

**Comparing two releases**

- `stackroom compare` aligns two productions of the same documents — by control
  number, text, word order, layout and image hash, through a Needleman–Wunsch
  global alignment with passes for swaps, moves and pages that share no text
  with themselves — and reports what a black box stopped covering and what it
  started covering, corroborated against the other release's text.
- Findings carry a confidence, pairs matched on position alone say so, and
  documents that cannot be aligned produce no claims at all.
- No text either release hid under a black box appears anywhere in the
  comparison.

**Honest limits, enforced rather than documented**

- Cold-start size for search is estimated and reported before publication
  (≈ 122 KB at 5,000 pages, ≈ 202 KB at 20,000). Above 20,000 pages the build
  report says what it will cost; above 50,000 the CLI refuses to write the site
  without `--i-know`.
- Very short queries are refused, because query latency tracks the number of
  hits rather than the size of the archive.
- `stackroom.toml` is looked for beside the documents and at most three
  directories above them, and the CLI prints which file it used — louder when
  that file is not inside the folder the operator named.
- `ocr.timeout`, `ocr.psm` and `search.min_query` are bounded, because a
  release often arrives with a `stackroom.toml` in it and those numbers reach a
  subprocess.
- The output directory is only emptied when Stackroom can tell it wrote it.
  `manifest.json` is also what a Web App Manifest is called, and `.nojekyll` is
  in every GitHub Pages site that has ever needed one.

**Optional metadata stripping**

- `safety.strip_metadata` rewrites each published PDF from its page tree, which
  drops `/Info`, XMP and every earlier revision an incremental save left
  behind. Two consequences the archive states rather than hides: the published
  file is no longer byte-identical to the source, so the manifest records both
  digests; and a rewrite is not a sanitiser — page annotations survive it,
  bookmarks, attachments and form fields do not. Off by default, because that
  metadata is often itself evidence about the production. A file that cannot be
  rewritten is published unchanged, with a warning that names it.

### Known limits at 0.1.0

- Redaction applied as an image, a clipping path, or a non-rectangular shape is
  not seen by the hidden-text check. `stackroom check` passing is evidence, not
  a guarantee — [SECURITY.md](SECURITY.md) lists what gets past it and
  [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) is the long argument.
- Text hidden by something other than a box over it — white on white, outside
  the crop box, in a switched-off optional-content layer — is missed *and then
  published as the page's transcription*. The one case that is handled is
  invisible render mode over blank paper.
- Skewed scans defeat the visible-redaction pass at about 0.2° of rotation;
  deskew first. There is no deskewing step yet. This costs the ledger, never
  the leak check.
- Without `SOURCE_DATE_EPOCH`, two builds on different days differ in every
  page, because the footer carries the build date. Set it when you publish.
- The exemption glosses, one `aria-label` in the transcription, and the command
  line itself are English in every archive. The stylesheet is not finished for
  right-to-left, so no right-to-left catalogue ships.
- PDFs and page images only. Not `.pst`, not audio, not video.
- No collaborative annotation, no accounts, no summaries.

[Unreleased]: https://github.com/igor-vuta/stackroom/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/igor-vuta/stackroom/releases/tag/v0.1.0
