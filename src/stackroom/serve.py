"""A local preview server for a built site.

An operator builds an archive and then has to look at it before publishing it,
which means a web server, because a static site of this shape does not open
properly from ``file://``: ES modules, the search worker and its WebAssembly
all need an origin. This is that server and nothing more. It imports only the
standard library, it binds to loopback, and it exists to be run for ninety
seconds and then interrupted.

Two things here are not optional decoration.

**MIME types.** Python's table is assembled from the interpreter's built-ins
*and* the machine's own ``/etc/mime.types`` (on Windows, the registry), so what
``mimetypes`` says depends on the box. ``.woff2`` is missing from CPython's
built-in table entirely, ``.pf_meta``, ``.pf_index``, ``.pf_fragment`` and
``.pagefind`` are known to nobody, and a Windows registry that maps ``.js`` to
``text/plain`` is a real thing that has broken real sites. The failure is
silent - fonts fall back, images do not decode, the search worker refuses to
start - so every type this project emits is stated explicitly below rather than
looked up.

**Caching.** An operator rebuilding a site and reloading it must see the new
bytes, so HTML and JSON and the search bundle are sent ``no-store``. Images and
fonts get a long cache because paging through a 400-page document otherwise
refetches every scan. The tradeoff is real: those paths carry a page number,
not a content hash, so re-rendering images at a different dpi needs a hard
reload to show up.
"""

from __future__ import annotations

import errno
import http.server
import ipaddress
import socket
import sys
import webbrowser
from functools import partial
from pathlib import Path
from typing import ClassVar, cast

__all__ = [
    "MIME_TYPES",
    "ServeError",
    "find_free_port",
    "make_server",
    "network_warning",
    "serve",
]


class ServeError(ValueError):
    """Something the operator has to fix before a preview can start."""


MIME_TYPES: dict[str, str] = {
    # Text. The charset is stated because a page whose <meta> the browser has
    # not reached yet is decoded as latin-1, and scanned documents are full of
    # names that stop being the person's name when that happens.
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    # Images. AVIF and WebP are the two formats every page uses.
    ".avif": "image/avif",
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    # Fonts are self-hosted; nothing loads from a font service.
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    # The originals.
    ".pdf": "application/pdf",
    # WebAssembly must be application/wasm or a streaming instantiation is
    # refused outright.
    ".wasm": "application/wasm",
    # Pagefind's own files. They are gzip streams that the client decompresses
    # itself, so they are opaque bytes: naming them octet-stream also keeps
    # most servers from trying to compress an already compressed file, which
    # measurably makes it bigger.
    ".pf_meta": "application/octet-stream",
    ".pf_index": "application/octet-stream",
    ".pf_fragment": "application/octet-stream",
    ".pagefind": "application/octet-stream",
}

_LONG_CACHE = frozenset(
    {".avif", ".webp", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff2", ".woff", ".pdf"}
)
"""Suffixes served with a long cache: renderings, fonts and originals.

Not HTML, not JSON, not the search bundle - those change on every rebuild and
an operator who cannot see their edit will not trust the tool again.
"""

_LONG_CACHE_SECONDS = 86_400
"""A day, not a year. These paths are not content-hashed, so the cache is a
convenience for scrolling through a long document within one preview session,
not a promise that the bytes can never change."""

_DENIED = "__stackroom_refused__"
"""A filename that cannot exist, used to turn a path outside the site root into
an ordinary 404."""

_PORT_SEARCH_SPAN = 20
"""How many ports to try past the requested one before giving up and asking the
kernel for any free port at all."""


# --------------------------------------------------------------------------
# the handler
# --------------------------------------------------------------------------


class _Handler(http.server.SimpleHTTPRequestHandler):
    """``SimpleHTTPRequestHandler`` with the types and headers this site needs."""

    server_version = "stackroom-preview"
    sys_version = ""
    """Do not advertise the Python version. It is nobody's business and it is
    one fewer thing to think about if someone binds this to a network."""

    protocol_version = "HTTP/1.1"
    """Keep-alive. A page of an archive pulls a scan, a thumbnail strip, a font
    and the search bundle; on HTTP/1.0 each of those is a fresh connection.
    Safe here because every response this handler produces has a
    Content-Length."""

    timeout = 30
    """Drop an idle connection after this long. Keep-alive means a browser
    holds several sockets open doing nothing, each pinning a thread; without a
    timeout they are only released when the browser feels like it, which on
    Chrome is minutes."""

    extensions_map: ClassVar[dict[str, str]] = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        **MIME_TYPES,
    }

    def translate_path(self, path: str) -> str:
        """Map a URL to a file, and refuse anything outside the site root.

        The stdlib already collapses ``..`` before joining, and it does hold up
        against the obvious attacks - ``/../../etc/passwd`` and its percent
        encodings are 404s on a stock handler, which the tests check. What it
        does *not* do is refuse a symlink inside the site that points outside
        it, and a build directory assembled from someone else's files can
        easily contain one. Resolving both ends and comparing closes that.
        """
        resolved = Path(super().translate_path(path)).resolve()
        root = cast("_PreviewServer", self.server).root
        if resolved != root and root not in resolved.parents:
            return str(root / _DENIED)
        return str(resolved)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", self._cache_control())
        super().end_headers()

    def _cache_control(self) -> str:
        suffix = Path(self.path.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
        if suffix in _LONG_CACHE:
            return f"public, max-age={_LONG_CACHE_SECONDS}"
        return "no-store"

    def log_message(self, format: str, *args: object) -> None:
        """One indented line per request, no timestamp.

        The operator is watching this window while clicking around the archive;
        what they need to see is the 404 for the scan that did not get written,
        not the date on every hit.
        """
        sys.stderr.write("  " + (format % args) + "\n")


class _PreviewServer(http.server.ThreadingHTTPServer):
    """Threaded so one slow request cannot stall the page it is part of."""

    daemon_threads = True

    block_on_close = False
    """Ctrl-C must not wait for idle connections.

    ``server_close()`` joins the handler threads, and with keep-alive a browser
    leaves several of them open and doing nothing. As it happens the stdlib
    already skips daemon threads when it builds that join list, so this is
    belt and braces - but it is the line that stays true if anyone ever turns
    ``daemon_threads`` off, and the cost of getting it wrong is a terminal that
    will not come back.
    """

    allow_reuse_address = True
    root: Path


# --------------------------------------------------------------------------
# ports and addresses
# --------------------------------------------------------------------------


def _family(host: str) -> int:
    """AF_INET6 for an IPv6 literal, AF_INET otherwise."""
    try:
        return socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    except ValueError:
        return socket.AF_INET


def _is_loopback(host: str) -> bool:
    """True when only this machine can reach the server.

    An empty host means every interface, which is the case this exists to
    catch.
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _can_bind(host: str, port: int) -> bool:
    """Is this port free right now?

    Errors other than "in use" are raised rather than swallowed: a permission
    error on port 80 means the operator needs to hear about port 80, not get
    quietly moved to 81.
    """
    with socket.socket(_family(host), socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
                return False
            raise
    return True


def find_free_port(preferred: int, host: str) -> int:
    """*preferred* if it is free, else the next free port, else any free port.

    There is an unavoidable race between finding a port and binding it, so the
    caller still has to be able to survive an ``OSError``. In practice the
    thing that takes port 8000 is the operator's own previous preview, which
    they have already stopped.
    """
    for port in range(max(1, preferred), min(preferred + _PORT_SEARCH_SPAN, 65_536)):
        if _can_bind(host, port):
            return port
    with socket.socket(_family(host), socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


# --------------------------------------------------------------------------
# serving
# --------------------------------------------------------------------------


def make_server(directory: Path, host: str, port: int) -> _PreviewServer:
    """A bound, not-yet-serving preview server rooted at *directory*.

    Split out from :func:`serve` so a test can drive it without a subprocess,
    and so a caller that wants to run it in a thread can.
    """
    root = Path(directory).resolve()
    handler = partial(_Handler, directory=str(root))
    _PreviewServer.address_family = _family(host)
    server = _PreviewServer((host, port), handler)
    server.root = root
    return server


def network_warning(host: str) -> str:
    """What to tell the operator before binding to *host*, or "" if it is safe.

    Separated from :func:`serve` so the CLI can show this while asking for
    confirmation, and so it can be tested without starting a server.
    """
    if _is_loopback(host):
        return ""
    return (
        f"Warning: binding to {host or '0.0.0.0'}, not localhost.\n"
        "  Everyone on this network can read this archive, including anything "
        "in it you have\n  not published yet. Use --host 127.0.0.1 if you did "
        "not mean that."
    )


def _check_site(directory: Path) -> None:
    """Refuse a directory that is not a built site, and say what to run."""
    if not directory.exists():
        raise ServeError(
            f"{directory}: no such directory.\n"
            "  Build the site first:  stackroom build <folder-of-documents> --out site"
        )
    if not directory.is_dir():
        raise ServeError(f"{directory}: is a file, not a built site directory.")
    if not (directory / "index.html").is_file():
        raise ServeError(
            f"{directory}: this is not a built site - there is no index.html in it.\n"
            "  If this is your folder of PDFs, build it first:\n"
            "      stackroom build "
            f"{directory} --out site\n"
            "  then preview the output directory, not the input one."
        )


def serve(
    directory: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = False,
) -> None:
    """Serve a built site until the operator interrupts it.

    Binds to loopback by default. Passing any other host publishes the archive
    to whatever network this machine is on, which is sometimes exactly what is
    wanted - showing an editor across the room - and sometimes a document leak,
    so it says so out loud.
    """
    directory = Path(directory)
    _check_site(directory)

    chosen = find_free_port(port, host)
    try:
        server = make_server(directory, host, chosen)
    except OSError as exc:
        raise ServeError(
            f"could not listen on {host}:{chosen} - {exc}\n"
            "  Ports below 1024 need root on most systems; try --port 8000."
        ) from exc

    exposed = network_warning(host)
    if exposed:
        print(f"\n{exposed}\n", file=sys.stderr)

    shown_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    if ":" in shown_host:  # an IPv6 literal has to be bracketed in a URL
        shown_host = f"[{shown_host}]"
    url = f"http://{shown_host}:{chosen}/"

    if chosen != port:
        print(f"Port {port} is already in use, so this is on {chosen} instead.")
    print(f"Serving {directory} at\n\n    {url}\n\nPress Ctrl-C to stop.\n")

    if open_browser:
        # The socket is already listening, so a browser that connects before
        # serve_forever runs waits in the accept queue rather than failing.
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Ctrl-C echoes into the middle of the line; the newline puts the
        # goodbye somewhere readable. No traceback: the operator did this on
        # purpose and it is not an error.
        print("\nStopped.")
    finally:
        server.server_close()
