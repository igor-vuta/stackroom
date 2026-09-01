"""Making a published archive readable with the network switched off.

An archive that only exists while a server is up is not an archive. This
module generates a service worker, at build time, that stores enough of a
Stackroom site for a reader to keep reading it on a train, on a plane, behind a
firewall, or after the site is taken down.

Three tiers, because an archive can be gigabytes and spending a stranger's disk
without asking is hostile:

``precache``
    The shell - the standing pages, the stylesheet, the scripts (including
    ``assets/i18n.js``, which is what decides the interface's language), the
    fonts those pages actually use, the favicon and the document list. Stored
    on the first visit, unasked, and it is what makes the archive open at all
    with no network. Measured on the demo collection on 2026-09-01: 25 files,
    496,322 bytes, of which the stylesheet is 131 KB and the fonts are 132 KB.
    Re-measure rather than trusting that figure - the number in this docstring
    had been 145 KB since before the stylesheet was assembled from parts.

``runtime``
    Page HTML, thumbnails, page images, word boxes and the search index, kept
    as the reader visits them. Original documents are excluded: they are the
    largest thing in the archive and nobody asked for a permanent copy.

``the whole archive``
    Everything, including the originals, on an explicit action, with the size
    stated before the first byte is fetched. That is what ``offline.json`` is
    for - it is the only generated file whose size is proportional to the
    collection, so it is fetched on demand and never precached.

Cache versioning
----------------
The cache name carries :func:`cache_version`, a digest over (a) the collection's
``source_digest``, (b) the generator version and the tools that built it, (c)
the bytes of every precached file, and (d) the path and size of every published
file. A rebuild that changes anything a reader would see changes the name, so
the new build lands in a new cache and the old one is deleted on activation.
Two builds of the same input produce the same digest, which is ARCHITECTURE.md
guarantee 6 and the reason a mirror can verify it published the same bytes.

Fonts
-----
Stackroom ships 24 font files and an English collection downloads a handful of
them; ``unicode-range`` sees to that at runtime, but a precache list has to make
the same decision up front or it would store every subset to use five. So the
ranges are parsed out of ``fonts.css`` and matched against the codepoints that
are actually in the precached HTML. Measured on the demo collection on
2026-09-01: 5 files, 132,456 bytes - sans, serif regular, serif italic, serif
semibold and mono - which is what a browser fetches for those pages.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .site import SiteBuilder

ASSETS = Path(__file__).resolve().parent.parent / "assets"

SW_NAME = "sw.js"
"""The worker goes in the site root and nowhere else.

A service worker's default scope is the directory it is served from, and a
static host cannot send the ``Service-Worker-Allowed`` header that would widen
it. A worker at ``assets/sw.js`` could only ever control ``assets/``.
"""

INVENTORY_NAME = "offline.json"

VERSION_LENGTH = 16
"""Hex characters of the digest that name the cache. Sixty-four bits: a
collision would need two different builds, and there is no adversary here."""

STANDING_PAGES = (
    "index.html",
    "browse/index.html",
    "about/index.html",
    "withheld/index.html",
    "search/index.html",
)
"""The pages every archive has, in the order the navigation lists them.

Not the whole list: :func:`standing_pages` also *discovers* any other page the
builder wrote outside ``d/``, so a section added later - ``withheld/negative/``,
say - is stored offline without anybody having to remember this tuple. The
names here are kept so the order is stable and the intent is readable.
"""

STANDING_DEPTH = 3
"""How far down to look for a standing page. ``withheld/negative/index.html``
is depth 3; nothing legitimate is deeper, and the limit stops a pathological
output directory from producing a precache manifest with a thousand entries."""

STANDING_LIMIT = 24
"""More standing pages than this and something has gone wrong. The extras are
still published and still work; they are simply not stored unasked."""

ORIGINALS = "files/"
"""Excluded from the automatic caches. Included when a reader asks for all."""

SHELL_ASSETS = (
    "assets/stackroom.css",
    "assets/i18n.js",
    "assets/viewer.js",
    "assets/search.js",
    "assets/favicon.svg",
)
"""The top-level assets the shell needs. Not the list that is stored.

:func:`shell_assets` *discovers* what the builder actually wrote, the way
:func:`standing_pages` discovers sections, because a hard-coded list stops being
the truth the first time somebody adds a file - and this one had already:
``assets/i18n.js`` is a ``<script src>`` in the head of every page and carries
the interface strings for every script, and the tuple that predates it left the
shell that promises to open offline without the file that decides its language.
These names are kept so that a file which *disappears* is still reported, and so
the intent is readable beside :data:`STANDING_PAGES`.
"""

SHELL_DATA = "data/docs.json"
"""The document list. Not under ``assets/``, and the one page of the shell that
is a data file rather than a page: with it, ``browse/index.html`` works offline."""

_TOKEN_BUILD = "__STACKROOM_BUILD__"
_TOKEN_PRECACHE = "['__STACKROOM_PRECACHE__']"
_TOKEN_INVENTORY = "__STACKROOM_INVENTORY__"
_TOKEN_TOTALS = '{"__STACKROOM_TOTALS__": 0}'

# One @font-face block: the file it names and the range it claims.
_FACE_RE = re.compile(r"@font-face\s*\{(?P<body>[^}]*)\}", re.S)
_SRC_RE = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)")
_RANGE_RE = re.compile(r"unicode-range\s*:\s*([^;}]+)")
_RANGE_ITEM_RE = re.compile(r"U\+([0-9A-Fa-f]+)(?:-([0-9A-Fa-f]+))?")
_STYLE_RE = re.compile(r"font-style\s*:\s*([a-z]+)")

# The elements that put a browser into an italic face. `<i>` is deliberately
# not on this list: Stackroom's own front-page key uses `<i></i>` as an empty
# swatch element styled entirely from CSS, so counting it would match every
# archive ever built and the filter would never do anything. Markdown emphasis
# renders as `<em>`, which is what the operator's about.md actually produces.
# Weight has no equivalent list - a heading is 600 by stylesheet, not by tag -
# so weight is not filtered and the semibold face is always precached.
_ITALIC_RE = re.compile(r"<(em|cite|var|dfn|address)\b", re.I)

# Text outside tags, which is where the codepoints a font has to draw are.
_TAG_RE = re.compile(r"<[^>]*>")
_ENTITY_RE = re.compile(r"&(#x?[0-9A-Fa-f]+|[a-zA-Z]+);")


@dataclass(slots=True)
class OfflineInfo:
    """What was generated, for the build report and for the tests."""

    version: str = ""
    precache: list[str] = field(default_factory=list)
    precache_bytes: int = 0
    total_files: int = 0
    total_bytes: int = 0
    originals_bytes: int = 0
    fonts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# working out which fonts a page needs
# --------------------------------------------------------------------------


def _parse_faces(css: str) -> list[tuple[str, list[tuple[int, int]], str]]:
    """``[(filename, ranges, style), ...]`` from a stylesheet's @font-face rules.

    A face with no ``unicode-range`` claims everything, which is what the CSS
    means, and is represented here as the full range rather than as a special
    case the caller has to remember.
    """
    faces: list[tuple[str, list[tuple[int, int]], str]] = []
    for match in _FACE_RE.finditer(css):
        body = match.group("body")
        src = _SRC_RE.search(body)
        if not src:
            continue  # a `local()`-only fallback face has no file to store
        name = src.group(1).rsplit("/", 1)[-1]
        if not name.endswith(".woff2"):
            continue
        ranges: list[tuple[int, int]] = []
        found = _RANGE_RE.search(body)
        if found:
            for lo, hi in _RANGE_ITEM_RE.findall(found.group(1)):
                start = int(lo, 16)
                ranges.append((start, int(hi, 16) if hi else start))
        style = _STYLE_RE.search(body)
        faces.append((name, ranges or [(0, 0x10FFFF)], style.group(1) if style else "normal"))
    return faces


def _codepoints(html: str) -> set[int]:
    """Every codepoint that will be drawn by a font on this page.

    Markup is stripped first: an attribute value is never rendered, and
    counting it would pull in whole subsets for a URL nobody sees. Numeric and
    named entities are approximated rather than resolved - the numeric ones
    exactly, the named ones by assuming they are Latin-1 or punctuation, which
    is true of every entity these templates emit.
    """
    text = _TAG_RE.sub(" ", html)
    # Whitespace and control characters are dropped. A browser does not pull
    # down a Latin subset because a Greek page contains a space, and counting
    # them would make every subset match every page.
    points = {
        ord(ch)
        for ch in _ENTITY_RE.sub(" ", text)
        if not ch.isspace() and ord(ch) >= 0x20
    }
    for entity in _ENTITY_RE.findall(text):
        if entity.startswith("#x") or entity.startswith("#X"):
            points.add(int(entity[2:], 16))
        elif entity.startswith("#"):
            points.add(int(entity[1:]))
        else:
            points.add(0x00A0)  # &nbsp; &amp; &mdash; - all Latin-1 or punctuation
    return points


def fonts_for(html_texts: list[str], css: str) -> list[str]:
    """The font files these pages will actually cause a browser to fetch.

    The same decision a browser makes from ``unicode-range``, made at build
    time so the precache does not store twenty subsets to use four.
    """
    faces = _parse_faces(css)
    points: set[int] = set()
    italic = False
    for text in html_texts:
        points |= _codepoints(text)
        italic = italic or bool(_ITALIC_RE.search(text))
    wanted: list[str] = []
    for name, ranges, style in faces:
        if name in wanted:
            continue
        if style != "normal" and not italic:
            # Measured: an English collection with no <em> anywhere still
            # matched the italic core face on codepoints alone, which would
            # have stored 28,768 bytes a browser never asks for.
            continue
        if any(lo <= p <= hi for p in points for lo, hi in ranges):
            wanted.append(name)
    return sorted(wanted)


# --------------------------------------------------------------------------
# the inventory
# --------------------------------------------------------------------------


def standing_pages(out: Path) -> list[str]:
    """Every page of the site that is not about one document.

    Document and page views are runtime-cached as a reader visits them - there
    can be twenty thousand of them - so the shell is everything else: the
    front page, the sections, and whatever a later section adds beside them.
    Discovered rather than listed, because a hard-coded list silently stops
    being the truth the first time somebody adds a page.
    """
    found: list[str] = []
    for path in sorted(out.rglob("index.html")):
        rel = path.relative_to(out).as_posix()
        parts = rel.split("/")
        if parts[0] in ("d", "files", "media", "data", search_bundle_dir()):
            continue
        if len(parts) > STANDING_DEPTH:
            continue
        found.append(rel)
    ordered = [p for p in STANDING_PAGES if p in found]
    ordered += [p for p in found if p not in ordered]
    return ordered[:STANDING_LIMIT]


def search_bundle_dir() -> str:
    """``_pagefind``, without importing the search module at module scope."""
    from . import search as search_mod

    return search_mod.BUNDLE_DIR


def _walk(out: Path) -> list[tuple[str, int]]:
    """Every published file as ``(path relative to the site root, bytes)``.

    Sorted, so the digest built from it is stable across filesystems that hand
    back directory entries in different orders.
    """
    files: list[tuple[str, int]] = []
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(out).as_posix()
        if rel in (SW_NAME, INVENTORY_NAME):
            continue  # the worker never stores itself
        files.append((rel, path.stat().st_size))
    files.sort()
    return files


def shell_assets(out: Path) -> list[str]:
    """Every file the builder wrote directly inside ``assets/``.

    The top level only, and that is the whole rule: ``assets/js/`` is filtered
    against the scripts the templates actually load and ``assets/fonts/``
    against the codepoints the precached HTML actually contains, so those two
    directories are decided elsewhere and on evidence. What is left in
    ``assets/`` itself is the shell: one stylesheet, one favicon, ``i18n.js``
    in the head of every page, and the two per-section scripts - ``viewer.js``
    on a page of a document and ``search.js`` on the search page - which are
    small, and which a reader with no network reaches by following a link the
    precached document list already gives them.

    Derived rather than listed because the list drifted: see
    :data:`SHELL_ASSETS`. Sorted, because the precache manifest goes into the
    cache name and two builds of the same site have to agree.
    """
    directory = out / "assets"
    if not directory.is_dir():
        return []
    return sorted(f"assets/{p.name}" for p in directory.iterdir() if p.is_file())


def precache_list(
    out: Path, *, extra_scripts: list[str] | None = None, search_enabled: bool = True
) -> tuple[list[str], list[str]]:
    """``(paths to precache, warnings)`` for the shell of the site at *out*.

    Only files that exist are listed. A precache manifest naming a file that
    is not there is the sort of thing that works on the machine that built it
    and 404s for everyone else.
    """
    warnings: list[str] = []
    shell: list[str] = []

    found = standing_pages(out)
    for page in found:
        if page == "search/index.html" and not search_enabled:
            continue
        shell.append(page)
    for page in STANDING_PAGES:
        if page == "search/index.html" and not search_enabled:
            continue
        if page not in found:
            warnings.append(f"{page} is missing, so it will not be readable offline")

    assets = shell_assets(out)
    shell.extend(assets)
    for asset in SHELL_ASSETS:
        if asset not in assets:
            warnings.append(f"{asset} is missing, so the archive will not open offline")
    if (out / SHELL_DATA).is_file():
        shell.append(SHELL_DATA)
    else:
        warnings.append(f"{SHELL_DATA} is missing, so the document list will not be readable offline")

    for script in sorted(extra_scripts or []):
        candidate = f"assets/js/{script}"
        if (out / candidate).is_file():
            shell.append(candidate)

    css_path = out / "assets" / "stackroom.css"
    if css_path.is_file():
        html = [(out / p).read_text(encoding="utf-8", errors="replace") for p in shell
                if p.endswith(".html")]
        css = css_path.read_text(encoding="utf-8", errors="replace")
        for font in fonts_for(html, css):
            candidate = f"assets/fonts/{font}"
            if (out / candidate).is_file():
                shell.append(candidate)
            else:
                warnings.append(f"{candidate} is named by the stylesheet but was not published")

    return shell, warnings


def cache_version(
    out: Path,
    *,
    source_digest: str,
    generator: str,
    precache: list[str],
    files: list[tuple[str, int]],
) -> str:
    """A name for this build's cache, stable for identical input.

    Four things go in, and each of them answers a way the archive can change
    without the others noticing:

    * ``source_digest`` - the documents themselves.
    * ``generator`` - the version of stackroom and the tools it shelled out to,
      because the same PDFs rendered by a newer encoder are different bytes.
    * the *content* of every precached file, because a stylesheet edit changes
      nothing else on this list.
    * the path and size of every published file, because adding, removing or
      re-encoding a page changes what the reader has stored.
    """
    h = hashlib.sha256()
    h.update(b"stackroom-offline-1\0")
    h.update(source_digest.encode("utf-8") + b"\0")
    h.update(generator.encode("utf-8") + b"\0")
    for path in sorted(precache):
        blob = out / path
        h.update(path.encode("utf-8") + b"\0")
        if blob.is_file():
            h.update(hashlib.sha256(blob.read_bytes()).digest())
        h.update(b"\0")
    h.update(b"inventory\0")
    for path, size in files:
        h.update(f"{path}:{size}\0".encode())
    return h.hexdigest()[:VERSION_LENGTH]


def render_service_worker(
    version: str,
    precache: list[str],
    *,
    totals: dict[str, int] | None = None,
    inventory: str = INVENTORY_NAME,
) -> str:
    """The worker template with its build-time constants filled in.

    The template is valid JavaScript before substitution as well as after, so
    it can be parsed, linted and read without running a build - which is also
    how :mod:`tests.test_offline` checks it.
    """
    template = (ASSETS / SW_NAME).read_text(encoding="utf-8")
    for token in (_TOKEN_BUILD, _TOKEN_PRECACHE, _TOKEN_INVENTORY, _TOKEN_TOTALS):
        if token not in template:
            raise ValueError(f"{SW_NAME} no longer contains {token}; the two files have drifted")
    body = template.replace(_TOKEN_BUILD, version)
    body = body.replace(
        _TOKEN_PRECACHE,
        json.dumps(sorted(precache), separators=(",", ":"), ensure_ascii=False),
    )
    body = body.replace(
        _TOKEN_TOTALS,
        json.dumps(totals or {"files": 0, "bytes": 0, "originals": 0}, sort_keys=True,
                   separators=(",", ":")),
    )
    return body.replace(_TOKEN_INVENTORY, inventory)


def build_inventory(
    version: str, files: list[tuple[str, int]], *, originals_bytes: int
) -> dict[str, Any]:
    """What "store the whole archive" will cost, itemised.

    ``files`` is a list of two-element arrays rather than objects: on a 20,000
    page collection that is about 120,000 entries, and ``["media/x/p1@900.avif",
    52776]`` is a third of the bytes of the same thing with keys on it.
    """
    return {
        "build": version,
        "files": [[path, size] for path, size in files],
        "bytes": sum(size for _, size in files),
        "originals_bytes": originals_bytes,
    }


# --------------------------------------------------------------------------
# the build hook
# --------------------------------------------------------------------------


def write_offline(builder: SiteBuilder) -> OfflineInfo:
    """Generate the service worker and its inventory into a finished site.

    Must run last: it takes an inventory of what is on disk, and anything
    written after it - the search index most of all - would be missing from it.
    """
    out = builder.out
    collection = builder.collection
    generator = " ".join(
        [collection.build.version]
        + [f"{k}={v}" for k, v in sorted(collection.build.tool_versions.items())]
    )

    precache, warnings = precache_list(
        out,
        extra_scripts=list(getattr(builder, "extra_scripts", []) or []),
        search_enabled=builder.cfg.search.enabled,
    )
    files = _walk(out)
    version = cache_version(
        out,
        source_digest=collection.build.source_digest,
        generator=generator,
        precache=precache,
        files=files,
    )

    originals_bytes = sum(size for path, size in files if path.startswith(ORIGINALS))
    totals = {
        "files": len(files),
        "bytes": sum(size for _, size in files),
        "originals": originals_bytes,
    }
    builder.write(SW_NAME, render_service_worker(version, precache, totals=totals))
    builder.write(
        INVENTORY_NAME,
        json.dumps(
            build_inventory(version, files, originals_bytes=originals_bytes),
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    )

    info = OfflineInfo(
        version=version,
        precache=precache,
        precache_bytes=sum((out / p).stat().st_size for p in precache if (out / p).is_file()),
        total_files=len(files),
        total_bytes=sum(size for _, size in files),
        originals_bytes=originals_bytes,
        fonts=[p.rsplit("/", 1)[-1] for p in precache if p.endswith(".woff2")],
        warnings=warnings,
    )
    if builder.report.warnings is not None:
        builder.report.warnings.extend(warnings)
    return info


__all__ = [
    "INVENTORY_NAME",
    "STANDING_PAGES",
    "SW_NAME",
    "OfflineInfo",
    "build_inventory",
    "cache_version",
    "fonts_for",
    "precache_list",
    "render_service_worker",
    "write_offline",
]
