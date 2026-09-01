"""Turning a :class:`~stackroom.model.Collection` into a folder of files.

Nothing in this module knows how to read a PDF, and nothing in the ingest
pipeline knows what HTML looks like. The seam between them is the data model,
which is what makes it possible to test either half without the other.

The output is deliberately dull: static files, relative links, no build-time
cleverness that a reader would need to reproduce. A reader who saves this
folder has the archive; a reader who serves it from anywhere has the site.
"""

from __future__ import annotations

import functools
import json
import os.path
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .. import __version__, i18n
from .. import compare as compare_mod
from ..config import Config
from ..ingest import exemptions as exemptions_mod
from ..ingest import pdf as pdf_mod
from ..lang import language_names, normalize_language_codes
from ..model import Collection, Document, Page, PageVerdict, to_jsonable
from ..textblock import render_markdown
from . import negative as negative_mod
from . import offline as offline_mod
from . import search as search_mod

ASSETS = Path(__file__).resolve().parent.parent / "assets"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# Pagefind ships a drop-in search UI - about 420 KB of it - that this project
# never loads, because it has its own. On a small archive that is a third of the
# index directory, mirrored by everyone who clones the site.
#
# `pagefind-component-ui` is the 1.5.x name for it and was missing from this
# list, so the prune ran, reported success and removed nothing: measured on the
# demo, 217,318 bytes of pagefind-component-ui.{js,css} were still being
# published - 47% of the whole index directory. If pagefind renames it again
# the symptom is silent, so docs/PERFORMANCE.md records how to re-measure it.
UNUSED_PAGEFIND = (
    "pagefind-ui",
    "pagefind-modular-ui",
    "pagefind-component-ui",
    "pagefind-highlight",
)

# What an original is published as, keyed by what `ingest.discover` decided the
# file *is*. The name its producer chose never reaches a path (finding F2).
#
# `discover._classify` accepts a `%PDF-` header anywhere in the first 1024
# bytes, so one file can be a valid HTML document and a valid PDF at the same
# time. Published under its own name it lands in `files/annual-report.html`,
# every page of the document links to it as "Download the original", and a
# static host serves it as `text/html` from this archive's own origin - where,
# unlike every page this module generates, there is no Content-Security-Policy.
# Script there is same-origin with the whole archive. `.svg`, `.xhtml`, `.xml`
# and `.js` are the same vector.
KIND_SUFFIX: dict[str, str] = {"pdf": ".pdf", "text": ".txt"}

# `discover` records only "image", so the concrete format is read back off the
# bytes: the same magic numbers it classified the file by, mapped to the
# extension a reader's operating system will open it with.
IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"BM", ".bmp"),
)

UNKNOWN_SUFFIX = ".bin"
"""For a file we cannot name from its own bytes. Deliberately not a guess: no
static host has a MIME type for it, so it is offered as a download rather than
interpreted as anything."""

# Scripts that have to run before the browser paints, and the templates that
# need them. A script opts in by naming itself here - the shell template loops
# over what this produces and drops the same names from the deferred sweep at
# the end of the body, so nothing in `templates/` changes when the list does.
#
# The value is the templates the script is wanted in, or `()` for all of them.
# Scoping matters as much as the opt-in: a parser-blocking script is a cost
# paid before the first pixel, and scan.js is 41 KB that only a page of a
# document has any use for that early.
HEAD_SCRIPTS: dict[str, tuple[str, ...]] = {
    # The theme the reader chose has to be on <html> before the first paint, or
    # they get a flash of the other one on every page they open.
    "prefs.js": (),
    # `pagereveal` fires at the first rendering opportunity, which is before
    # deferred scripts run; a listener registered from the deferred sweep
    # arrives after the transition has started and the page turn silently loses
    # its direction about four times in five.
    "scan.js": ("page.html.jinja",),
}


@dataclass(slots=True)
class BuildReport:
    out_dir: Path
    pages: int = 0
    documents: int = 0
    files_written: int = 0
    bytes_written: int = 0
    media_bytes: int = 0
    originals_bytes: int = 0
    search: search_mod.IndexInfo | None = None
    warnings: list[str] | None = None


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _root(depth: int) -> str:
    """The relative prefix that gets you back to the site root from *depth*."""
    return "../" * depth


def published_suffix(kind: str, source: str | Path | None = None) -> str:
    """The extension an original is published under: from what it *is*.

    Never from what it is called. The producer chose the name, and a name is
    all it takes to have a file served as active content from the archive's own
    origin - so a PDF is published as ``.pdf`` whatever it arrived as, and a
    file we cannot identify is published as ``.bin`` rather than under a guess.

    *kind* is :attr:`Document.kind`, which is what ``discover`` decided from the
    magic number. It is consulted first, in the order ``_classify`` uses, so the
    file is published as the thing it was ingested as - the two disagreeing is
    the whole shape of this bug. Falling back to the bytes covers a document
    assembled by hand, which has no ``kind``.
    """
    known = KIND_SUFFIX.get(kind)
    if known:
        return known
    head = b""
    if source is not None:
        try:
            with Path(source).open("rb") as fh:
                head = fh.read(1024)
        except OSError:
            head = b""
    for magic, suffix in IMAGE_MAGIC:
        if head.startswith(magic):
            return suffix
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    # Last, and only for a file with no `kind`, because `_classify` checks it
    # last for the same reason: a header allowed to be a few hundred bytes in is
    # a weaker signal than a magic number at offset zero.
    if kind != "image" and b"%PDF-" in head:
        return ".pdf"
    return UNKNOWN_SUFFIX


@functools.lru_cache(maxsize=1)
def _english() -> i18n.Translator:
    """The translator the module-level helpers fall back to.

    Every one of them is public, or is called from a test, or both, and none of
    those callers has a :class:`SiteBuilder` to take a translator from. English
    is what they produced before there was a catalogue, so English is what they
    produce when nobody names a language.
    """
    return i18n.translator_for(None)


def human_bytes(n: int, *, t: i18n.Translator | None = None) -> str:
    """A file size, in the units *t* writes sizes in.

    The arithmetic is kept here rather than delegated, because this is what
    ``stackroom build`` prints to an operator's terminal and it must not be able
    to fail on a catalogue. ``Translator.bytes`` mirrors it byte for byte in
    English, and ``test_i18n.py::test_file_sizes_match_the_builders_own_formatting_in_english``
    is what keeps the two from drifting.
    """
    if t is not None:
        return str(t.bytes(int(n)))
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}".replace(".0 ", " ")
    return f"{n:.1f} GB"


def page_state(page: Page) -> str:
    """One word for what a reader needs to know about this page."""
    if page.quality.verdict.is_failure:
        return "dark"
    if page.redaction_ratio >= 0.9:
        return "full"
    if page.redactions:
        return "part"
    if page.quality.verdict is PageVerdict.BLANK:
        return "void"
    return "plain"


def ribbon(
    pages: list[Page],
    *,
    base: str | None = None,
    height: int = 40,
    label: str | None = None,
    current: int | None = None,
    t: i18n.Translator | None = None,
) -> Markup:
    """A strip of ticks, one per page, run-length encoded.

    Consecutive pages in the same state become one rectangle, so a 2,000-page
    document is a handful of shapes rather than 2,000 - which is the difference
    between a 300-byte graphic and a 120 KB one, repeated for every document on
    the front page.

    *current* marks one page as the one being read. It is drawn as two
    ``<line>`` elements rather than a rectangle for two reasons. A rect would
    join the run-length encoding, and ``scan.js`` reads the state of a page off
    the last ``<rect>`` whose x it has passed - a marker in that list would
    make every page after it stateless. And a single stroke cannot be relied on
    to read: the ticks it sits on run from the ink colour to the paper colour,
    so the marker is a paper-coloured casing with the accent laid inside it,
    which has an edge against every one of them.
    """
    total = len(pages)
    if not total:
        return Markup("")

    runs: list[tuple[str, int, int]] = []
    for index, page in enumerate(pages):
        state = page_state(page)
        if runs and runs[-1][0] == state:
            runs[-1] = (state, runs[-1][1], index + 1)
        else:
            runs.append((state, index, index + 1))

    width = 1000.0
    rects: list[str] = []
    for state, start, stop in runs:
        x = start / total * width
        w = max(0.6, (stop - start) / total * width)
        rects.append(
            f'<rect class="r-{state}" x="{x:.3f}" y="0" width="{w:.3f}" height="{height}"/>'
        )

    here = current if current is not None and 1 <= current <= total else None
    if here is not None:
        at = (here - 0.5) / total * width
        for cls in ("here-casing", "here"):
            rects.append(
                f'<line class="r-{cls}" x1="{at:.3f}" y1="0" x2="{at:.3f}" y2="{height}"'
                ' vector-effect="non-scaling-stroke"/>'
            )

    # Every attribute is assembled as an escaped fragment and only then joined.
    # Concatenating a plain string onto a Markup escapes the *string*, which
    # turned aria-label="..." into aria-label=&#34;...&#34; - an unquoted value
    # plus a dozen junk boolean attributes, and an accessible name of `"16`.
    attrs = Markup("").join(
        [
            Markup('class="ribbon" viewBox="0 0 1000 %d" preserveAspectRatio="none"') % height,
            Markup(' role="img" aria-label="%s"') % (label or _ribbon_label(pages, t=t)),
            Markup(' data-pages="%d"') % total,
            Markup(' data-base="%s"') % base if base else Markup(""),
            Markup(' data-current="%d"') % here if here is not None else Markup(""),
        ]
    )
    return Markup("<svg %s>%s</svg>") % (attrs, Markup("").join(Markup(r) for r in rects))


_RIBBON_KEYS: tuple[tuple[str, str], ...] = (
    ("part", "ribbon.part"),
    ("full", "ribbon.full"),
    ("dark", "ribbon.dark"),
    ("void", "ribbon.void"),
)


def _ribbon_label(pages: list[Page], *, t: i18n.Translator | None = None) -> str:
    """The reading of the strip of ticks, as one sentence.

    Every count is a finished noun phrase from the catalogue before it is
    joined, because a language with four plural forms cannot get "4 partly
    withheld" out of a number and a suffix - and the separator and the full
    stop are catalogue entries too, since neither is a comma and a period
    everywhere.
    """
    t = t or _english()
    counts: dict[str, int] = {}
    for page in pages:
        state = page_state(page)
        counts[state] = counts.get(state, 0) + 1
    parts = [str(t.t("count.pages", count=len(pages)))]
    parts += [
        str(t.t(key, count=counts[state])) for state, key in _RIBBON_KEYS if counts.get(state)
    ]
    return str(t.t("ribbon.end", list=str(t.t("ribbon.join")).join(parts)))


# --------------------------------------------------------------------------
# the text layer
# --------------------------------------------------------------------------


@dataclass(slots=True)
class LineItem:
    kind: str
    text: str = ""
    index: int = 0
    doubtful: bool = False
    width: int = 0
    code: str = ""
    code_label: str = ""


def display_lines(page: Page, *, jurisdiction: str = "us") -> list[list[LineItem]]:
    """Weave the redactions back into the text, where they happened.

    A search result tells you a phrase is on a page. The bar in the middle of
    the sentence tells you what is *not* on it - and that is usually the thing
    the reader came for. Putting them in the reading order, rather than only on
    the image, is what turns a black rectangle into a sentence with a hole in
    it.

    Lines are preserved rather than reflowed. The scan sits beside this text,
    and a transcription that rewraps is much harder to check against it.
    """
    grouped: dict[int, list[tuple[float, LineItem]]] = {}
    for index, word in enumerate(page.words):
        grouped.setdefault(word.line, []).append(
            (
                word.box.x,
                LineItem(
                    kind="word",
                    text=word.text,
                    index=index,
                    doubtful=0 <= word.conf < 60,
                ),
            )
        )

    labels = dict(exemptions_mod.legend([c for r in page.redactions for c in r.codes],
                                        jurisdiction=jurisdiction))

    line_extent: dict[int, tuple[float, float]] = {}
    for line in grouped:
        ys = [w.box.y for w in page.words if w.line == line]
        hs = [w.box.h for w in page.words if w.line == line]
        if ys:
            line_extent[line] = (min(ys), max(y + h for y, h in zip(ys, hs, strict=False)))

    orphans: list[LineItem] = []
    for redaction in page.redactions:
        box = redaction.box
        centre = box.y + box.h / 2
        best, best_overlap = None, 0.0
        for line, (top, bottom) in line_extent.items():
            if top <= centre <= bottom:
                overlap = min(bottom, box.y2) - max(top, box.y)
                if overlap > best_overlap:
                    best, best_overlap = line, overlap
        code = redaction.codes[0] if redaction.codes else ""
        item = LineItem(
            kind="gap",
            # As a share of the page width, not a character count. A bar that
            # covers half the page should cover half the column, whatever size
            # the reader has the text set at - and a character estimate drifts
            # badly once the scan and the transcription are set differently.
            width=max(3, min(100, round(box.w * 100))),
            code=code,
            code_label=labels.get(code, ""),
        )
        if best is None:
            orphans.append(item)
        else:
            grouped[best].append((box.x, item))

    lines: list[list[LineItem]] = []
    for line in sorted(grouped):
        lines.append([item for _, item in sorted(grouped[line], key=lambda pair: pair[0])])
    if orphans:
        lines.append(orphans)
    return lines


def _json_block(value: object) -> Markup:
    """JSON for a ``<script type="application/json">`` block.

    Script content is raw text to the parser - HTML entities inside it are not
    decoded - so escaping the JSON would break ``JSON.parse``. The only string
    that can end the block early is ``</script``, so that is the one sequence
    that has to go, and it is escaped the way JSON allows.
    """
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return Markup(text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def page_payload(page: Page) -> str:
    """The word boxes for ``data/<doc>/<n>.json``, as compactly as JSON allows.

    Four integers per token in thousandths of the page. A 400-word page is
    about 1.6 KB raw and well under a kilobyte once a server has gzipped it.

    **Nothing in the site fetches these files.** :func:`page_payload_block`
    writes the same object into the page's own HTML and that is where
    ``viewer.js`` reads it, so the file is a duplicate by construction. It is
    published anyway, and deliberately: ``data/<doc>/<n>.json`` is a documented
    part of the output layout - a machine-readable side-channel for anyone
    building on the archive, beside ``data/docs.json`` and ``manifest.json`` -
    which is what ``docs/ARCHITECTURE.md`` says it is. Deleting them is a change
    to what this project publishes, not a tidy-up, and would want a
    ``CHANGELOG.md`` entry under the heading that file reserves for exactly
    that.

    What it costs, so nobody has to re-measure it to have the argument:
    4,664 bytes a page, 74,629 bytes over the demo collection, and 93 MB at the
    supported 20,000-page ceiling. ``docs/PERFORMANCE.md`` 8.4 lays out the
    three options; this is option 1.
    """
    boxes: list[int] = []
    for word in page.words:
        boxes.extend(word.box.to_ints())
    return json.dumps({"b": boxes, "n": len(page.words)}, separators=(",", ":"))


def page_payload_block(page: Page) -> Markup:
    boxes: list[int] = []
    for word in page.words:
        boxes.extend(word.box.to_ints())
    return _json_block({"b": boxes, "n": len(page.words)})


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------


class SiteBuilder:
    def __init__(self, collection: Collection, cfg: Config, out_dir: Path):
        self.collection = collection
        self.cfg = cfg
        self.out = Path(out_dir)
        self.report = BuildReport(out_dir=self.out, warnings=[])
        self.published: dict[str, pdf_mod.PublishedFile] = {}
        """What actually landed in ``files/``, per document id. Not the same
        bytes as the source once ``safety.strip_metadata`` has rewritten them,
        which is why the manifest records both digests."""

        self._original_names: dict[str, str] = {}
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATES)),
            autoescape=select_autoescape(["html", "xml", "jinja"]),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self.env.globals["search_enabled"] = cfg.search.enabled

        # One translator for the whole build, made before anything renders. It
        # is the interface language and only that: `cfg.language` used to mean
        # the search index's stemmer as well, and those are two different
        # questions - see `index_language()`.
        self.t = i18n.translator_for(cfg.language)
        i18n.install(self.env, self.t)

        self.extra_scripts = sorted(p.name for p in (ASSETS / "js").glob("*.js"))

    # -- writing ---------------------------------------------------------

    def write(self, relative: str, text: str) -> None:
        path = self.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        self.report.files_written += 1
        self.report.bytes_written += len(data)

    def render(self, template: str, relative: str, **context: Any) -> None:
        depth = relative.count("/")
        context.setdefault("root", _root(depth))
        context.setdefault("collection", self.collection)
        context.setdefault("favicon", True)
        # The masthead, the content and the colophon share one container so the
        # page has a single left edge from top to bottom. Two different
        # measures reads as two different websites stacked on each other.
        context.setdefault("wrap", "wrap--narrow")
        context.setdefault("extra_scripts", self.extra_scripts)
        context.setdefault("head_scripts", self.head_scripts(template))
        self.write(relative, self.env.get_template(template).render(**context))

    def head_scripts(self, template: str) -> list[str]:
        """The scripts *template* wants before the first paint, in run order.

        Declaration order, not alphabetical: prefs.js publishes the helpers the
        others read and has to be first, and "first" is a fact about the
        scripts rather than about their names.
        """
        return [
            name
            for name, templates in HEAD_SCRIPTS.items()
            if name in self.extra_scripts and (not templates or template in templates)
        ]

    # -- assets ----------------------------------------------------------

    def copy_assets(self) -> None:
        target = self.out / "assets"
        target.mkdir(parents=True, exist_ok=True)

        # One stylesheet, not two: the fonts have to be declared before anything
        # uses them, and a second request to discover that is a wasted round
        # trip on exactly the connection least able to afford one.
        fonts_css = (ASSETS / "fonts" / "fonts.css").read_text(encoding="utf-8")
        fonts_css = fonts_css.replace('url("', 'url("fonts/').replace("url('", "url('fonts/")
        fonts_css = fonts_css.replace('url(fonts/fonts/', 'url(fonts/')
        # The stylesheet is assembled from parts so that several people can work
        # on different areas of the design without meeting in one 1,200-line
        # file. Order is alphabetical and therefore stable; parts may only add,
        # never override earlier cascade decisions - if a part needs to fight
        # the base, the base is wrong.
        main_css = (ASSETS / "stackroom.css").read_text(encoding="utf-8")
        parts = [
            f"\n\n/* --- {p.name} --- */\n" + p.read_text(encoding="utf-8")
            for p in sorted((ASSETS / "parts").glob("*.css"))
        ]
        self.write("assets/stackroom.css", fonts_css + "\n\n" + main_css + "".join(parts))

        for name in ("viewer.js", "search.js"):
            self.write(f"assets/{name}", (ASSETS / name).read_text(encoding="utf-8"))
        for script in sorted((ASSETS / "js").glob("*.js")):
            self.write(f"assets/js/{script.name}", script.read_text(encoding="utf-8"))

        # The ~140 interface strings the scripts write, in the archive's own
        # language, translated here and not in the browser. A file rather than
        # an inline block because inlining costs it on every page; a script
        # rather than JSON because an archive opened from a folder is on
        # file://, where fetch() is refused and <script src> is not. The page
        # shell loads it in the head immediately before prefs.js, which is the
        # only file that reads it. See i18n.browser_script.
        self.write("assets/i18n.js", i18n.browser_script(self.t))

        fonts_dir = target / "fonts"
        fonts_dir.mkdir(exist_ok=True)
        for font in sorted((ASSETS / "fonts").glob("*.woff2")):
            shutil.copy2(font, fonts_dir / font.name)
            self.report.files_written += 1
            self.report.bytes_written += font.stat().st_size
        for extra in ("LICENSE-FONTS.md",):
            source = ASSETS / "fonts" / extra
            if source.is_file():
                shutil.copy2(source, fonts_dir / extra)

        self.write("assets/favicon.svg", _FAVICON)
        # GitHub Pages runs Jekyll, which deletes directories beginning with an
        # underscore - including the search index. Silent, total, and only
        # discovered by a reader.
        self.write(".nojekyll", "")

    def original_name(self, doc: Document) -> str:
        """The file name *doc*'s original is published under, inside ``files/``.

        The document's slug - already one safe URL segment - and an extension
        taken from the file's own bytes. Cached because the copy and every link
        to it have to agree, and because the answer involves reading the file.
        """
        name = self._original_names.get(doc.id)
        if name is None:
            name = f"{doc.id}{published_suffix(doc.kind, doc.source_path)}"
            self._original_names[doc.id] = name
        return name

    def original_url(self, doc: Document, root: str) -> str | None:
        """Where a reader downloads *doc*'s original, or ``None``."""
        if not self.cfg.safety.publish_originals or not doc.source_path:
            return None
        return f"{root}files/{self.original_name(doc)}"

    def copy_originals(self) -> None:
        if not self.cfg.safety.publish_originals:
            self.report.warnings.append(
                "originals were not published, so nobody can check these renderings "
                "against the files they came from"
            )
            return
        for doc in self.collection.documents:
            source = doc.source_path
            if source is None:
                continue
            destination = self.out / "files" / self.original_name(doc)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Not shutil.copy2: with safety.strip_metadata set, the file is
            # rewritten from its page tree on the way, which is what drops the
            # earlier revisions an incremental save left behind. It returns the
            # digest of what it actually wrote, because that is no longer the
            # digest of the source.
            self.published[doc.id] = pdf_mod.publish_pdf(
                Path(source), destination, strip=self.cfg.safety.strip_metadata
            )
            self.report.originals_bytes += destination.stat().st_size
            self.report.files_written += 1
        self._report_unstripped()

    def _report_unstripped(self) -> None:
        """One grouped warning for the files ``strip_metadata`` could not touch.

        A *silently* unstripped original is how this option becomes worse than
        not having it: the operator set it and believes something happened to
        every file in the release. One line per file would be five hundred lines
        on an image collection, so the count and the distinct reasons lead and a
        few names follow.
        """
        failed: list[tuple[str, str]] = []
        for doc in self.collection.documents:
            result = self.published.get(doc.id)
            if result is not None and result.note:
                failed.append((doc.filename, result.note))
        if not failed:
            return
        names = sorted(name for name, _ in failed)
        reasons = sorted({note for _, note in failed})
        listed = ", ".join(names[:4])
        if len(names) > 4:
            listed += f", and {len(names) - 4} more"
        self.report.warnings.append(
            f"strip_metadata was asked for and could not be applied to {len(names)} "
            f"file(s), which were published unchanged ({'; '.join(reasons)}): {listed}"
        )

    # -- pages -----------------------------------------------------------

    def _doc_items(self, root: str) -> list[dict[str, Any]]:
        items = []
        for doc in self.collection.documents:
            first = doc.pages[0] if doc.pages else None
            thumb = None
            if first and first.thumbs:
                webp = [t for t in first.thumbs if t.format == "webp"] or first.thumbs
                thumb = webp[0].path
            items.append(
                {
                    "doc": doc,
                    "url": f"{root}d/{doc.id}/index.html",
                    "thumb": thumb,
                    "size_human": human_bytes(doc.size_bytes, t=self.t),
                    "ribbon": ribbon(
                        doc.pages, base=f"{root}d/{doc.id}/p/{{n}}/index.html", t=self.t
                    ),
                }
            )
        return items

    def build_index(self) -> None:
        stats = self.collection.stats
        top = None
        if stats.exemption_counts:
            code = next(iter(stats.exemption_counts))
            labels = dict(exemptions_mod.legend([code], jurisdiction=self.collection.jurisdiction))
            top = (code, labels.get(code, ""))
        all_pages = [p for d in self.collection.documents for p in d.pages]
        self.render(
            "index.html.jinja",
            "index.html",
            nav="home",
            documents=self._doc_items(""),
            ribbon=ribbon(all_pages, height=44, t=self.t),
            ribbon_label=_ribbon_label(all_pages, t=self.t),
            fully_withheld=any(page_state(p) == "full" for p in all_pages),
            top_exemption=top,
            page_description=self.collection.description,
        )

    def build_browse(self) -> None:
        self.render(
            "browse.html.jinja",
            "browse/index.html",
            nav="browse",
            documents=self._doc_items("../"),
        )

    def build_about(self) -> None:
        self.render(
            "about.html.jinja",
            "about/index.html",
            nav="about",
            language_names=self.language_names() or [str(self.t.t("about.language_unknown"))],
            digests=self._digest_rows(),
        )

    def _digest_rows(self) -> dict[str, dict[str, Any]]:
        """Documents whose published file is not the file that arrived.

        Keyed by document id, and only the ones that differ - which today means
        the ones ``safety.strip_metadata`` rewrote. A reader who runs
        ``shasum -a 256`` on a download and gets a number the About page does
        not show has been told, by the archive itself, that something is wrong
        with it; showing both digests and saying which is which is the whole
        fix. Empty for an ordinary build, so the page it produces is unchanged.
        """
        rows: dict[str, dict[str, Any]] = {}
        for doc in self.collection.documents:
            published = self.published.get(doc.id)
            if published is None or published.sha256 == doc.sha256:
                continue
            rows[doc.id] = {"published": published.sha256, "stripped": published.stripped}
        return rows

    def language_names(self) -> list[str]:
        """The languages found in the documents, named in the interface's own.

        `language.<code>` where the catalogue has it, and ``lang.language_names``
        - which is English - where it does not. A Russian archive should say
        "английский", and an archive of a language nobody has written a
        catalogue entry for should still say something rather than a bare code.
        """
        english = language_names()
        out: list[str] = []
        for code in normalize_language_codes(self.collection.stats.languages):
            key = f"language.{code}"
            out.append(str(self.t.t(key)) if self.t.has(key) else english.get(code, code))
        return out

    def build_withheld(self) -> None:
        stats = self.collection.stats
        legend_rows = []
        for code, count in stats.exemption_counts.items():
            labels = dict(exemptions_mod.legend([code], jurisdiction=self.collection.jurisdiction))
            legend_rows.append(
                (code, labels.get(code, str(self.t.t("withheld.unknown_code"))), count)
            )

        worst = []
        for doc in self.collection.documents:
            for page in doc.pages:
                if page.redactions:
                    worst.append((page.redaction_ratio, doc, page))
        worst.sort(key=lambda row: -row[0])
        worst_items = []
        for _, doc, page in worst[:12]:
            thumb = None
            if page.thumbs:
                webp = [t for t in page.thumbs if t.format == "webp"] or page.thumbs
                thumb = webp[0].path
            worst_items.append(
                {
                    "doc": doc,
                    "page": page,
                    "thumb": thumb,
                    "url": f"../d/{doc.id}/p/{page.number}/index.html",
                }
            )

        gaps = [
            (doc, a, b)
            for doc in self.collection.documents
            for a, b in doc.bates_gaps
        ]
        self.render(
            "withheld.html.jinja",
            "withheld/index.html",
            nav="withheld",
            legend=legend_rows,
            worst=worst_items,
            gaps=gaps,
        )

    def index_language(self) -> str:
        """The language the search index stems in - the documents', not the interface's.

        ``language`` in ``stackroom.toml`` used to answer both questions, and
        once the interface is translated they stop being one question. Pagefind's
        ``--force-language`` chooses the stemmer and the stop-word list it
        indexes with, so taking it from the interface language would stem every
        English page of a Russian-language archive with a Russian stemmer:
        "filed" and "filing" would stop being the same word, and the wasm the
        reader downloads would be the wrong one.

        In order of authority:

        1. ``search.language``, if the operator set it. Passed through
           :func:`i18n.normalize_locale` rather than
           :func:`lang.normalize_language_codes`, because pagefind knows more
           languages than this project keeps stop-word lists for and an
           operator naming one should be believed.
        2. What the pages were actually read as - the most common
           :attr:`Page.language`, which is what ``stats.languages`` is sorted by.
        3. The interface language, for a collection with no readable text at
           all, where there is nothing to stem and this is the old behaviour.
        4. English.
        """
        if self.cfg.search.language:
            return i18n.normalize_locale(self.cfg.search.language)
        detected = normalize_language_codes(self.collection.stats.languages)
        if detected:
            return detected[0]
        return (normalize_language_codes([self.collection.language]) or ["en"])[0]

    def build_search(self, info: search_mod.IndexInfo | None = None) -> None:
        """The search page, written *after* the index it describes.

        The caveat above the results says how much of the archive the search
        can actually see. It used to be built from ``stats.unreadable_pages``,
        which is not that number: a blank page and a page that is a photograph
        are both perfectly readable and neither is in the index, because
        neither has any text to index. On the demo that was the difference
        between "Search covers 15 of 16 pages" and the 14 pagefind indexed -
        an error of one, in the direction that flatters the archive, in the one
        sentence whose whole job is not to.

        ``info.pages_indexed`` is what pagefind reports it took, so that is what
        this passes on. It is only known once the index has been built, which is
        why :meth:`run` writes this page near the end rather than rewriting it:
        one page written once, at the point where the number exists.

        Two numbers go into the page and both are stated, not derived.
        ``indexedPages`` is what pagefind reported it took and is what
        ``assets/search.js`` renders as *covered*; ``unreadablePages`` is the
        remainder and is what it counts as missing. The name is now the only
        inaccurate thing about that key - the pages it counts are every page
        the search can find nothing on, and a blank page and a photograph are
        two of them - which is why the sentences it feeds no longer claim the
        difference is ink nobody could read.
        """
        if not self.cfg.search.enabled:
            return
        stats = self.collection.stats
        indexed = max(0, min(stats.pages, info.pages_indexed if info is not None else 0))
        config = _json_block(
            {
                "root": "../",
                "minQuery": self.cfg.search.min_query,
                "pages": stats.pages,
                "indexedPages": indexed,
                "unreadablePages": stats.pages - indexed,
            }
        )
        self.render("search.html.jinja", "search/index.html", nav="search", search_config=config)

    def build_document(self, doc: Document) -> None:
        root = "../../"
        items = []
        for page in doc.pages:
            thumb = None
            if page.thumbs:
                webp = [t for t in page.thumbs if t.format == "webp"] or page.thumbs
                thumb = webp[0].path
            items.append(
                {
                    "page": page,
                    "thumb": thumb,
                    "url": f"p/{page.number}/index.html",
                    "aspect": f"{page.image_width or 1000} / {page.image_height or 1294}",
                }
            )
        original = self.original_url(doc, root)
        self.render(
            "document.html.jinja",
            f"d/{doc.id}/index.html",
            nav="browse",
            wrap="",
            doc=doc,
            pages=items,
            size_human=human_bytes(doc.size_bytes, t=self.t),
            original_url=original,
            ribbon=ribbon(doc.pages, base="p/{n}/index.html", height=44, t=self.t),
            ribbon_label=_ribbon_label(doc.pages, t=self.t),
            page_description=self.t.t(
                "meta.document",
                title=doc.title,
                pages=self.t.t("count.pages", count=doc.page_count),
            ),
        )

    def build_page(self, doc: Document, page: Page) -> None:
        root = "../../../../"
        base = f"d/{doc.id}/p/{page.number}/index.html"
        original = self.original_url(doc, root)

        legend_rows = exemptions_mod.legend(page.exemptions, jurisdiction=self.collection.jurisdiction)

        self.render(
            "page.html.jinja",
            base,
            nav="browse",
            wrap="",
            doc=doc,
            page=page,
            page_lang=page.language or None,
            doc_url=f"{root}d/{doc.id}/index.html",
            canonical=(self.collection.base_url.rstrip("/") + "/" + base)
            if self.collection.base_url
            else base,
            prev_url=f"../{page.number - 1}/index.html" if page.number > 1 else None,
            next_url=f"../{page.number + 1}/index.html" if page.number < doc.page_count else None,
            # A reader on page 7 of 200 has two links and no map. The strip is
            # the map the document page already carries, marked with where they
            # are, at the height it is given everywhere else it appears.
            ribbon=ribbon(
                doc.pages, base="../{n}/index.html", height=28, current=page.number, t=self.t
            ),
            # Two sentences joined by a catalogue entry rather than a space:
            # the strip's reading and "you are here" are separate statements,
            # and a language that wants them the other way round can have them.
            ribbon_label=self.t.t(
                "ribbon.with_here",
                label=_ribbon_label(doc.pages, t=self.t),
                here=self.t.t("ribbon.here", number=page.number),
            ),
            original_url=original,
            display_lines=display_lines(page, jurisdiction=self.collection.jurisdiction),
            exemption_legend=legend_rows,
            quality_note=_quality_note(page, t=self.t),
            empty_text=_empty_text(page, t=self.t),
            page_data=page_payload_block(page),
            page_description=self.t.t("meta.page", number=page.number, title=doc.title),
        )
        self.write(f"data/{doc.id}/{page.number}.json", page_payload(page))

    # -- data ------------------------------------------------------------

    def build_data(self) -> None:
        # One small file the whole client layer shares: search.js names a
        # document by its id from it, and it is the only place a control number
        # can be resolved to a page without opening the document it is in.
        #
        # Keys are document slugs, which are `[a-z0-9-]` and never start with an
        # underscore, so `_legend` cannot collide with one.
        docs: dict[str, Any] = {}
        for doc in self.collection.documents:
            entry: dict[str, Any] = {"t": doc.title, "p": doc.page_count}
            entry.update(_stamps(doc))
            docs[doc.id] = entry

        # The exemption vocabulary is a Python dict and nothing else can read
        # it, so a code found in the markup - or typed into the palette - has no
        # gloss anywhere on the client. Only the codes this release actually
        # cites are published; the whole vocabulary is several kilobytes of law
        # nobody on this site is withheld under.
        #
        # What the two additions cost, measured on the demo: this file goes from
        # 180 to 672 bytes. The stamps are 99 of that - 8.2 bytes per stamped
        # page, which is ~164 KB before compression at the 20,000-page ceiling,
        # and highly repetitive digits that gzip flattens. The legend is 393
        # bytes for four codes and is bounded by how many distinct exemptions a
        # release cites, not by how large it is. Only the search page fetches
        # this file, and the service worker precaches it.
        legend = dict(
            exemptions_mod.legend(
                self.collection.stats.exemption_counts,
                jurisdiction=self.collection.jurisdiction,
            )
        )
        if legend:
            docs["_legend"] = legend
        self.write("data/docs.json", json.dumps(docs, separators=(",", ":"), ensure_ascii=False))

        manifest = {
            "stackroom": __version__,
            "built_at": self.collection.build.built_at,
            "source_digest": self.collection.build.source_digest,
            "tools": self.collection.build.tool_versions,
            "title": self.collection.title,
            "base_url": self.collection.base_url,
            "stats": to_jsonable(self.collection.stats),
            "documents": [
                {
                    "id": doc.id,
                    "title": doc.title,
                    "filename": doc.filename,
                    "sha256": doc.sha256,
                    # Three fields about the copy in files/, because a copy is
                    # not always the source. `sha256` above is the file as the
                    # agency sent it - the number a source, a re-hoster or
                    # another archive can compare against. `published_sha256`
                    # is the file a reader can actually download and hash here.
                    # They are the same number unless `metadata_stripped` is
                    # true, in which case the original was rewritten from its
                    # page tree to drop its metadata and its earlier revisions,
                    # and a mismatch is the design rather than tampering.
                    #
                    # `published_as` has to be here too: the published name is
                    # the slug plus an extension taken from the file's own
                    # bytes, not from `filename`, so a reader cannot construct
                    # it. All three are null when nothing was published, which
                    # is what `safety.publish_originals = false` means.
                    "published_as": (
                        f"files/{self.original_name(doc)}"
                        if doc.id in self.published
                        else None
                    ),
                    "published_sha256": (
                        self.published[doc.id].sha256 if doc.id in self.published else None
                    ),
                    "metadata_stripped": (
                        self.published[doc.id].stripped if doc.id in self.published else False
                    ),
                    "bytes": doc.size_bytes,
                    "pages": doc.page_count,
                    "bates_prefix": doc.bates_prefix,
                    "bates_gaps": [list(g) for g in doc.bates_gaps],
                    "redacted_pages": doc.redacted_pages,
                    "unreadable_pages": doc.unreadable_pages,
                }
                for doc in self.collection.documents
            ],
        }
        self.write("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    # -- orchestration ---------------------------------------------------

    def run(self) -> BuildReport:
        self.copy_assets()
        # Before every other `build_*`: it sets the `compare_enabled` template
        # global the masthead reads, and the search index and the offline
        # bundle both take an inventory of what is on disk, so a section
        # written after them is in neither. A build with no comparison
        # attached sets the flag to false and writes nothing, which is why the
        # call is unconditional. See docs/COMPARING.md section 7.
        compare_mod.build(self)
        self.copy_originals()
        self.build_index()
        self.build_browse()
        self.build_about()
        self.build_withheld()
        negative_mod.build(self)
        # `build_search` is not here: the caveat it writes has to name the
        # number of pages the index actually holds, and that is not known until
        # the index has been built, below.
        for doc in self.collection.documents:
            self.build_document(doc)
            for page in doc.pages:
                self.build_page(doc, page)
                self.report.pages += 1
            self.report.documents += 1
        self.build_data()

        search_info: search_mod.IndexInfo | None = None
        if self.cfg.search.enabled:
            language = self.index_language()
            textless = sum(
                1 for doc in self.collection.documents for page in doc.pages if not page.words
            )
            search_info = info = search_mod.build_index(self.out, language=language)
            # Pages with no text are not indexed, and should not be: a blank
            # page and a page withheld in full have nothing to find. The index
            # cannot know that, so it reports them as a problem; we can, so we
            # translate it into something true.
            info.warnings = [
                w for w in info.warnings if not (textless and "no data-pagefind-body" in w)
            ]
            if textless:
                info.warnings.append(
                    f"{textless} page(s) hold no text at all - blank, pictorial, or withheld "
                    "in full - so they are not in the search index"
                )
            self.report.search = info
            _prune_pagefind_ui(self.out)

        # Now, and not before: the page says how many pages the index holds.
        # Still ahead of the offline bundle, which takes its inventory from the
        # disk and would otherwise leave the search page out of the stored copy.
        self.build_search(search_info)

        self.report.media_bytes = sum(
            f.stat().st_size for f in (self.out / "media").rglob("*") if f.is_file()
        )
        # Last, and it has to be last: it takes an inventory of what is on disk,
        # and anything written after it - the search index most of all - would
        # be missing from what a reader can store. It writes through
        # `builder.write`, so the file and byte counts stay true, and it appends
        # its own warnings to the report.
        offline_mod.write_offline(self)

        # Last: what the interface could not say in the language it was asked
        # for. It is a fact about this build like any other warning, and an
        # operator who added a catalogue wants to hear it on the build that
        # used it rather than from a reader.
        if self.report.warnings is None:
            self.report.warnings = []
        self.report.warnings.extend(self.t.report())
        return self.report


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
    _prune_unused_wasm(bundle)


def _prune_unused_wasm(bundle: Path) -> None:
    """Drop the stemmer files no page of this site can ask for.

    Pagefind writes `wasm.unknown.pagefind` - 68,024 bytes, the language-less
    fallback - beside the per-language one. A client loads the file named by
    its language's `wasm` key in pagefind-entry.json and nothing else, so when
    every language in that manifest names a real stemmer the fallback is dead
    weight in every clone of the archive. When any language has `wasm: null`
    the fallback is exactly what it will load, so it stays.
    """
    try:
        entry = json.loads((bundle / "pagefind-entry.json").read_text(encoding="utf-8"))
        languages = entry["languages"]
    except (OSError, ValueError, KeyError, TypeError):
        return  # if the manifest cannot be read, keep everything
    wanted = {info.get("wasm") for info in languages.values() if isinstance(info, dict)}
    if not wanted or None in wanted:
        return
    keep = {f"wasm.{name}.pagefind" for name in wanted}
    for path in bundle.glob("wasm.*.pagefind"):
        if path.name not in keep:
            path.unlink(missing_ok=True)


def _stamps(doc: Document) -> dict[str, Any]:
    """A document's control numbers, one per page, as compactly as is honest.

    A stamp is a prefix and a counter - ``OCA-2018-04412-000007`` - and the
    prefix is the same string on every page of a production, so it is written
    once as ``bp`` and taken off the front of each stamp. ``b`` stays index-
    aligned with the pages, so ``b[n - 1]`` is page *n* with no lookup table,
    and a page whose stamp could not be read is an empty string.

    The prefix is only factored out when every page has a stamp, which keeps
    the join unconditional: with ``bp`` present ``bp + b[i]`` is always the
    whole number, and without it ``b[i]`` already is. Otherwise an unread page
    would come back as the prefix on its own, which looks like a control number
    and is not one.

    Nothing is written at all for a document with no stamps anywhere, which is
    every collection that was never produced under a numbering scheme.
    """
    stamps = [page.bates or "" for page in doc.pages]
    if not any(stamps):
        return {}
    if not all(stamps):
        return {"b": stamps}
    prefix = os.path.commonprefix(stamps)
    if len(prefix) < 2:
        return {"b": stamps}
    cut = len(prefix)
    return {"bp": prefix, "b": [s[cut:] for s in stamps]}


def _quality_note(page: Page, *, t: i18n.Translator | None = None) -> dict[str, str] | None:
    """The warning that stands above a transcription the recogniser doubts."""
    t = t or _english()
    verdict = page.quality.verdict
    if verdict is PageVerdict.UNREADABLE:
        return {
            "heading": str(t.t("quality.unreadable_heading")),
            "body": str(t.t("quality.unreadable_body")),
        }
    if verdict is PageVerdict.SUSPECT:
        return {
            "heading": str(t.t("quality.suspect_heading")),
            "body": str(t.t("quality.suspect_body")),
        }
    return None


def _empty_text(page: Page, *, t: i18n.Translator | None = None) -> str:
    """What to print where a transcription would be, when there is none.

    Five different sentences and not one of them is "no text": a blank page, a
    photograph, a page the recogniser failed on and a page that never rendered
    are four different facts, and the reader is deciding what to trust.
    """
    t = t or _english()
    verdict = page.quality.verdict
    if verdict is PageVerdict.BLANK:
        return str(t.t("empty.blank"))
    if verdict is PageVerdict.PICTORIAL:
        return str(t.t("empty.pictorial"))
    if verdict is PageVerdict.UNREADABLE:
        return str(t.t("empty.unreadable"))
    if not page.images:
        # No rendering and no text: the page was queued because the document's
        # page tree claims it, and nothing could be produced for it. Saying "no
        # text was found" would be a claim about a page nobody looked at, which
        # is the same mistake as calling it clear.
        return str(t.t("empty.unrendered"))
    return str(t.t("empty.none"))


_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<rect width="16" height="16" fill="#fbfaf7"/>'
    '<g fill="#1c1a17">'
    '<rect x="2" y="3" width="2" height="10"/>'
    '<rect x="5" y="3" width="2" height="10"/>'
    '<rect x="8" y="4" width="2" height="9"/>'
    '<rect x="11" y="3" width="3" height="10" opacity=".45"/>'
    "</g></svg>"
)


def build_site(collection: Collection, cfg: Config, out_dir: Path) -> BuildReport:
    """Write *collection* to *out_dir* as a static website."""
    return SiteBuilder(collection, cfg, out_dir).run()


def attach_about(collection: Collection, cfg: Config) -> None:
    """Read ``about.md``, if the operator wrote one."""
    if cfg.about_path and cfg.about_path.is_file():
        collection.about_html = Markup(render_markdown(cfg.about_path.read_text(encoding="utf-8")))


__all__ = [
    "BuildReport",
    "SiteBuilder",
    "attach_about",
    "build_site",
    "display_lines",
    "human_bytes",
    "page_state",
    "ribbon",
]
