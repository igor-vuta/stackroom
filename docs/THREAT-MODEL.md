# Threat model

> This document is for someone deciding whether to trust Stackroom with a
> source's safety. It states who the attacker is, what they control, what the
> tool guarantees, what it explicitly does not, and where the current
> implementation falls short of both. `SECURITY.md` is the report-a-bug
> document; this is the map that report will land on.
>
> Everything in the findings table below was reproduced against this tree.
> `tests/test_security.py` contains a test for each one, marked
> `xfail(strict=True)` where the defect is unfixed, so the table cannot drift
> away from the code without the suite going red.
>
> **Statuses were last re-verified against the tree on 2026-09-01**, by running
> `tests/test_security.py` and reading the code behind each row. Seventeen of
> the nineteen findings are fixed; `xfail` markers survive on F13 and F14
> alone, which is the same answer from the other direction. There is no tagged
> release yet and the repository carries no history a reader could consult, so
> the dates below record **when a fix was confirmed present**, not when it
> landed.

---

## 1. Who the attacker is

The interesting attacker is **whoever produced the documents**, and they are
not hypothetical. Stackroom's whole purpose is to be pointed at a folder the
operator did not create and cannot trust:

- a government release, produced by an agency's redaction software;
- a leak from a stranger, arriving as a zip;
- a court production, assembled by opposing counsel;
- a pile of files from a source who may themselves be under pressure.

There are three people in this story and only one of them is the adversary.

| Party | Role | Trusted? |
|---|---|---|
| **The producer** | wrote every byte of every document | **No.** This is the attacker. |
| **The operator** | runs `stackroom build`, publishes the result | Yes — they are the person being protected, and the person who can be tricked |
| **The reader** | fetches the published archive over the web | Not an attacker, but a *victim* if the site is compromised |
| **The re-hoster** | mirrors the built folder somewhere else | Same as the reader, plus they inherit whatever is in `files/` |

### What the attacker controls

Completely:

- **Every byte of every document.** Header, cross-reference table, page tree,
  content streams, fonts, `ToUnicode` maps, embedded images, incremental-update
  history, `/Encrypt`, `/MediaBox`, `/CropBox`.
- **Every filename**, including its extension, its Unicode normalisation form,
  its length, and whether its bytes decode as UTF-8 at all.
- **The document's metadata**: `/Title`, `/Author`, `/Producer`, `/Creator`,
  XMP, and anything else in the file that is not page content.
- **The directory layout**, including symlinks, nesting, and duplicate content
  under different names.
- **`stackroom.toml` and `about.md`**, when the release ships with them —
  which the threat model treats as ordinary, because agencies and intermediaries
  do send "here is how to publish this" instructions alongside a production.

Not controlled:

- The operator's command line (`--out`, `--force`, `--unsafe-publish-leaks`).
- The operator's machine, environment variables, or installed binaries.
- The network. Stackroom makes no network request during a build, and this is
  enforced by test rather than asserted by comment.

### What the attacker wants

In descending order of how badly it matters:

1. **Get a failed redaction past the check** — so that the operator publishes,
   believing the tool cleared it. This exposes a source. Everything else on this
   list is less serious than this one by a wide margin.
2. **Get the operator to publish text that is invisible in the document** —
   white-on-white, under an image, in an optional-content layer, in render mode
   3. The operator proof-reads the *scan*; Stackroom publishes the *text layer*
   and indexes it for search. What a reader cannot see, a search engine can.
3. **Run script in the archive's origin** — to deanonymise readers, or to
   rewrite what the archive appears to say.
4. **Read or publish a file the operator did not choose** — through a symlink,
   a crafted filename, or a path that escapes the output layout.
5. **Make the build fail, hang, or fill the disk** — a hostile release that
   cannot be published is a release that stays unpublished.
6. **Deanonymise a reader** — get the published site to contact a third party.

---

## 2. Trust boundaries

```
   ┌───────────────────────── the attacker's side ─────────────────────────┐
   │  documents · filenames · metadata · stackroom.toml · about.md         │
   └───────────────────────────────┬───────────────────────────────────────┘
                                   │  ①  parse boundary
   ┌───────────────────────────────▼───────────────────────────────────────┐
   │  ingest/  — pdfminer, pdfplumber, pypdf (in-process)                  │
   │            pdftoppm, pdfinfo, tesseract (subprocesses, argv, bounded) │
   └───────────────────────────────┬───────────────────────────────────────┘
                                   │  ②  model boundary  (model.py, plain data)
   ┌───────────────────────────────▼───────────────────────────────────────┐
   │  the safety gate  — pipeline.check_safety()                           │
   │  nothing reaches build/ until this has run                            │
   └───────────────────────────────┬───────────────────────────────────────┘
                                   │  ③  render boundary  (Jinja autoescape)
   ┌───────────────────────────────▼───────────────────────────────────────┐
   │  build/  — HTML, JSON, manifest, pagefind index                       │
   │  files/  — the originals, copied byte for byte  ← NOT sanitised       │
   └───────────────────────────────┬───────────────────────────────────────┘
                                   │  ④  publication boundary
   ┌───────────────────────────────▼───────────────────────────────────────┐
   │  a static host, one origin, read by the public                        │
   └───────────────────────────────────────────────────────────────────────┘
```

Four boundaries, and each one is a different kind of promise:

- **① Parse.** Nothing here executes attacker code. Every subprocess is invoked
  with an argv list, never a shell, and every invocation carries a timeout
  (verified statically over the whole package by
  `test_no_subprocess_is_run_through_a_shell_or_without_a_timeout`). The risk
  here is resource exhaustion and parser divergence, not code execution.
- **② Model.** `model.py` is plain dataclasses. Anything the ingest half wants
  the build half to know has to become a field, which is what makes the safety
  gate possible at all.
- **③ Render.** Jinja autoescape is on for every template
  (`test_every_template_is_autoescaped`), the two hand-built markup helpers
  (`ribbon()` and `_json_block()`) escape their inputs, and `textblock.py`
  escapes first and re-introduces only a fixed list of constructs. This boundary
  holds; see §5.
- **④ Publication.** This is the leaky one. `files/<doc>.<ext>` is a byte-for-byte
  copy of an attacker-controlled file, published on the same origin as the
  archive, under an **attacker-chosen extension** (finding F2).

---

## 3. What Stackroom guarantees

These are the promises worth relying on, restated from `ARCHITECTURE.md` and
`SECURITY.md` and checked here against the code.

| # | Guarantee | Status, 2026-09-01 |
|---|---|---|
| G1 | Text found under a failed redaction is never published — not in the HTML, not in the JSON, not in the search index | **Holds.** It holds for findings that were *filtered* as well as reported (F7 fixed), and for a word a box only partly covers (F8 fixed): `pipeline` drops every token any obliterating box touches, in both `stop` and `warn` mode |
| G2 | A single finding stops the build | Holds |
| G3 | Recovered text is never written to disk, and the operator sees only a shape (`#####, ########`) | Holds. It is pickled between processes in a `ProcessPoolExecutor`, which is memory, not disk — but see §7. The page cache refuses to store a page that leaked at all |
| G4 | Document content cannot escape into the generated HTML as markup | **Holds**, for what Stackroom generates *and* for the original it copies: the published extension now comes from the file's own bytes (F2 fixed), so a PDF named `x.html` is published as `x.pdf` |
| G5 | A published archive makes no third-party request | Holds, enforced by test |
| G6 | The build never writes outside its output directory | **Holds**, for writing and now for reading: a symlink pointing out of the source folder is not followed and is reported as skipped (F5 fixed) |
| G7 | The build is deterministic | **Holds.** Two builds of the same folder differ only in `BuildInfo.built_at` in `manifest.json` and the date in every page footer, and `SOURCE_DATE_EPOCH` pins even those: with it set, two builds of the demo were byte-identical across all 204 files. A malformed value is refused rather than silently ignored. Verified with `diff -r` both ways |

## 4. What Stackroom explicitly does not guarantee

Stated plainly, because a tool that oversells this gets somebody hurt.

1. **`stackroom check` passing is evidence, not a certificate.** It tests one
   specific failure — recoverable text under an opaque rectangle, on a page it
   could read — and nothing else.
2. **The original file is published as it arrived**, unless you ask otherwise.
   Everything in it that is not page content is published too: metadata, XMP,
   attachments, annotations, form fields, bookmarks, and every earlier revision
   an incremental save left behind. `safety.strip_metadata` is a real partial
   mitigation now (F16 fixed): it rewrites each published PDF from its page
   tree, which drops `/Info`, XMP and the revision history, and the manifest
   then records the digest of what was published beside the digest of what
   arrived. It is not a sanitiser — page annotations survive it — and a file it
   cannot rewrite is published unchanged with a warning that names it. It is
   off by default, because production metadata is often itself evidence.
3. **Anything hidden by a means other than a rectangle is missed** — an image
   over the text, a clipping path, a non-rectangular shape, white-on-white with
   no box, text pushed outside the crop box, an optional-content layer that is
   off, or render mode 3. **And Stackroom then publishes that text as ordinary
   body text and indexes it for search**, which is a stronger and worse
   statement than "the check misses it". `SECURITY.md` says both halves now;
   the one case that *is* handled is invisible render mode over blank paper
   (F15 fixed), which is withheld and reported.
4. **Resource exhaustion is not treated as a vulnerability** by the project's own
   policy. This review disagrees mildly — see F6 — but reports them as bugs, not
   embargoed issues.
5. **It is not a sanitiser.** It does not rewrite documents. A document that
   arrives unsafe leaves unsafe; the only thing Stackroom can do is refuse.

---

## 5. Findings

Severity is about *this* tool's job — protecting a source — not about CVSS.
**Critical** means a source can be exposed while the tool reports success.

| # | Finding | Severity | Status | Test |
|---|---|---|---|---|
| F1 | A page that fails to rasterise is silently treated as checked and clean; `stackroom check` prints **"Clear."** and exits 0 | **Critical** | Fixed | `test_a_page_that_could_not_be_rendered_is_reported_as_unchecked`, `test_check_never_says_clear_about_pages_it_could_not_read` |
| F3 | `CropBox` ≠ `MediaBox` desynchronises the content-stream frame from the rendered frame; a textbook failed redaction is cleared outright | **Critical** | Fixed | `test_the_rendered_frame_and_the_content_stream_frame_are_the_same`, `test_a_cropbox_that_differs_from_the_mediabox_cannot_hide_a_leak` |
| F16 | `safety.strip_metadata` is parsed, validated, documented — and read by nothing. Author, producer and every earlier revision are published | **High** | Fixed | `test_strip_metadata_removes_author_and_producer_from_the_published_file`, `test_strip_metadata_removes_the_revision_history` |
| F2 | The published original keeps the attacker's filename extension: a PDF named `x.html` is served as `text/html` from the archive's origin, with no CSP. Stored XSS | **High** | Fixed | `test_a_published_original_never_gets_an_active_content_extension` |
| F4 | Every per-page ingest warning — including "this box has text under it, but the characters stand out of their own cells in the rendered page, so check it by hand" — is written to `PageOutcome.warnings` and read by nobody | **High** | Fixed | `test_a_warning_about_an_ambiguous_box_reaches_the_operator` |
| F7 | Text under a black box is published whenever the finding is *filtered* — a page whose boxes all hide bare dates passes, and the dates go into the HTML and the index. Default `stop` mode | **High** | Fixed | `test_text_under_a_box_is_never_published_even_when_the_finding_is_suppressed` |
| F8 | `_drop_hidden()` drops a *word* at 80% coverage while findings are assembled from *characters* at 80% coverage: a box over half a token reports the half and publishes the whole | **High** (in `warn` mode) | Fixed | `test_a_partly_covered_word_is_not_published_whole` |
| F5 | `discover()` follows a symlinked file out of the source folder; the target is hashed, ingested and republished byte for byte | **Medium** | Fixed | `test_discover_does_not_follow_a_symlink_out_of_the_source_folder`, `test_the_build_publishes_nothing_from_outside_the_source_folder` |
| F6 | `render.max_megapixels` is dead: the only code that consults it (`raster.render_pdf`) is never called by the pipeline | **Medium** | Fixed | `test_the_pixel_budget_applies_to_the_path_the_build_actually_uses`, `test_a_page_poppler_refuses_to_allocate_is_an_error_not_a_one_pixel_scan` |
| F15 | Render-mode-3 (invisible) text is published as body text and indexed. Not listed among the documented limits | **Medium** | Fixed | `test_invisible_text_is_not_promoted_into_the_published_page` |
| F17 | A filename whose bytes are not valid UTF-8 kills the build with a raw `UnicodeEncodeError`, after the output directory has been emptied | **Medium** | Fixed | `test_a_filename_that_is_not_valid_utf8_does_not_crash_the_build`, `tests/fuzz` case `undecodable-filename` |
| F13 | `config.find()` walks every parent directory to `/`: a `stackroom.toml` the operator has never seen can govern the build | **Medium** | Part-fixed | `test_configuration_is_not_taken_from_outside_the_document_folder` |
| F12 | `_prepare_out()` treats any directory containing `manifest.json` or `.nojekyll` as its own and empties it without asking. `manifest.json` is the standard name for a Web App Manifest | **Medium** | Fixed | `test_prepare_out_does_not_claim_a_directory_it_did_not_build` |
| F10 | `ocr.timeout` is unbounded by config validation, and `pytesseract` treats a falsy timeout as *no* timeout: `[ocr] timeout = 0` makes Tesseract unbounded | **Low** | Fixed | `test_a_configuration_cannot_switch_off_the_ocr_timeout` |
| F9 | `EXEMPTION_RE` backtracks cubically over a whitespace run. Not reachable from a PDF today, only because pdfplumber and Tesseract both strip `\s` | **Low** (latent) | Fixed | `test_the_exemption_scanner_does_not_backtrack_catastrophically` |
| F11 | `slugify()` emits Windows device names (`con`, `nul`, `aux`, `com1`): the archive cannot be built or re-hosted on Windows | **Low** | Fixed | `test_a_slug_is_never_a_windows_device_name` |
| F14 | `plain_text()` leaves an *unterminated* HTML comment in while `render_markdown()` strips it. Latent: nothing calls `plain_text()` today | **Low** (latent) | Unfixed | `test_plain_text_hides_what_render_markdown_hides` |
| F18 | The `--i-know` flag that `ARCHITECTURE.md` and `build/search.py` both promise above 50,000 pages does not exist | **Informational** | Fixed | `test_the_documented_page_ceiling_is_enforced` |
| F19 | `stackroom check` writes every rendered page image to `$TMPDIR`, though the README says it "builds nothing and writes nothing" | **Informational** | Fixed | `test_check_says_where_it_writes_and_can_be_pointed_somewhere_else` |

Every test in that last column is in `tests/test_security.py` except F17's second
one, which is in `tests/fuzz`. A finding that is still open is marked
`xfail(strict=True)` there: it fails today and reports XPASS-as-failure once the
defect is fixed, which is the signal to delete the marker. Two markers remain,
on F13 and F14; every other test in that file passes, which is what "Fixed"
means in the table above.

**F16 is now fixed all the way through.** The sanitiser is implemented in
`ingest/pdf.py` as `publish_pdf()`, `build/site.py:copy_originals()` calls it,
and the two end-to-end tests that used to skip themselves waiting for that call
site now run. `manifest.json` carries `published_sha256` and
`metadata_stripped` beside each document's `sha256`, and a file that could not
be rewritten produces a grouped warning naming it rather than being published
silently unstripped.

**F13 stays Part-fixed, and the remaining half cannot be fixed by a rule about
paths.** `config.find()` no longer walks to the filesystem root; it stops after
`config.MAX_CONFIG_DEPTH`, which is three. But the hostile case — a
`stackroom.toml` three directories above an empty folder — is indistinguishable
on disk from the legitimate one that `test_the_configuration_is_found_from_deep_inside_the_collection`
in `tests/test_config.py` pins the opposite answer for. So the residual risk is
answered by making it visible instead: `cli._load_config` prints which file it
used, and says *"which is not inside ‹the folder you named›"* when it came from
outside, with a line telling the operator to pass `--config`.

**F14 is the one finding still unfixed**, and it is latent: `plain_text()`
strips `<!--.*?-->` where `render_markdown()` strips `<!--.*?(?:-->|\Z)`, so an
unterminated comment in `about.md` survives one and not the other. Nothing
calls `plain_text()` today outside its own tests. The fix is to use the same
pattern in both, and it wants doing before anything feeds a
`<meta name="description">`.

### Properties that held under attack

Reported because a review that lists only holes is misleading about the shape
of the thing. Each of these is pinned by a passing test.

- **Jinja autoescape is genuinely on for every template**, and nothing marked
  `Markup` carries attacker text unescaped. `ribbon()` builds its attributes
  with `Markup.__mod__`, which escapes; `_json_block()` neutralises `<`, `>` and
  `&`, which is exactly the set that can close a `<script>` element.
- **`textblock.py` survived a battery of thirteen payloads.** Escape-then-restore
  is the right shape, the URL allowlist rejects `javascript:` and `data:`
  including their HTML-entity spellings, and nothing produces a tag or an
  attribute the module did not choose to write.
- **`slugify()` cannot produce a traversal.** Everything outside `[a-z0-9-]` is
  collapsed, so `../../etc/passwd`, `..`, `%2e%2e%2f`, absolute paths and NUL all
  fold to something inert, and a name that folds to nothing falls back to a
  digest-keyed slug. NFC/NFD collisions are resolved rather than overwritten.
- **`serve.py` refuses traversal *and* symlink escape.** Resolving both ends and
  comparing is the correct fix and it works, including for the encoded and
  doubled-dot variants that defeat naive handlers.
- **`_prepare_out()` cannot delete through a symlink.** A symlinked directory
  survives `rmtree`; a symlinked file loses the link, not the target.
- **Every subprocess is an argv list with a timeout.** No shell anywhere, and
  every config value that reaches an argv (`ocr.languages`, `psm`,
  `--force-language`) is filtered against a closed vocabulary first.
- **The build makes no network request**, verified by running it with every
  socket entry point replaced by something that raises, and the generated site
  fetches no subresource from any third-party origin.
- **In `stop` mode nothing leaks.** The site is never written and the recovered
  text appears nowhere under the output directory.
- **The leak report is safe to paste.** It prints a length and a shape, never
  the text.
- **Nothing streams-worthy is read whole.** `discover` hashes in 1 MB chunks,
  `copy_originals` uses `shutil.copy2`, pdfminer parses page by page, and
  `pdftoppm` writes PNGs to a temp directory rather than through a pipe. The two
  exceptions are `config.load()` and `attach_about()`, which `read_text()` an
  attacker-supplied `stackroom.toml` and `about.md` with no size cap — a
  gigabyte-sized `about.md` is an out-of-memory kill, and a one-line cap would
  close it. **Still open**, and re-checked on 2026-09-01: both still call
  `read_text()` unbounded. It is availability rather than exposure, which is why
  it is here rather than in the findings table. `render_markdown` itself is linear: 800 KB in 40 ms, and 8,000
  unterminated comments in 1 ms.
- **A hostile page costs what it should, not more.** One page carrying 20,000
  filled rectangles takes 3.5 s and 160 MB; 200,000 of them, from a 560 KB file,
  take 55 s and 550 MB — superlinear but not explosive, and every one of those
  rectangles then becomes an element in the page HTML, which is the amplification
  worth watching. `bates._gap_ranges` is capped at 5,000, so a control number
  jumping from 1 to a billion does not enumerate a billion gaps.

---

## 6. The findings in detail

Each entry gives how to reproduce it, what it costs, and the patch.

> **Everything below this line describes the tree as it stood when the review
> was written.** It is kept because the argument is what a reader needs in order
> to judge whether a fix was the right one, and an argument with its
> reproduction removed is an assertion.
>
> Read it accordingly. **The reproduction in each entry reproduces the original
> defect against the original code**, and the diff in each entry is the
> *proposal*, not necessarily what landed. Almost none landed exactly as drawn:
> F2, F4, F16 and F17 were implemented somewhere else entirely, and F1, F3, F6,
> F7, F9, F13 and F15 differ in at least one respect. Where the shipped answer
> differs in a way worth knowing, the entry carries an **As it landed** note.
> `tests/test_security.py` is the record of what the code does now, the status
> column in §5 is the summary, and §3 is the current answer for a reader who
> wants only one.

### F1 — A page that will not render is reported as a page with nothing to find

**Severity: Critical.** This is the failure `SECURITY.md` promises does not
happen: *"Pages it could not check … is reported as unchecked. `stackroom check`
says so in as many words — that is not a clean bill of health — and it exits
non-zero."*

**Reproduce.** Build a PDF whose page tree declares `/Count 1` over three
`/Kids`. Poppler believes `/Count` and reports one page; pdfminer walks the kids
and reports three. The pipeline queues three jobs; pages 2 and 3 fail in
`render_page_crop` with *"has 1 pages; asked for page 2"*. Put a textbook failed
redaction on pages 2 and 3 and leave page 1 clean:

```
$ stackroom check ./release
Clear. 3 pages checked; no text found under any black box.
$ echo $?
0
```

The same two pages, in a file whose `/Count` is honest, are both reported.

**Impact.** A document class that Stackroom cannot rasterise is a document class
Stackroom silently blesses. `/Count` is only the cheapest trigger; anything that
makes `pdftoppm` fail while `pdfminer` succeeds — a timeout, a poppler crash, a
`/MediaBox` poppler refuses to allocate, a page index the two parsers disagree
about — has the same effect. The operator is told the collection is clean and
publishes.

**Patch.**

```diff
--- a/src/stackroom/pipeline.py
+++ b/src/stackroom/pipeline.py
@@
     try:
         image = _rasterise(job, source)
     except Exception as exc:  # one bad page must not stop 2,999 good ones
         outcome.error = f"could not render: {_describe(exc)}"
+        # A page we could not draw is a page we could not check. Saying nothing
+        # here is what turns an unreadable page into a clean bill of health.
+        outcome.analysis_failed = True
+        outcome.warnings.append(
+            "this page could not be rendered, so it was never checked for text "
+            "hidden under a black box"
+        )
         outcome.seconds = time.perf_counter() - started
         return outcome
```

and, for the second half of the same hole — a PDF whose text layer will not
parse but whose pixels render fine reaches `_analyse_redactions` with
`raw is None` and gets only the *visible*-redaction pass, while still reporting
`analysed=True`:

```diff
--- a/src/stackroom/pipeline.py
+++ b/src/stackroom/pipeline.py
@@ def _analyse_redactions(
     if raw is None and image is None:
-        return empty, True
+        return empty, False
     cropper = _in_memory_cropper(image) if image is not None else None
     try:
         if raw is not None:
             return redaction.analyse_page(raw, image, crop_renderer=cropper), True
         boxes = redaction.find_visible_redactions(image)
         ratio = redaction.redaction_ratio(boxes, [], (page.width_pt, page.height_pt))
         return (
             redaction.RedactionFindings(
                 redactions=boxes, hidden=[], ratio=ratio, ink_box=None, warnings=[]
             ),
-            True,
+            # No content stream means no hidden-text pass ran at all. That is
+            # correct and expected for an image file, and it is a hole for a PDF.
+            bool(getattr(page, "is_image", False)),
         )
```

The second hunk needs a way to tell "this was always an image" from "this PDF's
text layer would not parse"; the cleanest form is to pass `job.is_image` into
`_analyse_redactions` rather than sniffing it off the page.

**As it landed.** Both halves are in `pipeline.py`, close to the proposal: a
page that will not rasterise sets `analysis_failed` and appends *"this page
could not be rendered, so it was never checked for text hidden under a black
box"*, which F4's reporting then puts in front of the operator. `stackroom
check` exits 2 on unchecked pages and says in as many words that this is not a
clean bill of health.

---

### F3 — `CropBox` ≠ `MediaBox` points the pixel check at the wrong pixels

**Severity: Critical**, and unlike F1 this one fires on *ordinary* documents.
Every Acrobat "crop pages" and a great many scanners emit a `CropBox` that
differs from the `MediaBox`.

**Reproduce.** `pdfinfo` reports the **CropBox** as "Page size". `pdftoppm`
renders the **MediaBox**. `ingest/pdf.py` measures the page from pdfminer's
`LTPage` bbox, which is also the MediaBox. So:

```
MediaBox 0 0 1224 1584, CropBox 0 0 612 792, at 150 dpi:

  pdfminer says the page is        1224 x 1584 pt
  pdfinfo says the page is          612 x  792 pt
  page_geometry().pixel_size(150)  1275 x 1650 px
  what pdftoppm actually draws     2550 x 3300 px
  what render_page_crop returns    the top-left 1275 x 1650 of it
```

`render_page_crop(FULL_PAGE)` therefore returns *a corner of the page*, and every
box the redaction check asks to confirm is mapped into it with the wrong scale.
A test in `tests/test_security.py` builds the same page twice — once with a
`CropBox`, once without — and puts grey noise in the part of the MediaBox that
lies outside the CropBox, where no reader will ever see it and where the
mis-mapped crop happens to look. The control reports `###### #### #######`. The
cropped file reports nothing at all:

```
control.pdf: image=(2550, 3300)  hidden=['###### #### #######']  warnings=0
cropped.pdf: image=(1275, 1650)  hidden=[]                       warnings=1
```

and the one warning — *"box (6%, 55%) has 19 character(s) painted under it in the
content stream, but 19 of them stand out of their own cells in the rendered
page … check this box by hand"* — is discarded by F4 and never reaches the
operator. (Every one of the nineteen: the crop landed on grey noise, so no
character's own cell was flat.)

**Impact.** Beyond the leak: the published page image is a mis-cropped corner,
the redaction overlay boxes are drawn in the wrong places on the scan, the page
dimensions printed under the scan are wrong, and OCR runs on the wrong pixels.
On a `CropBox` *larger* in one axis the geometry is wrong in the other
direction. This is a correctness bug on real releases before it is a security
bug on crafted ones.

**Patch.** Make one module the authority on the page box and make both halves
use it. `pdftoppm` has `-cropbox`; using it makes poppler agree with `pdfinfo`,
and pdfminer then has to be told the same thing.

```diff
--- a/src/stackroom/ingest/raster.py
+++ b/src/stackroom/ingest/raster.py
@@ def _pdftoppm(
     argv = [
         "pdftoppm",
         "-r", str(dpi),
         "-png",
+        # pdfinfo measures the CropBox, so pdftoppm must draw it. Without this
+        # every crop coordinate computed from page_geometry() is in a different
+        # frame from the pixels it is applied to.
+        "-cropbox",
         "-f", str(first),
         "-l", str(last),
         *extra,
         str(pdf),
         str(prefix),
     ]
```

and in `ingest/pdf.py`, take the page box from the CropBox where one is present,
intersected with the MediaBox:

```diff
--- a/src/stackroom/ingest/pdf.py
+++ b/src/stackroom/ingest/pdf.py
@@ def read_page(handle: PdfHandle, index: int) -> RawPage:
     x0, y0, x1, y1 = page_bbox
-    width = abs(x1 - x0) or _mediabox_size(page, rotation)[0]
-    height = abs(y1 - y0) or _mediabox_size(page, rotation)[1]
+    # pdfminer runs the content stream against the MediaBox; poppler and every
+    # viewer show the CropBox. Boxes measured in one frame and confirmed in the
+    # other are the bug this line exists to prevent.
+    width, height = _display_size(page, rotation, page_bbox)
```

with `_display_size()` returning the CropBox intersected with the MediaBox,
`/Rotate` applied, falling back to the current behaviour when there is no
CropBox. `PDFPage` already exposes `.cropbox` alongside `.mediabox`, so no new
parsing is needed. Both halves are required: `-cropbox` alone moves poppler's
origin to the CropBox's lower-left while pdfminer keeps measuring from the
MediaBox's, which leaves the two frames just as far apart.

Verified against the installed poppler: `pdftoppm -cropbox` on the fixture above
renders 612 × 792, which is exactly what `pdfinfo` reports, and `PDFPage.cropbox`
returns `(0, 0, 612, 792)` against a `mediabox` of `(0, 0, 1224, 1584)`.

**As it landed.** `raster.py` passes `-cropbox` to every `pdftoppm` invocation
and `ingest/pdf.py` measures the page from the same box, so the two frames are
one rectangle. Four tests pin it, including one that builds the same page with
and without a `CropBox` and requires the words to land where the ink is.

Whatever the fix, **`test_the_rendered_frame_and_the_content_stream_frame_are_the_same`
is the assertion to make pass**: `read_page()`'s page size, `page_geometry()`'s
page size, and the size of the image `render_page_crop(FULL_PAGE)` returns must
all be the same rectangle.

---

### F16 — `safety.strip_metadata` does nothing

**Severity: High**, because it is worse than absent: an operator who sets it
believes something happened.

**Reproduce.** `grep -rn strip_metadata src/` returns exactly two lines, both in
`config.py`: the field and its docstring. `copy_originals()` does an
unconditional `shutil.copy2`. With `strip_metadata = true` set, the published
file is byte-identical to the source, `/Author` and `/Producer` included — and
so is every earlier revision an incremental save left behind. A test builds a
PDF whose revision 1 says `WITHHELD NAME: Jonathan Smith` and whose revision 2
says `WITHHELD NAME: [REDACTED]`; `pdftotext` on the published file shows the
redacted line, and `grep` shows the name.

**Impact.** Incremental-update history is one of the two or three commonest ways
a "corrected" release still contains what was withheld. The option that claims
to address it is a no-op, and `SECURITY.md` describes it as working.

**Patch.** Either implement it or delete it; leaving it is the worst option.
Implementation, using `pypdf`, which is already a dependency:

```diff
--- a/src/stackroom/build/site.py
+++ b/src/stackroom/build/site.py
@@ def copy_originals(self) -> None:
             destination = self.out / "files" / f"{doc.id}{Path(doc.filename).suffix}"
             destination.parent.mkdir(parents=True, exist_ok=True)
-            shutil.copy2(source, destination)
+            if self.cfg.safety.strip_metadata and doc.filename.lower().endswith(".pdf"):
+                stripped = _strip_pdf_metadata(Path(source), destination)
+                if not stripped:
+                    self.report.warnings.append(
+                        f"{doc.filename}: strip_metadata was asked for and could not "
+                        "be applied, so the file was published unchanged"
+                    )
+                    shutil.copy2(source, destination)
+            else:
+                shutil.copy2(source, destination)
```

```python
def _strip_pdf_metadata(source: Path, destination: Path) -> bool:
    """Rewrite *source* without its metadata or its revision history.

    A full rewrite, not an edit: writing a new file from the page tree is what
    drops the earlier revisions an incremental save left behind, which is the
    part that actually hides somebody. Returns False if the file could not be
    rewritten, because publishing a *silently* unstripped original is how this
    option becomes worse than not having it.
    """
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(str(source))
        if reader.is_encrypted:
            reader.decrypt("")
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.add_metadata({})
        with destination.open("wb") as fh:
            writer.write(fh)
        return True
    except Exception:
        return False
```

Two things the docs must then say, because both are true and neither is
obvious: a rewritten original **no longer matches the SHA-256 in the manifest**,
so the manifest has to record both digests; and stripping is *not* the same as
sanitising — annotations, attachments and form fields survive.

**As it landed.** Not in `build/site.py`. The rewrite is
`ingest.pdf.publish_pdf()`, which `copy_originals()` calls for every original,
stripping or not, and which returns the digest of what it actually wrote.
`manifest.json` carries `published_sha256` and `metadata_stripped` beside each
`sha256`, the about page explains the pair, and the files that could not be
rewritten are collected into one warning naming them rather than being published
silently unstripped.

---

### F2 — The published original keeps the attacker's file extension

**Severity: High.** Stored cross-site scripting in the archive's own origin.

**Reproduce.** `discover._classify` accepts a `%PDF-` header anywhere in the
first 1024 bytes, so a file can begin with `<!doctype html><script>…</script>`
and still be classified as a PDF. `copy_originals()` publishes it as
`files/<slug><Path(doc.filename).suffix>` — and the suffix comes from the
attacker's filename:

```
$ ls out/files/
annual-report.html
$ head -c 80 out/files/annual-report.html
<!doctype html><html><head><title>Annual report</title></head><body><script>
```

Every page of the document links to it as *"Download the original"*. GitHub
Pages, and `stackroom serve`, both serve `.html` as `text/html`.

**Impact.** The archive's pages carry a strict CSP (`default-src 'none'`,
`script-src 'self'`), which is why injection *into* the generated pages is inert.
A copied original carries no CSP at all, and it is same-origin with everything
else in the archive: session-less though the site is, script there can read
`localStorage`, rewrite what a reader is looking at through the archive's own
pages, or beacon the reader's identity out. `.svg`, `.xhtml` and `.xml` are the
same vector.

**Patch.** Derive the published extension from what the file *is*, not from what
it is called.

```diff
--- a/src/stackroom/build/site.py
+++ b/src/stackroom/build/site.py
@@
+# Extensions a static host will serve as active content in this archive's own
+# origin. A source file that arrives named like one of these is republished
+# under an extension that matches its detected type instead.
+ACTIVE_SUFFIXES = frozenset(
+    {".html", ".htm", ".xhtml", ".shtml", ".xml", ".svg", ".js", ".mjs", ".css"}
+)
+
+
+def _published_suffix(filename: str) -> str:
+    suffix = Path(filename).suffix.lower()
+    if suffix in ACTIVE_SUFFIXES or len(suffix) > 8 or not suffix[1:].isalnum():
+        return ".pdf" if suffix in ("", ".pdf") else ".bin"
+    return suffix
+
@@ def copy_originals(self) -> None:
-            destination = self.out / "files" / f"{doc.id}{Path(doc.filename).suffix}"
+            destination = self.out / "files" / f"{doc.id}{_published_suffix(doc.filename)}"
```

Better still, take the suffix from `SourceFile.kind` (`pdf` → `.pdf`, `image` →
the real format detected by magic), which removes the guesswork entirely — that
needs `kind` carried into `Document`, which is a one-field change to `model.py`.

**As it landed.** The "better still" version, not the blocklist:
`build/site.py:published_suffix(kind, source)` takes the extension from
`Document.kind` — what `discover._classify` decided from the magic number —
falling back to the file's own leading bytes, and to `.bin` for anything it
cannot identify. There is no list of dangerous extensions to keep up to date,
because the name is never consulted.

Two supporting measures worth taking anyway, because neither costs anything:

- ship a `files/.htaccess`-equivalent where the host supports it, and document
  `add_header Content-Disposition attachment` for `/files/` in
  `docs/PUBLISHING.md`;
- add `download` (already present) *and* serve originals from a distinct path
  so a future host can be told to treat the directory as opaque.

---

### F4 — Every per-page warning is computed and thrown away

**Severity: High**, because of what the warnings say.

**Reproduce.** `PageOutcome.warnings` is appended to in five places in
`pipeline.py` and read in none. `grep -n "\.warnings" src/stackroom/cli.py
src/stackroom/build/site.py` finds only `IndexInfo.warnings` (pagefind) and
`BuildReport.warnings` (originals not published). The messages that vanish
include:

- *"the redaction check failed on this page (…), so we do not know whether
  anything is hidden underneath a black box here"*
- *"box (6%, 55%) has 19 character(s) painted under it in the content stream,
  but 19 of them stand out of their own cells in the rendered page, so the text
  is probably visible … Not reported as hidden — **check this box by hand**"*
- *"N hidden-text finding(s) rest on the content stream alone; no page rendering
  was available to confirm them"*
- *"could not read the text layer: …"*

`SECURITY.md` says *"Ambiguous evidence is reported for a human rather than
resolved silently."* It is resolved silently.

**Patch.** Carry them to the CLI. `check_safety` already returns two lists; give
it a third, or return the outcomes.

```diff
--- a/src/stackroom/cli.py
+++ b/src/stackroom/cli.py
@@ def build(...):
     collection, outcomes = _ingest(source, cfg, out, workers)
     findings, unchecked = _safety(outcomes, cfg)
+    _page_notes(outcomes)
@@
+def _page_notes(outcomes) -> None:
+    """Everything ingest wanted a person to know, grouped so it is readable.
+
+    Printed after the safety gate and before the site is written, because that
+    is the last moment an operator can act on it. Grouped by message rather
+    than listed per page: 400 copies of one sentence is not a report.
+    """
+    grouped: dict[str, list[str]] = {}
+    for outcome in outcomes:
+        if outcome.error:
+            grouped.setdefault(outcome.error, []).append(f"{outcome.doc_id} p{outcome.number}")
+        for warning in outcome.warnings:
+            grouped.setdefault(warning, []).append(f"{outcome.doc_id} p{outcome.number}")
+    if not grouped:
+        return
+    console.print()
+    for message, where in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
+        shown = ", ".join(where[:4]) + (f" and {len(where) - 4} more" if len(where) > 4 else "")
+        console.print(f"  [yellow]{escape(message)}[/]\n    [dim]{escape(shown)}[/]")
```

and the same call in `check`, where it matters more.

**As it landed.** `cli._page_notes`, called from `build`, from `check` and from
`compare`; and on the error stream beside the leak report when a build stops,
because half of what stops a build is *we could not check this page* and the
operator's next question is always *which pages*. Notes are grouped by message
rather than listed per page, safety notes sort first, and the list is capped at
twelve distinct messages with a count of the rest.

---

### F7 — Filtered findings publish the text they filtered

**Severity: High**, and it fires in the **default** `stop` mode.

**Reproduce.** A page with three black boxes over three bare dates. `redaction.
_all_dates()` suppresses the findings (documented: *"a page whose boxes all hide
dates is treated as a form"*). `outcome.hidden` is therefore empty, so
`pipeline.process_page` never calls `_drop_hidden`, so the dates under the boxes
go into `page.words`:

```
$ stackroom build ./dates -o out ; echo $?
0
$ published page text: 03/14/2019 07/02/2019 11/30/2020 the of and to a in …
```

The scan shows three black boxes. The transcription beside it, and the search
index, show what is under them. The same happens for every string
`_is_real_text()` rejects — `confidential`, `privileged`, `redacted`, a run of
one repeated character.

**Impact.** `SECURITY.md` says *"Text found under a failed redaction is never
published — not in the HTML, not in the JSON, not in the search index."* Here it
is published under the default settings, with the build exiting 0.

**Patch.** Separate *what to report to the operator* from *what to withhold from
the site*. `_scan_hidden` already computes both — `obliterates` is the second
one — so the fix is to carry the covered text out even when the finding is
filtered:

```diff
--- a/src/stackroom/ingest/redaction.py
+++ b/src/stackroom/ingest/redaction.py
@@ class RedactionFindings:
     hidden: list[HiddenText] = field(default_factory=list)
     """Boxes that did not. Any entry here should stop the build."""
+
+    covered: list[Box] = field(default_factory=list)
+    """Every box that obliterated characters, findings or not.
+
+    Reporting and withholding are different questions. A box over a bare date
+    is a form rather than a leak and does not need to wake anybody up - but the
+    date is still under a black box, and publishing our transcription of it
+    puts back exactly what the box removed."""
```

```diff
--- a/src/stackroom/pipeline.py
+++ b/src/stackroom/pipeline.py
@@
-    if outcome.hidden:
-        words = _drop_hidden(words, outcome.hidden)
+    # Withhold everything under a box that obliterated text, not only the
+    # findings we decided were worth reporting.
+    if findings.covered:
+        words = _drop_hidden(words, findings.covered)
```

**As it landed.** As proposed, and F8's fix went in on the same call:
`_drop_hidden` now takes the `covered` boxes and drops any word a box
*intersects at all*, rather than one it mostly overlaps. The two findings are
one line of code between them, and the second is why the first is worth having.

---

### F8 — A box over half a word publishes the whole word

**Severity: High** in `warn` / `--unsafe-publish-leaks` mode, where the CLI
explicitly promises *"Stackroom will still keep the recovered text out of the
site."*

**Reproduce.** Draw `ALPHABRAVOCHARLIEDELTAECHO` and cover its first ten
characters with a box. The finding is assembled from *characters* at ≥80%
coverage, so `check` correctly reports ten hidden characters:

```
Document         Page  Length  Shape
09-partial-word     1      10  ##########
```

`_drop_hidden` then removes *words* at >80% coverage. The word is 38% covered,
so it survives, and the published page reads:

```
ALPHABRAVOCHARLIEDELTAECHO the of and to a in that is …
```

**Impact.** The half of the token that was under the box is published in the
HTML and in the search index. Names, case numbers and email addresses are
exactly the tokens a redaction lands halfway across.

**Patch.** Drop a word that overlaps a covered box *at all*, not one that is
mostly inside it. Withholding a word that is only clipped by a box costs a
reader one word; publishing half a redacted name costs somebody rather more.

```diff
--- a/src/stackroom/pipeline.py
+++ b/src/stackroom/pipeline.py
@@ def _drop_hidden(words, hidden):
-    """Remove every token that lies under an opaque shape."""
+    """Remove every token that any opaque shape touches.

+    Any overlap at all, not a majority of one. Findings are assembled from
+    characters that are 80% covered, so a box across half a token yields a
+    finding for that half; a word-level majority test then keeps the token and
+    publishes the covered half with it.
+    """
     boxes = [h.box for h in hidden]
     kept: list[Word] = []
     for word in words:
-        if any(word.box.overlap_ratio(b) > 0.8 for b in boxes):
+        if any(word.box.intersection(b) is not None for b in boxes):
             continue
         kept.append(word)
     return kept
```

---

### F5 — A symlink is followed out of the source folder

**Severity: Medium.** `SECURITY.md` lists *"a symlink followed out of the source
folder"* as in scope.

**Reproduce.** `discover()` prunes symlinked *directories* (`os.walk(...,
followlinks=False)`) but `absolute.is_file()` follows a symlinked *file*:

```
release/appendix-b.pdf -> /home/operator/private/not-for-publication.pdf
```

is hashed, ingested, rendered and copied byte for byte into
`out/files/appendix-b.pdf`.

**Impact.** A tarball or a git checkout can carry symlinks; a zip usually
cannot. The attacker has to guess a path, which limits this — but `/etc/` and
predictable home-directory paths are guessable, and the more realistic version
is an operator who assembles a release folder with symlinks into their own
archive and does not expect the targets to be republished.

**Patch.**

```diff
--- a/src/stackroom/ingest/discover.py
+++ b/src/stackroom/ingest/discover.py
@@ def discover(root, ...):
     root = Path(root)
     if not root.is_dir():
         raise NotADirectoryError(f"{root}: not a directory")
+    real_root = root.resolve()
@@
             absolute = here / filename
-            if absolute.is_symlink() and not absolute.exists():
-                continue  # a broken link is not a document
+            if absolute.is_symlink():
+                # A link is not a document. Following one publishes a file the
+                # operator never put in the release - and the target of a link
+                # in someone else's folder is chosen by someone else.
+                try:
+                    target = absolute.resolve(strict=True)
+                except OSError:
+                    continue
+                if real_root not in target.parents:
+                    skipped_links.append(absolute)
+                    continue
             if not absolute.is_file():
                 continue
```

with the skipped links surfaced in the `skipped` list (`reason="a symbolic link
pointing outside the folder"`), because silently dropping a file is the other
failure this module is careful about.

---

### F6 — The pixel budget is not on the path the build uses

**Severity: Medium.**

**Reproduce.** `RenderSpec.max_pixels` is read in exactly one place,
`_effective_dpi()`, which is called from exactly one place, `render_pdf()` —
which the pipeline never calls. `pipeline._rasterise()` calls
`raster.render_page_crop(source, n, FULL_PAGE, dpi=job.dpi)`, and
`render_page_crop` computes its crop from `geometry.pixel_size(dpi)` with no
budget at all. `encode_page()` receives a `RenderSpec` carrying `max_pixels` and
never looks at it.

So `render.max_megapixels`, which the config file documents and validates, has
no effect on a build.

**Measured.** A 200-inch square page (`/MediaBox [0 0 14400 14400]`) asks
poppler for 30000 × 30000 px. Poppler refuses with *"Bogus memory allocation
size"*, exit status 0, and a **1 × 1 PNG** — which `render_page_crop` cannot
distinguish from a successful render, so the page is published as a one-pixel
scan with no error and, per F4, no warning. Below poppler's own ceiling the
budget is simply absent: an 80-inch page renders 12000 × 12000 = 144 megapixels,
about 430 MB resident in poppler and again in Pillow, and Pillow's
`DecompressionBombError` above ~179 MP turns into F1's silent clean bill of
health.

**Patch.**

```diff
--- a/src/stackroom/ingest/raster.py
+++ b/src/stackroom/ingest/raster.py
@@
-def render_page_crop(pdf: Path, page: int, box: Box, dpi: int = 150) -> Image.Image:
+def render_page_crop(
+    pdf: Path, page: int, box: Box, dpi: int = 150, *, max_pixels: int | None = None
+) -> Image.Image:
@@
     page_w, page_h = geometry[page - 1].pixel_size(dpi)
+    # The budget has to be here, not only in render_pdf(): this is the function
+    # the pipeline actually calls, and a poster-sized page asks for gigabytes.
+    if max_pixels and page_w * page_h > max_pixels:
+        dpi = max(1, math.floor(_effective_dpi(geometry[page - 1],
+                                               RenderSpec(dpi=dpi, max_pixels=max_pixels))))
+        page_w, page_h = geometry[page - 1].pixel_size(dpi)
@@
         png = prefix.with_suffix(".png")
         if not png.exists():
             raise RenderError(...)
+        # Poppler answers an impossible allocation with a 1x1 image and exit 0.
+        # Treating that as a rendered page publishes a one-pixel scan and clears
+        # the redaction check on a page nobody looked at.
+        if width > 4 and height > 4:
+            with Image.open(png) as probe:
+                if probe.width <= 1 or probe.height <= 1:
+                    raise RenderError(
+                        f"pdftoppm returned a {probe.width}x{probe.height} image for a "
+                        f"{width}x{height} crop of page {page}: it refused the allocation"
+                    )
```

with `pipeline._rasterise` passing `max_pixels=int(job.max_megapixels * 1e6)`.

**As it landed.** `render_page_crop` takes a keyword-only `max_pixels`, lowers
the resolution until the page fits, and raises `RenderError` when poppler
answers an impossible allocation with a 1×1 image and exit 0 — which used to be
published as a one-pixel scan that had cleared the redaction check. Two tests
pin it: one that the budget applies on the path the build uses, and one that the
refusal is an error rather than a scan.

---

### F15 — Invisible text is promoted into the published page

**Severity: Medium.** Not in `SECURITY.md`'s list of known limits, and it should
be, alongside a clearer statement of what the tool *does* with such text.

**Reproduce.** A page that paints `SOURCE NAME DELTA` in render mode 3 (`3 Tr`,
invisible) and `[REDACTED]` visibly on top. pdfminer does not implement render
mode, so the invisible run is extracted; there is no rectangle, so the check
does not fire; and the published page body reads
`S[ROEUDRACCET ENDA]ME DELTA` — both runs, interleaved by the word grouper.

**Impact.** Render mode 3 is how every OCR-under-image PDF is built, so this
cannot simply be dropped. But it is *also* how a bad redaction tool leaves the
original text behind after stamping a replacement over it, and Stackroom turns
that from "recoverable with `pdftotext`" into "on the page, in the search index,
crawlable".

**Patch.** Two parts, and the second matters more than the first.

1. Record it. `ingest/pdf.py` can capture the text render mode from the
   graphics state in `render_char` and put it on `RawChar`, so a page whose
   *visible* glyphs and *invisible* glyphs disagree can be flagged:

```diff
--- a/src/stackroom/ingest/pdf.py
+++ b/src/stackroom/ingest/pdf.py
@@ class RawChar:
     fontname: str
     size: float
+    invisible: bool = False
+    """Painted in text render mode 3. Normal under an OCR layer, and one of the
+    shapes a failed redaction takes when a tool stamps over the original."""
```

2. Say so. Add to `SECURITY.md`'s "What gets past it":

   > **Text that is in the file but not on the page.** Invisible text (render
   > mode 3), text clipped away, text outside the crop box, white on white. The
   > check is anchored on rectangles and finds none of these — and Stackroom
   > publishes that text as the page's transcription and indexes it for search.
   > If a document's text layer disagrees with what a reader sees, the archive
   > publishes the text layer.

**As it landed.** Both halves. Invisible text over blank paper is withheld from
the page and the index and the build says so, while an invisible OCR layer
*under a scan* — which is how every searchable scan is built — is still
published, because the discriminator is whether there is ink where the words
claim to be. `SECURITY.md`'s limits list carries the paragraph above.

---

### F17 — A filename that is not UTF-8 kills the build

**Severity: Medium.** Availability, and it destroys work: `_prepare_out()` has
already emptied the output directory by the time this fires.

**Reproduce.** A zip made on Windows with a cp1251 filename unpacks on Linux to
a name containing surrogate escapes (`\udcff`). That name reaches
`Document.filename` and `Document.title`, then `SiteBuilder.write`:

```
UnicodeEncodeError: 'utf-8' codec can't encode characters in position 2502-2503:
surrogates not allowed
```

with a full Rich traceback, absolute paths and all. `tests/fuzz` reproduces this
independently as the `undecodable-filename` hazard.

**Patch.** Normalise once, where names enter the model.

```diff
--- a/src/stackroom/ingest/discover.py
+++ b/src/stackroom/ingest/discover.py
@@
+def _printable(name: str) -> str:
+    """A name that can be written to a UTF-8 file.
+
+    os.walk hands back undecodable bytes as surrogate escapes, which every
+    string operation accepts and str.encode('utf-8') refuses - so the failure
+    lands three modules away, at the moment the site is written.
+    """
+    return name.encode("utf-8", "replace").decode("utf-8")
```

applied to `SourceFile` construction, and a matching belt-and-braces guard in
`SiteBuilder.write`:

```diff
-        data = text.encode("utf-8")
+        # Never fail here: by this point the output directory has been emptied
+        # and the whole ingest has already run.
+        data = text.encode("utf-8", "replace")
```

**As it landed.** `discover.printable()`, applied where names enter the model,
and used by `cli._load_config` for the folder name as well. The path on disk is
untouched: this is for the name shown, never for the name opened.

---

### F13 — Configuration is taken from anywhere above the documents

**Severity: Medium.**

**Reproduce.** `config.find()` walks `here` and every one of `here.parents` up to
the filesystem root, returning the first `stackroom.toml` it meets. During this
review a `stackroom.toml` written by an unrelated process two directories above
the release folder silently governed a build and aborted it with
`jurisdiction = 'au' is not one of …`. A file the operator has never seen — in
`/tmp`, in a shared parent, in their home directory — decides the title,
the jurisdiction, whether originals are published, and the OCR timeout.

**Patch.** Look beside the documents and one level up, and say which file was
used.

```diff
 def find(start: Path) -> Path | None:
-    """Look for ``stackroom.toml`` beside the documents, then above them."""
+    """Look for ``stackroom.toml`` beside the documents, and one level up.
+
+    Not all the way to the root. The configuration decides whether originals
+    are published and how long a subprocess may run, and a file the operator
+    has never opened should not be able to decide those.
+    """
     start = Path(start).resolve()
     here = start if start.is_dir() else start.parent
-    for candidate in (here, *here.parents):
+    for candidate in (here, here.parent):
         found = candidate / CONFIG_NAME
         if found.is_file():
             return found
-        if candidate == here.anchor:  # pragma: no cover - filesystem root
-            break
     return None
```

and print the path in the build report, so `Using stackroom.toml from …` is
visible when it is not the one the operator expected.

**As it landed, and why it is only half.** `MAX_CONFIG_DEPTH` is three rather
than one, because `stackroom build papers/2019/march` genuinely has to find
`papers/stackroom.toml` and `tests/test_config.py` pins that. Three levels is
out of reach of a home directory or `/tmp` from anywhere real, and no depth at
all can separate *the collection root* from *somebody else's directory*: the
two cases are the same shape on disk. So the rest of the answer is visibility —
`cli._load_config` prints the file it used and, when it came from outside the
folder the operator named, says so in yellow and tells them to pass `--config`.

---

### F12 — `_prepare_out()` claims other people's website directories

**Severity: Medium**, destructive, operator-triggered.

**Reproduce.** `looks_ours` is true when the directory contains `.stackroom`,
**or `manifest.json`, or `.nojekyll`**. `manifest.json` is the standard filename
for a Web App Manifest; `.nojekyll` is present in every GitHub Pages site that
has ever needed it. `stackroom build ./release -o ~/my-website` on either empties
it without a prompt.

**Patch.** Require the marker Stackroom writes, and treat the other two as
evidence only when they come with something Stackroom certainly wrote.

```diff
-        looks_ours = (
-            marker.is_file()
-            or (out / "manifest.json").is_file()
-            or (out / ".nojekyll").is_file()
-        )
+        # The marker is the only file that means "stackroom wrote this".
+        # manifest.json is also what a Web App Manifest is called, and .nojekyll
+        # is in every GitHub Pages site; either alone is somebody else's work.
+        looks_ours = marker.is_file() or (
+            (out / "manifest.json").is_file()
+            and (out / "assets" / "stackroom.css").is_file()
+        )
```

---

### F10 — A supplied configuration can switch off the OCR timeout

**Severity: Low**, and it needs the release to ship a `stackroom.toml` — which
this threat model treats as ordinary.

**Reproduce.** `config._validate()` bounds `render.dpi`, `render.widths`,
`render.formats`, `ocr.languages`, `ocr.mode`, `safety.hidden_text`,
`jurisdiction` and `base_url`. It does not bound `ocr.timeout`, `ocr.psm` or
`search.min_query`. `pytesseract.timeout_manager` begins `if not seconds:` and
runs `proc.communicate()` with no timeout at all, so `[ocr] timeout = 0` removes
the bound entirely and `timeout = 1e9` removes it in practice.

**Patch.**

```diff
+    if not 1.0 <= cfg.ocr.timeout <= 3600.0:
+        raise ConfigError(
+            f"{path}: ocr.timeout = {cfg.ocr.timeout} is outside 1-3600 seconds.\n"
+            "  0 does not mean 'no limit' here - it means tesseract is never "
+            "interrupted, and one page can stop the build for ever."
+        )
+    if not 0 <= cfg.ocr.psm <= 13:
+        raise ConfigError(f"{path}: ocr.psm = {cfg.ocr.psm} is outside 0-13.")
+    if not 1 <= cfg.search.min_query <= 20:
+        raise ConfigError(
+            f"{path}: search.min_query = {cfg.search.min_query} is outside 1-20."
+        )
```

---

### F9 — Cubic backtracking in `EXEMPTION_RE`

**Severity: Low**, because it is currently unreachable — but it is unreachable
by accident, not by design.

**Measured**, on `"(b" + " " * n + "x"`:

| n | time |
|---|---|
| 200 | 41 ms |
| 400 | 259 ms |
| 800 | 2.23 s |
| 1,600 | 16.6 s |
| 3,200 | > 2 minutes |

Roughly cubic. The shape is `[bB6&]\s*[\)\]\}]?\s*[-–—]?\s*[\(\[\{]`: two
unanchored `\s*` runs separated by an optional single character, inside a
pattern applied at every position by `finditer`.

**Why it is not reachable today.** Page text is `" ".join(w.text for w in
page.words)`, and both word sources strip whitespace: pdfplumber's
`extract_words` splits on any character `str.isspace()` accepts — which is
exactly the set Python's `re` calls `\s`, U+00A0 and U+001C included — and
Tesseract's TSV rows are `.strip()`ed and dropped when empty. So no `Word.text`
can carry a `\s` run, and consecutive single spaces are linear.
`test_the_word_extractor_drops_every_character_re_calls_whitespace` pins that
dependency explicitly, because it is load-bearing and invisible.

**Patch.** Make the whitespace runs possessive, which costs nothing and removes
the class:

```diff
-    [\(\[\{]?\s*[bB6&]\s*[\)\]\}]?
-    \s*[-–—]?\s*
+    [\(\[\{]?\s*+[bB6&]\s*+[\)\]\}]?
+    \s*+[-–—]?\s*+
```

Possessive quantifiers need Python 3.11; on 3.10 the equivalent is an atomic
group `(?>\s*)` — also 3.11 — so on 3.10 use a bounded repeat, `\s{0,8}`, which
is more than any real stamp contains. Whichever form is chosen, cap the scanned
text: `scan_document` should refuse a page longer than, say, 200 KB rather than
scanning it, because a bound on the input is the only defence that survives a
change of word grouper.

---

### F11, F14, F18, F19 — the short ones

- **F11 — Windows device names.** `slugify("CON")` is `"con"`, and
  `d/con/index.html` cannot be created on Windows. Fix: after slugification,
  suffix a reserved stem — `{"con","prn","aux","nul"} | {f"com{n}"} |
  {f"lpt{n}"}` — with `-doc`.
- **F14 — `plain_text()` and unterminated comments.** `render_markdown` strips
  `<!--.*?(?:-->|\Z)`; `plain_text` strips only `<!--.*?-->`. Nothing calls
  `plain_text` today, so this is latent — but the obvious future caller is the
  `<meta name="description">`, which is the one place an operator's private note
  would be most damaging. Fix: use the same pattern in both.
- **F18 — the missing `--i-know`.** `ARCHITECTURE.md` and
  `build/search.py:145` both say the CLI refuses above 50,000 pages without
  `--i-know`. It does not; there is no such flag and no such check. Fix: add it,
  or delete both sentences. A limit that is documented and unenforced is worse
  than one that is neither.
- **F19 — `check` writes.** The README says `stackroom check` "builds nothing
  and writes nothing". It renders and encodes every page into
  `tempfile.TemporaryDirectory()`, which is `$TMPDIR`. The images do not contain
  the hidden text — it is under an opaque box — but a tool people run on
  documents that must not touch disk should say what it writes and where, and
  ideally take `--scratch` so it can be pointed at a ramdisk.

**As those four landed.** F11: `slugify` suffixes a reserved stem with `-doc`.
F14 is the one finding in this document that is still open — see the note under
§5. F18: `cli._page_ceiling` refuses above `search.DEGRADED_PAGES` without
`--i-know`, and the test asserts the flag exists as well as that the check
fires. F19: `check` prints the folder it renders into on every run, takes
`--scratch`, and deliberately uses no cache in either direction, so the images
it makes live only where it said they would. F9 took the bounded-repeat form
rather than possessive quantifiers, because the project supports Python 3.10;
`\s{0,8}` throughout, plus `exemptions.MAX_SCAN_CHARS = 200_000` as the bound
on the input that survives a change of word grouper.

---

## 7. What I could not test

Being honest about the edges of this review.

- **Real documents.** Everything here is synthetic. A review against a hundred
  real agency releases would find failure modes no generator will produce —
  which is precisely why `SECURITY.md`'s request for real documents that break
  it is the right ask.
- **Windows and macOS.** F11 is derived from the reserved-name rules, not
  observed. Path-length limits, case-insensitive slug collisions on APFS/NTFS
  (two documents named `Memo.pdf` and `memo.pdf` fold to one slug *and* one
  filename) and `_prepare_out`'s behaviour with junction points are all
  untested here.
- **Concurrency.** The `ProcessPoolExecutor` path was not exercised; every test
  runs `workers=1`, because the pool hides tracebacks. A leak that only appears
  when a worker dies mid-page — `HiddenText` is pickled through a pipe to the
  parent — is not covered. Whether that pipe's buffer can reach swap is a
  question about the operating system that I did not attempt to answer, and it
  is the one place `HiddenText.text` is not purely in one process's memory.
- **Pagefind itself.** The index is built by a Rust binary over the generated
  HTML. I verified that recovered text reaches `.pf_fragment` files when it
  reaches the HTML, and that no third-party origin appears in the output, but I
  did not fuzz pagefind or audit its wasm.
- **Tesseract and poppler as attack surface.** Both are C/C++ parsers run on
  attacker bytes. They are invoked correctly — argv, timeouts, no shell — and a
  crash is contained by the process boundary, but memory-safety bugs in them are
  memory-safety bugs in the build. Running them under a seccomp profile or in a
  container is the operator's job, and `docs/PUBLISHING.md` should say so.
- **The generated JavaScript.** `assets/js/` was being actively rewritten by
  other work while this review ran, and has grown since: eight files now, plus
  a generated `assets/i18n.js` carrying the interface's translated strings. I
  checked the DOM sinks (`innerHTML`/`insertAdjacentHTML`) against their
  escaping helpers and found them correct at the time of reading, and the page
  CSP (`script-src 'self'`, no `unsafe-inline`) makes injection into those sinks
  inert — but that is a point-in-time observation, not a property, and it is
  older than the code it describes.
- **The comparison path.** `stackroom compare` did not exist when this review
  ran. It reads a second attacker-controlled folder, publishes text from it,
  and writes thumbnails of its pages into the site; `tests/test_compare.py`
  asserts that no text either release hid under a black box reaches the output,
  and that a leak in the *earlier* folder stops the build too. Nobody has
  attacked it on purpose.
- **The page cache.** Also newer than this review. `docs/CACHING.md` §5 and §9
  are its own account of what it refuses to store and how it degrades; the
  claim that recovered text never reaches it is tested by grepping the cache
  directory for a planted secret, its lowercase form, its first ten characters
  and its `redacted_repr()` shape. That is a good test and it is not the same
  thing as somebody having tried to break it.
- **Timing and traffic analysis of a published archive.** Out of scope: a
  reader's host sees which pages they fetch, and no static site can fix that.

## 8. If you are deciding whether to trust this

The honest summary, as of 2026-09-01, in the order I would want to hear it.

**The architecture is right.** The safety gate sits between ingest and build so
that nothing can be written before it has run; the recovered text is held in one
place with a shape-preserving representation for display; the templates
autoescape; the subprocesses are argv lists with timeouts; the site fetches
nothing from anywhere. Most of what this review tried bounced, and the parts
that did not have since been closed.

**The two defects that would have let a source be exposed while the tool said
"Clear" are fixed.** F1 — a page that will not render was silently blessed — now
marks the page unchecked, says so, and makes `check` exit non-zero. F3 — a
`CropBox` disagreeing with the `MediaBox` pointed the pixel check at a corner of
the page — is closed by rendering and measuring the same box, which matters
because that one fired on ordinary documents rather than crafted ones. F4, which
made both dangerous rather than merely wrong, now prints every per-page note to
the operator.

**`stackroom check` reporting "Clear" still means "nothing of one specific kind
was found on the pages it could read".** That is a property of the check, not a
defect in it, and the difference from before is that the operator is now told
which pages it could not read. §4 and `SECURITY.md` list what gets past it.

**The mitigation that did nothing now does something.** `safety.strip_metadata`
(F16) rewrites each published PDF from its page tree, dropping `/Info`, XMP and
the incremental-update history, and records both digests in the manifest. It is
still not a sanitiser: annotations survive it.

**Publishing an archive from documents you have not opened is safer than it
was.** The published extension comes from the file's own bytes (F2), so a source
file named `.html` is published as `.pdf` and cannot run as script in your
archive's origin.

**Two things are still open.** F13 is half-fixed and the other half cannot be
fixed by a rule about paths, so it is answered by telling the operator which
configuration file was used. F14 is latent: two comment-stripping patterns
disagree, and nothing calls the weaker one yet.

**One thing outside these findings is worth knowing.** The build is
reproducible, and `SOURCE_DATE_EPOCH` makes it reproducible down to the build
timestamp, so two people can compare archives byte for byte and expect no
differences at all. Publish the value you used.
`docs/ARCHITECTURE.md` guarantee 6 says how.

**None of this was a reason to go back to a black-box service.** Every finding
here was findable because the tool is a folder of readable Python that publishes
a folder of readable files, and every one of them was fixable in a few dozen
lines. That property is worth more than the bugs cost.
