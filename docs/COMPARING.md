# Comparing two releases

> This document is for somebody about to publish a claim about an agency. It
> says what `stackroom compare` does, how it decides which page is which, what
> it will and will not assert, and — at the end, at length — how it can be
> wrong. If you are going to quote a finding from this tool in print, read
> section 6 first.

---

## 1. The situation

An agency answers a request, withholds half of it, loses an appeal, and sends
the pages again with less blacked out. A court unseals exhibits in stages. The
National Security Archive requests the same Rwanda memo four times over twelve
years and gets four different sets of black boxes back; putting the four
productions beside each other shows text that one reviewer withheld and another
let through, and that text is the story.

Doing this by hand is a career's worth of work. No free tool does it.

```
stackroom compare ./release-2019 ./release-2024 -o site
```

## 2. What comes out, and why that shape

**The new release, as an ordinary Stackroom archive, plus a `compare/`
section.** Not a standalone "diff site", and not a comparison of two
already-built archives. Three reasons, in order of how much they matter:

- **A finding has to lead back to the page.** "This sentence was blacked out in
  2019" is worth nothing unless the reader can open the 2024 scan and see it.
  Those pages only exist in the archive of the new release, so the comparison
  has to live in the same tree as them.
- **A built archive does not hold enough to compare.** By design: the published
  site carries word *boxes* as JSON and the words themselves only as HTML, and
  it carries no machine-readable redaction geometry at all. Comparing two built
  sites would mean scraping our own HTML and would break the first time a
  template changed. The comparison runs on `Collection` objects, which is the
  contract every other module in this project writes against.
- **The operator's sentence is one sentence.** "Here is this morning's release,
  and here is what is new in it." That is one artefact.

The earlier release is read into a temporary folder and deleted on the way out.
The only part of it that is published is a thumbnail of each page where
something changed, so a reader can look at both sheets. Pass `--no-old-scans`
to withhold even those.

> **The earlier folder is published from, too.** Text that the 2019 release
> printed in the clear and the 2024 release covered is quoted on the comparison
> page — that is the whole point of the "newly withheld" finding — and it comes
> out of the 2019 folder. Only point `compare` at documents you are willing to
> publish. An unredacted draft you happen to have is not an earlier release.

## 3. Deciding which page is which

This is the hard part, and it is where a wrong answer does the damage: every
finding on a page inherits the correctness of the pairing that page rests on.

### 3.1 Four signals, and when each is worth anything

| Signal | What it is | Strong when | Useless when |
|---|---|---|---|
| **Control number** | The Bates stamp, punctuation folded out | both productions use the *same* numbering | the second production was re-stamped |
| **Text** | Bottom-*k* sketch of the page's character 6-grams, over OCR-folded tokens | both copies were read | either copy is blank, dark, or blacked out |
| **Word order** | Every adjacent pair of tokens, hashed whole | the vocabulary is repetitive | as above |
| **Layout** | An 8×12 occupancy grid over words *and* boxes | always available | two pages of the same shape |
| **Image** | 64-bit difference hash of the rendered page | neither copy could be read | the page was re-scanned very differently |

A channel is used only when it is *available for that pair*, and its absence is
recorded rather than scored as zero. "We could not read it" and "it is a
different page" are different findings, and collapsing them is exactly how an
aligner ends up confidently wrong.

**The control number is asked a question first**, and this is the detail most
easily got wrong. Two productions can both carry stamps under *different*
schemes, in which case equal numbers would be coincidence and unequal ones prove
nothing. `bates_regime()` answers `shared`, `disjoint` or `absent` by looking at
whether the two sets of stamps overlap at all, and the numbers are only believed
in the `shared` case. The built page says which answer it got.

**Text is compared with character 6-grams, not words and not 4-grams.** Six is
measured, not chosen. Character n-grams survive OCR noise, which is
per-character: one misread letter costs six shingles out of a page's two
thousand where a word shingle would cost the whole word. Four-grams saturate on
a repetitive production — most four-character windows lie inside a word, every
page uses every word — so two entirely different pages of a form-heavy release
score 0.56 alike, which is too close to two copies of one page under a bad scan
(0.69) to tell anything from. At six:

| shingle | same page | 30% token error | a different page | separation |
|---:|---:|---:|---:|---:|
| 4 | 1.00 | 0.69 | 0.56 | 0.13 |
| **6** | **1.00** | **0.59** | **0.35** | **0.25** |
| 8 | 1.00 | 0.65 | 0.21 | 0.44 |

Eight separates better and is not used, because that table's noise model
substitutes characters while real recognition also merges and splits words, and
a merge destroys every shingle that spans it.

**Word order is the backstop for the case that defeats everything else**: two
different documents that share a vocabulary, a margin and a line spacing. Their
adjacent token pairs overlap by about 3%; two copies of one page read at 45%
token error still share 19%. When both copies of the pages carry text and the
mean order similarity is under 0.10, the comparison is *refused*.

### 3.2 Pairing the documents first

Filenames change between productions, so the name is a tiebreak and never the
evidence. What decides a pairing is: an identical SHA-256 (the agency re-sent
the same file, and nothing is aligned or claimed); otherwise the share of
control numbers the two files have in common; otherwise the two documents'
whole text and word order together, with the filename worth about a fifth.

A pair is only made when it is the best available option **for both documents**,
so a short covering letter cannot be swallowed by whichever long document
happens to share the most vocabulary with it.

One special case: two files with the *same name* are paired at 0.45 even when
nothing else supports it. The most useful thing that can be said about two files
called `part1.pdf` that cannot be lined up is that they have the same name and
could not be lined up — which is what the page alignment then says. Leaving them
in two "no counterpart" lists would assert they are unrelated documents, and
that is a different and probably false claim.

### 3.3 The sequence

Similarities go into a **Needleman–Wunsch global alignment** over the two page
sequences. A monotone alignment is the point: a run of twelve inserted pages
costs twelve gaps and the alignment then carries on correctly, where a
nearest-neighbour matcher desynchronises and mis-pairs every page after them.
Each pair contributes `similarity − 0.45` and each skipped page costs `0.05`,
so two gaps beat any pair below about 0.35 and the aligner walks past a bad
match rather than taking it.

Three passes run on top of it:

1. **Release the dominated.** A monotone alignment cannot represent a swap: two
   pages that changed places look to it like two ordinary pairings that are each
   somewhat wrong, and on a repetitive document "somewhat wrong" still scores
   above the floor. Any pair scoring 0.15 below an available rival is unpicked.
2. **Find the moves.** Whatever is now unmatched on both sides is tested
   pairwise for a mutual best above 0.72 — much higher than the ordinary floor,
   because an out-of-order match no longer has position vouching for it. Those
   pairs are marked `moved` on the built page. Anything the pass cannot place is
   put back at the lowest confidence rather than being left as one removal and
   one addition, because "a page was dropped and a different one added" is a
   claim and a wrong one.
3. **Bracket.** A pair with weak content similarity whose neighbours on both
   sides are matched with certainty *and* immediately adjacent is lifted to
   `medium` with the evidence recorded as `position`. This is the case that
   matters most: a page withheld in full last time and released in full this
   time shares no text with itself, and what identifies it is that there is
   nothing else it could be. It is an inference and it is labelled as one, in
   the data and on the page.

The whole matrix is computed for documents under 200,000 page-pairs; above that
only a diagonal window is, and the page says the window was applied. Above
6,000,000 page-pairs the alignment refuses and asks you to split the document.

### 3.4 Confidence, and what it gates

Confidence is not a band of the score. Three things make a high score worth less
than it looks, and each caps it:

- **shape only** — recognition failed, or one page has less than a third of the
  other's text. Never better than `medium`.
- **a small margin** — the chosen partner is barely better than the next-best,
  so content did not decide this pairing and position did. Never better than
  `medium`, and the evidence records `position`.
- **conflicting stamps** — both pages carry control numbers under a shared
  scheme and they differ. `low`.

A shared control number overrides all three and gives `certain`.

**`low` pairs produce no findings at all.** They appear in the alignment table,
because a reader should see that we do not know, and nothing is claimed about
them.

## 4. The diff

### 4.1 Geometry leads, text corroborates

Word-level diffing over OCR'd text produces noise by the hundred. Two
recognitions of the same sheet disagree about 25% of its tokens at a
middling scan quality, and every one of those disagreements looks exactly like a
newly disclosed word.

So the text diff is not what makes the claim. **A disclosure is caused by a black
box going away**, so:

1. Match the two pages' redaction boxes by intersection-over-union, greedily,
   best first. Classify each pair `unchanged`, `shrunk`, `grown`, `moved`, and
   each unpartnered box `removed` or `added`. Boxes under 0.025% of the page are
   not compared — under that the detector's own noise is larger than the
   difference being measured.
2. Compute, by rectangle subtraction, the region this release **stopped**
   covering and the region it **started** covering.
3. Read the *new* page's words out of the stopped-covering region. That text is,
   by construction, text standing where the earlier release had a black box.
4. Check it against the earlier page's tokens with `difflib` over OCR-folded
   skeletons. If at least 70% of the passage is absent there, the finding is
   **corroborated**. If not, the two scans are probably not registered to each
   other and it is demoted to **suspected**, shown in a collapsed block and
   counted nowhere.
5. Symmetrically for the started-covering region, read out of the *earlier*
   page: that is a **newly withheld** passage, quoted from the release that
   published it.

Text that differs *outside* every changed region is counted as recognition noise
and printed as a number on the page, so a reader can see how much of it there is.

A re-scan therefore produces **nothing**: no box changed, so there is no region,
so there is no finding. That is checked at three noise levels in
`tests/test_compare.py` and end to end on a genuinely re-photographed page.

### 4.2 The confidences a finding can carry

| | means |
|---|---|
| **corroborated** | the box changed and the text agrees. This is the claim. |
| **suspected** | the box changed and the text does not agree. Shown, counted nowhere. |
| **no text** | a box was lifted and nothing legible was found underneath. Something changed; we cannot say what. |

## 5. Measured accuracy

`tests/test_compare.py::test_measured_accuracy` builds twenty-one perturbations
of a document whose correct alignment is known by construction and runs the real
aligner over all of them. Run `pytest tests/test_compare.py -k measured -s` to
reproduce the table.

```
case                                true  made  right   mean
identical                             10    10     10   1.00
one page inserted at 0                10    10     10   1.00
one page inserted at 4                10    10     10   1.00
one page inserted at 9                10    10     10   1.00
one page dropped at 0                  9     9      9   1.00
one page dropped at 5                  9     9      9   1.00
one page dropped at 9                  9     9      9   1.00
three pages prepended                 10    10     10   1.00
two pages swapped                     10    10     10   1.00
re-read at 15% noise                  10    10     10   0.78
re-read at 30% noise                  10    10     10   0.64
re-read at 45% noise                  10    10     10   0.55
one page withheld in full before      10    10     10   0.96
one page withheld in full now         10    10     10   0.96
a third of the pages redacted         10    10     10   0.99
stamped, same scheme                  10    10     10   1.00
stamped, re-numbered                  10    10     10   1.00
one page recognition failed on        10    10     10   1.00
eight identical forms                  8     8      8   1.00
identical forms, one inserted          8     8      8   1.00
one inserted and one dropped           9     9      9   1.00

precision 1.0000   recall 1.0000   (202 correct of 202 made, 202 to find)
unrelated documents refused: 12/12
```

That is the output of the command above, run against this tree on 2026-09-01,
pasted rather than transcribed. The `mean` column is the mean similarity of the
pairs it made, which is why the noise rows fall while their accuracy does not.

**Read this honestly.** The fixtures are synthetic pages built by the same file
that measures them, and they are generous in one specific way: their page images
are not real scans, so registration between the two copies is perfect. Real
productions are not. What the table does establish is that the *sequence
handling* is right — insertions, deletions, swaps, prepends and a full
withholding do not desynchronise it — and that the refusals fire. What it cannot
establish is the false-positive rate on real paper, because nobody has a labelled
corpus of paired FOIA productions. If you have one, that is the most valuable
contribution this feature could receive.

The end-to-end tests do run the real pipeline — poppler renders, Tesseract reads
— over PDFs from `tests/synth.py`, including a page photographed twice at
different sizes with grain added, which must and does produce zero findings.

## 6. How this fails, and how you would know

Every item here is stated on the built page as well as in this file, next to the
finding it weakens. That is deliberate: a caveat only a maintainer reads is not
a caveat.

**The pages may be mismatched.** The failure mode is a page that was *replaced*
between releases rather than unredacted, in a document whose neighbouring pages
match with certainty. The bracketing rule will pair it and the geometry diff
will then report every box on it as removed or added and quote text under them.
*How you would know:* the alignment table shows the pair at `medium` with the
evidence `position` and no `text`, and the page carries a warning above the
findings saying so in as many words. Treat any finding on a page matched by
position alone as a question for the agency, not an answer.

**The two scans may not register.** Everything is measured in fractions of a
page, so resolution does not matter — but a different crop or a skew moves every
box by about the same amount, and the regions are then read off the wrong part
of the page. *How you would know:* the module measures the median displacement
of the boxes that did not change and prints it on any page where it exceeds 1.6%
of the page width. Findings on such a page will also mostly land as `suspected`,
because the text under the shifted region turns out to be present on both
copies.

**A machine read both copies.** If recognition missed a passage on the earlier
copy and caught it on this one, and a box happens to have changed nearby, the
passage can be reported as disclosed. The 70% corroboration rule is what stands
between you and this, and it is a threshold, not a proof. *How you would know:*
compare the quoted passage against the two thumbnails printed beside it. That is
what they are there for.

**Absence of a finding is not evidence of no change.** A release that was
re-typed, re-paginated, or scanned so differently that the boxes cannot be
matched produces few findings and no warning that there was more to find. A
document withheld in full in both releases produces nothing because there is
nothing to compare. *How you would know:* you would not, directly. Read the
alignment table and the "pages that could not be compared" count as questions.

**Documents can be paired wrongly, or not at all.** Pairing requires the match to
be the best available for *both* documents, which is conservative, but a
production that re-cuts the same material into different files defeats it
entirely — five old files becoming two new ones will pair two and orphan three.
*How you would know:* the "documents with no counterpart" section, which is on
the index page and not hidden.

**Exemption codes are attributed from the layout, or to the page.** A code goes
to the black box on its own line at any distance across the page — the margin
stamp — and otherwise to a box right beside it; where the line holds several
boxes and none is near, the code is counted for the page rather than guessed at.
That is inherited from the rest of Stackroom and is still an inference from
where the ink is. A code appearing on a page is reported as "this release cites
(b)(6) here and the earlier one did not", which is weaker than "this passage was
withheld under a different law" and is worded as the weaker thing.

**What cannot happen.** No text that either release hid under a black box can
appear anywhere in the comparison. The module reads `Page.words` and
`Page.lines` only — which `pipeline.py` has already emptied of any token sitting
under an opaque shape — and it never touches `Page.hidden`.
`tests/test_compare.py` asserts that as a fact about the source, and separately
plants a real failed redaction in a fixture, builds the comparison with the
safety gate in `warn` mode, and greps every published file for the planted
string. Both releases go through the same failed-redaction check an ordinary
build runs, and a leak in *last year's* files stops the build too.

## 7. How it is wired

Nothing here is a to-do list: all of it is in the tree, and this section says
where, so that a change to the build order does not quietly break the
comparison.

`compare.py` sits at the top level of the package rather than under `build/`,
because it drives a whole ingest of its own. Its two templates
(`compare.html.jinja`, `compare_index.html.jinja`) are found by the ordinary
loader, and `assets/parts/compare.css` and `assets/js/compare.js` are picked up
by the globs in `SiteBuilder.copy_assets` — so neither is named anywhere, and
adding a second stylesheet or script for the section needs no wiring at all.

`SiteBuilder.run()` calls `compare_mod.build(self)` **unconditionally, first,
immediately after `copy_assets()`**. `build()` is a no-op on a build with no
comparison attached, which is what lets the call be unconditional, and the
position matters for three reasons:

- it sets the `compare_enabled` template global the masthead reads, and the
  masthead is on every page;
- the search index takes an inventory of the output directory, so a section
  written after it is not searchable;
- the offline bundle takes the same inventory, so a section written after it is
  not stored for offline reading.

The masthead link is in `templates/base.html.jinja`, guarded by
`compare_enabled`. Both comparison templates are translated — every string in
them is a `t()` call against the `compare.*` keys in the catalogue — but the
masthead link itself is still hard-coded as the English word **Compared** with
`lang="en" dir="ltr"` on it, even though `nav.compare` exists in the catalogue
and is translated. That is one line, and it is the last English thing about the
section; `docs/TRANSLATING.md` keeps the list.

`stackroom compare` is `cli.compare`, and it does not go through `_build_once`:
`compare_mod.run_comparison()` owns the whole run because it ingests two
folders, and the CLI's report is `_compare_report`, which leads with what
changed rather than with how many pages there are. Both folders go through the
same failed-redaction gate an ordinary build uses, so a leak in *last year's*
files stops this build too.

```
stackroom compare --help
```

lists the flags: `--out`/`-o`, `--config`/`-c`, `--title`, `--old-label`,
`--new-label`, `--workers`/`-j`, `--no-search`, `--no-old-scans`, `--force`,
`--unsafe-publish-leaks`, `--i-know`.

## 8. Dependencies

None beyond what `pyproject.toml` already lists. `numpy` for the alignment
matrix, `Pillow` for the optional image hash, `jinja2` for the templates. The
text diff is `difflib` from the standard library, chosen over a hand-written
longest-common-subsequence because it is deterministic and has had thirty years
of people finding its edge cases; `autojunk` is turned off, because it discards
tokens appearing in more than 1% of a long sequence, which on a page of prose is
every occurrence of "the".
