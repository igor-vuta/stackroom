# Security

> **Maintainers: fill this in before the repository goes public.**
> Replace `<SECURITY-CONTACT>` with a real, monitored address, and turn on
> **Settings → Code security → Private vulnerability reporting** so the first
> option below actually exists. Confirm the response times stated here are ones
> you can keep; a promise you miss is worse than a longer promise you keep.

Stackroom's job is to stop somebody publishing a document that exposes a
source. Most of what follows is about the one bug class that matters more than
all the others put together.

## Reporting a vulnerability

**Do not open a public issue.**

1. **Preferred:** GitHub's private vulnerability reporting — the **Security**
   tab of this repository, then **Report a vulnerability**. It is private to the
   maintainers and it keeps the whole exchange in one place.
2. **Or:** email **`<SECURITY-CONTACT>`**.

What to expect: an acknowledgement within three working days, an assessment
within ten, and a fix released before any public description of the problem. We
will credit you by whatever name you give us, or not at all if you prefer. If
we conclude it is not a vulnerability we will say so and why, and you are free
to write about it.

If you have not heard back in ten days, assume the mail went astray rather than
that you were ignored, and try the other channel.

## If the failed-redaction check missed a leak

This is the report the project most needs and the one most easily mishandled.

**A document where a black box covers text that Stackroom did not find is a
document that will burn somebody if it is handled carelessly — including by
you, reporting it.**

### Do not

- Do not open a public issue about it.
- Do not attach the document, to an issue or to anything else, unless it is
  already public and you are certain the exposure is already permanent.
- Do not paste the recovered text anywhere. Not in an issue, not in a pull
  request, not in a test fixture. Stackroom prints a shape-preserving stand-in
  for exactly this reason — `Smith, Jonathan` becomes `#####, ########` — and
  that is the form to quote.
- Do not "verify" the leak by opening the file somewhere that syncs, indexes or
  backs it up.

### Do

Report it privately, through one of the two channels above, with as much of
this as you can:

- **What class of document it is.** Producer and creator strings help most:
  `pdfinfo yourfile.pdf` prints them, and they usually name the tool the agency
  used. That tool is the bug.
- **How the redaction was applied**, as far as you can tell — a drawn
  rectangle, a highlight, an image pasted over the text, a shape that is not a
  rectangle, a whole page flattened to a scan.
- **What Stackroom said**: the output of `stackroom check` on the file, which
  contains no recovered text. `check` writes no site, but it does rasterise
  every page to look at the pixels: those images go to a temporary folder whose
  path it prints and which it deletes when it finishes, and `--scratch` puts
  that folder wherever you say. If the document must not touch a disk, point it
  at a ramdisk.
- **A public example, if one exists.** An agency's reading room, DocumentCloud
  or a court docket often has a document produced by the same tool with the same
  failure. That is what lets us write a test.
- **The redacted text only as its shape**, never as itself.

If the document is not public and no public example exists, say so and stop
there. We will work out how to reproduce it from a synthetic file —
`tests/synth.py` manufactures damaged pages for precisely this reason. A bug
report is never worth a second exposure.

If you have already published an archive built from such a document, take the
site down first and report second. The original PDF is published byte for byte,
so the leak is in the file itself, not only in the pages Stackroom generated.

## What the check does and does not guarantee

The honest summary: **`stackroom check` passing is evidence, not a guarantee.**
It tells you that one specific failure — text still present under an opaque
rectangle, on a page it was able to read — is not in the files you gave it. It
is not a certificate that a document is safe to publish.

### What it does

For each page, Stackroom takes every opaque filled rectangle larger than 4pt in
both dimensions, finds the characters at least 80% inside one, and treats a
character as hidden when the file paints it before the rectangle or in the same
colour. It then **renders the rectangle and looks at the pixels**: if they are
flat, whatever the file says is under there is invisible to a reader, which is
the question that actually matters. Rectangles of every colour are considered,
because white-on-white is a whole class of failed redaction.

When it finds one, the build stops. The recovered text is held in memory so the
CLI can show the operator what leaked, and is written to no file, no log, no
JSON payload and no search index. Ambiguous evidence is reported for a human
rather than resolved silently: a false positive costs an operator ten minutes,
and a false negative can cost somebody rather more.

### What gets past it

Every item here is a known limitation of the current implementation, not a
speculative one:

1. **A redaction that is not a rectangle in the content stream.** An image
   pasted over the text, a clipping path, a non-rectangular shape, or a page
   flattened to a scan with a marker drawn on it. There is no rectangle to
   find, so there is nothing to check.
2. **A rectangle 4pt or smaller in either dimension.** That threshold is what
   removes table rules, cell borders and underlines without special-casing
   them; a genuinely thin redaction goes with them.
3. **A redaction lying entirely within the top 43pt of the page** — a name
   blacked out of a letterhead. Rectangles wholly inside that band are treated
   as running headers. The blind spot is inherited from the reference
   implementation and is pinned in the module's tests.
4. **Text hidden by something other than a box over it.** White text on a white
   page with no rectangle, text pushed outside the crop box, text in an
   optional-content layer that is switched off. The check is anchored on
   rectangles, so it finds none of these — **and Stackroom then publishes that
   text as the page's transcription and indexes it for search**, which is a
   stronger and worse statement than "the check misses it". If a document's
   text layer disagrees with what a reader sees, the archive publishes the text
   layer.

   The one case that is handled is text painted in an invisible render mode
   (`3 Tr`) over blank paper: that is withheld from the page and the index, and
   the build says so. It cannot be handled by the rule "drop invisible text",
   because an invisible render mode is also how every searchable scan is built
   — an image of the page with an invisible transcription behind it — so the
   discriminator is whether there is ink where the words claim to be.
5. **Everything in the file that is not page content.** Document metadata, XMP,
   attachments, annotation and form-field contents, bookmarks, and the earlier
   revisions an incremental save leaves behind. Stackroom reads the characters
   and shapes on the page. It publishes the original file byte for byte, so
   anything else in that file is published too.

   `safety.strip_metadata = true` rewrites the published copy from its page
   tree, which drops `/Info`, XMP and — the part that actually hides somebody —
   every earlier revision an incremental save left behind. Two consequences,
   both real: the published file is **no longer byte-identical to the source**,
   so the manifest records the digest of what was published alongside the
   digest of what arrived; and a rewrite is not a sanitiser. Page annotations
   survive it; bookmarks, embedded attachments and the AcroForm field tree do
   not. It is off by default because that metadata is often itself evidence
   about the production.
6. **Pages where every finding is a bare date.** A page whose boxes all hide
   dates is treated as a form — a docket, a log, a table of filing dates —
   rather than a failed redaction, and the findings are suppressed. An agency
   that redacted only dates will therefore pass quietly.
7. **A short, closed list of strings deliberately ignored.** This is the whole
   of it, re-derived from `_is_real_text` and `_JUNK_WORD` in
   `ingest/redaction.py`. A recovered string is not reported when it is:

   - empty, or nothing but whitespace;
   - one character repeated — a row of box-drawing glyphs or padding dots that
     happens to sit under a filled shape;
   - free of word characters altogether — punctuation, rules, dingbats;
   - exactly, ignoring case, **`name redacted`** (with any run of whitespace
     between the two words), **`confidential`**, **`privileged`** or
     **`redacted`** — the word a tool printed *under* its own box before
     stamping over it.

   `name redacted` is on that list and is easy to miss when reading the four
   entries as three: it is the phrase a common redaction tool leaves behind,
   and it looks like a name until you read it.

   Suppressed in 6 and 7 means *not worth stopping the build over*. It has
   never meant *safe to publish*: the text was under a black box either way, so
   it is withheld from the page and from the index in both cases.
8. **Pages it could not check.** A PDF that will not parse, or a page that
   would not render, is reported as unchecked. `stackroom check` says so in as
   many words — *that is not a clean bill of health* — and it exits non-zero.
   Without a page rendering, a finding rests on the content stream alone and is
   marked unconfirmed in both directions.
9. **Anything you did not run it on.** Checking half a release tells you about
   half a release.

A separate limit, about the ledger rather than the leak check: skewed scans
beyond about 0.2° of rotation defeat visible-redaction detection, and a uniform
dark band left by a scanner is genuinely indistinguishable from a black box.
Those affect the accuracy of *"37% of this page was withheld"*. They are not
safety failures, and they are reported as warnings rather than treated as
findings.

### What is guaranteed

- Text under a black box is never published — not in the HTML, not in the JSON,
  not in the search index — and never written to disk anywhere. That covers
  findings that were suppressed rather than reported, and words a box covers
  only partly: a token any opaque shape touches is withheld whole, because half
  a redacted name is still a redacted name.
- A single finding stops the build. `--unsafe-publish-leaks` exists for the case
  where the text underneath is already public; even then Stackroom keeps the
  recovered text out of the generated site, and the original file you publish
  still contains it.
- A published archive makes no third-party requests. No CDN, no font service,
  no analytics, no update check. An archive that phones home is a log of who
  read what, and a change that introduces one is a security bug, not a feature.

## In scope

Report these privately:

- The failed-redaction check missing a real leak, in any document class.
- Recovered text reaching disk, the generated site, the search index, a log
  file, or a crash traceback.
- Document content escaping into the generated HTML as markup — the templates
  autoescape and the JSON payloads escape `<`, `>` and `&`, so a document that
  gets script into a page is a bug. (`about.md` is deliberately rendered as
  HTML: it is the operator's own file, and it is trusted.)
- A build that writes outside its output directory: a crafted filename escaping
  the media or `files/` layout, a symlink followed out of the source folder.
- An original published under an extension a static host will serve as active
  content. The published extension is derived from the file's own bytes and
  never from its name, precisely so that a PDF called `report.html` cannot run
  as script in the archive's origin; a way round that is a vulnerability.
- A filename or document value reaching a subprocess (`pdftoppm`, `tesseract`,
  `pagefind`) as anything other than an argument.
- A generated site that requests anything from a third-party host.
- Anything that makes a reader of a published archive identifiable to anyone
  other than the host they chose to fetch it from.

## Not a vulnerability

These are ordinary bugs, and a public issue is the right place for them:

- OCR errors, and pages marked unreadable that a person can read.
- Exemption codes missed, mis-parsed, or attached to the wrong box.
- A withheld percentage that is wrong because a scan band was counted as a
  redaction, or a real redaction was missed.
- Resource exhaustion from a hostile PDF. Stackroom is run by an operator on
  files they chose, on their own machine; it is not a service, and it has no
  attacker-controlled input in the sense that word usually implies. Report it as
  a bug — it is worth fixing — but it is not embargoed.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| < 0.1 | No — there is no earlier release |

Fixes go to the latest release. There are no long-term support branches, and
there will not be until there is somebody to maintain them.
