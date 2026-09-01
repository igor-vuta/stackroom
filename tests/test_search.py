"""Tests for ``build.search`` and ``serve``.

Everything here runs the real pagefind binary over a real directory of HTML,
and the important test drives the real client in a real browser. That is
deliberate. The thing this module has to be right about is not a Python API,
it is a contract between three programs - the page writer, the Rust indexer and
the JavaScript client - and every part of that contract has already been
silently broken by someone somewhere:

* Pagefind reports matches as *word positions*. The viewer turns those into
  boxes drawn on a scan (guarantee 3 in ARCHITECTURE.md). If positions and
  tokens ever disagree, every highlight in the archive lands on the wrong word
  and nothing anywhere raises an error.
* ``.nojekyll`` is one empty file, and without it GitHub Pages deletes
  ``_pagefind`` and search fails completely with no message.
* Without ``--force-language`` a mixed-language collection quietly ends up with
  one index per language, and a reader can only ever search the one matching
  the page they are standing on.

Tests skip rather than fail when pagefind or Chromium is missing: a machine
without them can still develop everything else.

    cd stackroom && PYTHONPATH=src python -m pytest tests/test_search.py -q
"""

from __future__ import annotations

import gzip
import json
import random
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from stackroom.build import search
from stackroom.build.search import (
    BUNDLE_DIR,
    META_BYTES_PER_PAGE,
    RUNTIME_BYTES,
    IndexInfo,
    SearchError,
    build_index,
    ensure_nojekyll,
    estimate_cold_start,
    pagefind_available,
    scale_warnings,
)
from stackroom.serve import (
    MIME_TYPES,
    ServeError,
    _Handler,
    find_free_port,
    make_server,
    network_warning,
)

PAGEFIND_OK, PAGEFIND_WHY = pagefind_available()
needs_pagefind = pytest.mark.skipif(not PAGEFIND_OK, reason=f"pagefind unavailable: {PAGEFIND_WHY}")


# --------------------------------------------------------------------------
# a small site to index
# --------------------------------------------------------------------------

VOCAB = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliett", "kilo", "lima", "mike", "november", "oscar", "papa",
]
"""Sixteen words with sixteen distinct stems, so a query for one of them cannot
match another and an assertion about positions means what it says. Repeats
within a page are wanted: a token that occurs three times must come back as
three positions, in the right places."""

CHROME = "Browse the collection. Built with stackroom."
"""Navigation and footer text. None of these words are in VOCAB, so if any of
it leaks into the index a position assertion fails rather than passing by
luck."""


def page_tokens(doc: str, number: int, count: int = 24) -> list[str]:
    """A deterministic but different token list for every page.

    Different matters: if every page held the same words in the same order, a
    result attributed to the wrong page would still line up and the test would
    pass while the archive highlighted the wrong document.
    """
    rng = random.Random(f"{doc}/p{number}")
    return [rng.choice(VOCAB) for _ in range(count)]


def write_page(root: Path, doc: str, number: int, tokens: list[str], lang: str = "en") -> Path:
    """One page of the site, laid out exactly as the search contract requires.

    One element carries ``data-pagefind-body`` and it contains only the page's
    tokens, one per span, whitespace separated.
    """
    body = " ".join(f"<span>{token}</span>" for token in tokens)
    path = root / "d" / doc / "p" / str(number) / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"<!doctype html>\n<html lang=\"{lang}\">\n<head><meta charset=\"utf-8\">"
        f"<title>Doc {doc} page {number}</title></head>\n"
        f"<body>\n<nav>{CHROME}</nav>\n"
        f"<div data-pagefind-body>{body}</div>\n"
        f"<footer>{CHROME}</footer>\n</body>\n</html>\n",
        encoding="utf-8",
    )
    return path


def write_site(root: Path, docs: int = 3, pages: int = 4) -> dict[str, list[str]]:
    """A whole fake site. Returns ``{url: tokens}`` keyed the way pagefind
    reports results, so a test can look up what it emitted for a given hit."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>A collection</title></head><body><h1>A collection</h1></body></html>",
        encoding="utf-8",
    )
    emitted: dict[str, list[str]] = {}
    for d in range(docs):
        doc = f"memo-{d}"
        for n in range(1, pages + 1):
            tokens = page_tokens(doc, n)
            write_page(root, doc, n, tokens)
            emitted[f"/d/{doc}/p/{n}/"] = tokens
    return emitted


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A built site with a real index, shared by every test that only reads it."""
    root = tmp_path_factory.mktemp("site")
    write_site(root)
    if PAGEFIND_OK:
        build_index(root)
    return root


@pytest.fixture(scope="module")
def emitted(site: Path) -> dict[str, list[str]]:
    """``{url: tokens}`` for the shared site, rebuilt from the same seed."""
    out: dict[str, list[str]] = {}
    for page in sorted(site.glob("d/*/p/*/index.html")):
        doc = page.parent.parent.parent.name
        number = int(page.parent.name)
        out[f"/d/{doc}/p/{number}/"] = page_tokens(doc, number)
    return out


# --------------------------------------------------------------------------
# building the index
# --------------------------------------------------------------------------


def test_pagefind_available_reports_a_version_or_a_reason() -> None:
    ok, why = pagefind_available()
    assert isinstance(ok, bool)
    assert why.strip()
    if ok:
        assert "pagefind" in why.lower()
    else:
        # Whatever it says, it has to tell someone what to do about it.
        assert "pip install" in why or "npx" in why


@needs_pagefind
def test_indexes_every_page_and_nothing_else(tmp_path: Path) -> None:
    root = tmp_path / "site"
    emitted = write_site(root, docs=2, pages=3)
    info = build_index(root)

    assert info.pages_indexed == len(emitted) == 6
    assert info.ok
    assert "pagefind" in info.tool  # which binary ran, for the build stamp
    assert info.warnings == []
    assert info.language == "en"
    assert info.seconds > 0
    assert info.files > 0
    assert info.index_bytes > 0

    bundle = root / BUNDLE_DIR
    assert (bundle / "pagefind.js").is_file()
    assert list(bundle.glob("*.pf_meta"))
    assert list(bundle.glob("index/*.pf_index"))
    # One fragment per page: the overview page is outside the glob and must not
    # have been indexed.
    assert len(list(bundle.glob("fragment/*.pf_fragment"))) == 6


@needs_pagefind
def test_nojekyll_is_written(tmp_path: Path) -> None:
    """Without this file GitHub Pages deletes _pagefind and search vanishes."""
    root = tmp_path / "site"
    write_site(root, docs=1, pages=1)
    assert not (root / ".nojekyll").exists()

    build_index(root)

    assert (root / ".nojekyll").is_file()


def test_nojekyll_is_created_once_and_never_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "site"
    root.mkdir()
    assert ensure_nojekyll(root) is True
    assert (root / ".nojekyll").read_bytes() == b""

    # An operator may have put something in theirs; leave it alone.
    (root / ".nojekyll").write_text("mine", encoding="utf-8")
    assert ensure_nojekyll(root) is False
    assert (root / ".nojekyll").read_text(encoding="utf-8") == "mine"


def test_nojekyll_is_written_even_when_pagefind_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Jekyll trap is about the host, not about search."""
    root = tmp_path / "site"
    write_site(root, docs=1, pages=1)
    monkeypatch.setattr(search, "_resolve_runner", lambda: None)

    info = build_index(root)

    assert (root / ".nojekyll").is_file()
    assert info.pages_indexed == 0


def test_missing_pagefind_is_a_warning_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site without search still keeps every guarantee, so the build goes on."""
    root = tmp_path / "site"
    write_site(root, docs=1, pages=2)
    monkeypatch.setattr(search, "_resolve_runner", lambda: None)

    info = build_index(root)

    assert isinstance(info, IndexInfo)
    assert not info.ok
    assert info.pages_indexed == 0
    assert len(info.warnings) == 1
    assert "pagefind was not found" in info.warnings[0]
    assert "pip install" in info.warnings[0]
    assert not (root / BUNDLE_DIR).exists()


@needs_pagefind
def test_a_glob_that_matches_nothing_fails_loudly(tmp_path: Path) -> None:
    """A site that builds cleanly with no search is the worst outcome available."""
    root = tmp_path / "site"
    write_site(root, docs=1, pages=2)

    with pytest.raises(SearchError) as caught:
        build_index(root, glob="pages/*.html")

    message = str(caught.value)
    assert "0 pages" in message
    assert "pages/*.html" in message  # the glob it actually used
    assert "d/memo-0/p/1/index.html" in message  # and what is really there


@needs_pagefind
def test_pages_without_a_body_element_are_reported(tmp_path: Path) -> None:
    """Pagefind falls back to indexing the whole <body>, chrome included.

    It succeeds, so nothing fails - but the positions it then reports count the
    navigation too, which breaks every highlight.
    """
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<html lang=en><body>overview</body></html>", encoding="utf-8")
    page = root / "d" / "memo" / "p" / "1" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        f"<html lang=en><head><title>t</title></head><body><nav>{CHROME}</nav>"
        "<p>alpha bravo charlie</p></body></html>",
        encoding="utf-8",
    )

    info = build_index(root)

    assert info.pages_indexed == 1
    assert any("data-pagefind-body" in w for w in info.warnings)


def test_a_timeout_is_reported_as_a_search_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    root = tmp_path / "site"
    write_site(root, docs=1, pages=1)
    monkeypatch.setattr(
        search, "_resolve_runner", lambda: search._Runner(("pagefind",), "fake", "pagefind 1.5.2")
    )

    def hang(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="pagefind", timeout=120.0)

    monkeypatch.setattr(subprocess, "run", hang)

    with pytest.raises(SearchError) as caught:
        build_index(root)

    assert "did not finish" in str(caught.value)
    assert "pagefind" in str(caught.value)


def test_a_failing_pagefind_is_reported_with_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    root = tmp_path / "site"
    write_site(root, docs=1, pages=1)
    monkeypatch.setattr(
        search, "_resolve_runner", lambda: search._Runner(("pagefind",), "fake", "pagefind 1.5.2")
    )

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["pagefind"],
            returncode=2,
            stdout="Indexed 1 page\n",
            stderr="Error: could not write to disk\n",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(SearchError) as caught:
        build_index(root)

    assert "exited 2" in str(caught.value)
    assert "could not write to disk" in str(caught.value)


# --------------------------------------------------------------------------
# languages
# --------------------------------------------------------------------------


def _languages(root: Path) -> dict[str, Any]:
    entry = json.loads((root / BUNDLE_DIR / "pagefind-entry.json").read_text(encoding="utf-8"))
    return dict(entry["languages"])


@needs_pagefind
def test_force_language_keeps_the_whole_collection_in_one_index(tmp_path: Path) -> None:
    """A released set of documents is routinely not all in one language.

    Pagefind builds one index per ``<html lang>`` and the client only ever
    loads the one matching the page it is running on, so without this flag the
    French page can search one document and the English pages cannot find it.
    """
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<html lang=en><body>x</body></html>", encoding="utf-8")
    for i, lang in enumerate(("en", "en", "fr", "de")):
        write_page(root, f"memo-{i}", 1, page_tokens(f"memo-{i}", 1), lang=lang)

    forced = build_index(root, language="en", force_language=True)
    assert forced.pages_indexed == 4
    assert list(_languages(root)) == ["en"]
    assert _languages(root)["en"]["page_count"] == 4
    assert forced.warnings == []

    split = build_index(root, language="en", force_language=False)
    assert split.pages_indexed == 4
    assert set(_languages(root)) == {"en", "fr", "de"}
    assert any("split across 3 languages" in w for w in split.warnings)


@needs_pagefind
def test_a_language_that_is_not_iso_639_1_is_reported(tmp_path: Path) -> None:
    """``eng`` is a Tesseract code, and pagefind accepts it with a shrug.

    It indexes without a stemmer and search stops matching across word endings.
    Exit code 0, no error, worse search.
    """
    root = tmp_path / "site"
    write_site(root, docs=1, pages=1)

    info = build_index(root, language="eng")

    assert info.pages_indexed == 1
    assert any("ISO 639-1" in w for w in info.warnings)


# --------------------------------------------------------------------------
# what it costs a reader
# --------------------------------------------------------------------------


def test_cold_start_matches_the_table_in_the_docs() -> None:
    """ARCHITECTURE.md promises about 123 KB at 5,000 pages and 203 KB at 20,000."""
    assert estimate_cold_start(5_000) == pytest.approx(123 * 1024, rel=0.03)
    assert estimate_cold_start(20_000) == pytest.approx(203 * 1024, rel=0.03)


def test_cold_start_is_linear_in_pages() -> None:
    """The per-page term is the whole reason the ceiling exists."""
    assert estimate_cold_start(0) == RUNTIME_BYTES
    step = estimate_cold_start(2_000) - estimate_cold_start(1_000)
    assert step == pytest.approx(1_000 * META_BYTES_PER_PAGE, abs=2)
    assert estimate_cold_start(-5) == RUNTIME_BYTES


@needs_pagefind
def test_measured_cold_start_agrees_with_the_estimate(site: Path) -> None:
    """The estimate is a formula; this is the sum of the real files.

    They only converge on a big corpus - a twelve-page index still pays the
    100-odd byte floor of an empty ``.pf_meta`` - so the check is that the
    fixed cost dominates and the estimate is not wildly off.
    """
    info = build_index(site)
    estimated = estimate_cold_start(info.pages_indexed)
    assert info.cold_start_bytes == pytest.approx(estimated, rel=0.02)
    assert 90_000 < info.cold_start_bytes < 110_000


def test_scale_warnings_fire_at_the_documented_thresholds() -> None:
    assert scale_warnings(5_000) == []
    assert scale_warnings(20_000) == []
    supported = scale_warnings(20_001)
    assert len(supported) == 1
    assert "supported ceiling" in supported[0]
    degraded = scale_warnings(50_001)
    assert len(degraded) == 1
    assert "stand behind" in degraded[0]
    # Both mention what it will actually cost, in KB, not just that it is a lot.
    assert "KB" in supported[0] and "KB" in degraded[0]


@needs_pagefind
def test_the_index_files_must_not_be_gzipped_again(site: Path) -> None:
    """Evidence for the hosting advice: these files are compressed already.

    A host that gzips by extension makes them bigger and costs the reader
    bytes on the critical path.
    """
    bundle = site / BUNDLE_DIR
    grew: list[str] = []
    for path in [*bundle.glob("*.pf_meta"), *bundle.glob("index/*.pf_index")]:
        raw = path.read_bytes()
        if len(gzip.compress(raw, 6)) >= len(raw):
            grew.append(path.name)
    assert grew, "expected .pf_meta and .pf_index to grow under gzip"
    assert "wasm" in search.hosting_notes()
    assert ".nojekyll" in search.hosting_notes()


# --------------------------------------------------------------------------
# the preview server
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def preview(site: Path) -> Iterator[str]:
    """The real preview server, in a thread, serving the built site.

    Using our own server rather than a stub is the point: the browser test
    below only works if the MIME types this module sets are right, so the two
    halves check each other.
    """
    # Some extra file types the fake site does not otherwise contain.
    (site / "assets").mkdir(exist_ok=True)
    (site / "assets" / "font.woff2").write_bytes(b"wOF2fake")
    (site / "assets" / "app.js").write_text("export const x = 1;\n", encoding="utf-8")
    (site / "assets" / "app.css").write_text("body{}\n", encoding="utf-8")
    (site / "media").mkdir(exist_ok=True)
    (site / "media" / "p1.webp").write_bytes(b"RIFFfake")
    (site / "media" / "p1.avif").write_bytes(b"\x00\x00\x00 ftypavif")
    (site / "media" / "p1.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (site / "data").mkdir(exist_ok=True)
    (site / "data" / "collection.json").write_text("{}", encoding="utf-8")

    server = make_server(site, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(url: str) -> tuple[int, Message, bytes]:
    """``(status, headers, body)``, treating a 404 as an answer, not an error.

    The headers come back as the mapping urllib built, which is
    case-insensitive - the handler sends ``Content-type`` and a test that asked
    for ``Content-Type`` out of a plain dict would be testing capitalisation.
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_mime_table_covers_every_type_the_site_emits() -> None:
    """Stated explicitly, not looked up: the machine's table is not ours."""
    required = {
        ".avif", ".webp", ".wasm", ".pf_meta", ".pf_index", ".pf_fragment",
        ".json", ".woff2", ".html", ".css", ".js", ".pdf",
    }
    assert required <= set(MIME_TYPES)
    # And they must actually win: the handler consults its own table before it
    # asks the machine, which is the whole point of writing them down.
    assert all(_Handler.extensions_map[ext] == value for ext, value in MIME_TYPES.items())


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html; charset=utf-8"),
        ("/index.html", "text/html; charset=utf-8"),
        ("/media/p1.avif", "image/avif"),
        ("/media/p1.webp", "image/webp"),
        ("/media/p1.wasm", "application/wasm"),
        ("/assets/font.woff2", "font/woff2"),
        ("/assets/app.js", "text/javascript; charset=utf-8"),
        ("/assets/app.css", "text/css; charset=utf-8"),
        ("/data/collection.json", "application/json"),
    ],
)
def test_content_types(preview: str, path: str, content_type: str) -> None:
    status, headers, _ = fetch(preview + path)
    assert status == 200
    assert headers["Content-Type"] == content_type


@needs_pagefind
def test_pagefind_files_are_served_with_a_type_that_will_not_be_compressed(
    preview: str, site: Path
) -> None:
    for path in [
        *(site / BUNDLE_DIR).glob("*.pf_meta"),
        *(site / BUNDLE_DIR).glob("index/*.pf_index"),
        *(site / BUNDLE_DIR).glob("fragment/*.pf_fragment"),
        *(site / BUNDLE_DIR).glob("wasm.*.pagefind"),
    ]:
        url = preview + "/" + str(path.relative_to(site)).replace("\\", "/")
        status, headers, body = fetch(url)
        assert status == 200, url
        assert headers["Content-Type"] == "application/octet-stream", url
        assert body == path.read_bytes(), url


def test_html_is_never_cached_and_media_is(preview: str) -> None:
    """An operator who rebuilds and reloads has to see the new bytes."""
    _, html_headers, _ = fetch(preview + "/index.html")
    assert html_headers["Cache-Control"] == "no-store"
    _, json_headers, _ = fetch(preview + "/data/collection.json")
    assert json_headers["Cache-Control"] == "no-store"

    _, media_headers, _ = fetch(preview + "/media/p1.webp")
    assert "max-age" in media_headers["Cache-Control"]
    _, font_headers, _ = fetch(preview + "/assets/font.woff2")
    assert "max-age" in font_headers["Cache-Control"]


@pytest.mark.parametrize(
    "path",
    [
        "/../../etc/passwd",
        "/..%2f..%2fetc/passwd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/....//....//etc/passwd",
        "//etc/passwd",
        "/d/../../etc/passwd",
    ],
)
def test_nothing_outside_the_site_is_reachable(preview: str, path: str) -> None:
    status, _, body = fetch(preview + path)
    assert status == 404
    assert b"root:" not in body


def test_a_symlink_pointing_out_of_the_site_is_refused(preview: str, site: Path) -> None:
    """The stdlib handler stops ``..``; it does not stop this.

    A build directory assembled out of someone else's tree can easily contain a
    symlink, and following it would serve a file the operator never meant to
    publish.
    """
    secret = site.parent / "not-published.txt"
    secret.write_text("private", encoding="utf-8")
    link = site / "escape.txt"
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("this filesystem does not do symlinks")

    status, _, body = fetch(preview + "/escape.txt")

    assert status == 404
    assert b"private" not in body


def test_refuses_a_directory_that_is_not_a_built_site(tmp_path: Path) -> None:
    from stackroom.serve import _check_site

    (tmp_path / "papers").mkdir()
    (tmp_path / "papers" / "memo.pdf").write_bytes(b"%PDF-1.4\n")

    with pytest.raises(ServeError) as caught:
        _check_site(tmp_path / "papers")
    assert "no index.html" in str(caught.value)
    assert "stackroom build" in str(caught.value)  # says what to run instead

    with pytest.raises(ServeError) as missing:
        _check_site(tmp_path / "nowhere")
    assert "no such directory" in str(missing.value)


def test_find_free_port_prefers_the_port_it_was_given() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert find_free_port(free, "127.0.0.1") == free


def test_find_free_port_steps_over_a_busy_one() -> None:
    """Losing a preview to "address already in use" is a silly way to fail."""
    with socket.socket() as taken:
        taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]

        chosen = find_free_port(busy, "127.0.0.1")

        assert chosen != busy
        assert busy < chosen <= busy + 20


def test_binding_off_localhost_says_so() -> None:
    assert network_warning("127.0.0.1") == ""
    assert network_warning("localhost") == ""
    assert network_warning("::1") == ""
    for host in ("0.0.0.0", "", "192.168.1.10"):
        warning = network_warning(host)
        assert "network" in warning
        assert "127.0.0.1" in warning


# --------------------------------------------------------------------------
# the contract that cannot be checked from Python
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    """Headless Chromium, or a skip.

    The client is JavaScript, WebAssembly and a web worker. Reading the index
    files from Python would test a format nobody promised us; running the real
    client tests the thing the reader will actually run.
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - depends on the machine
            pytest.skip(f"no Chromium for playwright: {exc}")
        try:
            yield instance
        finally:
            instance.close()


SEARCH_IN_PAGE = """
async ({ base, query }) => {
    const pagefind = await import(base + "/_pagefind/pagefind.js");
    await pagefind.init();
    const search = await pagefind.search(query);
    const results = await Promise.all(search.results.map((r) => r.data()));
    return results.map((r) => ({
        url: r.url,
        locations: r.locations,
        word_count: r.word_count,
        content: r.content,
    }));
}
"""


@needs_pagefind
def test_match_positions_index_into_the_tokens_we_emitted(
    browser: Any, preview: str, emitted: dict[str, list[str]]
) -> None:
    """The one that matters.

    Pagefind hands the viewer word *positions*, and the viewer looks those up
    in ``Page.words`` to draw a box on the scan. Nothing checks that the two
    sequences agree - a page that indexes cleanly and highlights the wrong word
    looks exactly like one that works. So: ask the real client where a word is,
    and compare against every position where we actually put it.
    """
    page = browser.new_page()
    try:
        page.goto(f"{preview}/d/memo-0/p/1/", wait_until="load")
        checked = 0
        for query in ("hotel", "alpha", "papa", "november"):
            results = page.evaluate(SEARCH_IN_PAGE, {"base": preview, "query": query})
            assert results, f"{query!r} matched nothing at all"
            for result in results:
                tokens = emitted[result["url"]]
                expected = [i for i, token in enumerate(tokens) if token == query]
                assert sorted(result["locations"]) == expected, (
                    f"{query!r} on {result['url']}: pagefind says "
                    f"{sorted(result['locations'])}, we emitted it at {expected}"
                )
                # The count has to match too: an off-by-one in the total means
                # something outside data-pagefind-body was indexed.
                assert result["word_count"] == len(tokens)
                assert "Browse" not in result["content"]  # no chrome in the index
                checked += 1
        assert checked >= 12, "too few page/query pairs were actually compared"
    finally:
        page.close()


@needs_pagefind
def test_multi_character_cjk_tokens_desynchronise_the_positions(
    browser: Any, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Why ARCHITECTURE.md says one character per token for CJK.

    Pagefind's extended build re-segments CJK text, so three two-character
    tokens do not stay three tokens, and every position after them shifts. This
    test asserts the *broken* behaviour on purpose: it is the tripwire for
    anyone who decides that emitting whole CJK words would be tidier.
    """
    root = tmp_path_factory.mktemp("cjk")
    (root / "index.html").write_text("<html lang=zh><body>x</body></html>", encoding="utf-8")
    tokens = ["中文", "測試", "文件", "alpha", "zulu"]
    write_page(root, "memo", 1, tokens, lang="zh")
    build_index(root, language="zh")

    server = make_server(root, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    page = browser.new_page()
    try:
        page.goto(f"{base}/d/memo/p/1/", wait_until="load")
        results = page.evaluate(SEARCH_IN_PAGE, {"base": base, "query": "zulu"})
        assert len(results) == 1
        reported = results[0]["locations"]
        assert reported != [tokens.index("zulu")], (
            "pagefind no longer re-segments CJK: if this passes, the one-character-"
            "per-token rule in ARCHITECTURE.md can be revisited"
        )
        assert results[0]["word_count"] > len(tokens)
    finally:
        page.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
