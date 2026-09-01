# Performance

Every number in this file was measured on the demo collection, on the machine
described below, with the scripts described below. None of it is an estimate
unless it says so. Where a measurement contradicts a comment already in the
source, the contradiction is called out rather than quietly fixed.

The order is deliberate: what was measured, what it cost, what was changed,
and — at the end, at the same length — what was tried and rejected, with the
numbers that killed it.

> **This is a lab notebook, and it is dated.** The measurements in §2 to §7 were
> taken during one piece of work in the summer of 2026, against the source
> snapshots named in §1. The tree has moved since — the stylesheet, the scripts
> and the message catalogues have all grown — so a byte figure here is a record
> of what a thing cost when it was measured, not a promise about what it costs
> now. §0 is the short list of what has been re-measured since and what changed.

---

## 0. Re-measured on 2026-09-01

Only the numbers below were re-taken; everything else stands as the record of
when it was written.

**Build time was the one number three documents disagreed about, and this is
the reconciliation.** Same collection, same machine, `--no-cache`, `workers=1`,
runs interleaved between configurations so that a busy machine costs each alike.
The machine was shared while these ran, so the honest reading of each cell is
the **minimum**: contention can only make a build slower.

| configuration | runs | best | typical | CPU (best) |
|---|---:|---:|---:|---:|
| `avif_speed = 7` (today's default), with search | 6 | **46.2 s** | 48–51 s | 44.8 s |
| `avif_speed = 7`, `--no-search` | 5 | 51.0 s | 51–55 s | 42.1 s |
| `avif_speed = 6`, with search | 5 | **52.3 s** | 53–60 s | 50.8 s |
| `avif_speed = 6`, `--no-search` | 2 | 53.4 s | — | 50.3 s |

`avif_speed` was overridden for these runs from outside the package, so nothing
in the tree changed between cells.

- **§4's 52.66 s reproduces.** It is an `avif_speed = 6` build with search, and
  the current tree at speed 6 measures 52.3 s at best. That row is sound; it is
  simply not the default any more, and §4 now says so in its heading.
- **§9's "~39.5 s at `avif_speed = 7`" does not reproduce, and never was a
  measurement.** It is 52.7 − 13.2 from §4.2's encoder table, and whole-build
  arithmetic from an encoder-only A/B does not survive contact with a build that
  also rasterises and OCRs. Measured end to end, the saving from 6 to 7 is about
  **six seconds of fifty-two, not thirteen of fifty-three**. §9 is corrected.
- **`docs/CACHING.md`'s 81.2 s does not reproduce.** That is the same
  configuration as the last row above, which measures 53–59 s here. The most
  likely explanation is contention: the encoder and OCR figures in this document
  swing by 50% between a quiet machine and a busy one, and 81 s is inside that
  swing. `CACHING.md` now carries the measured range and says what its number
  was.
- **Two independent readings of 59 s and 62 s** sit inside the `avif_speed = 6`
  band, or are a speed-7 build on a machine that was not idle. Nothing needs to
  be reconciled beyond saying which encoder setting a figure was taken at, which
  is what every build-time number in these documents now does.

**Writing the site costs nothing at this scale, and the search index costs less.**
Instrumented on a cold build: `site.build_site` — every HTML file, every JSON
payload, every asset, and the pagefind index inside it — is **0.57 s**, of which
`search.build_index` is **0.26 s**. Against a 46-second build that is a little
over 1%, and it is the same 1% §4 measured. `--no-search` is not a performance
option; it is a "the binary is not installed" option. (Both figures roughly
triple on a contended machine, which is the same swing everything else here
shows.)

**The phase shares have shifted with the encoder change.** Instrumented at
`avif_speed = 7`: `encode_page` 45%, OCR 37% over four pages, rasterise 13%,
redaction analysis 1.8%, everything else under 1%. At speed 6, §4's table, the
shares were 61% / 24% / 11%. Nothing moved except the cost of AVIF.

**Cached rebuilds**, same collection, cache warm:

| | wall |
|---|---:|
| nothing changed | 1.5–1.7 s |
| one 4-page born-digital document edited (12 of 16 pages from cache) | 12.7 s |
| one 4-page *scanned* document edited (12 of 16 from cache) | 92 s |

The last row is the useful one: the four pages that came back are the four that
need OCR, and OCR is 37% of a cold build over a quarter of its pages.

**The published site has grown.** 204 files, 10,714,008 bytes, against the
10,624,017 in §5.0 — the stylesheet, the scripts and a generated
`assets/i18n.js` account for the difference. `_pagefind/` is 175,616 bytes,
which is exactly the figure §5.0 records for the two prunes in §8.1, so those
are still doing their job.

---

## 1. How to reproduce this

### The machine

| | |
|---|---|
| CPU | 2 cores (so `pipeline._default_workers()` returns **1**: see §4.5) |
| RAM | 8 GB |
| Python | 3.11, Pillow 12.2.0 with AVIF, poppler-utils, Tesseract |
| Browser | Chromium 141.0.7390.37 via Playwright |
| Fallback fonts installed | DejaVu, Liberation, Noto, FreeFont (this matters — §5.2) |

### The collection

`demo/release`: three PDFs, 16 pages, 3.5 MB of originals. One is a real
scan (`scanned-annexes.pdf`, 3.7 MB); the other two are born-digital. Four of
the sixteen pages need OCR.

### Serving it

`python3 -m http.server` does not compress anything, and on a 3G profile that
makes HTML and CSS three to four times heavier than any real host would send.
Measurements below were therefore taken against a small server that behaves
like a real one: correct MIME types, gzip level 6 for HTML/CSS/JS/JSON/SVG,
no gzip for `.woff2`, `.avif`, `.webp` or the pagefind `.pf_*` and `.pagefind`
files (which are gzip streams already — recompressing a 207-byte `.pf_meta`
grows it). Both the raw and the on-the-wire figures are given below, so the
tables can be checked either way.

### Network profiles

Chrome DevTools' **Fast 3G**, applied through CDP
`Network.emulateNetworkConditions`:

```
downloadThroughput 188743.68 B/s   uploadThroughput 86400 B/s   latency 562.5 ms
```

and **desktop**, meaning no throttling at all (`-1`, `-1`, `0`). Network only:
the CPU is not throttled, so the CPU-side numbers below are optimistic for a
phone and the byte-side numbers are exact.

### One trap, stated plainly

> **Emulated offline does not reach a service worker.**
> `page.set_offline(True)` and `Network.emulateNetworkConditions {offline:
> true}` apply to the page's network stack, not to fetches a service worker
> makes on its own behalf. Measured here: with emulated offline, a page that
> had *never been visited* still loaded, because the worker fetched it happily
> over a connection the page could not see. Every offline claim in this
> document was verified by **stopping the web server process** and confirming
> that a control `fetch()` throws. `tests/test_offline.py` does the same thing
> for the same reason.

### Two source snapshots

Other work landed in this tree while these measurements were being taken, so
two snapshots are named where it matters:

- **Snapshot A** — the state at the start of this work. `assets/stackroom.css`
  57,594 B, no files in `assets/js/`.
- **Snapshot B** — `sha256` of all `.py`/`.js`/`.css`/`.jinja` under
  `src/stackroom`: `13670c0b7114cd73ee75c054c55639d1ffc96b094d837dd5c46eabbfe5119c0b`,
  2026-08-31T19:23Z. The published `assets/stackroom.css` was 108,630 B and
  there were seven files in `assets/js/`.
- **2026-09-01** — the tree §0 was measured against. 56 source files, published
  `assets/stackroom.css` 131,471 B, eight files in `assets/js/` and a generated
  `assets/i18n.js`. Recipe for the digest, since the one above does not state
  its ordering:

  ```sh
  find src/stackroom -type f \( -name '*.py' -o -name '*.js' -o -name '*.css' \
    -o -name '*.jinja' \) -print0 | sort -z | xargs -0 sha256sum | sha256sum
  ```

Build-side numbers (§4) are Snapshot A. Browser numbers are labelled. Both
`raster.py` and `pipeline.py` have moved since — `pdftoppm` is now invoked with
`-cropbox` and the pixel budget is enforced on the build's own path — which is
part of why §0 re-took the build times rather than trusting §4's.

---

## 2. Where the bytes go

### 2.1 By page type (Snapshot A, 1280×900, on the wire)

| page | transfer | decoded | requests | biggest single thing |
|---|---:|---:|---:|---|
| front | 152,435 | 196,118 | 9 | fonts, 104,888 (69%) |
| browse | 119,178 | 161,322 | 8 | fonts, 72,480 (61%) |
| document | 121,181 | 163,375 | 12 | thumbnails, 60,276 (50%) |
| page view | 221,619 | 269,627 | 7 | the scan, 126,211 (57%) |
| search | 77,726 | 156,747 | 7 | fonts 42,024, pagefind.js 12,859 |

The headline is not the images. **On every page that is not a page view,
self-hosted fonts are the largest single category** — more than the HTML, the
CSS and the JavaScript put together. Four files, 103,688 bytes, and they are
not preloaded, so on a cold connection they are not even *discovered* until
the stylesheet has arrived and parsed.

### 2.2 The font shipment

24 `.woff2` files are published, 375,476 bytes. Measured by loading every
standing page and every page type in Chromium and recording which files were
actually requested:

| page | subsets fetched |
|---|---|
| front | mono-core, sans-core, serif-400-core, serif-600-core |
| browse, withheld, page view | mono-core, sans-core, serif-400-core |
| document, search | mono-core, sans-core |
| about | mono-core, sans-core, serif-400-core, serif-600-core |

**Four files of twenty-four, 103,688 bytes of 375,476.** The other 271,788
bytes — every Cyrillic, Greek, Latin-Extended and italic subset — are never
requested by an English collection. `unicode-range` is doing its job perfectly;
the cost is only borne by whoever mirrors or clones the folder. This is also
the fact that decides what the service worker precaches (§6.2).

### 2.3 Assets on disk

Two columns, because this is the table that has aged most: Snapshot B is what
the byte figures elsewhere in this document were taken against, and 2026-09-01
is what a build produces today.

| file | Snapshot B raw | gzip | 2026-09-01 raw | gzip |
|---|---:|---:|---:|---:|
| `assets/stackroom.css` | 108,630 | 30,005 | 131,471 | 36,444 |
| `assets/js/*.js` | 118,748 (7 files) | 40,469 | 140,133 (8 files) | 48,276 |
| `assets/viewer.js` | 12,959 | 4,762 | 14,494 | 5,337 |
| `assets/search.js` | 9,268 | 3,269 | 9,161 | 3,292 |
| `assets/i18n.js` | — | — | 12,478 | 4,174 |
| `sw.js` | 19,593 | 7,178 | 19,694 | 7,194 |
| `offline.json` | 9,904 | 1,781 | 9,884 | 1,775 |
| a page-view HTML | 4,867 | 1,724 | 6,036 | 2,146 |

| category | Snapshot B files | bytes | 2026-09-01 files | bytes |
|---|---:|---:|---:|---:|
| `media/` | 96 | 5,577,078 | 96 | 5,588,421 |
| `files/` (originals) | 3 | 3,721,248 | 3 | 3,721,248 |
| `_pagefind/` | 23 | 460,958 | 20 | 175,616 |
| `assets/fonts/` | 24 | 375,476 | 24 | 375,476 |
| `data/<doc>/<n>.json` | 16 | 74,629 | 16 | 74,629 |
| whole site | — | 10,892,124 | 204 | 10,714,008 |

`_pagefind/` is the §8.1 prunes, applied. `assets/i18n.js` is new: it is the
interface's own strings for the scripts that write them, generated per build
from the catalogue named by `language`.

### 2.4 Two things shipped that nothing loads — since fixed

**Both prunes in §8.1 are applied.** A build today writes a 175,616-byte
`_pagefind/` of 20 files, with no `pagefind-component-ui` and no
`wasm.unknown.pagefind` in it. What follows is the finding as written, because
the way pagefind renames things means somebody will have to check it again.


**`_pagefind/pagefind-component-ui.{js,css}` — 217,318 bytes, 47% of the search
bundle.** `build/site.py` prunes the drop-in search UI with the prefix list
`("pagefind-ui", "pagefind-modular-ui", "pagefind-highlight")`. Pagefind 1.5.2
names that bundle `pagefind-component-ui`, which none of those three prefixes
match. The pruning code runs, reports success, and removes nothing. Verified:
nothing outside `_pagefind/` references the string `component-ui`. Diff in §8.1.

**`_pagefind/wasm.unknown.pagefind` — 68,024 bytes.** The language-less
fallback stemmer. `pagefind-entry.json` for this build reads
`{"en":{"hash":"en_a3d6b1842d","wasm":"en",...}}`, so the client loads
`wasm.en.pagefind` and never this one. It cannot be deleted unconditionally —
a language with no stemmer needs it — but it can be deleted when every language
in the entry names a specific wasm. Diff in §8.1.

Together: **285,342 bytes, 62% of `_pagefind/`**, carried by every mirror.

### 2.5 The word boxes are stored twice

`docs/ARCHITECTURE.md` used to describe `data/<doc>/<n>.json` as "word boxes for
the page viewer", and `build/site.py`'s docstring used to say they are "only
fetched when a reader actually opens the page".

Neither was true, and both have since been corrected (§8.4). `viewer.js` reads
the boxes from `<script type="application/json" id="page-data">`, **inlined in
the page HTML**. Confirmed by recording every request on every page type: no
`data/<doc>/<n>.json` is ever requested. Re-checked on 2026-09-01: still true,
and the set of things the published scripts do fetch is `data/docs.json` for
`search.js` and `palette.js`, and `manifest.json` for `cite.js` and
`palette.js` — all four through the one shared `sr.json()` helper in
`prefs.js`, which caches per path. `docs/ARCHITECTURE.md` now says what those
files are for rather than what they were assumed to be for.

The two copies are byte-for-byte the same data:

| | raw | gzipped |
|---|---:|---:|
| inline in the 16 page HTMLs | 74,629 | 22,962 |
| `data/<doc>/<n>.json`, 16 files | 74,629 | — |

4,664 bytes per page of pure duplication, which at the 20,000-page supported
ceiling is **93 MB** in a published archive that no reader ever downloads. It is
a documented part of the output layout, so this is reported rather than
changed; §8.4 has the options.

---

## 3. Core Web Vitals

Median of three cold loads per cell. LCP, FCP and TBT in milliseconds.

### 3.1 Snapshot B, first visit, no service worker

| page | profile | LCP | FCP | CLS | TBT | transfer |
|---|---|---:|---:|---:|---:|---:|
| front | Fast 3G | 1400 | 1400 | **0.4631** | 0 | 202,058 |
| document | Fast 3G | 1464 | 1464 | **0.1663** | 0 | 170,806 |
| page view | Fast 3G | 4184 | 2344 | 0.0044 | 0 | 272,288 |
| search | Fast 3G | 1508 | 1508 | 0.0352 | 0 | 127,350 |
| front | desktop | 160 | 160 | 0.4727 | 4 | 202,058 |
| document | desktop | 156 | 156 | 0.1649 | 0 | 170,806 |
| page view | desktop | 224 | 180 | 0.0040 | 0 | 272,288 |
| search | desktop | 132 | 132 | 0.0494 | 0 | 127,350 |

**TBT is zero.** There is no JavaScript problem here at any size measured; the
scripts are small, deferred and do almost nothing on load. INP was not measured
directly because there is no meaningful interaction on these pages other than
search, which is timed separately in §3.3.

**LCP is fine everywhere except the page view**, where it is the scan — which is
the correct LCP element and is 126 KB of document. 4.2 s on Fast 3G.

**CLS is the problem, and it is entirely the font swap.** §5.2.

### 3.2 Time to a picture of the page

On a page view over Fast 3G, measured by screenshotting the scan frame and
watching its pixel variance:

| | frame has picture detail | scan image arrives |
|---|---:|---:|
| as built | never (blank until the image lands) | 2,950 ms |
| with an inline LQIP | **622 ms** | 2,940 ms |

2.3 seconds of a framed white rectangle, for 140 gzipped bytes. §5.1.

### 3.3 Search

Measured in the browser. "First query" is the honest cold-start number: it is
the click that pays for `pagefind-worker.js`, `wasm.en.pagefind`, the `.pf_meta`
and the `.pf_index`.

| | desktop | Fast 3G |
|---|---:|---:|
| bytes on the search page's own load | 75,626 (12,859 of it `pagefind.js`) | same |
| **first query** (`contract`, 14 hits) | 358 | **4,217** |
| second query (`authority`, 13 hits) | 28 | 11 |
| `deliberative`, 1 hit | 27 | 2 |
| `zzzznotfound`, 0 hits | 9 | 18 |

Cold-start bytes measured from the built bundle: `pagefind.js` 12,859 gzipped +
`pagefind-worker.js` 11,912 gzipped + `wasm.en.pagefind` 72,209 + `.pf_meta` 207
+ `pagefind-entry.json` 172 = **97,359 bytes**, against the 97,151 that
`build/search.py` documents. The module's arithmetic is correct.

One correction to `assets/search.js`'s own docstring: it says the index "is
loaded the first time someone puts the cursor in the box", and "a reader who
came to browse never pays for it". True of the worker and the wasm, but the
last line of that file is `input.focus({preventScroll: true})`, which fires the
`focus` handler and imports `pagefind.js` on every load of the search page. So
12,859 bytes of it are paid unconditionally. That is a small number and
arguably the right trade; the comment is what is wrong.

---

## 4. Where the build time goes

**`avif_speed = 6`, which was the default when this was measured.** Seven is the
default now, and §0 has the current figures; the shares below are the shape of
the build rather than today's absolute numbers.

`workers=1`, explicit timers around each phase, whole-collection wall clock
52.66 s for 16 pages (3.3 s/page). That figure was re-taken on 2026-09-01 at
speed 6 and reproduces: 52.3 s at best over five runs. The `~90 s` in the task
description is not reproducible here; a clean run of `stackroom build ./release
-o site` reports "read in 50s".

| phase | seconds | calls | per call | share |
|---|---:|---:|---:|---:|
| **`encode_page` total** | **32.15** | 16 | 2009 ms | **61.0%** |
|  ├ AVIF @1275px | 15.88 | 16 | 993 ms | 30.2% |
|  ├ AVIF @900px | 6.86 | 16 | 429 ms | 13.0% |
|  ├ WebP @1275px | 4.34 | 16 | 272 ms | 8.3% |
|  ├ WebP @900px | 2.35 | 16 | 147 ms | 4.5% |
|  ├ AVIF @240px (thumb) | 0.80 | 16 | 50 ms | 1.5% |
|  ├ WebP @240px (thumb) | 0.24 | 16 | 15 ms | 0.5% |
|  ├ denoise (median 3×3) | 0.67 | **1** | 671 ms | 1.3% |
|  ├ resize (Lanczos) | 0.57 | 48 | 12 ms | 1.1% |
|  ├ grain_level | 0.23 | 16 | 14 ms | 0.4% |
|  └ colourfulness | 0.16 | 16 | 10 ms | 0.3% |
| OCR (Tesseract) | 12.76 | **4** | 3190 ms | 24.2% |
| rasterise (`pdftoppm` + PNG decode) | 5.95 | 16 | 372 ms | 11.3% |
| redaction analysis | 1.15 | 32 | 36 ms | 2.2% |
| `pdf.read_page` (text layer) | 0.43 | 16 | 27 ms | 0.8% |
| `quality.score_page` | 0.21 | 16 | 13 ms | 0.4% |
| **render (all HTML + JSON)** | **0.26** | 1 | 260 ms | 0.5% |
| **index (pagefind)** | **0.17** | 1 | 172 ms | 0.3% |
| `pdf.open_pdf` | 0.06 | 19 | 3 ms | 0.1% |
| `pdfinfo` (uncached) | 0.04 | 16 | 2 ms | 0.1% |

### 4.1 The earlier claim, checked

> *"AVIF encoding was ~1.6 s per page against ~0.44 s to rasterise."*

**Confirmed, near enough.** Measured: AVIF across all three widths is
23.54 s / 16 = **1.47 s per page**; rasterising is 5.95 / 16 = **0.37 s per
page**. So AVIF costs 4.0× what rasterising costs, and it is 45% of the whole
build on its own.

Rendering HTML and building the search index together are **0.43 s of a 52.7 s
build — 0.8%.** There is no point optimising either. This is an image-encoding
program that also emits HTML.

### 4.2 `avif_speed`: 6 is not the knee, 7 is

`RenderSpec.avif_speed` documents "speed 4 = 97 KB in 4.7 s, speed 6 = 105 KB in
1.0 s, speed 8 = 108 KB in 0.4 s, speed 9 = 127 KB in 0.12 s. Six is the knee."
Speed 7 was never tested. On a real 1275×1650 grayscale page:

| speed | born-digital | | scan p1 | | scan p3 | |
|---|---:|---:|---:|---:|---:|---:|
| | ms | bytes | ms | bytes | ms | bytes |
| 4 | 5189 | 41,130 | 7193 | 49,676 | 7597 | 49,580 |
| **6** | 831 | 53,255 | 1258 | 65,298 | 1082 | 65,172 |
| **7** | **553** | 53,879 | **711** | **62,829** | **647** | **63,109** |
| 8 | 263 | 90,931 | 343 | 79,736 | 369 | 80,741 |
| 9 | 72 | 110,670 | 75 | 92,738 | 78 | 93,674 |

On both scans speed 7 is **strictly better than 6**: faster *and* smaller. On
the born-digital page it is 33% faster and 1.2% larger.

Across the whole demo, encoding all 48 AVIF variants, runs interleaved so a
busy machine costs both alike:

| avif_speed | encode seconds | total AVIF bytes |
|---|---:|---:|
| 6 | 32.24 | 1,890,446 |
| **7** | **19.02** (−41.0%) | **1,901,789** (+0.6%) |
| 8 | 12.28 (−61.9%) | 2,330,244 (+23.3%) |

**Speed 7 saves 13.2 s of the 32.2 s spent encoding, for 11,343 bytes across 48
files.** It does *not* take 13.2 s off the wall clock of a whole build: measured
end to end (§0) the saving is about six seconds of fifty-two, because a build
also rasterises and recognises, and those do not get faster. Speed 8 is firmly on the wrong side of the knee — and note that the
docstring's 105 KB → 108 KB for the 6 → 8 step does not reproduce here at all:
these pages are grayscale, encoded `4:0:0`, and the jump is 53 KB → 91 KB.
Both encoders are byte-for-byte deterministic over four repeats at both speeds,
so guarantee 6 survives. **Recommended: `avif_speed = 7`.** Diff in §8.2.

### 4.3 `webp_method`: 6 is defensible, barely

Same page, WebP at quality 78:

| method | ms | bytes | vs method 6 |
|---|---:|---:|---|
| 0 | 41 | 174,660 | +26.2% bytes, 19% of the time |
| 2 | 59 | 142,370 | +2.9% bytes, 28% of the time |
| 4 | 121 | 139,830 | +1.1% bytes, 57% of the time |
| **6** | 212 | 138,372 | — |

Method 4 would save about 2.9 s of a 52.7 s build (5.5%) and cost 1.1% more
WebP. Since AVIF is what modern browsers actually fetch (verified: Chromium
takes `p0002@900.avif`), that 1.1% falls only on browsers without AVIF.
**Not recommended either way** — this is inside the noise of the decision, and
the existing value errs towards the reader's bytes, which is the right
direction for an archive. Reported so nobody has to measure it again.

### 4.4 The "1600 px" variant is 1275 px

`RenderConfig.widths` defaults to `(1600, 900)`, but the pipeline rasterises
with `raster.render_page_crop(..., dpi=job.dpi)` at a fixed 150 dpi, which for
a US Letter page is 1275 px. `_resize` refuses to upscale, so the file called
`p0002@1600.avif` is 1275 px wide. `_effective_dpi()` — which exists precisely
to raise the dpi until the raster covers the widest requested variant — is only
reachable from `render_pdf()`, which the pipeline does not call.

Nothing is *lied about*: `srcset` carries the real `1275w`, so the browser
picks correctly. But `widths` reads as a promise the pipeline does not keep.
See §8.3.

### 4.5 The process pool is not used on this machine

```python
def _default_workers() -> int:
    cpus = os.cpu_count() or 2
    return max(1, min(8, cpus - 1)) if cpus > 2 else 1
```

On 2 cores this returns 1, so `build_collection` takes the serial branch and
`ProcessPoolExecutor` never runs. That is a defensible choice on a 2-core
laptop. Two observations for whoever owns it:

- The pool is per **page**, and `process_page` opens the PDF (`pdf.open_pdf`)
  once per page — 19 opens for 16 pages, 3 ms each, 0.06 s total. Not worth
  hoisting to per-document: measured, it is 0.1% of the build.
- `pdftoppm` is invoked exactly **once per page** (16 calls, 372 ms each), plus
  `pdfinfo` 16 times at 2 ms (memoised by `_geometry_cached`, so the misses are
  only the first per file). The redaction check renders **no** extra crops: it
  is handed `_in_memory_cropper`. **`pdftoppm` is not being invoked more than
  necessary.** The one available saving is batching (`raster.render_pdf`'s
  `-f/-l` path, documented at 1.05×–1.33×), which the pipeline cannot use
  because its unit of work is a page. Not worth restructuring for 5%.

Nothing is recomputed per page that could be per document. `annotate_document`
already does the document-level passes once.

---

## 5. What was changed, and what it bought

| # | change | owner | effect | cost |
|---|---|---|---|---|
| 1 | **Offline service worker** (`build/offline.py`, `assets/sw.js`, `assets/js/offline.js`, `assets/parts/offline.css`) | mine, shipped | second visit over Fast 3G: front page LCP **1400 → 108 ms**, transfer **202,058 → 0 B**; page view LCP **4184 → 132 ms**; works with the server stopped | +6,363 B on first load; first-visit LCP unchanged within noise |
| 2 | **Precache only the fonts a page needs** | mine, shipped | 4 subsets, 103,688 B, instead of 24 subsets, 375,476 B | build-time `unicode-range` analysis, 9 ms |
| 3 | **Font fallback reorder** (`assets/fonts/fonts.css`) | §8.5, **applied** | front CLS **0.4631 → 0.0085**, document CLS **0.1658 → 0.0101**, page view 0.1236 → 0.1040 | two lines, zero bytes, zero requests |
| 4 | **LQIP on the page image** | §8.3, **applied** | picture of the page at **622 ms** instead of 2,950 ms | 140 gzipped B/page, 1.34 ms/page to generate |
| 5 | **`avif_speed = 7`** | §8.2, **applied** | encoding **−13.2 s of 32.2 s (−41%)**; end to end, re-measured, **−6 s of 52 s (−12%)** — see §0 | +11,343 B across 48 images (+0.6%) |
| 6 | **Prune `pagefind-component-ui`** | §8.1, **applied** | −217,318 B in every published archive and every clone | one string in a tuple |
| 7 | **Prune `wasm.unknown.pagefind` when unused** | §8.1, **applied** | −68,024 B | four lines, conditional |

### 5.0 Everything applied, measured

Every diff in §8 was applied to a copy of the package and the demo rebuilt from
it, so the "after" column below is a real build and not a hand-edited site. The
resulting archive is **10,624,017 bytes against 10,892,124** — 268,107 bytes
smaller despite gaining a service worker, an inventory and sixteen inline
placeholders — and `tests/test_offline.py` (33 tests), `tests/test_site.py` and
`tests/test_raster.py` all pass against it.

All of it has since been applied to the package itself; the copy is only how it
was measured. One number here has not aged well: the front page's **0.0639** is
a three-run median that happened to catch the masthead-nav wrap described at
the end of §5.2. Re-measured over seven runs it is **0.0085**, with 0.0639 as
the worst run rather than the typical one.

| page | | LCP | CLS | transfer |
|---|---|---:|---:|---:|
| front | before | 1400 | 0.4631 | 202,058 |
| | after, first visit | 1432 | **0.0639** | 208,957 |
| | **after, second visit** | **64** | **0.0245** | **0** |
| document | before | 1464 | 0.1663 | 170,806 |
| | after, first visit | 1420 | **0.0102** | 177,705 |
| | **after, second visit** | **104** | 0.0407 | **7,234** |
| page view | before | 4184 | 0.0044 | 272,288 |
| | after, first visit | 4108 | 0.0024 | 283,384 |
| | **after, second visit** | **184** | 0.0050 | **5,382** |
| search | before | 1508 | 0.0352 | 127,350 |
| | after, first visit | 1452 | 0.0308 | 120,656 |
| | **after, second visit** | **80** | 0.0018 | **0** |

Fast 3G, median of three cold loads in a fresh browser context. "Second visit"
means the front page was opened once beforehand over an unthrottled connection
— i.e. what a reader who has been here before gets.

Where the byte difference went, on disk:

| | before | after |
|---|---:|---:|
| `_pagefind/` | 460,958 | **175,616** (−62%) |
| all AVIF | 1,890,446 | 1,901,789 (+0.6%) |
| whole site | 10,892,124 | **10,624,017** |

### 5.1 LQIP: what it does and, more usefully, what it does not

A 24 px-wide grayscale WebP of the page, base64'd into the figure's
`background-image`. Sizes, averaged over six real pages:

| variant | raw | as a `data:` URI |
|---|---:|---:|
| 16 px WebP q40 | 71 | 120 |
| **24 px WebP q40** | **96** | **152** |
| 32 px WebP q40 | 112 | 174 |
| 16 px JPEG q40 | 196 | 286 |

24 px WebP: **147 B raw per page, 140 B after gzip**, 1.34 ms/page to generate
from the thumbnail the encoder already has in memory, byte-identical across
repeats. On the whole demo: 21 ms of build time, 2,352 raw bytes.

**It does nothing for CLS, and the task's hypothesis that it would is wrong.**
Page-view CLS is 0.0044 before and 0.0024 after — noise. The `aspect-ratio`
already on `.scan__figure` reserves the exact box before the image arrives, so
there is no shift left to remove. What LQIP buys is entirely perceptual, and
that part is real: 2.3 seconds of *something* instead of 2.3 seconds of a white
rectangle with a border.

**Do not implement it with `opacity: 0` on the `<img>` plus a `load` handler.**
That is the usual blur-up recipe and it breaks two of this project's rules at
once: with JavaScript disabled the scan never becomes visible at all, and an
element at `opacity: 0` is not an LCP candidate, so the metric would improve by
lying. The version measured here is CSS-only: background behind, `<img>` on
top, nothing hidden, no script. Verified with `java_script_enabled=False` — the
scan renders identically. LCP is unchanged (3004 → 3008 ms), which is the
correct outcome: the real image is still the largest paint.

### 5.2 CLS is the font swap, and the fix is free

Isolated by blocking `.woff2` requests entirely:

| | front CLS | document CLS |
|---|---:|---:|
| as built, Fast 3G | 0.0925 | 0.1638 |
| **fonts blocked** | **0** | **0** |
| fonts preloaded, desktop | 0 | 0 |
| fonts preloaded, Fast 3G | 0.0925 | 0.1638 |

*(Snapshot A. Snapshot B's front page shifts more — 0.4631 — because there is
more above the fold to move.)*

Blocking the fonts removes every shift. So it is the swap, and specifically the
**advance widths**: `fonts.css` sets `ascent-override`, `descent-override` and
`line-gap-override`, which make the fallback's *line box* identical, but do
nothing about how wide the glyphs are. Paragraphs re-wrap, blocks move.

Measured in the browser with `canvas.measureText` on a representative string:

| | web font | fallback | ratio |
|---|---:|---:|---|
| Sans | 546 | 661 | 0.826 |
| Serif | 582 | 667 | 0.873 |
| Mono | 810 | 810 | 1.000 |

A 21% width mismatch on the sans face. The reason was in the fallback stack,
which used to read:

```css
src: local("Segoe UI"), local("Roboto"), local("Helvetica Neue"),
     local("Arial"), local("DejaVu Sans"), local("Liberation Sans");
```

On Linux, `DejaVu Sans` was listed **before** `Liberation Sans`. DejaVu is a
notably wide face; Liberation Sans is metric-compatible with Arial, which is
what the stack is reaching for two entries earlier. So on every Linux reader —
and in every headless-Chromium CI run and Lighthouse score — the stack picked
the worst available option. Swapping those two (§8.5, since applied):

| | ratio | front CLS | document CLS | page-view CLS |
|---|---:|---:|---:|---:|
| as built (DejaVu first) | 0.826 | 0.4631 | 0.1658 | 0.1236 |
| **Liberation before DejaVu** | **0.943** | **0.0085** | **0.0101** | **0.1040** |

**A 98% reduction on the front page and 94% on a document page, for a two-line
change that adds no bytes and no requests and changes nothing on macOS or
Windows** (where `Segoe UI` or `Helvetica Neue` resolves first and neither
entry is ever reached).

Re-measured on the current tree, because the pair this table used to carry —
0.0925 → 0.0075 — was a Snapshot A number for a front page that has since
grown: the "as built" column is now the same 0.4631 the §3.1 table reports.
Method: the demo built once, then a byte-identical copy of the output with the
two `local()` entries swapped back inside `assets/stackroom.css`, so nothing
but the font order differs; both served with gzip; five cold Fast-3G loads per
cell in a fresh browser context with service workers blocked; CLS is the sum of
every `layout-shift` entry with `hadRecentInput` false; median reported. The
advance-width ratios above reproduce exactly.

**The page view barely moves, and that is the informative part.** What shifts
on a page view is not the type: it is the transcription being pushed by the
scan arriving beside it, at ~3.5 s on this profile, and the reorder cannot
touch that. 0.1236 → 0.1040 is the whole effect. The figure is for a
representative page view (`d/correspondence-march-2019/p/2/`); across all
sixteen page views in the demo the medians are 0.1205 → 0.0995, with the four
pages that carry no transcription at all sitting near zero in both columns
(0.0039 → 0.0021) — which is the same fact seen from the other side.

One honest caveat about the front page's *remaining* 0.0085. It is bimodal: in
seven runs, five landed on 0.0085 and one on 0.0639, the difference being
whether the masthead nav wraps to a second row as prefs.js and palette.js
insert their two buttons into it, which moves `<main>` down. A three-run median
can land on either, which is where the 0.0639 quoted in §5 and §5.0 comes from.
It is a separate, much smaller shift with a separate cause, and it is not the
font swap.

The comment in `fonts.css` explaining why `size-adjust` is *not* set remains
correct and should stay: a single value tuned to one platform's fallback would
mis-size the majority of readers. Reordering the stack is the version of that
fix that carries no platform assumption at all.

The service worker attacks the same shift from the other end. Once the fonts
are in the cache they arrive before first paint, and front-page CLS on a second
visit is 0.117 rather than 0.4631 — a 75% reduction from caching alone, and
0.0149 with the reorder as well.

---

## 6. Offline

### 6.1 What was built

| file | what it is |
|---|---|
| `src/stackroom/build/offline.py` | generates the worker and its inventory at build time |
| `src/stackroom/assets/sw.js` | the worker template; valid JavaScript before substitution as well as after |
| `src/stackroom/assets/js/offline.js` | registration and the indicator |
| `src/stackroom/assets/parts/offline.css` | the indicator's styles |
| `tests/test_offline.py` | 25 tests without a browser, 8 with one |

Two files land in the published site: **`sw.js`** at the root (a worker under
`assets/` could only ever control `assets/`, and a static host cannot send the
`Service-Worker-Allowed` header that would widen its scope) and
**`offline.json`**, the full inventory, fetched only when a reader asks to store
everything.

### 6.2 Three tiers

**Precache, on the first visit, unasked** — the standing pages, the stylesheet,
the scripts, the four font subsets those pages actually use, the favicon and
`data/docs.json`. The standing pages are *discovered* (every `index.html`
outside `d/`, at most three levels deep) rather than listed, so a section added
later — `withheld/negative/`, say — is stored offline without anyone having to
remember to update a tuple; a hard-coded list stops being the truth the first
time somebody adds a page, and the failure is silent. 21 files, 387,280 bytes
raw (about 145 KB on the wire, since the CSS gzips to 30 KB and the fonts are
already compressed). **Re-measured 2026-09-01: 24 files, 482,771 bytes raw**,
the growth being the stylesheet, an eighth script and one more standing page.

One gap, found while re-measuring and not fixed here: the asset list in
`offline.py` is a hard-coded tuple for everything that is not a page, a script
under `assets/js/` or a font — and **`assets/i18n.js` is not in it**. That file
is loaded synchronously from the head of every page and carries the interface's
own strings for the scripts. It is picked up by the runtime tier the first time
a reader loads any page online, so in practice it is there; but a shell that
promises to be enough to open the archive with no connection is missing the file
that decides what language the archive's controls speak. It degrades safely —
`prefs.js` reads `window.stackroomMessages || {}` and falls back to English —
which is why it is a gap rather than a break. Chosen by parsing
`@font-face` blocks out of the built stylesheet and matching each
`unicode-range` against the codepoints in the precached HTML — the same decision
a browser makes, made at build time. Whitespace and control characters are
excluded, or every subset would match every page; italic faces are stored only
if the HTML contains `<em>`, `<cite>`, `<var>`, `<dfn>` or `<address>`, which is
worth 28,768 bytes on this collection. Result on the demo: exactly the four
files a browser fetches, and no others.

**Runtime, as the reader goes** — page HTML, thumbnails, page images, word
boxes, the search index. Cache-first, because the archive is immutable for the
life of a build and the cache name says which build.

**Everything, on an explicit action** — a button reading `Store all of it
(10 MB)`. The size is a build-time constant in the worker, so no reader
downloads a 120,000-entry file list to find out how big the archive is.
Originals (`files/`) are excluded from both automatic tiers and included here:
3.7 MB of the demo's 10 MB, and on a real collection the great majority.

### 6.3 Cache versioning

The cache name is `stackroom-{shell,runtime}-<16 hex>` where the digest covers,
in order:

1. `BuildInfo.source_digest` — the documents themselves;
2. the generator version and `tool_versions` — the same PDFs through a newer
   encoder are different bytes;
3. the **content** of every precached file — a stylesheet edit changes nothing
   else on the manifest;
4. the path and size of every published file — a page added, removed or
   re-encoded.

Deterministic: two full builds of the same input produced byte-identical
`sw.js` and `offline.json`. (The only file that differs between two builds of
this demo is `manifest.json`, which carries `built_at`. That is pre-existing
and not introduced here, but it is a real qualification on guarantee 6.)

On activation every `stackroom-*` cache that is not this build's is deleted.
The new worker **does not** call `skipWaiting()` during install: it waits, so a
page already open keeps the build it started with rather than being served
new CSS for old HTML. When a waiting worker is detected the indicator says
"A newer version of this archive has been published" and offers a button.

### 6.4 The indicator

One line in the colophon, injected by `offline.js` — no template change needed —
next to the build stamp and the source digest, because it belongs to the same
idea: here is exactly what you have.

```
This archive is available offline. 24 of 202 files are stored (12%) — the pages
you have opened, and enough of the site to open it with no connection.
                                       [Store all of it (10 MB)]  [Remove it]
```

(The inventory counts 202 rather than the 204 files on disk: `sw.js` and
`offline.json` are not in their own inventory.)

While storing: `Storing the archive… 42%, 4.1 MB so far.` with a progress bar
and a Stop button. When complete: `The whole archive is stored on this device —
10 MB, all 202 files.`

### 6.5 Every failure mode, and what the reader is told

Verified in Chromium; `tests/test_offline.py` covers 1, 4, 5, 6 and 7.

| # | situation | what happens |
|---|---|---|
| 1 | `file://` | *"You are reading this from a folder on this device, so the whole archive is already offline. (Browsers do not run service workers from file:// addresses…)"* — and the archive renders normally |
| 2 | no service-worker support | *"This browser cannot store the archive for offline reading. Everything here still works…"* |
| 3 | registration refused (private window, policy) | *"This archive could not be stored for offline reading in this browser…"* |
| 4 | quota exceeded | **warned before the click**: *"There may not be room for all of it here: this browser is offering about 928 KB."* Then: *"This device ran out of room. 53 of 180 files were stored before it filled up; what is here still reads offline."* Site still works afterwards — verified, 310 words on a page view |
| 5 | reader wants it gone | `?stackroom-offline=off` on any page: registration and every `stackroom-*` cache deleted, a flag remembered so it stays off on later visits, and a "Turn it back on" button |
| 6 | operator shipped a broken worker | publish a file called `sw-kill` beside `sw.js`. Every worker checks for it on install, on activation, and once per start-up when a page asks for its storage figures; on finding it, it deletes its caches, tells every open page to do the same, stops answering anything, and unregisters. Verified: caches 0, worker inert, site renders |
| 7 | JavaScript disabled | 310 words and the scan render; the indicator does not exist |

Two notes on 6. It costs **one 404 per worker start-up**, not per request —
Chrome spins an idle worker down after about thirty seconds, so a kill lands
within a page or two of the file appearing, with no new build. And a killed
worker becomes *inert* rather than merely unregistered, because the page will
register again on the next load and the replacement has to do nothing too.

### 6.6 Security properties

- **Cross-origin: never.** A request whose origin or path prefix is not this
  archive's is not inspected, not cached and not answered. There is nothing
  cross-origin in a Stackroom site, and the worker enforces that rather than
  assuming it.
- **Range requests: never answered.** `caches.match` ignores the `Range` header
  and hands back the whole body with a 200, which is a lie to anything paging
  through a PDF. A request carrying `Range` goes straight to the network.
- **Fail open.** Precaching uses `allSettled`, so one missing file does not
  kill the install. Every path in the fetch handler ends in a plain `fetch()`.
  Non-GET, `only-if-cached`, and out-of-scope requests are not touched. Only
  `response.ok && response.type === 'basic'` is ever stored, so an opaque or
  error response can never be parked in front of a real file.
- **Content Security Policy: unchanged.** The existing meta policy already
  allows this — `worker-src 'self' blob:` covers registering `sw.js`,
  `connect-src 'self'` covers its fetches, and `img-src 'self' data:` covers
  the LQIP in §5.1. No policy change is needed for any of this.
- **Relative throughout.** Every URL in the worker resolves against its own
  location, so the archive still works from a subdirectory. Asserted by a test.

### 6.7 What it is worth

Server **stopped**, not emulated:

| | result |
|---|---|
| control `fetch()` with the server down | throws (the test is real) |
| front page | renders, 3 documents, real fonts, real stylesheet |
| a page visited earlier | 310 words, scan loads |
| a page never visited, nothing stored | HTTP 503 and *"This page is not stored on this device."* with a link home — not the browser's error screen |
| after `Store all of it` (2.0 s over loopback, every file in the inventory) | any page renders, every image loads |
| search, server down | 112 ms, correct hits |
| the original PDF, server down | served from cache |

And on a live but slow connection, the search cold start:

| | first query on Fast 3G |
|---|---:|
| no service worker | 4,217 ms |
| service worker warm (front page visited once) | **733 ms** |

The worker does not precache the pagefind runtime, and this is why it does not
need to: simply having the shell out of the way clears the pipe for pagefind's
84 KB and gets 5.8× of the win. Precaching it would spend 97 KB of a reader's
connection on a feature they may never use. Once search *has* been used, its
files are runtime-cached and later queries are 11–36 ms and work offline.

---

## 7. Tried and rejected, with the numbers

### 7.1 Preloading the fonts — rejected

`<link rel="preload" as="font">` for the four core subsets:

| | front FCP | front CLS | document CLS |
|---|---:|---:|---:|
| as built, Fast 3G | 1312 | 0.0925 | 0.1638 |
| **preloaded, Fast 3G** | **1636** (+25%) | 0.0925 | 0.1638 |
| as built, desktop | 156 | 0.1158 | 0.1638 |
| preloaded, desktop | 124 | **0** | **0** |

On desktop it eliminates CLS, because the fonts win the race to first paint. On
Fast 3G it does not reduce the shift **at all** — it only moves it earlier
(2522 ms → 1804 ms) — and it costs **324 ms of FCP**, because four preloads
compete with the stylesheet for a 1.6 Mbit pipe. Trading a quarter of a second
of first paint for a shift that still happens is a bad trade on exactly the
connection that can least afford it. The fallback reorder in §5.2 gets the same
CLS improvement on both profiles for zero bytes.

### 7.2 `content-visibility: auto` on the thumbnail grid — rejected below ~1,000 tiles

The demo's largest document is 8 pages, so 300- and 2,000-tile grids were
synthesised from the real markup and the real thumbnails.

**300 tiles:**

| | FCP | LCP | TBT | forced relayout | reflow (root font-size change) |
|---|---:|---:|---:|---:|---:|
| as built | 158 | 158 | 0 | 6.60 ms | 33.3 ms |
| `content-visibility` | 138 | 138 | 0 | 3.75 ms | 33.4 ms |

**2,000 tiles:**

| | FCP | LCP | TBT | forced relayout | reflow |
|---|---:|---:|---:|---:|---:|
| as built | 336 | 336 | 140 | 58.2 ms | 123.2 ms |
| `content-visibility` | **290** | 290 | **99.5** | 125.6 ms | **105.6 ms** |
| `content-visibility`, **no `contain-intrinsic-size`** | **616** | 616 | **438** | 111.8 ms | — |

Three findings.

At 300 tiles it buys 20 ms of FCP and nothing else — scrolling is vsync-bound
at 33 ms a frame either way, and a realistic reflow is unchanged. At 2,000
tiles it buys 46 ms of FCP, 40 ms of TBT and 18 ms of reflow: real, but small
against a page that is already fast, and the forced-relayout column gets
*worse* because containment adds per-element bookkeeping that a whole-document
layout pays for without benefit.

And the third row is the reason not to ship it casually: **without
`contain-intrinsic-size` it is a catastrophe** — FCP nearly doubles, TBT
triples, and only 525 of 2,000 tiles have a non-zero height, so the scrollbar
lies about the length of the document.

**Verdict: not worth it as an unconditional rule.** If it is ever added it must
carry `contain-intrinsic-size`, and it should be applied only above roughly a
thousand tiles, which the builder knows at render time.

### 7.3 Fetching the word boxes lazily instead of inlining them — rejected as posed

The premise does not hold: nothing fetches `data/<doc>/<n>.json` (§2.5). The
boxes are already inline, which is the *better* of the two options the task
asks between — 4,664 bytes per page, gzipping to 1,435, with no second round
trip and no dependence on the network for a highlight. Making the fetch lazy
would add a request to the one operation the viewer exists to do.

The real finding is the other way round: the separate JSON files are dead weight
(§8.4).

### 7.4 `webp_method = 4` — rejected

5.5% off the build for 1.1% more bytes, on the format that only non-AVIF
browsers download. §4.3.

### 7.5 `avif_speed = 8` — rejected

61.9% off encoding, but **+23.3% on every page image in the archive**. An
archive is downloaded many more times than it is built. §4.2.

### 7.6 Batching `pdftoppm` in the pipeline — rejected

`raster.render_pdf` already batches with `-f/-l` and documents the win as
1.05×–1.33×. The pipeline cannot use it: its unit of work is a page, so that
one process can be a worker. Rasterising is 11.3% of the build, so the ceiling
on this is about 3% of wall clock, for a restructuring of the pool. Measured
and left alone.

### 7.7 Hoisting `pdf.open_pdf` out of `process_page` — rejected

19 opens for 16 pages, 3 ms each, 0.057 s total: **0.1% of the build.**

### 7.8 A third image variant for phones — not recommended, but here is what a phone downloads

What each device actually fetches for a page view:

| device | viewport | DPR | scan displayed at | device px needed | variant chosen | bytes |
|---|---|---:|---|---:|---|---:|
| small phone | 320 | 2 | 262 px | 524 | `@900` | 125,911 |
| iPhone 14 | 390 | 3 | 332 px | 996 | `@1600` (1275 px) | **218,723** |
| Pixel 7 | 412 | 2.625 | 354 px | 929 | `@1600` | 218,723 |
| iPad portrait | 768 | 2 | 683 px | 1366 | `@1600` | 218,723 |
| laptop | 1280 | 1 | 448 px | 448 | `@900` | 125,911 |
| laptop hidpi | 1440 | 2 | 446 px | 892 | `@1600` | 218,723 |
| desktop | 1920 | 1 | 445 px | 445 | `@900` | 125,911 |

`srcset` and `sizes` are behaving **correctly**: at every breakpoint the browser
picks the smallest candidate that covers the device pixels it needs. A modern
phone at DPR 3 genuinely needs ~1,000 device pixels for a 332 px column, so it
takes the 1275 px file — 219 KB, 1.2 s of Fast 3G, 1.7× what the desktop pays.

That is uncomfortable but it is not a bug, and the fix is not obviously an
improvement: a scanned document is a thing readers pinch and zoom into, and
serving a phone a 600 px page would make the archive less readable on the device
most people read it on. Reported, with the numbers, and left alone. If it is
ever revisited, note that the widest variant is 1275 px and not 1600 (§4.4), so
DPR-3 phones are already only just covered.

### 7.9 Precaching the pagefind runtime — rejected

97,359 bytes on every reader's first visit for a feature many never use, to save
733 ms on a first search that the service worker has already cut from 4,217 ms
by other means. §6.7.

---

## 8. Diffs in files this work does not own

**Status: all four diffs below have since been applied**, checked against the
tree and against a fresh build of the demo — no `pagefind-component-ui` and no
`wasm.unknown.pagefind` in `_pagefind/` (§8.1), `avif_speed = 7` in
`ingest/raster.py` (§8.2), an inline `data:image/webp` placeholder on every page
view (§8.3), and Liberation ahead of DejaVu in `fonts.css` (§8.5). They are kept
here because each one is the argument for the change, and the argument is what a
reader needs to decide whether to keep it. §8.4 is not a diff and is still open.

### 8.1 `src/stackroom/build/site.py` — two prunes and the offline hook

```diff
@@
 from ..model import Collection, Document, Page, PageVerdict, to_jsonable
 from ..textblock import render_markdown
+from . import offline as offline_mod
 from . import search as search_mod
@@
-# Pagefind ships a drop-in search UI - about 420 KB of it - that this project
-# never loads, because it has its own. On a small archive that is a third of the
-# index directory, mirrored by everyone who clones the site.
-UNUSED_PAGEFIND = ("pagefind-ui", "pagefind-modular-ui", "pagefind-highlight")
+# Pagefind ships a drop-in search UI - about 420 KB of it - that this project
+# never loads, because it has its own. On a small archive that is a third of the
+# index directory, mirrored by everyone who clones the site.
+#
+# `pagefind-component-ui` is the 1.5.x name for it and was missing from this
+# list, so the prune ran, reported success and removed nothing: measured on the
+# demo, 217,318 bytes of pagefind-component-ui.{js,css} were still being
+# published - 47% of the whole index directory. If pagefind renames it again,
+# the symptom is silent, so docs/PERFORMANCE.md records how to re-measure it.
+UNUSED_PAGEFIND = (
+    "pagefind-ui",
+    "pagefind-modular-ui",
+    "pagefind-component-ui",
+    "pagefind-highlight",
+)
@@
 def _prune_pagefind_ui(out: Path) -> None:
     bundle = out / search_mod.BUNDLE_DIR
     if not bundle.is_dir():
         return
     for entry in bundle.iterdir():
         if any(entry.name.startswith(prefix) for prefix in UNUSED_PAGEFIND):
             if entry.is_dir():
                 shutil.rmtree(entry, ignore_errors=True)
             else:
                 entry.unlink(missing_ok=True)
+    _prune_unused_wasm(bundle)
+
+
+def _prune_unused_wasm(bundle: Path) -> None:
+    """Drop the stemmer files no page of this site can ask for.
+
+    Pagefind writes `wasm.unknown.pagefind` - 68,024 bytes, the language-less
+    fallback - beside the per-language one. A client loads the file named by
+    its language's `wasm` key in pagefind-entry.json and nothing else, so when
+    every language in that manifest names a real stemmer the fallback is dead
+    weight in every clone of the archive. When any language has `wasm: null`
+    the fallback is exactly what it will load, so it stays.
+    """
+    try:
+        entry = json.loads((bundle / "pagefind-entry.json").read_text(encoding="utf-8"))
+        languages = entry["languages"]
+    except (OSError, ValueError, KeyError, TypeError):
+        return  # if the manifest cannot be read, keep everything
+    wanted = {info.get("wasm") for info in languages.values() if isinstance(info, dict)}
+    if not wanted or None in wanted:
+        return
+    keep = {f"wasm.{name}.pagefind" for name in wanted}
+    for path in bundle.glob("wasm.*.pagefind"):
+        if path.name not in keep:
+            path.unlink(missing_ok=True)
@@
         self.report.media_bytes = sum(
             f.stat().st_size for f in (self.out / "media").rglob("*") if f.is_file()
         )
+        # Last, and it has to be last: it takes an inventory of what is on disk,
+        # and anything written after it - the search index most of all - would
+        # be missing from what a reader can store.
+        offline_mod.write_offline(self)
         return self.report
```

`write_offline` uses `builder.write`, so `files_written` and `bytes_written`
stay correct, and it appends its own warnings to `report.warnings`.

### 8.2 `src/stackroom/ingest/raster.py` — `avif_speed = 7`

```diff
@@
-    avif_speed: int = 6
-    """libavif effort, 0 slowest to 10 fastest. On a 1600x2071 page: speed 4 =
-    97 KB in 4.7 s, speed 6 = 105 KB in 1.0 s, speed 8 = 108 KB in 0.4 s,
-    speed 9 = 127 KB in 0.12 s. Six is the knee."""
+    avif_speed: int = 7
+    """libavif effort, 0 slowest to 10 fastest.
+
+    Seven, not six. Measured over all 48 AVIF variants of the demo collection,
+    interleaved so a busy machine costs each speed alike: speed 6 takes 32.24 s
+    and writes 1,890,446 bytes; speed 7 takes 19.02 s and writes 1,901,789 -
+    41% less time for 0.6% more bytes. On two of three sample pages speed 7 is
+    strictly better than 6, smaller as well as faster.
+
+    Eight is the wrong side of the knee: 12.28 s but 2,330,244 bytes, +23% on
+    every page image in the archive, which is downloaded far more often than it
+    is built. Note that the older figures quoted here (105 KB at 6, 108 KB at 8)
+    were taken on an RGB page; these pages encode as 4:0:0 monochrome, where the
+    6-to-8 step is 53 KB to 91 KB.
+
+    Both speeds are byte-for-byte deterministic over four repeats with
+    `encoder_threads = 1`, so guarantee 6 is unaffected. See docs/PERFORMANCE.md
+    for the full table and how to re-measure."""
```

### 8.3 LQIP — `model.py`, `ingest/raster.py`, `pipeline.py`, `templates/page.html.jinja`

Four small pieces. Measured cost: 1.34 ms and 147 raw bytes per page; measured
benefit: a picture of the page 2.3 s earlier on Fast 3G (§5.1).

**`model.py`** — one field on `Page`:

```diff
@@ class Page:
     thumbs: list[ImageVariant] = field(default_factory=list)
+    placeholder: str = ""
+    """A 24 px-wide WebP of this page as a `data:` URI, or "".
+
+    Inline in the HTML so the scan's frame holds a picture of the page while
+    the real image is still arriving - measured at 622 ms against 2,950 ms on
+    a Fast 3G connection, for 140 gzipped bytes. It is a placeholder and never
+    evidence: it is 24 pixels wide and no text on it is legible."""
```

**`raster.py`** — produce it from pixels already in memory, and carry it on
`RenderedPage`:

```diff
@@
+import base64
+import io
@@ class RenderedPage:
     thumbs: list[Variant] = field(default_factory=list)
+    placeholder: str = ""
@@
+PLACEHOLDER_WIDTH = 24
+PLACEHOLDER_QUALITY = 40
+
+
+def _placeholder(img: Image.Image) -> str:
+    """A 24 px-wide grayscale WebP of the page, as a `data:` URI.
+
+    Grayscale because it is 24 pixels wide and colour buys nothing at that
+    size; WebP because it is a third of the JPEG (96 B against 236 B measured
+    over six real pages). Deterministic: `method=6` is pinned like every other
+    encoder setting in this module.
+    """
+    small = img.convert("L")
+    height = max(1, round(small.height * PLACEHOLDER_WIDTH / small.width))
+    small = small.resize((PLACEHOLDER_WIDTH, height), Image.Resampling.LANCZOS)
+    buffer = io.BytesIO()
+    small.save(buffer, format="WEBP", quality=PLACEHOLDER_QUALITY, method=6)
+    return "data:image/webp;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
@@ def encode_page(...):
     variants, thumbs = _write_variants(img, out_dir, number, spec, gray)
     return RenderedPage(
         number=number,
@@
         thumbs=thumbs,
+        placeholder=_placeholder(img),
         is_grayscale=gray,
```

**`pipeline.py`** — one line in `_publish_images`:

```diff
@@ def _publish_images(job: PageJob, image: Image.Image, page: Page) -> None:
     page.thumbs = [
         ImageVariant(f"{prefix}/{v.path.name}", v.format, v.width, v.height, v.bytes)
         for v in rendered.thumbs
     ]
+    page.placeholder = rendered.placeholder
```

**`templates/page.html.jinja`** — the placeholder goes *behind* the image and
nothing is hidden:

```diff
-        <figure class="scan__figure" id="scan"
-                style="aspect-ratio: {{ page.image_width or 1000 }} / {{ page.image_height or 1294 }}">
+        {#- The placeholder is a background the real scan paints over. It is
+            deliberately not an opacity transition on the <img>: an element at
+            opacity 0 is not an LCP candidate, and with scripting off the scan
+            would never become visible at all. -#}
+        <figure class="scan__figure" id="scan"
+                style="{% if page.placeholder %}background-image:url({{ page.placeholder }});background-size:cover;background-position:top center;background-repeat:no-repeat;{% endif %}aspect-ratio: {{ page.image_width or 1000 }} / {{ page.image_height or 1294 }}">
```

No CSP change: `img-src 'self' data:` already permits it.

### 8.4 `data/<doc>/<n>.json` — a decision for someone else

74,629 bytes on this collection, 93 MB at the supported 20,000-page ceiling,
byte-identical to what is already inline in the HTML, and fetched by nothing
(§2.5). Three options, in order of how much they change:

1. **Keep them and say what they are. Applied.** `build/site.py:page_payload`
   used to say they were "only fetched when a reader actually opens the page",
   which was never true. Both comments now say the same thing:
   `docs/ARCHITECTURE.md` calls them "a machine-readable side-channel for
   anyone building on the archive" and points here for the argument, and the
   docstring says nothing in the site fetches them, that publishing them is
   deliberate, what they cost — 4,664 bytes a page, 74,629 over the demo, 93 MB
   at the ceiling — and that deleting them would be a change to what this
   project publishes rather than a tidy-up. Nothing was removed; the two
   options below are still open.
2. **Emit them only above a threshold.** Inline for pages under, say, 2,000
   tokens; file-only above, with `viewer.js` fetching when the inline block is
   absent. Keeps small pages at zero round trips and stops a 5,000-word page
   from carrying 20 KB of integers in its HTML.
3. **Drop them.** A published-layout change, so a `CHANGELOG.md` entry under
   the "output layout" heading the file reserves for exactly this.

No recommendation: this is a question about the archive's contract, not about
performance.

### 8.5 `src/stackroom/assets/fonts/fonts.css` — the largest single win here

**Applied.** The file now carries it, with its own longer note; what remains
here is the shape of the change and what it turned out to be worth.

```diff
 @font-face {
   font-family: "Stackroom Sans Fallback";
-  src: local("Segoe UI"), local("Roboto"), local("Helvetica Neue"),
-       local("Arial"), local("DejaVu Sans"), local("Liberation Sans");
+  src: local("Segoe UI"), local("Roboto"), local("Helvetica Neue"),
+       local("Arial"), local("Liberation Sans"), local("DejaVu Sans");
 @font-face {
   font-family: "Stackroom Serif Fallback";
-  src: local("Georgia"), local("Charter"), local("Times New Roman"),
-       local("DejaVu Serif"), local("Liberation Serif"), local("Noto Serif");
+  src: local("Georgia"), local("Charter"), local("Times New Roman"),
+       local("Liberation Serif"), local("Noto Serif"), local("DejaVu Serif");
```

The mono stack is deliberately not reordered: the three faces in it agree on
their one advance width (1020.0 px on the same test line), so there is no swap
to absorb.

Re-measured on the current tree, five cold Fast-3G loads per cell, service
workers blocked, against a byte-identical copy of the same build with only the
two `local()` entries swapped back — method and the fuller argument in §5.2:

| | front CLS | document CLS | page-view CLS |
|---|---:|---:|---:|
| DejaVu first | 0.4631 | 0.1658 | 0.1236 |
| **Liberation first** | **0.0085** | **0.0101** | **0.1040** |

This section previously quoted 0.0925 → 0.0075 for the front page. That was a
Snapshot A figure for a front page with much less above the fold than the
current one has; the "before" column above is the same 0.4631 that §3.1 reports
for Snapshot B. The document pair reproduces within noise. The page view is new
here and is the interesting one: it barely moves, because what shifts on a page
view is the scan arriving beside the transcription rather than the type
re-wrapping — the reorder has almost nothing to do there, and saying so is more
useful than leaving the row out.

The block below these explaining why `size-adjust` is **not** set should stay
as it is. It is correct, and this change is the version of the same fix that
carries no per-platform assumption.

---

## 9. The remaining bottleneck, honestly

**For the reader: the first visit, and specifically the fonts.** 104 KB of
`.woff2` on the front page — more than the HTML, CSS and JavaScript together —
which cannot be preloaded without costing 324 ms of FCP (§7.1), cannot be
compressed further (woff2 is Brotli already), and cannot be subset harder
without knowing the collection's language at font-build time. The service
worker removes this cost from every visit after the first and the fallback
reorder removes the shift it causes, but the first visit still pays it in full.
The only real lever left is shipping fewer faces: three families at four
weights is a design decision, not a performance one, and it is not this
document's to make.

**For the reader on a page view: the scan, and there is no way round it.**
126 KB at desktop widths, 219 KB on a DPR-3 phone. That is the document. AVIF
is already 40% smaller than the WebP beside it, the quality is already 50, and
the LQIP makes the wait *look* shorter without making it shorter. Anything
further trades away the legibility of a scanned page, which is the archive.

**For the operator: AVIF encoding, still, after the speed change.** Re-measured
on 2026-09-01 (§0): at `avif_speed = 7` the build is **about 46–48 s for 16
pages**, of which encoding is 45% and OCR of four pages is 37%. Rendering all
the HTML and building the search index together are **0.57 s — a little over
1%**. On a
2-core machine `_default_workers()` returns 1 and the process pool never runs,
so at about 3 s a page a 2,000-page collection is roughly **100 minutes**
serial; on 8 cores the pool should take it to something near a fifth of that,
which nobody here has measured. **The single highest-value build change available is not in
this repository: it is `pillow-avif-plugin` or a Pillow built against a libavif
with more aggressive threading**, since `encoder_threads` is pinned to 1 for
determinism and that pinning costs whatever multi-threaded encoding would have
saved. That is a real trade — reproducible builds against a faster build — and
determinism is the right side of it for an evidence archive.

---

## 10. Re-measuring

The scripts used are not committed: they are measurement scaffolding, and a
number that cannot be reproduced from a description is not worth keeping. Each
section above states its method precisely enough to rebuild. The pieces that
are easy to get wrong:

- **Serve with compression.** Uncompressed HTML/CSS makes a 3G profile look 3–4×
  worse than any real host.
- **Stop the server to test offline.** Emulated offline does not reach the
  service worker (§1).
- **Interleave encoder A/B runs.** Wall clock on a shared machine drifts by 50%;
  interleaving speed 6 and speed 7 encodes of the same pixels does not.
- **Take the median of at least three cold loads**, in a fresh browser context
  each time. A service worker and an HTTP cache both persist across navigations.
  Three is not always enough: the front page's residual CLS is bimodal (§5.2),
  and a three-run median of it landed on 0.0639 where a seven-run median lands
  on 0.0085. If the runs of a cell do not agree with each other, say so or take
  more of them.
- **Watch the source snapshot.** `assets/stackroom.css` nearly doubled and seven
  files appeared in `assets/js/` during this exercise; a number from before that
  is not comparable with one from after.
