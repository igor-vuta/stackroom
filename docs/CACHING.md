# Caching, and watch mode

A 5,000-page collection takes about four hours on the machine in
`docs/PERFORMANCE.md`. Encoding images is 45% of that, recognition 37%,
rasterising 13%; building the HTML and the search index together are under 2%.
Fixing a typo in one document and running the build again used to do all of it a
second time.

It no longer does. This document is what the cache keys on, what it stores,
what it refuses to store, and what happens when the disk is full.

---

## 1. The claim, and how it is checked

> Same input bytes, same output bytes, cached or not.

That is `docs/ARCHITECTURE.md` guarantee 6 with three words added, and it is not
a hope. `tests/test_cache.py::test_a_warm_build_is_byte_identical_to_a_cold_one`
builds a collection into one directory with an empty cache, builds it again into
another with a full one, and compares the two trees file by file, byte for byte
— the HTML, the JSON word boxes, the manifest, the service worker's inventory
and every encoded image.

It passes with one field pinned: `BuildInfo.built_at`, which reads the clock and
is written into `manifest.json` and printed in every page footer. That field is
the only thing in a Stackroom site that is not a function of the input bytes. It
is decided once for the whole collection and has nothing to do with the cache.

`SOURCE_DATE_EPOCH` is what pins it outside the test. Set it to a Unix timestamp
and `built_at` comes from there, so *every* file in the tree is a function of
the input bytes — verified by building the demo twice with it set and comparing
all 204 files. `docs/ARCHITECTURE.md` guarantee 6 is the canonical statement of
this and the two documents agree.

Run the same comparison by hand:

```sh
stackroom build ./release -o /tmp/cold --no-cache
stackroom build ./release -o /tmp/warm
diff -r /tmp/cold /tmp/warm        # manifest.json, and nothing else

export SOURCE_DATE_EPOCH=1717200000
stackroom build ./release -o /tmp/a --no-cache
stackroom build ./release -o /tmp/b
diff -r /tmp/a /tmp/b              # nothing at all
```

Run on 2026-09-01, the first prints one line of difference across 204 files and
the line is `built_at`; the second prints nothing.

---

## 2. What it costs and what it saves

Measured on the demo collection — `demo/release`, three PDFs, 16 pages, 3.5 MB,
four of the pages needing OCR — on the two-core machine of
`docs/PERFORMANCE.md`, so `pipeline._default_workers()` is 1. Wall clock for
the whole `stackroom build --no-search`.

| | Wall | Pages from cache | Against cold |
|---|---:|---:|---:|
| Cold, `--no-cache` | 51–59 s | — | 1.0× |
| Cold, cache empty | 48 s | 0 of 16 | 1.0× |
| **Warm, nothing changed** | **1.5 s** | 16 of 16 | **~35×** |
| **Warm, one of three documents edited** | **12.7 s** | 12 of 16 | **~4×** |
| Warm, a file `touch`ed but not changed | 1.5 s | 16 of 16 | ~35× |

> **On the cold rows.** This table used to say 81.2 s, and that figure does not
> reproduce: re-measured on 2026-09-01, five interleaved runs of the same
> command on the same collection came out at 51–59 s, and a run with the cache
> *enabled* and empty at 48 s. The machine here is shared, and a build's wall
> clock swings by half again under contention — encoding and OCR both do — which
> is the most likely explanation for the old number and is the reason the row
> now carries a range rather than three significant figures. `docs/PERFORMANCE.md`
> §0 has the method, the per-configuration figures, and the reconciliation with
> the other two build-time numbers in these documents. Note also that the demo's
> edited-document row depends on *which* document: editing the four-page
> born-digital memo costs 12.7 s, editing the four-page **scan** costs 92 s,
> because those are the pages that need OCR.

Instrumented, the ingest phase alone:

| | |
|---|---|
| Cold ingest | 70.4 s |
| …of which inside the cache | **0.13 s (0.18%)** |
| Warm ingest | **0.16 s** |
| …of which restoring pages and images | 0.038 s, i.e. **2.4 ms a page** |
| Restoring with `STACKROOM_CACHE_VERIFY=0` | 1.0 ms a page |
| Cache on disk | 5.4 MB for 16 pages, **≈340 KB a page** |

Of those, only the last was re-taken on 2026-09-01, and it reproduces exactly:
5.4 MB for the demo's sixteen pages. The rest are from the same instrumented run
as the cold-build figure above and are subject to the same caveat — the *ratios*
are what they are for; the absolute cold-ingest second count should be read
against the 51–59 s range in the table above rather than as 70.4.

Two things to take from that. Writing to the cache costs a cold build about two
parts in a thousand, which is inside the noise of the build it is measuring — so
there is no case for turning it off to make a first build faster. And restoring
a page costs 2.4 ms, of which 1.4 ms is re-reading each image to check its
digest; that check is what makes the byte-identity claim checked rather than
assumed, and it is worth 1.4 ms.

### Extrapolating to 5,000 pages, honestly

At about 3 s a page — the demo's cold build divided by its sixteen pages, and a
collection of real scans will be slower, not faster, because only a quarter of
the demo's pages need OCR — 5,000 pages is **about four hours** at one worker.

| | 5,000 pages, estimated |
|---|---|
| Cold | ≈ 4 h |
| Warm, nothing changed: restoring | 12 s |
| Warm: hashing the sources in `discover` | ≈ 20 s for 20 GB at 970 MB/s |
| Warm: emptying and rewriting the site | ≈ 70 s, plus the search index |
| **Warm, total** | **a few minutes** |
| Warm, one 20-page document edited | the few minutes, plus ≈ 60 s if it is born-digital, and several times that if it is a scan |

The honest caveat is in the third row. **The cache makes ingest incremental; it
does not make the site builder incremental.** At 5,000 pages a fully-cached
rebuild is dominated by deleting 30,000 files and writing them again, and by
Pagefind. That is the next thing worth fixing and it is not in this module.

The other honest caveat is in the last row, and it is section 3's subject: the
key is the *file's* digest, so editing one page of a 500-page PDF re-reads all
500 of its pages. It does not touch the other 4,500 pages of the collection,
which is the promise that mattered.

---

## 3. The key

```
sha256(canonical JSON of:
    format      the on-disk entry format version
    schema      a digest of every model dataclass this cache serialises
    source      { sha256 of the file's bytes }
    job         13 fields of PageJob (below)
    env         stackroom, poppler, tesseract, Pillow + its codecs,
                pdfplumber, pdfminer, numpy, encodable formats,
                installed fonts, OMP_THREAD_LIMIT, platform
    tessdata    size and mtime of each .traineddata that will be loaded
    salt        STACKROOM_CACHE_SALT, normally empty
)
```

`stackroom.cache.key_inputs()` returns exactly that structure, so the reasoning
below can be checked against something executable rather than against this
paragraph.

A key that is too narrow silently serves a stale result. A key that is too broad
never hits and the cache is an expensive no-op. Every field of `PageJob` is
therefore in one of two named sets in `cache.py`, and **a field in neither
disables the cache** rather than being quietly ignored (§9).

### The job fields that are in the key

| Field | Why |
|---|---|
| `number` | which page |
| `is_image` | whether there is a content stream to read at all |
| `dpi`, `max_megapixels` | the resolution — and therefore every word box, the ink coverage, what recognition can resolve, and whether the page was shrunk to fit the budget |
| `widths`, `thumb_width`, `formats` | which image files exist, at what size, in which encoding. The cache hands those files back; they are the answer, not a detail of it |
| `ocr_mode`, `psm`, `auto_rotate` | the text |
| `ocr_languages` | the text — **in order**. `-l eng+fra` is not `-l fra+eng`; Tesseract weights the first, and a cache keyed on a *set* would serve one for the other, invisibly, until somebody read a page of French |
| `doc_id`, `media_prefix` | they are written into the answer: `PageOutcome.doc_id`, and every `ImageVariant.path` — which is published, in the HTML |

### The job fields that are deliberately not

| Field | Why not |
|---|---|
| `pdf` | the *path*. Replaced by the SHA-256 of the bytes, which is strictly better: moving a collection, restoring it from a backup, or building the same release from two directories keeps every hit. `touch` costs nothing (measured above, and pinned by a test) |
| `media_dir` | where images are written. A destination, not an input. Leaving it out is what lets `--out` change without re-encoding anything: the same blobs are linked somewhere else |
| `ocr_timeout` | a resource bound, not an output determinant. It cannot change a successful result, only turn one into a failure — and failures are never cached (§5). Keying on it would invalidate every entry the first time somebody raised the limit, for nothing |

### The environment, and why so much of it

A cached page is a claim that re-running the work would produce the same bytes.
Everything that could falsify that claim is in the key.

- **Tesseract's version.** An upgrade changes the text on every scanned page. A
  cache that ignored it would be worse than no cache: it would publish last
  year's recognition under this year's version stamp.
- **The `.traineddata` files**, by size and mtime, for every language requested
  plus `osd` (which `auto_rotate` loads). Tesseract's version is not the whole
  story — swapping the fast `eng.traineddata` for the `best` one changes every
  scanned page without changing a single version number. Size and mtime rather
  than a content hash because these files run to 15 MB and this is computed once
  per build; it is a fingerprint, not a proof, and the cache is machine-local
  anyway.
- **poppler's version.** Rasterising is 11% of a build and everything downstream
  reads its pixels: a change can move a word box, change what OCR sees, and
  change whether the redaction check believes a box is uniform.
- **Pillow, and the libraries it was built against.** Pillow's own version is not
  enough — the bytes of a WebP or an AVIF are decided by libwebp and libavif,
  which are separately versioned shared libraries, and the cache hands those
  exact bytes back to be published.
- **Which formats this machine can encode.** `raster.AVIF_AVAILABLE` is probed
  at import; a machine without an AVIF encoder publishes different files.
- **The installed fonts**, as a digest of `fc-list` output, sorted. This is the
  one that is easy to miss. A PDF that does not embed its fonts is drawn by
  poppler with whatever substitute the machine has, so the *pixels* — and
  therefore the OCR text, the ink coverage, the redaction analysis and the
  published image — depend on which fonts are installed. `docs/PERFORMANCE.md`
  already says the fallback font set matters; it matters here too.
- **`OMP_THREAD_LIMIT`.** `ingest/ocr.py` pins it to 1 with a `setdefault`, so an
  operator who exports something else gets a Tesseract running a different
  number of threads, and OpenMP reductions are not obliged to be bit-identical
  across thread counts. It costs nothing to include.
- **Stackroom's own version — and in a working tree, its source.** A version
  number does not change when somebody edits `ingest/quality.py`, and in a
  checkout that happens hourly. When the package is running from a working tree
  (a `src/` layout, or a `.git` or `pyproject.toml` above it) the key includes a
  digest of every `.py` file in the package. Two milliseconds, once per process,
  and it turns *my cache is serving results from the code I just changed* from a
  bug report into an impossibility. If that digest cannot be computed, the cache
  disables itself: at that point it cannot say what code an entry was written
  by, and a cache that cannot say that is the dangerous kind.

`STACKROOM_CACHE_SALT` is the manual override — set it to anything to invalidate
every entry without deleting them.

### What is *not* in the key, and is checked another way

The **shape of the model** is not in the key as a version number; it is in it as
a digest over the field names, order and declared types of `Box`, `Word`,
`ImageVariant`, `Redaction`, `OcrQuality`, `Page` and the three enums. Add a
field to `Page` and every entry written before becomes unreachable, immediately,
without anybody remembering to bump a constant. That already earned its keep:
`Page.placeholder` was added while this module was being written, and the digest
changed under it without a word.

### Why not a per-page digest of the PDF?

Because editing one page of a 500-page PDF re-reads all 500. The alternative is
a stable per-page content digest: hash that page's content streams plus the
resources it references. It was rejected. Resources are shared and inherited,
`/Resources` can sit on any ancestor node, a font swapped in a shared dictionary
changes the rendering of pages that did not otherwise move, incremental updates
and object streams mean the same page can be spelled several ways, and a
linearisation rewrites everything. Getting it *nearly* right yields stale
renders — the exact failure this whole file exists to prevent — and getting it
right means reimplementing a large part of a PDF parser inside a cache. The
file's digest is coarse, obviously correct, and free: `discover` has already
computed it.

---

## 4. What is stored

```
<cache>/CACHEDIR.TAG                   so backup tools skip this
<cache>/README.txt                     what is in here and how to delete it
<cache>/pages/v1/entries/ab/<key>.json.gz    the serialised PageOutcome
<cache>/pages/v1/entries/ab/<key>.refs       the blob digests it needs
<cache>/pages/v1/blobs/cd/<sha256>           one encoded image, named by its digest
```

The entry is gzipped JSON, versioned by `FORMAT`, with its own key written
inside it as well as being its name — so a truncated-then-refilled file, or a
file moved between shards by hand, is caught rather than served. Blobs are named
by their own SHA-256, which gives deduplication for nothing: the same blank page
in two productions, or the same exhibit attached to two memos, is stored once.
Editing one page of the demo's four-page memo added four entries and *zero*
blobs, because the other three pages re-encoded to identical bytes.

Images are hard-linked into the output directory rather than copied, falling back
to a copy across filesystems (or with `STACKROOM_CACHE_COPY=1`). The link is
also how they get *into* the cache, so storing a page costs a read and a hash
rather than a second write.

`model.to_jsonable` exists and is deliberately not used. It writes a `Box` as
four integers at 1/10,000 of the page — right for the JSON a browser reads,
wrong for a cache. `build/negative.py` draws redaction rectangles to three
decimal places of a percent, so a warm build that rounded would publish
different SVG from a cold one. Every float here is stored as a float; JSON
round-trips a finite double exactly, so this is lossless rather than nearly
lossless, and there is a test that two boxes 1e-5 apart — indistinguishable at
`model.SCALE` — come back distinct.

The `.refs` sidecar exists so that eviction can decide which blobs are still
wanted without decompressing every entry: 5,000 entries is five seconds of gzip
and a tenth of a second of sidecars.

---

## 5. What is never stored

### Text found underneath a black box

`model.HiddenText` says the recovered text is *"never written to the published
site, and never to any file on disk."* A cache is a file on disk. So:

**A page that leaked is not cached at all.** It is re-read from the original on
every build. The entry that would have been written is not written; there is no
partial record, no length, no shape.

The brief for this module asked for the box, the length and the shape from
`redacted_repr()`, and asked whether the shape is too much. It is, and so is the
length, and here is the argument.

`redacted_repr()` maps alphanumerics to `#` and leaves everything else. A name
comes out as `####### ######`; an email address keeps its `@` and its dots; a
case number keeps its hyphens. That is not a redaction, it is a crossword clue.
Word count, word lengths and punctuation are exactly the features that narrow a
candidate list, and the candidate list for text under a black box in a FOIA
release is often short and often a person. `SECURITY.md` and
`ingest/redaction.py` both treat a false negative here as something that can
burn a source; writing a fingerprint of the source's name into a long-lived
directory in `~/.cache` is not a proportionate thing to do for a few
milliseconds.

And it buys nothing, because of the second argument, which is the stronger one.
The operator's report — the full-screen message in `cli.py` — is built from
`len(item.text)` and `item.redacted_repr()`. If those came from a cache they
would have to be *stored*; if they were not stored, a warm build's warning would
be weaker than a cold build's. **A safety report that gets quieter the second
time you run it is worse than no cache.** Re-reading the page is the only way
the warm report equals the cold one, and it costs a handful of pages out of
5,000 — in a collection that, at the default `safety.hidden_text = "stop"`, is
not going to be published until they are fixed anyway.

The test greps every file in the cache directory — entries decompressed first —
for a planted secret, for its lowercase form, for its first ten characters, and
for its `redacted_repr()` shape.

What the cache *does* record about such a page is nothing, because there is no
entry. What `stackroom cache` can therefore say about leaks is also nothing,
which is the right trade: a file saying *document X page 7 leaked* is a map to
the page worth extracting, and the only thing it would buy is a faster warning
that we get for free by re-reading the page.

### Anything the build could not vouch for

| Refused | Because |
|---|---|
| `outcome.hidden` is non-empty | above |
| `outcome.analysis_failed` | *we could not check this page* is not *this page is clean*, and a cache that writes the first down and serves it back turns it into the second. Anything unchecked is checked again, every build |
| `outcome.error` is set | almost always the environment rather than the document |
| a warning containing `failed`, `timed out`, `could not`, `not found`, `unavailable` | Tesseract killed by the OOM reaper, or `pdftoppm` timing out on a busy machine, is not a fact about the document. Writing it down would poison that page until somebody cleared the cache |
| a non-finite number anywhere in the page | a NaN round-trips through JSON as a value nothing else accepts. `allow_nan=False` catches it at the door |
| a page that has already been annotated | see below |
| a source file that changed since `discover` hashed it | see §8 |

The transient-warning list is deliberately broad. A false positive costs one
page of work on every build; a false negative poisons a page until somebody
notices.

### Pages that have already been annotated

`annotate_document` decides exemption codes and control numbers by looking at
every page of a document at once, and writes them *into* the pages. A page
stored after that carries an answer its own job does not determine. The wiring
therefore stores each outcome as it arrives, before annotation; the cache also
refuses a page whose `exemptions`, `bates` or `redactions[].codes` are already
populated. The order is the fix and the check is the alarm.

Those passes then run again, over restored pages, on every build — they are part
of the 0.8% and they are not worth caching.

---

## 6. Where it lives, and whether to share it

`STACKROOM_CACHE_DIR`, then `XDG_CACHE_HOME/stackroom`, then the platform's own
convention (`~/Library/Caches/stackroom`, `%LOCALAPPDATA%\stackroom\Cache`,
`~/.cache/stackroom`). `XDG_CACHE_HOME` wins everywhere it is set, including on
macOS and Windows: it is not the native convention there, but it is an explicit
instruction from the operator. `--cache-dir` overrides all of it.

Two commands print a path and they now print the **same** one. `stackroom cache`
shows the base beside "Where", and `stackroom cache path` prints exactly that —
the directory `--cache-dir` takes — so this reopens the cache you had rather
than making a new one:

```sh
stackroom build ./release --cache-dir "$(stackroom cache path)"
```

`cache path` used to print the entry directory *inside* the base,
`<base>/pages/<layout>`, which is the one path here that cannot be fed back in:
`--cache-dir` pointed at it opens a second cache nested inside the first,
silently, and every page misses. That path is still available by name —
`stackroom cache path --entries` — and `stackroom cache` labels both, "Where"
and "Entries in".

**If you have a script that consumed `stackroom cache path`, it now gets a
different string.** A script that fed it to `--cache-dir` was broken and is now
right; a script that wanted the entry directory needs `--entries` adding to it.

**The default is one shared cache for every collection on the machine**, and it
is shared safely: the key is the file's content plus the environment, so two
collections can only collide on a page that would produce identical output. The
gains from sharing are real — the same exhibit attached to two productions, the
same release built by two people from the same folder, a build repeated with a
different `--out`, and every `--watch` cycle.

The argument for scoping it to a collection is not correctness, it is
accumulation. One directory that quietly grows page images of every document
anybody on that machine has built, months after they finished with them, is a
liability of a kind this project takes seriously elsewhere. If that is your
situation:

```sh
stackroom build ./release --cache-dir ./release/.stackroom-cache
```

and the cache lives and dies with the working folder. Deleting the folder
deletes the cache; there is nothing else to remember.

Either way: `stackroom cache clear`, before you hand a machine on and after you
finish with documents that are not yours to keep. It removes every entry and
every page image, including entries written by layout versions this build does
not recognise. The `CACHEDIR.TAG` and the README stay, because they contain
nothing but this paragraph.

`stackroom check` **never uses the cache**, in either direction. It promises
that the pages it renders live in the temporary folder it names and are deleted
when it finishes — and somebody passing `--scratch /ramdisk` is asking for
exactly that. A cache that kept copies elsewhere would break the promise on the
line above it. It is also the command whose whole job is to look at the file, so
it looks.

---

## 7. Bounded, and prunable

Default limit **5 GiB**, from `--cache-max`, `STACKROOM_CACHE_MAX`, or the
default. At the demo's ≈340 KB a page that is roughly three 5,000-page
collections.

Eviction is least-recently-**used**, over whole entries. Whole entries because
half an entry is a miss with the storage cost of a hit; least recently *used*
because a page nobody has rebuilt since March is exactly the one to lose and a
page rebuilt every hour is not, which is why a cache hit touches the entry it
serves. Blobs are then swept if no surviving entry names them.

A blob nothing references may still be one that another build wrote a second ago
and is about to claim — blobs are written before the entry that names them — so
an unreferenced blob younger than an hour is kept *while there is room*, and
removed when keeping it would break the limit. The limit is a promise; the grace
period is a courtesy.

```
stackroom cache                 where it is, how many pages, how big
stackroom cache prune --max 2GB trim to a size
stackroom cache clear           delete all of it
stackroom cache path            the base directory, for scripts (see §6)
stackroom cache path --entries  the entry directory inside it
```

All four take `--cache-dir`.

---

## 8. Two builds at once

**There is no lock, and none is needed.** The design is:

1. Every write is to a temporary file in the same directory followed by
   `os.replace`, which is atomic on POSIX and on Windows. A reader sees the old
   file or the new one, never half of either.
2. Blobs are content-addressed, so two processes writing "the same" blob write
   identical bytes and the winner does not matter.
3. An entry is written **last**, after its blobs and its sidecar. It is the
   commit point.
4. Every failure to read anything is a miss, and a miss is a page processed.

So the worst a race can do is make one build redo a page it was going to be able
to skip. A prune running underneath a build can delete a blob that build was
about to link: the restore fails its digest or its size check, returns a miss,
and the page is processed. No lock, no lock file to go stale when a build is
killed, nothing to clean up after a crash, and no serialisation between two
operators sharing a machine.

There is no `fsync`. A cache that loses its last few entries to a power cut has
lost nothing that cannot be recomputed, and an fsync per page would cost more
than the cache saves. A torn write is caught by gzip's CRC and by the key stored
inside the entry.

The one race the cache cannot fix is upstream of it: `discover` hashes a file,
and the workers read it again a few minutes later. If it is replaced in between,
*that build* is already wrong — the manifest records a digest the renderings did
not come from. What the cache can do is refuse to remember it, so the mistake
dies with the build instead of being served to every build afterwards: the size
and mtime of each source are noted when its digest is handed over, and re-checked
before anything from it is stored.

---

## 9. Every way it can go wrong

Everything in this table has a test.

| What | What happens |
|---|---|
| **Full disk** | The write fails with `ENOSPC`, the cache stops writing for the rest of the build and says so once, reads keep working, the build finishes normally |
| **Read-only cache directory** | The first write fails with `EACCES`/`EROFS` and writing is disabled with a message. Hits still serve — including the mtime touch, whose failure is ignored, so a cache on a read-only mount is a perfectly good read-only cache |
| **Cache directory cannot be created** | The cache disables itself, records why, and every lookup is a miss |
| **Truncated entry** | gzip's CRC fails; the entry is deleted and the page is processed |
| **Entry full of noise** | Same |
| **Entry whose stored key is not its filename** | Miss, entry deleted |
| **Entry from a newer `FORMAT`** | Miss |
| **Entry written against an older model** | Unreachable: the schema digest is in the key |
| **A blob has been deleted** | Miss, and anything already restored for that page is removed, so no page is published with half its `<picture>` sources missing |
| **A blob's bytes have changed** | Caught by the digest check on every hit; the blob is deleted and the page is re-encoded. This is what makes byte-identity a checked claim |
| **An entry names an image outside the media folder** | Refused. An entry is a file this process wrote, but it is still a file, and a name is used to write to disk |
| **A source file that cannot be hashed** | Miss |
| **A source file changed mid-build** | Not stored (§8) |
| **`PageJob` grows a field nobody classified** | **The cache disables itself**, with a message naming the field. A new field might change the output while sitting outside the key; that is the one bug a cache must not have, so it is not survivable |
| **The codec stops covering a model field** | Same |
| **A working tree whose source cannot be hashed** | The cache disables itself: it cannot say what code an entry was written by |
| **Cache and output on different filesystems** | Hard links fail with `EXDEV`; it says so once and copies from then on |
| **A cache full of entries and every single lookup misses** | Not a failure — the key moved, which is the cache being careful. The build says so instead of leaving "0 of 16 pages came from the cache" to look like a broken cache, and usually names what moved (*tesseract moved from 5.3.3 to 5.3.4*) from the `environment.json` each layout directory carries. Two cases it cannot name: a cache written before that file existed, and one where nothing in the environment changed at all — meaning the miss was the documents or the job rather than the setup. Both fall back to printing this build's fingerprint |

The pattern is one rule: **degrade to doing the work.** Never a crash, never a
wrong answer, and never a silent one — anything that disables the cache says so
on the way past.

---

## 10. Things in the pipeline that are not a pure function of the job

Looked for specifically, because a cache is only as honest as this list.

| | |
|---|---|
| `PageOutcome.seconds` | `time.perf_counter`. Not published. A restored page reports the time it took to *restore*, which is the honest number; the time the work originally took is accumulated separately and is what the CLI's "saving about 1m 31s" is |
| `BuildInfo.built_at` | The clock — or `SOURCE_DATE_EPOCH` where that is set — and it **is** published (§1). Collection-level, not page-level, so the cache neither stores it nor reads it, and a cached rebuild takes whichever answer this build's environment gives |
| `BuildInfo.duration_seconds` | The clock; not published |
| Font substitution in poppler | The filesystem, for PDFs with non-embedded fonts. In the key, via `fc-list` |
| `OMP_THREAD_LIMIT` | A global set with `setdefault` in `ingest/ocr.py`. In the key |
| `TESSDATA_PREFIX` and the `.traineddata` files | The filesystem. In the key |
| `raster.AVIF_AVAILABLE` | A module-level probe at import. In the key, as the encodable formats |
| `pipeline._page_count` | Reads the file with two parsers and takes the larger count. Decides which jobs exist, not what a job returns — a page that appears after a poppler upgrade is a new page number and therefore a new key |
| `raster`'s temporary directories | Names vary per run; nothing derived from them reaches the output |
| `ingest/discover.py` | Deliberately deterministic — it sorts with its own key rather than trusting the filesystem — and that is load-bearing for slugs, URLs and this cache alike |

---

## 11. Watch mode

```sh
stackroom build ./release --watch
```

Builds once, then rebuilds whenever anything under the folder changes. It sits
on top of the cache, because without one *rebuild when a file changes* means
*spend another six hours on the 4,999 pages that did not change*, and nobody
would leave that running.

```
[14:03:52] 16 pages, 16 cached, 0 read — 0.9s
Watching /home/j/release for changes. Press Ctrl-C to stop.
[14:04:09] correspondence-march-2019.pdf changed — rebuilding
[14:04:20] 16 pages, 12 cached, 4 read — 11.0s
[14:05:49] contract-award-memo.pdf gone — rebuilding
[14:05:50] 12 pages, 12 cached, 0 read — 0.2s
```

**It polls**, every second by default (`--watch-interval`). inotify, FSEvents and
`ReadDirectoryChangesW` are each faster and each behave differently — on a
network share, on a bind mount, and over a queue that overflows silently under a
large copy — and none of them is in the standard library. A stat of every file
in a collection is a few milliseconds; the build is slower by four orders of
magnitude.

- **Size and mtime**, in nanoseconds where the filesystem keeps them. Between the
  two, an edit that changes neither within one clock tick is the only thing that
  can be missed.
- **It debounces.** A change is not a change until the folder has held still for
  two seconds. A 300 MB PDF appears the moment its first byte lands, and
  building then reads half a file: poppler reports a page count that is about to
  be wrong and the last page renders as a stripe. The same wait collapses the
  twelve events of unpacking a zip into one rebuild.
- **A file that disappears** is a change like any other; the collection rebuilds
  without it.
- **`stackroom.toml` and `about.md` are watched** even when they are not inside
  the folder.
- **The output directory is not watched**, or the site would be a change that
  triggers a build that writes the site. Neither is the cache. The baseline is
  taken again after each build, so anything a build writes inside the collection
  cannot start a loop either.
- **Dotfiles, `__pycache__`, `.git`, `node_modules` and `*.part` / `*.tmp` /
  `*.swp` / `*~` are ignored.** A rebuild triggered by an editor's swap file is a
  rebuild of nothing.
- **A build that fails does not end the watch** — including a build stopped by a
  failed redaction. The operator's next move is to fix the file that broke it,
  and making them restart the watcher as well would be a small unkindness at the
  exact moment they are least in the mood for one.

---

## 12. Knobs

| | |
|---|---|
| `--cache` / `--no-cache` | on by default |
| `--cache-dir DIR` | where it lives |
| `--cache-max 5GB` | size limit |
| `stackroom cache path` | prints the base directory, which is what `--cache-dir` takes |
| `stackroom cache path --entries` | prints `<base>/pages/<layout>` instead (§6) |
| `--watch`, `--watch-interval 1.0` | watch mode |
| `STACKROOM_CACHE=0` | turn it off everywhere |
| `STACKROOM_CACHE_DIR` | where it lives |
| `STACKROOM_CACHE_MAX` | size limit |
| `STACKROOM_CACHE_SALT` | any value invalidates every entry |
| `STACKROOM_CACHE_VERIFY=0` | skip the per-hit digest check; saves 1.4 ms a page and gives up the guarantee |
| `STACKROOM_CACHE_COPY=1` | copy images instead of hard-linking them |

---

## 13. If you are changing the pipeline

Two tripwires will tell you before your users do.

**Adding a field to `PageJob`** disables the cache with a message naming the
field, until you put it in `KEYED_JOB_FIELDS` or `UNKEYED_JOB_FIELDS` in
`cache.py`. The question to answer is the only one that matters: *can this change
what a page comes out as?* If yes, it is keyed. If it is a path, a destination
or a resource limit, it is not — and write down which, next to the set, because
the next person will ask.

**Adding a field to a model dataclass** makes every existing entry unreachable
(the schema digest moves) and makes `_check_codec()` report an uncovered field,
which also disables the cache. Add the field to `encode_page`/`decode_page` and
to the expected set beside them, and remember that a `float` must be stored as a
float.

**Adding something to `process_page` that reads the clock, the filesystem or a
global** is the case neither tripwire catches. §10 is the list to add to, and
the key is where it has to end up.
