"""Building the Pagefind search index over the generated pages.

Search is an accelerant, never a prerequisite. Every page of the archive is a
real HTML file that reads, cites and crawls with JavaScript switched off, so a
failure in this module has to degrade the site rather than destroy it. Almost
everything here is about failing in a way the operator can see and act on.

Why a subprocess and never the Python API
-----------------------------------------
The ``pagefind`` PyPI package ships both a binary and a Python API, and the API
is two orders of magnitude slower for byte-identical output. Measured here on
100 synthetic pages of 120 words each: ``add_custom_record`` takes 10.11 s -
101 s per 1,000 pages - while handing the same corpus to the binary costs about
1.0 s per 1,000 pages. The API is a base64 JSON pipe to that same Rust binary
with one round trip per page, and the framing is the whole difference. So we
write the pages to disk, point the binary at the directory, and let it walk.

What a reader downloads before they can type
--------------------------------------------
Measured in Chromium against a real 1.5.2 bundle, five things are fetched
before the first keystroke can be answered - the last three from inside the
web worker, where they do not show up in the page's own resource timings:

===========================  ==========  =============================
file                         on disk     on the wire
===========================  ==========  =============================
``pagefind.js``                45,555 B   12,859 B gzipped
``pagefind-worker.js``         41,255 B   11,912 B gzipped
``pagefind-entry.json``           171 B   fetched with a cache-buster
``wasm.<lang>.pagefind``       72,209 B   already gzipped, sent as-is
``pagefind.<hash>.pf_meta``   5.5 B/page  already gzipped, sent as-is
===========================  ==========  =============================

That is ``97,151 + 5.5 x pages`` bytes: 122 KB at 5,000 pages, 202 KB at
20,000, which is the table in ARCHITECTURE.md. The per-page term is the reason
the honest ceiling exists, and :func:`estimate_cold_start` exists so the CLI
can put the number in front of the operator before they publish.

Hosting
-------
``.pf_meta``, ``.pf_index``, ``.pf_fragment`` and ``wasm.*.pagefind`` are
already compressed; gzipping them again *grows* them (measured: a 103-byte
``.pf_meta`` becomes 124 bytes, the 72,209-byte wasm becomes 72,252). See
:func:`hosting_notes`, which is written for pasting into an nginx config or a
hosting README.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = [
    "BUNDLE_DIR",
    "META_BYTES_PER_PAGE",
    "PAGE_GLOB",
    "RUNTIME_BYTES",
    "IndexInfo",
    "SearchError",
    "build_index",
    "ensure_nojekyll",
    "estimate_cold_start",
    "hosting_notes",
    "pagefind_available",
    "scale_warnings",
]

_LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# where things go
# --------------------------------------------------------------------------

BUNDLE_DIR = "_pagefind"
"""Where the index goes, relative to the site root.

Pagefind 1.5.2 defaults to ``pagefind`` - it moved out of ``_pagefind`` in 1.0
precisely because of the Jekyll problem :func:`ensure_nojekyll` deals with -
but ARCHITECTURE.md fixes the published layout at ``_pagefind/`` and the page
templates link into it. So we pass ``--output-subdir`` and pin the name rather
than tracking whatever pagefind's default happens to be this year.
"""

PAGE_GLOB = "d/*/p/*/index.html"
"""Only per-page files are indexed.

The overview, browse and about pages contain the same words as the pages they
summarise; indexing them would return a hit that no highlight can be drawn on,
because they have no word boxes. See "The search contract" in ARCHITECTURE.md.
"""

NOJEKYLL = ".nojekyll"


# --------------------------------------------------------------------------
# measured constants
# --------------------------------------------------------------------------

PAGEFIND_JS_GZIP = 12_859
"""``pagefind.js``, gzip -6 (45,555 B raw). gzip -9 saves 10 bytes."""

WORKER_JS_GZIP = 11_912
"""``pagefind-worker.js``, gzip -6 (41,255 B raw)."""

WASM_BYTES = 72_209
"""``wasm.en.pagefind`` as shipped. It is a gzip stream already; the
language-less fallback is 68,024 B, so this is the pessimistic one."""

ENTRY_JSON_BYTES = 171
"""``pagefind-entry.json``. Tiny, but it is on the critical path and it is
requested with a ``?ts=`` cache-buster, so it is never served from cache."""

RUNTIME_BYTES = PAGEFIND_JS_GZIP + WORKER_JS_GZIP + WASM_BYTES + ENTRY_JSON_BYTES
"""Everything a reader downloads that does not depend on how big the archive
is: 97,151 bytes."""

META_BYTES_PER_PAGE = 5.5
"""``.pf_meta`` grows linearly with the number of pages, and it is downloaded
in full before the first query.

Measured on synthetic corpora: 5.63 B/page at 250 pages, 5.41 at 1,000, 5.31 at
4,000, 5.24 at 12,000, 5.69 on a denser 1,200-page corpus, and 109,909 B at
20,000. The spread comes from vocabulary size rather than from any curve in the
line - the growth itself is straight - so 5.5 is the middle of it and the
estimate lands within about 4% either way. Close enough to warn on; not a
number to quote to three figures.
"""

SUPPORTED_PAGES = 20_000
"""The ceiling stackroom stands behind. Above it, search still works and the
CLI says what it will cost."""

DEGRADED_PAGES = 50_000
"""Above this the CLI requires ``--i-know``."""

_VERSION_TIMEOUT = 15.0
"""Seconds to wait for ``pagefind --version``. Long enough for a cold start on
a laptop that has just decompressed the binary, short enough that a hung
candidate does not stall the build."""

_TIMEOUT_FLOOR = 120.0
_TIMEOUT_PER_PAGE = 0.01
"""The binary indexes about 1,000 pages/s here, so a hundredth of a second per
page is a 10x margin. A timeout is not a performance budget; it is the line
past which something is wrong."""

_INSTALL_HINT = (
    "Install it with `pip install stackroom[search]`, or `npx pagefind`, and "
    "rebuild. The archive is fully readable without it: every page is a real "
    "HTML file. Only the search box will be missing."
)


class SearchError(RuntimeError):
    """The index could not be built and the operator has to decide something.

    Deliberately *not* raised when pagefind is simply not installed: that is a
    choice someone already made, search is an optional extra, and a site
    without it still keeps every guarantee in ARCHITECTURE.md.
    :func:`build_index` returns an :class:`IndexInfo` with a warning for that
    case. This exception is for pagefind being present and going wrong -
    including indexing nothing at all, which is the failure that would
    otherwise ship a site whose search box finds no documents.
    """


@dataclass(slots=True)
class IndexInfo:
    """What the index cost and what it will cost a reader.

    ``pages_indexed == 0`` with a warning means there is no search on this
    site; the caller decides whether that is acceptable and the build carries
    on. Any other zero is a bug in this module.
    """

    pages_indexed: int = 0
    index_bytes: int = 0
    """Total size of ``_pagefind/`` on disk.

    Not what anyone downloads. About 420 KB of it is the drop-in search UI that
    pagefind always writes and stackroom never loads, and most of the rest is
    fragments, which are fetched one per result the reader actually looks at.
    """

    files: int = 0
    cold_start_bytes: int = 0
    """What a reader downloads before they can type. Measured from the built
    bundle where possible, estimated otherwise."""

    language: str = "en"
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    tool: str = ""
    """Which pagefind ran, and how it was found, e.g. ``"pagefind 1.5.2
    (python -m pagefind)"``. An archive earns trust by being checkable, so this
    belongs in ``BuildInfo.tool_versions`` and in the footer with the rest of
    the build stamp. It is also the first thing to look at when two people
    build the same input and get different bytes."""

    @property
    def ok(self) -> bool:
        """True when this site has a working search index."""
        return self.pages_indexed > 0


# --------------------------------------------------------------------------
# finding the binary
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Runner:
    argv: tuple[str, ...]
    how: str
    """How it was found, for the build log. An operator debugging a version
    mismatch needs to know *which* pagefind ran, not just that one did."""

    version: str

    @property
    def described(self) -> str:
        return f"{self.version} ({self.how})"


def _candidates() -> Iterator[tuple[list[str], str]]:
    """Ways to run pagefind, best first.

    ``python -m pagefind`` leads because it is the one that came with this
    virtualenv, so its version matches whatever ``pip install`` resolved and it
    cannot be shadowed by a stale binary on PATH. The environment variable
    comes first only because someone who sets it is overriding on purpose;
    upstream's own service module honours it too.
    """
    override = os.environ.get("PAGEFIND_BINARY_PATH")
    if override:
        yield [override], f"PAGEFIND_BINARY_PATH={override}"
    yield [sys.executable, "-m", "pagefind"], "python -m pagefind"
    for name in ("pagefind", "pagefind_extended"):
        found = shutil.which(name)
        if found:
            yield [found], found
    # The wheels that carry the binary. Importing them is the last resort
    # because it only works when they are installed *and* `python -m pagefind`
    # is somehow broken, but it costs nothing to try.
    for module_name in ("pagefind_bin_extended", "pagefind_bin"):
        try:
            module = __import__(module_name)
            executable = Path(module.get_executable())
        except Exception:  # any import problem at all just means "not this one"
            continue
        yield [str(executable)], f"{module_name}.get_executable()"


def _resolve_runner() -> _Runner | None:
    """The first candidate that answers ``--version``, or None.

    Existence is not usability: the ``pagefind`` package can be installed
    without the platform binary it wraps, in which case ``python -m pagefind``
    imports fine and then dies. Running it is the only honest test.
    """
    for argv, how in _candidates():
        try:
            proc = subprocess.run(
                [*argv, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_VERSION_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _LOG.debug("pagefind candidate %s failed: %s", how, exc)
            continue
        if proc.returncode == 0:
            printed = f"{proc.stdout}\n{proc.stderr}".strip().splitlines()
            return _Runner(tuple(argv), how, printed[0] if printed else "pagefind")
        _LOG.debug("pagefind candidate %s exited %d", how, proc.returncode)
    return None


def pagefind_available() -> tuple[bool, str]:
    """``(usable, version-or-reason)``.

    The second element is written to be printed: either ``"pagefind 1.5.2
    (python -m pagefind)"`` or a sentence saying what to install.
    """
    runner = _resolve_runner()
    if runner is None:
        return False, f"pagefind was not found. {_INSTALL_HINT}"
    return True, runner.described


# --------------------------------------------------------------------------
# cold start
# --------------------------------------------------------------------------


def estimate_cold_start(pages: int) -> int:
    """Bytes a reader downloads before they can type, for a corpus this size.

    Use this before building - to warn an operator what they are about to
    publish - and :func:`build_index` for the measured number afterwards.
    """
    return RUNTIME_BYTES + round(META_BYTES_PER_PAGE * max(0, pages))


def _gzipped_size(path: Path) -> int:
    """What a normal host will put on the wire for this file.

    Level 6 is what nginx, Caddy and Cloudflare use unless told otherwise;
    level 9 buys ten bytes on ``pagefind.js`` and is not worth pretending to.
    """
    return len(gzip.compress(path.read_bytes(), 6))


def _measure_cold_start(bundle: Path, entry_language: dict[str, object], pages: int) -> int:
    """Add up the real files this build produced, falling back to the constants.

    Anything missing is counted at its measured size rather than zero: an
    under-report here would tell an operator their archive is cheaper to open
    than it is, which is the one direction this number must not be wrong in.
    """
    total = 0

    for name, fallback in (
        ("pagefind.js", PAGEFIND_JS_GZIP),
        ("pagefind-worker.js", WORKER_JS_GZIP),
    ):
        path = bundle / name
        total += _gzipped_size(path) if path.is_file() else fallback

    entry = bundle / "pagefind-entry.json"
    total += entry.stat().st_size if entry.is_file() else ENTRY_JSON_BYTES

    # The wasm is per-language and already gzipped, so it goes on the wire at
    # its size on disk. `wasm: null` in the entry means this language has no
    # stemmer and the client loads the generic one.
    wasm_key = entry_language.get("wasm") or "unknown"
    wasm = bundle / f"wasm.{wasm_key}.pagefind"
    total += wasm.stat().st_size if wasm.is_file() else WASM_BYTES

    meta_hash = entry_language.get("hash")
    meta = bundle / f"pagefind.{meta_hash}.pf_meta" if meta_hash else None
    if meta is not None and meta.is_file():
        total += meta.stat().st_size
    else:
        total += round(META_BYTES_PER_PAGE * pages)

    return total


# --------------------------------------------------------------------------
# the honest ceiling
# --------------------------------------------------------------------------


def scale_warnings(pages: int) -> list[str]:
    """Warnings that depend only on how many pages there are.

    Separated out so the CLI can say this *before* a three-hour ingest rather
    than after it.
    """
    warnings: list[str] = []
    if pages > DEGRADED_PAGES:
        warnings.append(
            f"{pages:,} pages is past what stackroom will stand behind. Readers "
            f"download {estimate_cold_start(pages) // 1024:,} KB before they can "
            "type, and a two-letter query matches most of the corpus, which "
            "takes seconds rather than milliseconds. Split the collection, or "
            "publish it and say plainly on the search page that it is slow."
        )
    elif pages > SUPPORTED_PAGES:
        warnings.append(
            f"{pages:,} pages is above the supported ceiling of {SUPPORTED_PAGES:,}. "
            f"Search still works: cold start is about "
            f"{estimate_cold_start(pages) // 1024:,} KB and common queries get "
            "slower, because latency tracks the number of hits rather than the "
            "size of the corpus."
        )
    return warnings


# --------------------------------------------------------------------------
# GitHub Pages
# --------------------------------------------------------------------------


def ensure_nojekyll(site_dir: Path) -> bool:
    """Write ``.nojekyll`` into the site root. Returns True if it created it.

    GitHub Pages runs Jekyll over anything without this file, and Jekyll
    deletes directories whose names begin with an underscore - which is exactly
    ``_pagefind``. The site deploys, every page renders, and search finds
    nothing at all, with no error anywhere. It is the most common way for an
    archive to be quietly broken on the most common host, and the fix is an
    empty file.
    """
    marker = site_dir / NOJEKYLL
    if marker.exists():
        return False
    marker.write_bytes(b"")
    return True


# --------------------------------------------------------------------------
# reading pagefind's mind
# --------------------------------------------------------------------------

_PAGES_RE = re.compile(r"Indexed\s+([\d,]+)\s+pages?\b")
_FOUND_RE = re.compile(r"Found\s+([\d,]+)\s+files?\s+matching")
_WORDS_RE = re.compile(r"Indexed\s+([\d,]+)\s+words?\b")

_NO_BODY = "Did not find a data-pagefind-body element"


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def _collect_warnings(output: str, language: str | None) -> list[str]:
    """Turn pagefind's chatter into things worth telling a person.

    Pagefind prints its degradations as ``Note:`` and carries on with exit
    code 0, so a build that succeeds can still have lost stemming or lost the
    body selector. Those are the two failures that make search look fine and
    behave wrongly, so they are promoted to warnings here.

    *language* is None when it was not forced, in which case complaining about
    the shape of the code would be complaining about something we never used.
    """
    warnings: list[str] = []

    if _NO_BODY in output:
        warnings.append(
            "No element carried data-pagefind-body, so pagefind indexed the "
            "whole <body> of each page - navigation, footer and all. Match "
            "positions then count those words too and every highlight lands on "
            "the wrong token. This breaks guarantee 3 in ARCHITECTURE.md."
        )

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Note:", "Warning:")):
            # Pagefind prints its notes to stdout and stderr both, so the same
            # sentence arrives twice; _dedupe below collapses them.
            warnings.append(f"pagefind: {stripped}")

    words = _WORDS_RE.search(output)
    if words and _int(words.group(1)) == 0:
        warnings.append(
            "pagefind indexed 0 words. The pages matched the glob but their "
            "data-pagefind-body elements are empty, so search will find nothing."
        )

    if language is not None and not re.fullmatch(r"[a-z]{2}(-[A-Za-z0-9]{2,8})?", language):
        warnings.append(
            f"language {language!r} is not an ISO 639-1 code, so pagefind will "
            "index it without a stemmer and searches will not match across word "
            "endings. Tesseract codes are three letters; pagefind wants two "
            "('eng' -> 'en', 'fra' -> 'fr', 'deu' -> 'de')."
        )

    return warnings


def _survey(site_dir: Path, glob: str) -> tuple[int, int, list[str]]:
    """``(files matching the glob, HTML files in total, a few example paths)``.

    Only ever used for diagnostics and for sizing the timeout, never to decide
    what gets indexed: pagefind's glob syntax has braces and ``**`` that
    :meth:`PurePosixPath.match` does not, so this can undercount a caller's
    exotic pattern. That is why a disagreement with pagefind is never an error.
    """
    matched = 0
    html = 0
    examples: list[str] = []
    for root, dirs, names in os.walk(site_dir):
        dirs[:] = [d for d in dirs if d != BUNDLE_DIR]
        for name in names:
            if not name.endswith(".html"):
                continue
            html += 1
            rel = PurePosixPath(Path(root, name).relative_to(site_dir).as_posix())
            if rel.match(glob):
                matched += 1
            elif len(examples) < 5:
                examples.append(str(rel))
    return matched, html, examples


def _bundle_size(bundle: Path) -> tuple[int, int]:
    """``(bytes, files)`` under the bundle directory."""
    total = 0
    count = 0
    for path in bundle.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            count += 1
    return total, count


def _read_entry(bundle: Path) -> dict[str, object]:
    """``pagefind-entry.json``, or an empty dict if it is unreadable.

    This is the authoritative record of what was indexed - which languages,
    how many pages each, which hash names the meta file - so it is preferred
    over the printed summary wherever both exist.
    """
    try:
        return json.loads((bundle / "pagefind-entry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _LOG.debug("could not read pagefind-entry.json: %s", exc)
        return {}


# --------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------


def build_index(
    site_dir: Path,
    *,
    language: str = "en",
    glob: str = PAGE_GLOB,
    force_language: bool = True,
    verbose: bool = False,
    timeout: float | None = None,
) -> IndexInfo:
    """Index the per-page files under *site_dir* and report what it cost.

    *language* is an ISO 639-1 code, and with *force_language* left on it is
    applied to every page regardless of what each page's ``<html lang>`` says.
    That flag is not a nicety. Pagefind builds one index per language it finds
    and the client only ever loads the index matching the language of the page
    it is running on, so a collection holding four English pages and one French
    one ends up with a French page that can search one document and English
    pages that cannot find it. Measured on a mixed site: without the flag,
    three indexes of 2, 1 and 1 pages; with it, one index of 4.

    Raises :class:`SearchError` if pagefind is present and fails, times out, or
    indexes nothing. Returns an :class:`IndexInfo` carrying a warning - and no
    index - if pagefind is not installed at all.
    """
    started = time.monotonic()
    site_dir = Path(site_dir)
    if not site_dir.is_dir():
        raise SearchError(
            f"{site_dir}: there is no such directory to index. "
            "build_index runs after the pages have been written."
        )

    warnings: list[str] = []

    # Written whether or not pagefind runs: it is about the host, not about us,
    # and an operator who adds search later should not have to remember it.
    if ensure_nojekyll(site_dir):
        _LOG.debug("wrote %s", site_dir / NOJEKYLL)

    runner = _resolve_runner()
    if runner is None:
        warnings.append(f"No search index was built: pagefind was not found. {_INSTALL_HINT}")
        return IndexInfo(
            language=language,
            seconds=time.monotonic() - started,
            warnings=warnings,
        )

    matched, html_total, examples = _survey(site_dir, glob)
    if timeout is None:
        timeout = max(_TIMEOUT_FLOOR, _TIMEOUT_PER_PAGE * max(matched, html_total))

    bundle = site_dir / BUNDLE_DIR
    # Guarantee 6 is byte-for-byte reproducibility. Fragment and index files
    # are content-hashed, so a rebuild after an edit leaves the old ones behind
    # as dead weight that also makes index_bytes a lie.
    shutil.rmtree(bundle, ignore_errors=True)

    argv = [
        *runner.argv,
        "--site", str(site_dir),
        "--glob", glob,
        "--output-subdir", BUNDLE_DIR,
    ]
    if force_language:
        argv += ["--force-language", language]
    if verbose:
        argv.append("--verbose")

    _LOG.info("indexing with %s", runner.described)
    _LOG.debug("running %s", " ".join(argv))
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SearchError(
            f"pagefind ({runner.described}) did not finish within {timeout:.0f}s "
            f"while indexing {matched:,} pages under {site_dir}.\n"
            "  It normally manages about 1,000 pages a second. Either the disk "
            "is very slow or the process is stuck; try again with a larger "
            "timeout=, or run it by hand:\n"
            f"    {' '.join(argv)}"
        ) from exc
    except OSError as exc:
        raise SearchError(f"could not run pagefind ({runner.described}): {exc}") from exc

    output = f"{proc.stdout}\n{proc.stderr}"
    if verbose:
        for line in output.splitlines():
            if line.strip():
                _LOG.info("pagefind: %s", line.rstrip())

    pages_match = _PAGES_RE.search(output)
    pages = _int(pages_match.group(1)) if pages_match else 0
    found_match = _FOUND_RE.search(output)
    found = _int(found_match.group(1)) if found_match else None

    if proc.returncode != 0 and pages_match is None:
        # It never got as far as printing a summary, so this is a crash rather
        # than an empty corpus and the output is the only useful thing we have.
        raise SearchError(
            f"pagefind ({runner.described}) exited {proc.returncode}.\n"
            f"{_indent(_tail(output))}"
        )

    if pages == 0:
        # The worst outcome available is a site that builds cleanly and cannot
        # search itself, so this is loud, and it says what to look at.
        hint = (
            f"  Nothing under {site_dir} matched --glob {glob!r}."
            if matched == 0
            else f"  {matched:,} file(s) matched the glob, so the pages themselves were rejected."
        )
        if examples and matched == 0:
            hint += "\n  The site does contain, for example: " + ", ".join(examples)
        raise SearchError(
            "pagefind indexed 0 pages, so this site would publish with a search "
            "box that finds nothing.\n"
            f"{hint}\n"
            f"  Pages must be at <site>/{PAGE_GLOB} and carry a data-pagefind-body element.\n"
            f"  pagefind said:\n{_indent(_tail(output))}"
        )

    if proc.returncode != 0:
        raise SearchError(
            f"pagefind ({runner.described}) exited {proc.returncode}.\n"
            f"{_indent(_tail(output))}"
        )

    warnings += _collect_warnings(output, language if force_language else None)
    if found is not None and found != pages:
        warnings.append(
            f"pagefind found {found:,} files matching {glob!r} but indexed only "
            f"{pages:,} of them; {found - pages:,} page(s) were skipped, most "
            "likely because they have no data-pagefind-body element."
        )

    entry = _read_entry(bundle)
    languages = entry.get("languages") or {}
    if isinstance(languages, dict) and languages:
        if len(languages) > 1:
            counts = ", ".join(
                f"{code} ({info.get('page_count', '?')} pages)" for code, info in languages.items()
            )
            warnings.append(
                f"The index is split across {len(languages)} languages: {counts}. "
                "A reader only ever loads the one matching the <html lang> of "
                "the page they are on, so most of the archive is unsearchable "
                "from most of its pages. Pass force_language=True."
            )
        # Prefer the language that was asked for; fall back to whatever
        # pagefind decided, so the figures below describe the same index that
        # IndexInfo.language names.
        indexed_language = language if language in languages else next(iter(languages))
        entry_language = languages[indexed_language]
    else:
        indexed_language = language
        entry_language = {}
        warnings.append(
            "pagefind-entry.json is missing or unreadable, so the cold-start "
            "figure below is an estimate rather than a measurement."
        )

    entry_pages = entry_language.get("page_count") if isinstance(entry_language, dict) else None
    if isinstance(entry_pages, int) and entry_pages != pages and len(languages) == 1:
        warnings.append(
            f"pagefind reported {pages:,} pages indexed but its manifest lists "
            f"{entry_pages:,}."
        )

    if not (bundle / "pagefind.js").is_file():
        warnings.append(
            f"pagefind reported success but {BUNDLE_DIR}/pagefind.js is not "
            "there, so the search page has nothing to import."
        )

    index_bytes, files = _bundle_size(bundle)
    cold_start = _measure_cold_start(
        bundle, entry_language if isinstance(entry_language, dict) else {}, pages
    )
    warnings += scale_warnings(pages)

    info = IndexInfo(
        pages_indexed=pages,
        index_bytes=index_bytes,
        files=files,
        cold_start_bytes=cold_start,
        language=str(indexed_language),
        seconds=time.monotonic() - started,
        warnings=_dedupe(warnings),
        tool=runner.described,
    )
    _LOG.info(
        "indexed %d pages in %.1fs: %d files, %d KB on disk, %d KB cold start",
        info.pages_indexed,
        info.seconds,
        info.files,
        info.index_bytes // 1024,
        info.cold_start_bytes // 1024,
    )
    return info


def _dedupe(items: list[str]) -> list[str]:
    """Drop repeats, keep order. A warning said twice reads like two problems."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _tail(output: str, lines: int = 12) -> str:
    """The end of pagefind's output, which is where it says what went wrong."""
    kept = [line.rstrip() for line in output.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# --------------------------------------------------------------------------
# hosting
# --------------------------------------------------------------------------


def hosting_notes() -> str:
    """Advice for whoever puts this directory behind a web server.

    Returned rather than written to disk: it belongs in the CLI's output and in
    a project's own README, not as a stray file inside a published archive
    whose layout is fixed by ARCHITECTURE.md.
    """
    return f"""\
Serving the search index

  Do not compress {'/'.join(('.pf_meta', '.pf_index', '.pf_fragment'))} or wasm.*.pagefind.
  They are gzip streams already and compressing them again makes them bigger:
  measured, a 103-byte .pf_meta becomes 124 bytes and the 72,209-byte wasm
  becomes 72,252. Most servers compress by MIME type, so serving them as
  application/octet-stream is usually enough to keep gzip off them.

  Do compress the JavaScript: pagefind.js is 45,555 bytes raw and 12,859
  gzipped, and every reader downloads it.

  Serve .wasm as application/wasm and .pf_* as application/octet-stream.

  Keep {NOJEKYLL} in the site root. Without it GitHub Pages runs Jekyll, which
  deletes {BUNDLE_DIR}/ because the name starts with an underscore, and search
  fails completely with no error shown anywhere.

  Cache: fragment and index filenames contain a content hash and can be cached
  forever. pagefind.js, pagefind-worker.js and pagefind-entry.json cannot -
  the client already appends a cache-buster to the entry file.
"""
