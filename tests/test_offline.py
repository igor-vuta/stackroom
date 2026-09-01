"""The offline layer: the generated worker, and what it does in a browser.

Two halves, and the split matters.

The first half needs nothing but Python. It builds a small site by hand, in the
same way ``test_site.py`` does, generates the worker over it, and asserts three
things that can be checked without a browser: that the JavaScript parses, that
every path in the precache manifest is a file that exists, and that the cache
name moves when - and only when - something a reader would notice has changed.
Those are the failures that would ship silently: a broken substitution 404s
nothing and throws in the reader's browser, a manifest naming a missing file
fails the install for everyone but the person who built it, and a cache name
that does not move serves last month's archive forever.

The second half drives Chromium, and it *stops the web server* rather than
telling the browser to pretend it is offline. This is not fussiness. Chrome
DevTools' network emulation - which is what ``page.set_offline`` and
``Network.emulateNetworkConditions`` reach - applies to the page's own network
stack and not to fetches the service worker makes on its own behalf. Measured
here: with emulated offline, a page that had never been visited still loaded,
because the worker fetched it perfectly happily. Stopping the server is the
only way to test what a reader on a train actually experiences.

To run the browser half::

    pip install playwright && playwright install chromium
    pytest tests/test_offline.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from stackroom.build import offline as offline_mod
from stackroom.build.site import build_site
from stackroom.config import Config
from stackroom.model import (
    Box,
    Collection,
    CollectionStats,
    Document,
    ImageVariant,
    Page,
    Word,
)

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# a small site to generate a worker over
# --------------------------------------------------------------------------


def _page(number: int, words: list[str]) -> Page:
    page = Page(
        number=number,
        words=[
            Word(text=t, box=Box(0.1 + 0.05 * i, 0.2, 0.04, 0.02), line=0, conf=90)
            for i, t in enumerate(words)
        ],
        lines=[" ".join(words)],
        width_pt=612.0,
        height_pt=792.0,
    )
    page.images = [
        ImageVariant("media/memo/p0001@900.webp", "webp", 900, 1165, 41_000),
        ImageVariant("media/memo/p0001@1600.webp", "webp", 1275, 1650, 78_000),
    ]
    page.thumbs = [ImageVariant("media/memo/p0001@thumb.webp", "webp", 240, 310, 6_000)]
    return page


@pytest.fixture(scope="session")
def built(tmp_path_factory) -> Path:
    """A real site, built the way the builder builds one.

    Search is off: pagefind is a separate binary with its own tests, and every
    property asserted here is about the shell rather than the index.
    """
    out = tmp_path_factory.mktemp("offline") / "site"
    doc = Document(
        id="memo",
        title="Memorandum, March 2019",
        filename="memo.pdf",
        sha256="b" * 64,
        size_bytes=204_800,
        pages=[_page(1, ["the", "office", "withheld", "six", "pages"])],
    )
    collection = Collection(
        title="Papers of the Commission",
        documents=[doc],
        stats=CollectionStats(documents=1, pages=1, words=5),
    )
    collection.build.source_digest = "c" * 64
    cfg = Config()
    cfg.search.enabled = False
    build_site(collection, cfg, out)
    # Something in files/, so the "originals are not stored unasked" rule has
    # something to be true about.
    (out / "files").mkdir(exist_ok=True)
    (out / "files" / "memo.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * 4096)
    return out


@pytest.fixture(scope="session")
def generated(built: Path) -> dict:
    precache, warnings = offline_mod.precache_list(built, extra_scripts=[], search_enabled=False)
    files = offline_mod._walk(built)
    version = offline_mod.cache_version(
        built, source_digest="c" * 64, generator="0.1.0", precache=precache, files=files
    )
    return {
        "precache": precache,
        "warnings": warnings,
        "files": files,
        "version": version,
        "source": offline_mod.render_service_worker(
            version, precache, totals={"files": len(files), "bytes": 1, "originals": 0}
        ),
    }


# --------------------------------------------------------------------------
# is it JavaScript?
# --------------------------------------------------------------------------

_NODE = shutil.which("node") or shutil.which("nodejs")


def _balanced(source: str) -> tuple[bool, str]:
    """Bracket balance over a source with strings, comments and regexes removed.

    A real parser would be better and Python does not ship one. This is the
    check that catches what actually goes wrong here - a substitution that
    ate a quote or a bracket - and it has to know the difference between a
    division and a regex literal to do it, which is the one genuinely awkward
    part of scanning JavaScript. The rule below is the usual one: a slash
    starts a regex unless the last thing before it could end an expression.
    """
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    i = 0
    n = len(source)
    prev = ""
    while i < n:
        ch = source[i]
        two = source[i : i + 2]
        if two == "//":
            i = source.find("\n", i)
            if i < 0:
                break
            continue
        if two == "/*":
            end = source.find("*/", i + 2)
            if end < 0:
                return False, "unterminated block comment"
            i = end + 2
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n and source[i] != quote:
                i += 2 if source[i] == "\\" else 1
            if i >= n:
                return False, f"unterminated {quote} string"
            i += 1
            prev = "x"
            continue
        if ch == "/" and prev not in ("x", ")", "]", "}"):
            i += 1
            inside = False
            while i < n and (inside or source[i] != "/"):
                if source[i] == "\\":
                    i += 1
                elif source[i] == "[":
                    inside = True
                elif source[i] == "]":
                    inside = False
                elif source[i] == "\n":
                    return False, "newline inside a regular expression"
                i += 1
            i += 1
            prev = "x"
            continue
        if ch in "([{":
            stack.append(ch)
            prev = ch
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]:
                return False, f"unbalanced {ch!r} at offset {i}"
            prev = ch
        elif not ch.isspace():
            prev = "x" if (ch.isalnum() or ch in "_$") else ch
        i += 1
    if stack:
        return False, f"{len(stack)} unclosed bracket(s)"
    return True, ""


def parse_js(source: str, name: str) -> None:
    """Fail the test if *source* is not parseable JavaScript."""
    if _NODE:
        proc = subprocess.run(
            [_NODE, "--check", "-"], input=source.encode("utf-8"),
            capture_output=True, check=False,
        )
        if proc.returncode != 0:
            pytest.fail(f"{name} is not valid JavaScript:\n"
                        f"{proc.stderr.decode('utf-8', 'replace')[:2000]}")
        return
    ok, why = _balanced(source)
    assert ok, f"{name} does not scan as JavaScript: {why}"


def test_the_scanner_notices_broken_javascript():
    """The fallback check has to be able to fail, or it is not a check."""
    assert _balanced("function f() { return 1; }")[0]
    assert _balanced("var r = /a\\/b/; var q = 4 / 2;")[0]
    assert not _balanced("function f() { return 1;")[0]
    assert not _balanced("var s = 'unterminated;")[0]


def test_the_template_in_the_source_tree_is_valid_javascript():
    """It has to parse *before* substitution too: that is what makes it
    editable, lintable and readable without running a build."""
    parse_js((offline_mod.ASSETS / offline_mod.SW_NAME).read_text(encoding="utf-8"), "sw.js")


def test_the_generated_worker_is_valid_javascript(generated):
    parse_js(generated["source"], "the generated sw.js")


def test_the_registration_script_is_valid_javascript():
    parse_js((offline_mod.ASSETS / "js" / "offline.js").read_text(encoding="utf-8"), "offline.js")


def test_no_placeholder_survives_generation(generated):
    assert "__STACKROOM_" not in generated["source"]


def test_generation_refuses_a_template_that_has_drifted(monkeypatch, tmp_path):
    """If someone edits sw.js and renames a token, this must stop the build
    rather than publish a worker with a literal ``__STACKROOM_BUILD__`` in it."""
    (tmp_path / "sw.js").write_text("const BUILD = 'nope';\n", encoding="utf-8")
    monkeypatch.setattr(offline_mod, "ASSETS", tmp_path)
    with pytest.raises(ValueError, match="drifted"):
        offline_mod.render_service_worker("abc", ["index.html"])


# --------------------------------------------------------------------------
# the precache manifest
# --------------------------------------------------------------------------


def _precache_from(source: str) -> list[str]:
    match = re.search(r"^const PRECACHE = (\[.*?\]);$", source, re.M)
    assert match, "the generated worker has no PRECACHE array"
    return json.loads(match.group(1))


def test_every_precached_path_is_a_file_that_exists(built, generated):
    listed = _precache_from(generated["source"])
    assert listed, "the precache manifest is empty"
    missing = [p for p in listed if not (built / p).is_file()]
    assert not missing, f"precached paths that do not exist: {missing}"


def test_the_shell_is_enough_to_open_the_archive(built, generated):
    listed = set(_precache_from(generated["source"]))
    assert "index.html" in listed
    assert "assets/stackroom.css" in listed
    assert any(p.endswith(".woff2") for p in listed), "no font would be stored"
    assert not generated["warnings"], generated["warnings"]


def test_a_section_added_later_is_stored_too(built, tmp_path):
    """The standing pages are discovered, not listed.

    A hard-coded tuple stops being the truth the first time somebody adds a
    section, and the failure is silent: the page publishes fine and is simply
    missing when the reader is offline.

    The section has to be one the *test* invents. This used to use
    ``withheld/negative/``, which was hypothetical when it was written and is
    now a page the builder writes on every build - so the directory already
    existed and ``mkdir`` raised. Making it tolerate that (``exist_ok=True``)
    would have been the wrong repair twice over: the assertion would then be
    about a page :data:`STANDING_PAGES` discovery finds because the builder put
    it there, so it would keep passing with discovery removed entirely.

    So: a name no section will ever be given, and an assertion that the build
    really did not write it. If a future builder ever does, this fails loudly
    on the line below and asks for a new name, rather than going quiet.
    """
    section = "withheld/a-section-this-test-invented"
    copy = tmp_path / "with-a-new-section"
    shutil.copytree(built, copy)
    assert not (copy / section).exists(), f"{section} is no longer hypothetical"
    (copy / section).mkdir(parents=True)
    (copy / section / "index.html").write_text(
        "<!doctype html><html><body><h1>What is not here</h1></body></html>", encoding="utf-8"
    )
    precache, _ = offline_mod.precache_list(copy, extra_scripts=[], search_enabled=False)
    assert f"{section}/index.html" in precache


def test_the_interface_language_is_stored_too(built, generated):
    """``assets/i18n.js`` decides what language the shell speaks.

    It is a ``<script src>`` in the head of every page and carries every string
    the scripts write. A shell precached without it opens offline with the
    search box, the citation panel, the offline controls and the full-size
    viewer all silent or English, in an archive whose interface is not.

    It was missing for exactly as long as the asset list was hard-coded, which
    is the argument for :func:`offline.shell_assets` below.
    """
    assert "assets/i18n.js" in _precache_from(generated["source"])


def test_an_asset_added_later_is_stored_too(built, tmp_path):
    """The shell's assets are discovered, not listed.

    Same failure as the standing pages, one directory down and already
    realised once: the tuple predated ``assets/i18n.js`` and nobody noticed,
    because a missing precache entry publishes fine and is only wrong with the
    network off.

    Only the top level of ``assets/`` - ``js/`` and ``fonts/`` are filtered on
    evidence elsewhere - so the file this test invents goes there and the
    directory below it must not be swept up with it.
    """
    copy = tmp_path / "with-a-new-asset"
    shutil.copytree(built, copy)
    invented = "assets/an-asset-this-test-invented.js"
    assert not (copy / invented).exists(), f"{invented} is no longer hypothetical"
    (copy / invented).write_text("/* nothing */\n", encoding="utf-8")
    (copy / "assets" / "js").mkdir(parents=True, exist_ok=True)
    (copy / "assets" / "js" / "not-in-the-shell.js").write_text("/* */\n", encoding="utf-8")

    precache, _ = offline_mod.precache_list(copy, extra_scripts=[], search_enabled=False)
    assert invented in precache
    assert "assets/js/not-in-the-shell.js" not in precache, (
        "assets/js/ is filtered against the scripts the templates load; "
        "discovering it here would store every script on every archive"
    )


def test_a_missing_shell_asset_is_reported(built, tmp_path):
    """Discovery cannot notice an absence, so the names are still kept."""
    copy = tmp_path / "missing-an-asset"
    shutil.copytree(built, copy)
    (copy / "assets" / "i18n.js").unlink()
    precache, warnings = offline_mod.precache_list(copy, extra_scripts=[], search_enabled=False)
    assert "assets/i18n.js" not in precache
    assert any("i18n.js" in w for w in warnings), warnings


def test_document_and_page_views_are_not_precached(built, generated):
    """There can be twenty thousand of them; they are runtime-cached instead."""
    assert not [p for p in _precache_from(generated["source"]) if p.startswith("d/")]


def test_the_originals_are_never_precached(built, generated):
    listed = _precache_from(generated["source"])
    assert not [p for p in listed if p.startswith(offline_mod.ORIGINALS)], (
        "an archive can be gigabytes of originals; storing them unasked is the "
        "one thing this must not do"
    )


def test_the_worker_and_its_inventory_are_not_in_their_own_inventory(built, generated):
    paths = {path for path, _ in generated["files"]}
    assert offline_mod.SW_NAME not in paths
    assert offline_mod.INVENTORY_NAME not in paths


def test_the_inventory_accounts_for_every_published_file(built, generated):
    inventory = offline_mod.build_inventory(
        generated["version"], generated["files"], originals_bytes=0
    )
    on_disk = {
        p.relative_to(built).as_posix()
        for p in built.rglob("*")
        if p.is_file() and p.name not in (offline_mod.SW_NAME, offline_mod.INVENTORY_NAME)
    }
    assert {path for path, _ in inventory["files"]} == on_disk
    assert inventory["bytes"] == sum(size for _, size in generated["files"])


def test_only_the_fonts_a_page_needs_are_stored():
    """Stackroom ships 24 subsets and an English page fetches four. A precache
    that stored all of them would spend 372 KB to use 104 KB."""
    css = """
    @font-face { font-family: A; src: url("latin.woff2"); unicode-range: U+0000-00FF; }
    @font-face { font-family: A; src: url("greek.woff2"); unicode-range: U+0370-03FF; }
    @font-face { font-family: A; src: url("cyril.woff2"); unicode-range: U+0400-045F; }
    @font-face { font-family: A; src: local("Arial"); ascent-override: 100%; }
    """
    assert offline_mod.fonts_for(["<p>plain english</p>"], css) == ["latin.woff2"]
    assert offline_mod.fonts_for(["<p>ελληνικά</p>"], css) == ["greek.woff2"]
    assert offline_mod.fonts_for(["<p>english and ελληνικά</p>"], css) == [
        "greek.woff2", "latin.woff2",
    ]


def test_markup_is_not_mistaken_for_text():
    """An attribute is never drawn, so a Cyrillic slug in a URL must not pull
    down a Cyrillic subset for a page that shows no Cyrillic."""
    css = """
    @font-face { font-family: A; src: url("latin.woff2"); unicode-range: U+0000-00FF; }
    @font-face { font-family: A; src: url("cyril.woff2"); unicode-range: U+0400-045F; }
    """
    assert offline_mod.fonts_for(['<a href="/д/">english</a>'], css) == ["latin.woff2"]


def test_an_italic_face_is_stored_only_when_something_is_italic():
    css = """
    @font-face { font-family: A; src: url("roman.woff2"); font-style: normal;
                 unicode-range: U+0000-00FF; }
    @font-face { font-family: A; src: url("italic.woff2"); font-style: italic;
                 unicode-range: U+0000-00FF; }
    """
    assert offline_mod.fonts_for(["<p>plain</p>"], css) == ["roman.woff2"]
    assert offline_mod.fonts_for(["<p>an <em>aside</em></p>"], css) == [
        "italic.woff2", "roman.woff2",
    ]


# --------------------------------------------------------------------------
# cache versioning
# --------------------------------------------------------------------------


def test_the_cache_name_is_the_same_for_the_same_build(built, generated):
    again = offline_mod.cache_version(
        built, source_digest="c" * 64, generator="0.1.0",
        precache=generated["precache"], files=generated["files"],
    )
    assert again == generated["version"], "guarantee 6: same input, same bytes"


def test_the_cache_name_changes_when_the_source_digest_changes(built, generated):
    other = offline_mod.cache_version(
        built, source_digest="d" * 64, generator="0.1.0",
        precache=generated["precache"], files=generated["files"],
    )
    assert other != generated["version"]


def test_the_cache_name_changes_when_the_generator_changes(built, generated):
    """The same PDFs through a newer encoder are different bytes on the wire."""
    other = offline_mod.cache_version(
        built, source_digest="c" * 64, generator="0.2.0",
        precache=generated["precache"], files=generated["files"],
    )
    assert other != generated["version"]


def test_the_cache_name_changes_when_a_precached_file_changes(built, generated, tmp_path):
    """A stylesheet edit changes nothing else on the manifest, so the digest
    has to be over the file's content and not over its name."""
    copy = tmp_path / "edited"
    shutil.copytree(built, copy)
    css = copy / "assets" / "stackroom.css"
    css.write_text(css.read_text(encoding="utf-8") + "\n/* one more rule */\n", encoding="utf-8")
    files = offline_mod._walk(copy)
    other = offline_mod.cache_version(
        copy, source_digest="c" * 64, generator="0.1.0",
        precache=generated["precache"], files=files,
    )
    assert other != generated["version"]


def test_the_cache_name_changes_when_a_page_is_added(built, generated):
    files = generated["files"] + [("d/memo/p/2/index.html", 4096)]
    other = offline_mod.cache_version(
        built, source_digest="c" * 64, generator="0.1.0",
        precache=generated["precache"], files=files,
    )
    assert other != generated["version"]


def test_the_cache_name_appears_in_the_worker(generated):
    assert f"'{generated['version']}'" in generated["source"]
    assert len(generated["version"]) == offline_mod.VERSION_LENGTH


def test_the_worker_deletes_caches_from_older_builds():
    """The rule that makes a rebuild land cleanly rather than as a mixture."""
    source = (offline_mod.ASSETS / offline_mod.SW_NAME).read_text(encoding="utf-8")
    assert "caches.delete" in source
    assert "self.skipWaiting" in source, "there must be a way to take the new build"
    assert "skipWaiting()" not in source.split("addEventListener('install'")[1].split("});")[0], (
        "the new worker must not seize control mid-session: a page from one "
        "build with assets from another is exactly the stale mixture the cache "
        "name exists to prevent"
    )


def test_the_worker_gets_out_of_the_way_of_range_requests():
    source = (offline_mod.ASSETS / offline_mod.SW_NAME).read_text(encoding="utf-8")
    assert "headers.has('range')" in source, (
        "caches.match ignores Range and answers 200 with the whole body, which "
        "breaks anything paging through a PDF"
    )


def test_the_worker_never_touches_another_origin():
    source = (offline_mod.ASSETS / offline_mod.SW_NAME).read_text(encoding="utf-8")
    assert "u.origin !== self.location.origin" in source


def test_every_url_in_the_worker_is_relative():
    """The site may be served from a subdirectory, so nothing may be rooted."""
    source = (offline_mod.ASSETS / offline_mod.SW_NAME).read_text(encoding="utf-8")
    rooted = re.findall(r"""(?:new Request|fetch|url)\(\s*['"]/""", source)
    assert not rooted, f"absolute paths in the worker: {rooted}"
    assert "https://" not in source and "http://" not in source


def test_the_worker_is_written_to_the_site_root():
    """A worker under assets/ could only ever control assets/, and a static
    host cannot send the header that would widen its scope."""
    assert "/" not in offline_mod.SW_NAME


# --------------------------------------------------------------------------
# the browser half
# --------------------------------------------------------------------------

# No module-level `importorskip` here, deliberately. It used to sit on this
# line, and `pytest.importorskip` raised during import skips the *whole*
# module - so on a checkout with no Playwright the first half, which needs
# nothing but Python, was skipped along with the browser half it has nothing to
# do with. The browser fixture in tests/conftest.py does the skipping now, per
# test, where it belongs.


class _Server:
    """A server that can be stopped and started again mid-test.

    Emulated offline is not enough: Chrome's network emulation does not reach
    the fetches a service worker makes for itself, so a page the worker had
    never seen still loaded. Stopping the process is the only honest test.
    """

    def __init__(self, root: Path):
        self.root = str(root)
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0

    def start(self) -> str:
        root = self.root

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=root, **k)

            def log_message(self, *a): pass

            def guess_type(self, path):
                if str(path).endswith(".js"):
                    return "text/javascript"
                if str(path).endswith(".json"):
                    return "application/json"
                if str(path).endswith(".woff2"):
                    return "font/woff2"
                return super().guess_type(path)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port or 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                time.sleep(0.05)
            except OSError:
                return
        raise AssertionError("the server did not stop")


def _site_with_worker(tmp_path_factory) -> Path:
    """The built demo, if there is one, with a freshly generated worker in it.

    A real collection is used rather than the hand-built one above because the
    browser half is about page images, thumbnails and a search index, none of
    which the synthetic site has.
    """
    named = os.environ.get("STACKROOM_TEST_SITE")
    candidates = [Path(named)] if named else []
    candidates += [
        REPO / "demo" / "site",
        REPO.parent / "demo" / "site",
        Path("/home/claude/demo/site"),
    ]
    for path in candidates:
        if (path / "index.html").is_file() and (path / "assets").is_dir():
            copy = tmp_path_factory.mktemp("served") / "site"
            shutil.copytree(path, copy)
            _generate_into(copy)
            return copy
    pytest.skip(
        "no built site found; build one and point STACKROOM_TEST_SITE at it "
        "(stackroom build ./demo/release -o ./demo/site)"
    )


def _generate_into(out: Path) -> None:
    scripts = sorted(p.name for p in (out / "assets" / "js").glob("*.js")) \
        if (out / "assets" / "js").is_dir() else []
    if "offline.js" not in scripts:
        (out / "assets" / "js").mkdir(parents=True, exist_ok=True)
        shutil.copy2(offline_mod.ASSETS / "js" / "offline.js", out / "assets" / "js" / "offline.js")
        scripts.append("offline.js")
        for html in out.rglob("*.html"):
            text = html.read_text(encoding="utf-8")
            if "assets/js/offline.js" in text:
                continue
            depth = len(html.relative_to(out).parts) - 1
            tag = f'<script src="{"../" * depth}assets/js/offline.js" defer></script>\n'
            html.write_text(text.replace("</body>", tag + "</body>"), encoding="utf-8")
    for name in (offline_mod.SW_NAME, offline_mod.INVENTORY_NAME):
        (out / name).unlink(missing_ok=True)
    precache, _ = offline_mod.precache_list(out, extra_scripts=scripts, search_enabled=True)
    files = offline_mod._walk(out)
    totals = {
        "files": len(files),
        "bytes": sum(size for _, size in files),
        "originals": sum(size for p, size in files if p.startswith(offline_mod.ORIGINALS)),
    }
    version = offline_mod.cache_version(
        out, source_digest="test", generator="test", precache=precache, files=files
    )
    (out / offline_mod.SW_NAME).write_text(
        offline_mod.render_service_worker(version, precache, totals=totals), encoding="utf-8"
    )
    (out / offline_mod.INVENTORY_NAME).write_text(
        json.dumps(offline_mod.build_inventory(
            version, files, originals_bytes=totals["originals"])),
        encoding="utf-8",
    )


# `chromium` is tests/conftest.py's, one Chromium for the whole session. This
# module used to launch its own from its own Playwright driver, which is what
# made running the three browser suites together a matter of which one pytest
# collected first.
#
# The contexts below are made with `new_context()` and no arguments, and that
# matters: they must *not* block service workers the way the other two suites
# now do. The worker is what this half is about.


@pytest.fixture(scope="module")
def served_with_worker(tmp_path_factory):
    """A copy of the built demo with a freshly generated worker, on a server
    that can be stopped mid-test.

    Named apart from conftest's `served`, which is the demo as the builder left
    it and hands back a bare URL. These two are not interchangeable and a test
    asking for the wrong one would get a string where it wanted a tuple.
    """
    site = _site_with_worker(tmp_path_factory)
    server = _Server(site)
    url = server.start()
    try:
        yield server, url, site
    finally:
        if server.httpd:
            server.stop()


def _controlled(page, url: str) -> None:
    page.goto(url + "/index.html", wait_until="load")
    page.wait_for_function(
        "() => navigator.serviceWorker.controller !== null", timeout=60_000, polling=250
    )
    page.wait_for_timeout(1500)


@pytest.mark.browser
def test_the_worker_registers_and_stores_the_shell(served_with_worker, chromium):
    _, url, _ = served_with_worker
    context = chromium.new_context()
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        _controlled(page, url)
        state = page.evaluate("""async () => {
            const reg = await navigator.serviceWorker.getRegistration();
            const names = await caches.keys();
            let entries = 0;
            for (const n of names) entries += (await (await caches.open(n)).keys()).length;
            return {scope: reg.scope, active: reg.active.state, names, entries};
        }""")
        assert state["active"] == "activated"
        assert state["scope"].endswith("/")
        assert any(n.startswith("stackroom-shell-") for n in state["names"])
        assert state["entries"] >= 5
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.browser
def test_a_second_load_with_the_server_stopped_still_renders(served_with_worker, chromium):
    """The whole point. Not emulated offline - the server is actually gone."""
    server, url, _ = served_with_worker
    context = chromium.new_context()
    page = context.new_page()
    try:
        _controlled(page, url)
        page.goto(url + "/browse/index.html", wait_until="load")
        page.wait_for_timeout(800)

        server.stop()
        try:
            dead = page.evaluate(
                "async () => { try { const r = await fetch('./nothing-here?x=' + Date.now(),"
                " {cache:'no-store'}); return r.status; } catch (e) { return 'threw'; } }"
            )
            assert dead == "threw", "the server is still answering; the test proves nothing"

            page.goto(url + "/index.html", wait_until="load", timeout=30_000)
            assert page.query_selector("h1") is not None
            assert page.evaluate(
                "() => getComputedStyle(document.body).fontFamily.length > 0"
            )
            assert page.evaluate("() => document.styleSheets.length > 0"), "no stylesheet"

            page.goto(url + "/browse/index.html", wait_until="load", timeout=30_000)
            assert page.query_selector("h1") is not None
        finally:
            server.start()
    finally:
        context.close()


@pytest.mark.browser
def test_a_page_that_was_never_visited_says_so_rather_than_breaking(served_with_worker, chromium):
    server, url, _ = served_with_worker
    context = chromium.new_context()
    page = context.new_page()
    try:
        _controlled(page, url)
        target = None
        for candidate in (page.eval_on_selector_all(
            ".docs a[href], .doc__title a[href]", "els => els.map(e => e.getAttribute('href'))"
        ) or []):
            target = candidate
            break
        if not target:
            pytest.skip("this site has no document pages to look for")
        server.stop()
        try:
            response = page.goto(url + "/" + target.lstrip("./"), wait_until="load", timeout=30_000)
            assert response is not None
            assert response.status == 503
            assert "not stored" in page.inner_text("h1").lower()
        finally:
            server.start()
    finally:
        context.close()


@pytest.mark.browser
def test_storing_the_whole_archive_makes_every_page_readable(served_with_worker, chromium):
    server, url, site = served_with_worker
    context = chromium.new_context()
    page = context.new_page()
    try:
        _controlled(page, url)
        label = page.evaluate(
            "() => { const b = document.querySelector('.offline__btn--go'); return b && b.textContent; }"
        )
        assert label and re.search(r"\d", label), (
            "the size has to be on the button before anyone agrees to it"
        )
        page.click(".offline__btn--go")
        page.wait_for_function(
            "() => /whole archive is stored/.test(document.querySelector('.offline__say').textContent)",
            timeout=300_000,
        )
        unvisited = sorted(site.glob("d/*/p/*/index.html"))
        if not unvisited:
            pytest.skip("this site has no page views")
        target = unvisited[-1].relative_to(site).as_posix()
        server.stop()
        try:
            page.goto(f"{url}/{target}", wait_until="load", timeout=30_000)
            assert page.query_selector(".scan__img, .empty") is not None
            assert page.evaluate(
                "() => [...document.images].every(i => i.complete && i.naturalWidth > 0)"
            ), "an image did not load with the server down"
        finally:
            server.start()
    finally:
        context.close()


@pytest.mark.browser
def test_the_reader_can_switch_it_off_and_it_stays_off(served_with_worker, chromium):
    _, url, _ = served_with_worker
    context = chromium.new_context()
    page = context.new_page()
    try:
        _controlled(page, url)
        page.goto(url + "/index.html?stackroom-offline=off", wait_until="load")
        page.wait_for_timeout(4500)
        after = page.evaluate("""async () => ({
            registrations: (await navigator.serviceWorker.getRegistrations()).length,
            caches: (await caches.keys()).filter(n => n.startsWith('stackroom-')).length,
        })""")
        assert after == {"registrations": 0, "caches": 0}
        page.goto(url + "/index.html", wait_until="load")
        page.wait_for_timeout(2000)
        assert page.evaluate(
            "async () => (await navigator.serviceWorker.getRegistrations()).length"
        ) == 0, "it came back after being switched off"
        assert page.query_selector("h1") is not None, "the archive must still read"
    finally:
        context.close()


@pytest.mark.browser
def test_the_operator_can_disarm_it_with_a_file(served_with_worker, chromium):
    """Publishing `sw-kill` disarms every reader's worker, with no new build.

    The worker checks for the file once each time it starts up. Chrome spins an
    idle worker down after about thirty seconds, so in a real session this
    lands within a page or two; the test asks DevTools to stop the worker
    rather than sleeping for it.
    """
    _, url, site = served_with_worker
    context = chromium.new_context()
    page = context.new_page()
    try:
        _controlled(page, url)
        cdp = context.new_cdp_session(page)
        (site / "sw-kill").write_text("disarmed\n", encoding="utf-8")
        try:
            cdp.send("ServiceWorker.enable")
            cdp.send("ServiceWorker.stopAllWorkers")   # what thirty idle seconds does
            page.goto(url + "/index.html", wait_until="load")
            page.wait_for_timeout(5000)
            left = page.evaluate(
                "async () => (await caches.keys()).filter(n => n.startsWith('stackroom-')).length"
            )
            assert left == 0, "the kill file left caches behind"
            assert page.query_selector("h1") is not None, "the archive must still read"
        finally:
            (site / "sw-kill").unlink(missing_ok=True)
    finally:
        context.close()


@pytest.mark.browser
def test_it_says_so_honestly_on_a_file_url(served_with_worker, chromium, tmp_path_factory):
    _, _, site = served_with_worker
    context = chromium.new_context()
    page = context.new_page()
    try:
        page.goto((site / "index.html").as_uri(), wait_until="load")
        page.wait_for_timeout(1200)
        line = page.evaluate(
            "() => { const e = document.querySelector('.offline'); "
            "return e && !e.hidden ? e.innerText : null; }"
        )
        assert line and "file://" in line, (
            "on file:// the reader must be told why, not shown a control that "
            "silently does nothing"
        )
        assert page.query_selector("h1") is not None
    finally:
        context.close()


@pytest.mark.browser
def test_nothing_appears_and_nothing_breaks_with_javascript_off(served_with_worker, chromium):
    _, url, _ = served_with_worker
    context = chromium.new_context(java_script_enabled=False)
    page = context.new_page()
    try:
        page.goto(url + "/index.html", wait_until="load")
        assert page.query_selector("h1") is not None
        assert page.query_selector(".offline") is None, (
            "the offline line is an enhancement and must not be in the HTML"
        )
    finally:
        context.close()
