"""What the browser suites share: one driver, one browser, one server.

Three test modules drive a real Chromium - ``test_viewer_browser.py``,
``test_qol_browser.py`` and the second half of ``test_offline.py`` - and until
this file existed they fought each other for the one thing a process is allowed
one of.

Playwright's synchronous API is a greenlet over an asyncio loop, and there can
only be one running per process: a second ``sync_playwright().start()`` while
the first is alive raises *"It looks like you are using Playwright Sync API
inside the asyncio loop"*. Each of the three modules starts its own, so running
them together meant one suite won and the others skipped or errored - and which
one won depended on collection order, so the failure moved when a file was
renamed.

Two things here fix that, and they are separate on purpose.

**The fixtures** are the way out: ``browser`` (and ``chromium``, the name
``test_offline.py`` uses for the same thing) hand out one Chromium for the
whole session, and ``site`` / ``base`` / ``base_url`` / ``manifest`` serve the
built demo over HTTP once. A module that drops its own copies of these gets
them from here and stops competing.

**The shim** below is what makes the suites work *before* they do that. A
fixture in a test module shadows one of the same name in a conftest, so the
fixtures alone change nothing for a file that still defines its own. The shim
makes ``sync_playwright()`` itself hand back a shared driver however many times
it is called, which is enough for every one of them. It is a transitional
measure: once the three modules use the fixtures above it, delete it.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# one Playwright driver per process
# --------------------------------------------------------------------------


class _KeptDriver:
    """The real Playwright driver with its ``stop()`` taken away.

    Handed out instead of the driver itself because one of the three modules
    does its own cleanup::

        play = sync_playwright().start()
        try: ...
        finally: play.stop()

    and that call lands on the *driver*, not on whatever ``sync_playwright()``
    returned - so a no-op ``stop()`` on the outer object is not enough. With
    this in the way, a module tidying up after itself releases its own claim
    and leaves the driver running for the module that comes next. Everything
    else is delegated untouched.
    """

    def __init__(self, driver) -> None:
        self._driver = driver

    def stop(self) -> None:
        """Deliberately nothing. See ``_close_shared_driver``."""

    def __getattr__(self, name: str):
        return getattr(self._driver, name)


class _SharedDriver:
    """A stand-in for ``sync_playwright()`` that only ever starts one driver.

    Callers use it three different ways - ``.start()``, ``with ... as p:`` and
    ``.stop()`` in a ``finally`` - and all three have to keep working. Only the
    session finaliser really stops the driver.
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self._driver = None
        self._kept: _KeptDriver | None = None

    def start(self) -> _KeptDriver:
        if self._driver is None:
            self._driver = self._factory().start()
            self._kept = _KeptDriver(self._driver)
        assert self._kept is not None
        return self._kept

    def stop(self) -> None:
        """Deliberately nothing. See ``_close_shared_driver``."""

    def __enter__(self) -> _KeptDriver:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        if self._driver is not None:
            self._driver.stop()
            self._driver = None
            self._kept = None


_SHARED: _SharedDriver | None = None


def _install_shim() -> None:
    """Point ``playwright.sync_api.sync_playwright`` at the shared driver.

    Done at import rather than in a fixture, because the test modules read the
    name once at *their* import time::

        sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    and conftest is imported before them, so they pick this up. Patching later
    would leave them holding the original.
    """
    global _SHARED
    try:
        import playwright.sync_api as sync_api
    except Exception:  # playwright is optional; every browser test skips
        return
    if getattr(sync_api.sync_playwright, "_stackroom_shared", False):
        return
    _SHARED = _SharedDriver(sync_api.sync_playwright)

    def sync_playwright() -> _SharedDriver:
        assert _SHARED is not None
        return _SHARED

    sync_playwright._stackroom_shared = True  # type: ignore[attr-defined]
    sync_api.sync_playwright = sync_playwright


_install_shim()


@pytest.fixture(scope="session", autouse=True)
def _close_shared_driver() -> Iterator[None]:
    """Stop the one driver at the end of the session, and only there."""
    yield
    if _SHARED is not None:
        _SHARED.close()


@pytest.fixture(scope="session")
def playwright_driver():
    """The driver itself, for a test that needs more than a browser."""
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="Playwright is not installed"
    ).sync_playwright
    return sync_playwright().start()


@pytest.fixture(scope="session")
def browser(playwright_driver):
    """One headless Chromium for the whole session.

    Contexts are cheap and isolated; browsers are neither. Every test that
    wants its own cookie jar, viewport, permissions or colour scheme should
    make a context off this rather than a second browser.
    """
    try:
        engine = playwright_driver.chromium.launch()
    except Exception as exc:  # pragma: no cover - environment, not code
        pytest.skip(f"Chromium is not available: {exc}")
    try:
        yield engine
    finally:
        engine.close()


@pytest.fixture(scope="session")
def chromium(browser):
    """``test_offline.py``'s name for the same browser."""
    return browser


# --------------------------------------------------------------------------
# the built demo, and a server in front of it
# --------------------------------------------------------------------------


def _candidates() -> list[Path]:
    """Where a built demo site might be, most explicit first.

    Two environment variables because two suites named one each before this
    file existed, and breaking either would be a worse fix than reading both.
    """
    named = [
        os.environ.get("STACKROOM_DEMO_SITE"),
        os.environ.get("STACKROOM_TEST_SITE"),
    ]
    found = [Path(value) for value in named if value]
    found += [
        REPO / "demo" / "site",
        REPO.parent / "demo" / "site",
        Path("/home/claude/demo/site"),
    ]
    return found


@pytest.fixture(scope="session")
def site() -> Path:
    """A site somebody has actually built. Skips rather than builds one.

    Building takes a minute and needs `pdftoppm`, Tesseract and the pagefind
    binary; a contributor working on the PDF reader should not be made to have
    all three before `pytest` will run.
    """
    for path in _candidates():
        if (path / "index.html").is_file() and (path / "assets").is_dir():
            return path
    pytest.skip(
        "no built demo site; build one and point STACKROOM_DEMO_SITE at it "
        "(stackroom build ./demo/release -o ./demo/site)"
    )


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Quiet, and honest about content types.

    `SimpleHTTPRequestHandler` guesses from `mimetypes`, whose table depends on
    what is installed on the machine: `.js` has come back as `text/plain` on a
    stock container, which makes a module script fail to load and a service
    worker refuse to register, for reasons that have nothing to do with the
    code under test.
    """

    _TYPES: ClassVar[dict[str, str]] = {
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".json": "application/json",
        ".woff2": "font/woff2",
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".svg": "image/svg+xml",
        ".wasm": "application/wasm",
    }

    def guess_type(self, path):  # type: ignore[override]
        for suffix, kind in self._TYPES.items():
            if str(path).endswith(suffix):
                return kind
        return super().guess_type(path)

    def log_message(self, *args: object) -> None:
        pass


class _Quiet(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A page closed mid-request is normal and is not a test failure."""


@pytest.fixture(scope="session")
def served(site: Path) -> Iterator[str]:
    """The built site over HTTP, on a port the OS picks.

    A real origin is part of what is under test: the pages carry a content
    policy, `localStorage` throws on `file://` because there is no origin, and
    the clipboard and cross-document view transitions need a secure context -
    which 127.0.0.1 is. A fixed port would collide with whatever else is
    running on a shared machine.
    """
    handler = functools.partial(_Handler, directory=str(site))
    server = _Quiet(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def base_url(served: str) -> str:
    """``http://127.0.0.1:PORT`` - join a path beginning with ``/`` onto it."""
    return served


@pytest.fixture(scope="session")
def base(served: str) -> str:
    """``http://127.0.0.1:PORT/`` - join a relative path onto it.

    The same server as ``base_url`` with the other convention on the end,
    because the two suites that grew up separately each chose one and neither
    is worth rewriting to settle it.
    """
    return served + "/"


@pytest.fixture(scope="session")
def manifest(site: Path) -> dict:
    """``manifest.json``: what was built, from what, by which version."""
    return json.loads((site / "manifest.json").read_text(encoding="utf-8"))
