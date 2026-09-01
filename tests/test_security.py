"""Adversarial tests: what a hostile document can do to the operator and the readers.

The threat model these are written against is ``docs/THREAT-MODEL.md``. In one
sentence: **the attacker wrote every byte of every input file, including its
name, its metadata and its internal structure**, the operator runs
``stackroom build`` on it, and the result is published to the public web.

Three kinds of test live here, labelled so a reader can tell them apart:

* **Pinned properties.** Things that are true today and must stay true. These
  pass, and a regression makes them fail. Most of this file is these, because a
  security review whose only output is a list of holes is worth much less than
  one that also nails down the floor.
* **``xfail(strict=True)`` findings.** A reproduced defect. The test demonstrates
  it; when the fix lands the test goes green and pytest reports XPASS as a
  failure, which is the signal to delete the marker. Each carries its finding
  number from the threat model.
* **Documented limits.** Behaviour that is wrong-ish but stated in
  ``SECURITY.md``. Pinned so the documentation and the code cannot drift apart
  silently.

Speed: every document is one page unless the defect needs more, the render
resolution is the lowest poppler will do useful work at, and ``workers=1``
throughout - a process pool hides tracebacks and costs a second of startup.
"""

from __future__ import annotations

import ast
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parent / "fuzz"))

import rawpdf

from stackroom import cli as cli_mod
from stackroom import pipeline as pipeline_mod
from stackroom.build import site as site_mod
from stackroom.build.site import _json_block, build_site, ribbon
from stackroom.config import Config, ConfigError
from stackroom.config import find as config_find
from stackroom.config import load as config_load
from stackroom.ingest import discover as discover_mod
from stackroom.ingest import exemptions as exemptions_mod
from stackroom.ingest import pdf as pdf_mod
from stackroom.ingest import raster as raster_mod
from stackroom.ingest import redaction as redaction_mod
from stackroom.model import Box, Page
from stackroom.pipeline import (
    PageJob,
    SafetyStop,
    build_collection,
    check_safety,
    process_page,
)
from stackroom.textblock import render_markdown

SRC = Path(site_mod.__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def finding(number: str, what: str) -> pytest.MarkDecorator:
    """Mark a test that demonstrates an unfixed finding.

    ``strict`` so that fixing the defect turns the test red and whoever fixed it
    is told to delete the marker. A security suite full of stale xfails is a
    suite nobody reads.
    """
    return pytest.mark.xfail(reason=f"{number}: {what}", strict=True)


EXIT = (SystemExit, typer.Exit)
"""What ``cli._die`` raises. ``typer.Exit`` is a plain exception, not a
``SystemExit``: it becomes an exit status only once Click's runner unwinds it."""


def fast_config(**kw) -> Config:
    """A build configuration that does the least work poppler will accept."""
    cfg = Config(**kw)
    cfg.render.dpi = 100
    cfg.render.widths = [400]
    cfg.render.thumb_width = 200
    cfg.render.formats = ["webp"]
    cfg.ocr.mode = "never"
    cfg.search.enabled = False
    return cfg


def one_page_job(path: Path, media: Path, number: int = 1) -> PageJob:
    return PageJob(
        pdf=str(path),
        doc_id="d",
        number=number,
        media_dir=str(media),
        media_prefix="media/d",
        dpi=100,
        widths=(400,),
        thumb_width=200,
        formats=("webp",),
        ocr_mode="never",
    )


def ingest(folder: Path, out: Path, cfg: Config | None = None):
    return build_collection(folder, cfg or fast_config(), out, workers=1)


def build_folder(folder: Path, out: Path, cfg: Config | None = None):
    """Ingest *folder* and write the site, in the order ``stackroom build`` does."""
    cfg = cfg or fast_config()
    collection, outcomes = build_collection(folder, cfg, out, workers=1)
    check_safety(outcomes, cfg)
    site_mod.attach_about(collection, cfg)
    report = build_site(collection, cfg, out)
    return collection, outcomes, report


def leaking_page(secret: str = "SOURCE NAME ALPHA") -> bytes:
    """A textbook failed redaction: text painted first, opaque box painted on it."""
    return (
        f"BT /F1 14 Tf 72 700 Td ({secret}) Tj ET\n".encode()
        + b"0 0 0 rg 68 694 190 24 re f\n"
        + rawpdf.prose_lines()
    )


def generated_text_files(out: Path):
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.suffix in (".html", ".json", ".js", ".css", ".txt"):
            yield path, path.read_text(encoding="utf-8", errors="replace")


def tokens_of(page_html: str) -> list[str]:
    """The tokens inside the ``data-pagefind-body`` element, in order."""
    body = re.search(r"data-pagefind-body[^>]*>(.*?)</div>", page_html, re.S)
    if not body:
        return []
    return re.findall(r'<span class="w"[^>]*>([^<]*)</span>', body.group(1))


# --------------------------------------------------------------------------
# 1. Injection into the published site
# --------------------------------------------------------------------------

XSS = '</title></script><img src=x onerror=alert(1)>"><script>alert(2)</script>'
SVG_XSS = '"><svg onload=alert(3)>'


@pytest.fixture(scope="module")
def hostile_site(tmp_path_factory):
    """One build from a document that is hostile in every field it controls."""
    folder = tmp_path_factory.mktemp("hostile-src")
    out = tmp_path_factory.mktemp("hostile-out")
    content = (
        b"BT /F1 11 Tf 72 700 Td (</script> </title> --> onmouseover=alert) Tj ET\n"
        b"BT /F1 11 Tf 72 680 Td (<script>alert</script> javascript:alert) Tj ET\n"
        + rawpdf.prose_lines(3)
    )
    # Parentheses are the PDF string delimiter, so the payload that goes through
    # the metadata dictionary carries escaped ones.
    pdf_safe = XSS.replace("(", r"\(").replace(")", r"\)")
    (folder / "release.pdf").write_bytes(
        rawpdf.page_pdf(content, info={"Title": pdf_safe, "Author": SVG_XSS})
    )
    (folder / "stackroom.toml").write_text('title = "Hostile"\n', encoding="utf-8")
    cfg = fast_config()
    cfg.title = XSS
    cfg.description = SVG_XSS
    build_folder(folder, out, cfg)
    return out


def test_every_template_is_autoescaped():
    """Autoescape is selected by file extension, and ours end in ``.jinja``.

    ``select_autoescape(["html", "xml", "jinja"])`` matches on the *last*
    extension. Renaming a template to ``.j2``, or adding one, silently turns
    escaping off for that file alone - which is exactly the kind of change that
    looks like tidying.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    chooser = select_autoescape(["html", "xml", "jinja"])
    env = Environment(loader=FileSystemLoader(str(SRC / "templates")), autoescape=chooser)
    names = sorted(p.name for p in (SRC / "templates").glob("*"))
    assert names, "no templates found; the path in this test is wrong"
    for name in names:
        assert chooser(name) is True, f"{name} would be rendered without autoescaping"
        assert env.get_template(name) is not None


def test_no_generated_page_carries_unescaped_document_markup(hostile_site):
    """Nothing a document says can become markup in the pages built from it.

    The document controls ``/Title``, ``/Author`` and every glyph on the page,
    and all three reach the HTML. The assertion is deliberately blunt: the
    literal payload must not appear anywhere in any generated page.
    """
    offenders = []
    for path, text in generated_text_files(hostile_site):
        if path.suffix == ".json":
            continue  # JSON is data; checked separately below
        for needle in ("<img src=x onerror=", "<svg onload=", "</script><script>"):
            if needle in text:
                offenders.append((path.name, needle))
    assert offenders == []


def test_document_text_is_escaped_token_by_token_in_the_page_html(hostile_site):
    page = next(hostile_site.glob("d/*/p/1/index.html")).read_text(encoding="utf-8")
    tokens = tokens_of(page)
    assert tokens, "the page published no tokens at all"
    assert "&lt;/script&gt;" in page
    for token in tokens:
        assert "<" not in token and ">" not in token


def test_hostile_metadata_in_json_payloads_stays_data_not_markup(hostile_site):
    """``manifest.json`` and ``data/docs.json`` may hold the raw string.

    They are fetched and parsed as JSON, never inserted as HTML - ``search.js``
    puts a title in ``textContent`` and ``assets/js/palette.js`` escapes it - so
    the raw value there is correct. What must hold is that the files really are
    valid JSON, because a payload that breaks the parse takes search with it.
    """
    manifest = json.loads((hostile_site / "manifest.json").read_text(encoding="utf-8"))
    docs = json.loads((hostile_site / "data" / "docs.json").read_text(encoding="utf-8"))
    assert manifest["documents"], "no documents in the manifest"
    assert docs
    titles = [d["title"] for d in manifest["documents"]]
    assert any("script" in t for t in titles), "the hostile title never reached the manifest"


def test_json_block_cannot_close_the_script_element():
    """``<script type="application/json">`` ends only at ``</script``.

    ``_json_block`` escapes ``<``, ``>`` and ``&`` as ``\\uXXXX``, which is legal
    JSON and inert HTML. This pins the property rather than the implementation.
    """
    payload = {"t": "</script ></SCRIPT><script>alert(1)</script>&amp;<!--"}
    block = str(_json_block(payload))
    assert "<" not in block and ">" not in block and "&" not in block
    assert json.loads(block)["t"] == payload["t"]


def test_json_block_keeps_line_separators_parseable():
    """U+2028/U+2029 are legal inside a JSON string and inert in script data.

    They break a JavaScript *string literal*, which is why they matter when JSON
    is pasted into JS source. This block is parsed with ``JSON.parse``, not
    evaluated, so passing them through is correct - but a future change that
    starts writing JSON into a JS literal has to escape them, and this test is
    where that will be noticed.
    """
    value = "a\u2028b\u2029c"
    block = str(_json_block({"t": value}))
    assert json.loads(block)["t"] == value


def test_ribbon_escapes_every_value_it_interpolates():
    """``ribbon()`` is hand-built ``Markup``, so its inputs must not be trusted.

    Parsed rather than grepped: the payload characters *should* appear in the
    output, escaped, as an attribute value. What must not appear is a second
    element, or an attribute the function did not intend to write - which is
    what breaking out of the quoting would produce.
    """
    import xml.etree.ElementTree as ET

    label = '" onload=alert(1) x="'
    base = '"><script>x</script>'
    svg = str(ribbon([Page(number=1)], label=label, base=base))
    root = ET.fromstring(svg)

    assert root.tag == "svg"
    assert set(root.attrib) == {"class", "viewBox", "preserveAspectRatio", "role",
                                "aria-label", "data-pages", "data-base"}
    assert root.attrib["aria-label"] == label
    assert root.attrib["data-base"] == base
    children = list(root)
    assert [c.tag for c in children] == ["rect"]
    assert set(children[0].attrib) == {"class", "x", "y", "width", "height"}


@pytest.mark.parametrize(
    "source",
    [
        "[x](javascript:alert(1))",
        "[x](javascript&colon;alert&lpar;1&rpar;)",
        "[x](data:text/html,<script>alert(1)</script>)",
        "[x](vbscript:msgbox(1))",
        '[a](https://x/" onmouseover="alert(1))',
        "[<img src=x onerror=alert(1)>](https://a.example/)",
        "**`</code><script>alert(1)</script>`**",
        "# <h1 onclick=alert(1)>heading</h1>",
        "*<script>alert(1)</script>*",
        "> <iframe src=javascript:alert(1)></iframe>",
        "- <object data=x>",
        "<!--> <script>alert(1)</script>",
        "1. [x](  javascript:alert(1)  )",
    ],
)
def test_about_md_cannot_emit_a_tag_textblock_did_not_write(source):
    """``about.md`` is trusted, but it is usually pasted from an agency letter.

    ``textblock`` escapes first and re-introduces a fixed set of constructs, so
    the only tags in its output should be ones it chose. Anything else is a hole
    in that argument.

    Parsed, not grepped: the payload text is *meant* to appear in the output as
    visible text. What must not appear is a tag or an attribute made out of it.
    """
    from html.parser import HTMLParser

    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, attrs))

        handle_startendtag = handle_starttag

    reader = Reader()
    reader.feed(render_markdown(source))

    allowed_tags = {
        "p", "h2", "h3", "h4", "ul", "ol", "li", "blockquote", "hr",
        "strong", "em", "code", "a",
    }
    for tag, attrs in reader.tags:
        assert tag in allowed_tags, f"{source!r} produced <{tag}>"
        for name, value in attrs:
            assert not name.startswith("on"), f"{source!r} produced {name}="
            assert name in ("href", "rel"), f"{source!r} produced {name}="
            if name == "href":
                scheme = (value or "").split(":", 1)[0].lower() if ":" in (value or "") else ""
                assert scheme in ("", "http", "https", "mailto"), (
                    f"{source!r} produced href={value!r}"
                )


def test_about_md_links_are_limited_to_safe_schemes():
    html = render_markdown("[ok](https://example.org/a) [rel](./b) [mail](mailto:a@b.c)")
    hrefs = re.findall(r'href="([^"]*)"', html)
    assert hrefs == ["https://example.org/a", "./b", "mailto:a@b.c"]
    assert 'rel="noopener noreferrer"' in html


@finding(
    "F14",
    "plain_text() leaves an unterminated HTML comment in while render_markdown() "
    "strips it, so notes the operator believes are hidden would be published if "
    "plain_text ever fed the meta description",
)
def test_plain_text_hides_what_render_markdown_hides():
    from stackroom.textblock import plain_text

    hidden = "<!-- internal note: the source is at 14 Elm Street"
    assert render_markdown(hidden) == ""
    assert "Elm Street" not in plain_text(hidden)


# --------------------------------------------------------------------------
# 2. The published original, and the extension it is published under
# --------------------------------------------------------------------------

ACTIVE_SUFFIXES = {".html", ".htm", ".xhtml", ".svg", ".xml", ".js", ".mjs", ".css"}


def test_a_published_original_never_gets_an_active_content_extension(tmp_path):
    """A file that is a PDF by magic but named ``.html`` is published as a PDF.

    The polyglot below is a valid HTML document *and* a valid PDF: ``discover``
    accepts a ``%PDF-`` header anywhere in the first 1024 bytes, so the file can
    open with ``<script>``. It used to land in ``files/`` under its own name,
    linked from every page as "Download the original" and served as text/html
    from the archive's origin with no Content-Security-Policy - the CSP is a
    ``<meta>`` tag in the pages Stackroom generates, and a copied original is not
    one of them. The published extension now comes from what ``discover`` decided
    the file *is*, so it is republished as ``.pdf``.
    """
    folder = tmp_path / "src"
    folder.mkdir()
    payload = (
        b"<!doctype html><html><head><title>Annual report</title></head><body>"
        b"<script>document.title='XSS '+location.origin</script></body></html>\n"
    )
    (folder / "annual-report.html").write_bytes(
        payload + rawpdf.page_pdf(rawpdf.prose_lines(3))
    )
    (folder / "stackroom.toml").write_text('title = "P"\n', encoding="utf-8")

    out = tmp_path / "out"
    build_folder(folder, out)

    published = sorted((out / "files").iterdir())
    assert published, "nothing was published"
    for path in published:
        assert path.suffix.lower() not in ACTIVE_SUFFIXES, (
            f"{path.name} will be served as active content from the archive's origin"
        )
    assert [p.name for p in published] == ["annual-report.pdf"]

    # And nothing links to the name it arrived under, or the file would be
    # published safely and then advertised under the extension that is the bug.
    for path in out.rglob("*.html"):
        assert "annual-report.html" not in path.read_text(encoding="utf-8")


def test_the_polyglot_really_is_ingested_as_a_pdf(tmp_path):
    """The premise of the test above, pinned separately so it cannot rot."""
    path = tmp_path / "annual-report.html"
    path.write_bytes(
        b"<!doctype html><script>1</script>\n" + rawpdf.page_pdf(rawpdf.prose_lines())
    )
    usable, _ = discover_mod.discover(tmp_path)
    kinds = {f.path.name: f.kind for f in usable}
    assert kinds["annual-report.html"] == "pdf"


def test_the_preview_server_labels_html_as_html():
    """Why the extension matters: the preview server believes the suffix too."""
    from stackroom.serve import MIME_TYPES

    assert MIME_TYPES[".html"].startswith("text/html")
    assert MIME_TYPES[".svg"] == "image/svg+xml"


# --------------------------------------------------------------------------
# 3. Path handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "..",
        "...",
        "....",
        "  ",
        "/absolute/path",
        "a/b/c",
        "\\windows\\path",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "a" * 500,
        " nul",
        ".htaccess",
        "-",
        "---",
        "\U0001f600",
        "АБВ",
        "中文文件",
    ],
)
def test_a_slug_is_always_one_safe_url_segment(name):
    """Slugs become both URL segments and directory names, so they must be inert.

    Everything outside ``[a-z0-9-]`` is collapsed, which kills traversal,
    absolute paths, separators and NUL in one rule. The fallback for a name that
    folds to nothing is keyed on the file's digest, so it is stable for the file
    rather than for its position in the walk.
    """
    slug = discover_mod.slugify(name, "deadbeefcafebabe")
    assert slug, "an empty slug would collide with the parent directory"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug), slug
    assert len(slug) <= discover_mod.MAX_SLUG
    assert Path("root", slug).resolve().parent == Path("root").resolve()


def test_slugs_are_made_unique_across_unicode_normalisation(tmp_path):
    """NFC and NFD spellings of one name are two files that fold to one slug."""
    for i, name in enumerate(("Å.pdf", "Å.pdf")):
        (tmp_path / name).write_bytes(
            rawpdf.page_pdf(
                f"BT /F1 11 Tf 72 700 Td (v{i}) Tj ET\n".encode() + rawpdf.prose_lines()
            )
        )
    if len(list(tmp_path.iterdir())) < 2:  # pragma: no cover - depends on the host
        # A normalisation-insensitive filesystem (APFS, HFS+) folds the two
        # spellings into one file, so the collision this test guards against
        # cannot arise there.
        pytest.skip("this filesystem folds NFC and NFD into one name")
    usable, _ = discover_mod.discover(tmp_path)
    slugs = [f.slug for f in usable if f.kind == "pdf"]
    assert len(slugs) == 2
    assert len(set(slugs)) == 2, f"two documents were given the same URL: {slugs}"


@pytest.mark.parametrize("name", ["CON", "PRN", "NUL", "AUX", "COM1", "LPT1"])
def test_a_slug_is_never_a_windows_device_name(name):
    reserved = {"con", "prn", "nul", "aux", "com1", "com2", "lpt1", "lpt2"}
    assert discover_mod.slugify(name, "deadbeef") not in reserved


def test_discover_does_not_follow_a_symlink_out_of_the_source_folder(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "operators-own-source.pdf"
    secret.write_bytes(rawpdf.page_pdf(leaking_page("PRIVATE MATERIAL")))

    folder = tmp_path / "release"
    folder.mkdir()
    os.symlink(secret, folder / "appendix-b.pdf")

    usable, _ = discover_mod.discover(folder)
    followed = [f for f in usable if f.path.name == "appendix-b.pdf"]
    assert not followed, "a symlink was followed out of the source folder"


def test_the_build_publishes_nothing_from_outside_the_source_folder(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "private.pdf"
    secret.write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(3)))

    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "S"\n', encoding="utf-8")
    # A real document as well as the link, so the build has work to do and the
    # assertion is about what was published rather than about an empty release.
    (folder / "appendix-a.pdf").write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(3)))
    os.symlink(secret, folder / "appendix-b.pdf")

    out = tmp_path / "out"
    build_folder(folder, out)
    assert (out / "files" / "appendix-a.pdf").is_file(), "the ordinary document was dropped too"
    published = out / "files" / "appendix-b.pdf"
    assert not published.exists() or published.read_bytes() != secret.read_bytes()


def test_the_preview_server_refuses_traversal_and_symlink_escape(tmp_path):
    """``serve.py`` resolves both ends and compares. Pinned, because it works.

    The stdlib collapses ``..`` before joining, which handles the obvious
    attacks; what it does not do is refuse a symlink *inside* the served folder
    that points outside it, and a built site assembled from someone else's files
    can easily contain one. Both are checked here.
    """
    import http.client

    from stackroom.serve import make_server

    site = tmp_path / "site"
    (site / "files").mkdir(parents=True)
    (site / "index.html").write_text("<!doctype html>ok", encoding="utf-8")
    (site / "files" / "a.pdf").write_bytes(b"%PDF-1.7\n")
    secret = tmp_path / "outside.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    os.symlink(secret, site / "leak.txt")

    server = make_server(site, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        def get(path: str) -> tuple[int, bytes]:
            # Closed rather than left to the collector. The preview server is
            # threaded and speaks keep-alive, so a connection nobody hangs up
            # leaves its handler thread parked in `readline()` for as long as
            # the socket is open - past `server_close()`, which joins only the
            # non-daemon ones. `_PreviewServer` sets `daemon_threads` precisely
            # so that those parked threads cannot stop the process exiting, and
            # a test that relies on that is a test that would hang the day
            # somebody turns it off - which `serve.py` says out loud is the
            # case its `block_on_close = False` is written for.
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                return response.status, response.read(64)
            finally:
                conn.close()

        assert get("/index.html")[0] == 200
        for path in (
            "/../../../../etc/passwd",
            "/..%2f..%2f..%2fetc/passwd",
            "/%2e%2e/%2e%2e/etc/passwd",
            "/....//....//etc/passwd",
            "/files/../../outside.txt",
            "/leak.txt",
            "/./leak.txt",
        ):
            status, body = get(path)
            assert status == 404, f"{path} was served"
            assert b"TOP SECRET" not in body and b"root:" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "the preview server is still serving after server_close()"


POOLED = ("asyncio_", "ThreadPoolExecutor-")
"""Thread names belonging to a ``concurrent.futures`` pool.

Those are non-daemon and they are still fine: ``concurrent.futures`` registers
its own interpreter-shutdown handler, which puts a sentinel in every pool's
queue before joining, so an idle worker leaves on its own. ``asyncio_`` is the
prefix an event loop gives its default executor, and Playwright's driver reaches
that executor whenever it reads or writes a file - so one of these is alive for
as long as a browser is, and stops when the driver does.
"""


def test_no_server_thread_is_left_where_it_can_stop_the_process_exiting():
    """Every thread still running here can be left behind, so `pytest` returns.

    A leaked non-daemon thread fails nothing. It waits until the last test has
    run and the summary has printed, and then ``threading._shutdown()`` joins
    it - for ever, if it is sitting in ``serve_forever``. What CI shows is a
    green run that never ends, and what a contributor learns from that is to
    stop running the suite.

    Three suites here put an HTTP server behind a real browser -
    ``tests/conftest.py``'s ``served``, ``test_search.py``'s ``preview`` and
    ``test_offline.py``'s ``_Server`` - and each is one missing ``daemon=True``
    away from exactly that. This module is collected after all three (files run
    in name order, and `test_security` follows `test_offline`,
    `test_qol_browser` and `test_search`) and is not marked ``browser``, so both
    CI jobs reach it.

    It says nothing about how long a thread lives: a session-scoped server is
    *supposed* to still be serving at this point. The only question is whether
    the process could leave without it.
    """
    stuck = [
        f"{t.name} ({t})"
        for t in threading.enumerate()
        if t is not threading.main_thread()
        and not t.daemon
        and t.is_alive()
        and not t.name.startswith(POOLED)
    ]
    assert not stuck, (
        "a non-daemon thread outlived the test that started it, so pytest will "
        "hang after the summary instead of exiting: " + ", ".join(stuck)
    )


def test_prepare_out_never_deletes_through_a_symlink(tmp_path):
    """Emptying the output folder must not follow a link out of it.

    ``shutil.rmtree`` refuses a symlink-to-directory and ``Path.unlink`` removes
    the link rather than its target, so both cases are safe - but only by
    accident of two library behaviours, so they are pinned.
    """
    out = tmp_path / "out"
    out.mkdir()
    keep_dir = tmp_path / "elsewhere"
    keep_dir.mkdir()
    (keep_dir / "important.txt").write_text("do not delete", encoding="utf-8")
    keep_file = tmp_path / "important-file.txt"
    keep_file.write_text("do not delete", encoding="utf-8")

    # The marker stackroom itself writes. `.nojekyll` alone no longer counts as
    # ours - see test_prepare_out_does_not_claim_a_directory_it_did_not_build.
    (out / ".stackroom").write_text("", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    os.symlink(keep_dir, out / "dirlink")
    os.symlink(keep_file, out / "filelink")

    cli_mod._prepare_out(out, force=False)

    assert (keep_dir / "important.txt").exists()
    assert keep_file.exists()


def test_prepare_out_does_not_claim_a_directory_it_did_not_build(tmp_path):
    out = tmp_path / "my-website"
    out.mkdir()
    (out / "manifest.json").write_text('{"name": "My site"}', encoding="utf-8")
    (out / "index.html").write_text("<h1>ten years of work</h1>", encoding="utf-8")

    with pytest.raises(EXIT):
        cli_mod._prepare_out(out, force=False)
    assert (out / "index.html").exists()
    assert (out / "manifest.json").exists()


@finding(
    "F13",
    "PARTIALLY FIXED, and this half cannot be. find() no longer walks to the "
    "filesystem root - it stops after config.MAX_CONFIG_DEPTH - but this case is "
    "indistinguishable on disk from "
    "test_the_configuration_is_found_from_deep_inside_the_collection in "
    "tests/test_config.py, which pins the opposite answer for the identical "
    "shape: a stackroom.toml three directories above an empty folder. No rule "
    "over paths can separate 'the collection root' from 'somebody else's "
    "directory', so the residual risk is answered by making it visible instead - "
    "see test_the_cli_says_which_configuration_file_it_used",
)
def test_configuration_is_not_taken_from_outside_the_document_folder(tmp_path):
    (tmp_path / "stackroom.toml").write_text('title = "Not theirs"\n', encoding="utf-8")
    release = tmp_path / "somewhere" / "deep" / "release"
    release.mkdir(parents=True)
    assert config_find(release) is None


def test_the_configuration_search_does_not_reach_the_filesystem_root(tmp_path):
    """What *was* fixed: the walk is bounded, so a distant file cannot govern.

    A ``stackroom.toml`` in a home directory or in ``/tmp`` used to be found
    from any depth below it. Four levels down is now out of reach, which is the
    half of F13 that a rule over paths can actually decide.
    """
    from stackroom.config import MAX_CONFIG_DEPTH

    (tmp_path / "stackroom.toml").write_text('title = "Not theirs"\n', encoding="utf-8")
    deep = tmp_path.joinpath(*[f"d{i}" for i in range(MAX_CONFIG_DEPTH + 1)])
    deep.mkdir(parents=True)
    assert config_find(deep) is None
    assert config_find(deep.parent) is not None, "the bound is off by one"


def test_the_cli_says_which_configuration_file_it_used(tmp_path, capsys):
    """And the other half: a file from outside the named folder is announced.

    The operator cannot be protected from a configuration file they have never
    seen by a rule about directories, because the legitimate case has the same
    shape. They can be told, in the one place they are looking.
    """
    (tmp_path / "stackroom.toml").write_text('title = "Not theirs"\n', encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir()

    cli_mod._load_config(release, None)
    # Rich wraps the announcement around the path, and where the line breaks
    # fall depends on how long tmp_path is - so read it with the whitespace
    # squashed to single spaces.
    out = " ".join(capsys.readouterr().out.split())
    assert "stackroom.toml" in out
    assert "not inside" in out, "a config from outside the named folder was used quietly"

    (release / "stackroom.toml").write_text('title = "Theirs"\n', encoding="utf-8")
    cli_mod._load_config(release, None)
    assert "not inside" not in " ".join(capsys.readouterr().out.split())


# --------------------------------------------------------------------------
# 4. Resource exhaustion
# --------------------------------------------------------------------------


def test_the_pixel_budget_applies_to_the_path_the_build_actually_uses(tmp_path):
    """A poster-sized page must be rasterised inside the configured budget.

    ``render.max_megapixels`` used to be read only by ``raster.render_pdf()``,
    which the pipeline never calls: the build path is
    ``render_page_crop(FULL_PAGE)``, which had no budget at all. Asserted at
    both levels, because the finding was precisely that the two had come apart -
    the function honouring the budget was not the function being used.
    """
    path = tmp_path / "poster.pdf"
    path.write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(), mediabox="0 0 2880 2880"))
    geometry = raster_mod.page_geometry(path)[0]
    budget = 4_000_000
    wanted = geometry.pixel_size(150)
    assert wanted[0] * wanted[1] > budget, "the fixture is not big enough to test"

    image = raster_mod.render_page_crop(path, 1, Box(0, 0, 1, 1), dpi=150, max_pixels=budget)
    assert image.width > 1, "poppler refused and returned a 1x1 image rather than an error"
    assert image.width * image.height <= budget

    # And through the pipeline, which is where the budget was missing.
    job = one_page_job(path, tmp_path / "m")
    job.dpi = 150
    job.max_megapixels = budget / 1e6
    outcome = process_page(job)
    assert not outcome.error, outcome.error
    published = outcome.page.images or outcome.page.thumbs
    assert published, "the page was not rendered at all"
    rendered, _ = pipeline_mod._rasterise(job, path)
    assert rendered.width * rendered.height <= budget


def test_a_page_poppler_refuses_to_allocate_is_an_error_not_a_one_pixel_scan(tmp_path):
    """Poppler answers an impossible allocation with a 1x1 PNG and exit 0.

    "Bogus memory allocation size" goes to stderr and nothing else says the call
    failed, so a renderer that trusts the exit status publishes a one-pixel scan
    - and clears the redaction check on a page nobody looked at, because a 1x1
    image is uniform. It has to be an error.
    """
    path = tmp_path / "huge.pdf"
    path.write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(), mediabox="0 0 14400 14400"))
    with pytest.raises(raster_mod.RenderError) as caught:
        raster_mod.render_page_crop(path, 1, Box(0, 0, 1, 1), dpi=150)
    assert "1x1" in str(caught.value)

    # Inside a budget the page renders instead, softer but real.
    job = one_page_job(path, tmp_path / "m")
    job.max_megapixels = 4.0
    assert not process_page(job).error

    # And where the budget does not bind, the pipeline reports a page it could
    # not check rather than a page with nothing to find.
    job.max_megapixels = 2000.0
    outcome = process_page(job)
    assert outcome.error and outcome.analysis_failed


def test_the_exemption_scanner_does_not_backtrack_catastrophically():
    """Growth must be roughly linear in the length of the input.

    Timed rather than asserted structurally, because the shape of the blow-up -
    ``\\s*`` then an optional dash then ``\\s*``, inside an unanchored pattern -
    is not visible from outside the module.
    """

    def elapsed(n: int) -> float:
        text = "(b" + " " * n + "x"
        start = time.perf_counter()
        exemptions_mod.scan_text(text)
        return time.perf_counter() - start

    small = max(elapsed(200), 1e-4)
    large = elapsed(800)
    assert large / small < 20, (
        f"quadrupling the input multiplied the time by {large / small:.0f}"
    )


def test_realistic_page_text_scans_in_linear_time():
    """Pins the reachable case: page text is tokens joined by single spaces."""

    def elapsed(text: str) -> float:
        start = time.perf_counter()
        exemptions_mod.scan_text(text)
        return time.perf_counter() - start

    small = max(elapsed("( b ) ( 1 " * 500), 1e-4)
    large = elapsed("( b ) ( 1 " * 4000)
    assert large / small < 40


def test_the_word_extractor_drops_every_character_re_calls_whitespace():
    """The reason the ReDoS above is not reachable from a PDF today.

    If this ever stops being true - a different word grouper, a text ingest path,
    an OCR engine that keeps U+00A0 - the exemption scanner becomes a denial of
    service on one page of text, so the dependency is pinned explicitly here.
    """
    from pdfplumber.utils import extract_words

    exotic = ("\u00a0", "\u2007", "\u202f", "\u3000", "\u000b", "\u001c")
    for ch in exotic:
        assert re.match(r"\s", ch), f"U+{ord(ch):04X} is no longer matched by \\s"
        chars = [
            {
                "text": t, "x0": i * 5.0, "x1": i * 5.0 + 5, "top": 0.0,
                "bottom": 10.0, "doctop": 0.0, "upright": True, "size": 10.0,
            }
            for i, t in enumerate(["b", ch, ch, ch, "x"])
        ]
        assert [w["text"] for w in extract_words(chars)] == ["b", "x"]


def test_bates_patterns_are_linear_in_token_length():
    """A single token has no length limit, so the stamp patterns must not care."""
    from stackroom.ingest import bates

    def elapsed(token: str) -> float:
        start = time.perf_counter()
        for _ in range(200):
            bates.PREFIXED_RE.match(token)
            bates.NUMERIC_RE.match(token)
        return time.perf_counter() - start

    small = max(elapsed("A" + "-0.9" * 250), 1e-4)
    large = elapsed("A" + "-0.9" * 25000)
    assert large / small < 20


def test_bates_gap_expansion_is_capped():
    """A stamp jumping from 1 to a billion must not enumerate a billion numbers."""
    from stackroom.ingest.bates import MAX_GAP_SPAN, _gap_ranges

    assert MAX_GAP_SPAN <= 5000
    assert _gap_ranges(1, 1_000_000_000, frozenset()) == [(2, 999_999_999)]


@pytest.mark.parametrize("value", [0, 0.0, -1, 10**9])
def test_a_configuration_cannot_switch_off_the_ocr_timeout(tmp_path, value):
    path = tmp_path / "stackroom.toml"
    path.write_text(f"[ocr]\ntimeout = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        config_load(path)


def test_pytesseract_treats_a_falsy_timeout_as_no_timeout():
    """The upstream behaviour the finding above depends on."""
    import inspect

    from pytesseract import pytesseract as pt

    source = inspect.getsource(pt.timeout_manager)
    assert "if not timeout" in source or "if not seconds" in source


def test_no_subprocess_is_run_through_a_shell_or_without_a_timeout():
    """Static check over the whole package: argv lists, always bounded.

    ``pdftoppm``, ``pdfinfo``, ``tesseract`` and ``pagefind`` all run on
    attacker-chosen bytes. A shell would turn a filename into an injection, and
    a missing timeout turns one hostile page into a hung build.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            if target in ("os.system", "os.popen"):
                offenders.append((path.name, node.lineno, "runs a shell"))
                continue
            if target not in (
                "subprocess.run", "subprocess.Popen",
                "subprocess.call", "subprocess.check_output",
            ):
                continue
            kwargs = {k.arg for k in node.keywords}
            if "shell" in kwargs:
                offenders.append((path.name, node.lineno, "passes shell="))
            if "timeout" not in kwargs:
                offenders.append((path.name, node.lineno, "no timeout"))
    assert offenders == []


def test_a_configuration_cannot_choose_the_argument_a_subprocess_receives():
    """Every config value that reaches an argv is filtered to a known vocabulary."""
    from stackroom.lang import normalize_language_codes

    for hostile in ("--output-subdir", "; rm -rf /", "../../etc", "$(id)", "-rf"):
        assert normalize_language_codes([hostile]) == []
    assert normalize_language_codes(["eng"]) == ["en"]


# --------------------------------------------------------------------------
# 5. The safety guarantee
# --------------------------------------------------------------------------


def test_the_control_leak_is_caught(tmp_path):
    """The fixture every evasion below is measured against.

    If this ever fails, every ``xfail`` in this section is meaningless.
    """
    path = tmp_path / "release.pdf"
    path.write_bytes(rawpdf.page_pdf(leaking_page()))
    outcome = process_page(one_page_job(path, tmp_path / "m"))
    assert [h.redacted_repr() for h in outcome.hidden] == ["###### #### #####"]


def test_a_page_that_could_not_be_rendered_is_reported_as_unchecked(tmp_path):
    path = tmp_path / "unrenderable.pdf"
    path.write_bytes(rawpdf.page_pdf(leaking_page(), pages=3, declared_count=1))

    outcome = process_page(one_page_job(path, tmp_path / "m", number=2))
    assert outcome.error, "the fixture rendered after all"
    assert outcome.analysis_failed, (
        "a page nobody looked at was recorded as a page with nothing to find"
    )


def test_check_never_says_clear_about_pages_it_could_not_read(tmp_path):
    """A ``/Count`` that lies is enough to switch the check off.

    Poppler trusts ``/Count`` and reports one page; pdfminer walks ``/Kids`` and
    reports three. Pages 2 and 3 are queued, fail to rasterise, and then vanish -
    not as findings, not as unchecked pages, not as warnings. Page one is clean
    on purpose, so nothing here rests on the leak the tool *can* see.
    """
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "R"\n', encoding="utf-8")
    (folder / "release.pdf").write_bytes(
        rawpdf.multi_page_pdf(
            [
                rawpdf.prose_lines(3),
                leaking_page("SOURCE NAME ALPHA"),
                leaking_page("SOURCE NAME BRAVO"),
            ],
            declared_count=1,
        )
    )

    _, outcomes = ingest(folder, tmp_path / "out")
    assert len(outcomes) == 3, "the fixture no longer queues the pages poppler cannot see"
    unchecked = [o for o in outcomes if o.analysis_failed]
    leaking = [o for o in outcomes if o.hidden]
    assert leaking or unchecked, (
        "three pages processed, two of them unreadable and both leaking, "
        "and `stackroom check` reports Clear"
    )
def test_a_page_only_one_parser_can_see_is_still_queued_and_reported(tmp_path):
    """The same disagreement in the other direction, which nothing named.

    ``/Count 3`` over a single ``/Kid``: pdfminer walks the kids and reports one
    page, poppler believes ``/Count`` and reports three. The page count used to
    come from pdfminer alone, so two pages a viewer will show were never queued,
    never rendered and never checked - and the archive published a truncated
    document without a word about it, which is F1 wearing a different hat.
    """
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "R"\n', encoding="utf-8")
    (folder / "release.pdf").write_bytes(
        rawpdf.page_pdf(rawpdf.prose_lines(3), pages=1, declared_count=3)
    )

    _, outcomes = ingest(folder, tmp_path / "out")
    assert len(outcomes) == 3, "a page the document claims to have was never looked at"
    assert sum(1 for o in outcomes if o.analysis_failed) == 2, (
        "the pages that could not be produced were not reported as unchecked"
    )




def test_the_rendered_frame_and_the_content_stream_frame_are_the_same(tmp_path):
    """The invariant the whole pixel-confirmation step rests on.

    ``ingest/pdf.py`` reports boxes relative to pdfminer's page box; the crop
    renderer maps those fractions onto poppler's raster. If the two disagree
    about the size of the page, every confirmation looks at the wrong pixels -
    and ``CropBox != MediaBox`` is ordinary, not exotic: every Acrobat "crop
    pages" and a great many scanners produce it.
    """
    path = tmp_path / "cropped.pdf"
    path.write_bytes(
        rawpdf.page_pdf(
            rawpdf.prose_lines(3), mediabox="0 0 1224 1584", cropbox="0 0 612 792"
        )
    )
    geometry = raster_mod.page_geometry(path)[0]
    with pdf_mod.open_pdf(path) as handle:
        raw = pdf_mod.read_page(handle, 0)
    image = raster_mod.render_page_crop(path, 1, Box(0, 0, 1, 1), dpi=150)

    assert (raw.width_pt, raw.height_pt) == (geometry.width_pt, geometry.height_pt)
    assert image.size == geometry.pixel_size(150)


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("no crop box at all", {"mediabox": "0 0 612 792"}),
        ("crop box inside the media box", {"mediabox": "0 0 1224 1584", "cropbox": "0 0 612 792"}),
        ("crop box with its own origin", {"mediabox": "0 0 842 1191", "cropbox": "20 20 800 1150"}),
        ("crop box written back to front", {"mediabox": "0 0 612 792", "cropbox": "900 900 -100 -100"}),
        ("crop box larger than the page", {"mediabox": "0 0 612 792", "cropbox": "0 0 1000 1000"}),
        (
            "crop box under /Rotate 90",
            {"mediabox": "0 0 1224 1584", "cropbox": "0 0 612 792", "page_extra": "/Rotate 90 "},
        ),
        (
            "crop box under /Rotate 270",
            {"mediabox": "0 0 842 1191", "cropbox": "20 20 800 1150", "page_extra": "/Rotate 270 "},
        ),
    ],
)
def test_the_two_frames_agree_however_the_page_is_cropped(tmp_path, name, kwargs):
    """The invariant, over every crop box shape a file can legally carry.

    Three components have to describe the same rectangle: ``read_page`` (which
    reports the boxes), ``page_geometry`` (which converts them to pixels) and
    the raster ``render_page_crop`` actually returns. Any pair disagreeing
    points the pixel confirmation at a different part of the page - silently,
    with no finding and no warning.
    """
    path = tmp_path / "page.pdf"
    path.write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(3), **kwargs))

    geometry = raster_mod.page_geometry(path)[0]
    with pdf_mod.open_pdf(path) as handle:
        raw = pdf_mod.read_page(handle, 0)
    image = raster_mod.render_page_crop(path, 1, Box(0, 0, 1, 1), dpi=150)

    assert image.size == geometry.pixel_size(150), f"{name}: geometry does not predict the raster"
    assert (round(raw.width_pt, 3), round(raw.height_pt, 3)) == (
        round(geometry.rendered_pt[0], 3),
        round(geometry.rendered_pt[1], 3),
    ), f"{name}: the content stream frame is not the rendered frame"


def test_a_cropped_page_reports_its_words_where_the_ink_is(tmp_path):
    """Not only the same size - the same origin.

    A crop box whose lower-left corner is not the media box's shifts every
    coordinate. Getting the size right and the offset wrong looks correct in
    every dimension check and still points each box at the wrong words.
    """
    import numpy as np

    path = tmp_path / "offset.pdf"
    path.write_bytes(
        rawpdf.page_pdf(
            b"BT /F1 40 Tf 300 900 Td (XXXX) Tj ET\n" + rawpdf.prose_lines(3, x=300, top=860),
            mediabox="0 0 1000 1200",
            cropbox="200 200 900 1100",
        )
    )
    with pdf_mod.open_pdf(path) as handle:
        raw = pdf_mod.read_page(handle, 0)
    image = raster_mod.render_page_crop(path, 1, Box(0, 0, 1, 1), dpi=100)

    word = next(w for w in raw.words if w.text.startswith("XXXX"))
    pixels = np.asarray(image.convert("L"))
    x0, x1 = int(word.box.x * image.width), int(word.box.x2 * image.width)
    y0, y1 = int(word.box.y * image.height), int(word.box.y2 * image.height)
    patch = pixels[y0:y1, x0:x1]
    assert patch.size, "the word's box falls outside the rendered page"
    assert patch.min() < 128, "there is no ink where the word says it is"


def test_a_cropbox_that_differs_from_the_mediabox_cannot_hide_a_leak(tmp_path):
    """The same page twice; the only difference is a ``/CropBox``.

    The grey noise sits in the part of the MediaBox *outside* the CropBox - no
    reader ever sees it - and it is exactly where the mis-mapped crop looks. The
    check finds textured pixels where it expected a flat box, concludes the text
    underneath must be visible, and reports nothing.
    """
    content = (
        b"BT /F1 14 Tf 72 700 Td (SOURCE NAME FOXTROT) Tj ET\n"
        b"0 0 0 rg 68 694 190 24 re f\n"
        + b"".join(
            f"BT /F1 11 Tf 40 {620 - i * 20} Td ({rawpdf.PROSE[:60]}) Tj ET\n".encode()
            for i in range(20)
        )
        + b"q 200 0 0 60 0 1110 cm /Im0 Do Q\n"
    )
    shared = {
        "mediabox": "0 0 1224 1584",
        "extra_objects": (rawpdf.noise_image(),),
        "xobjects": "/XObject << /Im0 6 0 R >> ",
    }
    control = tmp_path / "control.pdf"
    cropped = tmp_path / "cropped.pdf"
    control.write_bytes(rawpdf.page_pdf(content, **shared))
    cropped.write_bytes(rawpdf.page_pdf(content, cropbox="0 0 612 792", **shared))

    def findings_for(path: Path) -> list[str]:
        with pdf_mod.open_pdf(path) as handle:
            raw = pdf_mod.read_page(handle, 0)
        image = raster_mod.render_page_crop(path, 1, Box(0, 0, 1, 1), dpi=150)
        result = redaction_mod.analyse_page(
            raw, image, crop_renderer=pipeline_mod._in_memory_cropper(image)
        )
        return [h.redacted_repr() for h in result.hidden]

    assert findings_for(control), "the control did not leak; the fixture is wrong"
    assert findings_for(cropped) == findings_for(control)


def test_a_warning_about_an_ambiguous_box_reaches_the_operator():
    """SECURITY.md: "Ambiguous evidence is reported for a human." It is not.

    Asserted structurally rather than by scraping the terminal: the CLI has to
    at least *read* the field before it can print it.
    """
    read = False
    for path in (SRC / "cli.py", SRC / "build" / "site.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "warnings":
                text = ast.unparse(node)
                if "outcome" in text or "PageOutcome" in text:
                    read = True
    assert read, "PageOutcome.warnings is written by the pipeline and read by nobody"


def test_the_ambiguous_box_warning_is_printed_and_grouped(tmp_path, capsys):
    """And it reaches the terminal, once, however many pages carry it.

    Grouped rather than one line per page: 400 copies of one sentence is not a
    report, and an operator who scrolls past it has been told nothing.
    """
    outcomes = [
        pipeline_mod.PageOutcome(
            doc_id="d",
            number=n,
            page=Page(number=n),
            warnings=[
                "box (6%, 55%) has 19 character(s) painted under it in the content "
                "stream, but 19 of them stand out of their own cells in the rendered "
                "page - check this box by hand.",
                "the page was scanned 90 degrees out of upright",
            ],
        )
        for n in range(1, 8)
    ]
    cli_mod._page_notes(outcomes)
    # Wrapping is the terminal's business and it moves with the width the test
    # happens to run at: this note is long enough that rich breaks it, and a
    # newline landing inside the phrase being looked for would fail this test
    # for a reason that has nothing to do with what it is about.
    out = " ".join(capsys.readouterr().out.split())

    assert "check this box by hand" in out
    assert out.count("check this box by hand") == 1, "one line per page is not a report"
    assert "and 3 more" in out, "the pages carrying the note were not summarised"
    # Safety first: an operator deciding whether to publish reads from the top.
    assert out.index("check this box by hand") < out.index("degrees out of upright")


def test_a_page_that_could_not_be_checked_says_so_on_the_way_out(tmp_path, capsys):
    """The note that matters most, end to end through the CLI's own reporting."""
    path = tmp_path / "unrenderable.pdf"
    path.write_bytes(rawpdf.page_pdf(leaking_page(), pages=3, declared_count=1))
    outcome = process_page(one_page_job(path, tmp_path / "m", number=2))

    cli_mod._page_notes([outcome])
    out = capsys.readouterr().out
    assert "never checked" in out or "could not be rendered" in out


def test_text_under_a_box_is_never_published_even_when_the_finding_is_suppressed(tmp_path):
    """Default ``stop`` mode, so nothing here depends on --unsafe-publish-leaks."""
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "D"\n', encoding="utf-8")
    ops = []
    for i, when in enumerate(("03/14/2019", "07/02/2019", "11/30/2020")):
        y = 700 - i * 40
        ops.append(f"BT /F1 12 Tf 72 {y} Td ({when}) Tj ET\n".encode())
        ops.append(f"0 0 0 rg 68 {y - 6} 90 22 re f\n".encode())
    # The prose sits below the lowest box. With the default top of 640 the third
    # box lands on it, the finding stops being a bare date, and the page no
    # longer demonstrates the suppression this test is about.
    (folder / "docket.pdf").write_bytes(
        rawpdf.page_pdf(b"".join(ops) + rawpdf.prose_lines(top=560))
    )

    out = tmp_path / "out"
    build_folder(folder, out)
    page = (out / "d" / "docket" / "p" / "1" / "index.html").read_text(encoding="utf-8")
    assert "03/14/2019" not in " ".join(tokens_of(page))


def test_a_partly_covered_word_is_not_published_whole(tmp_path):
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "P"\n', encoding="utf-8")
    (folder / "partial.pdf").write_bytes(
        rawpdf.page_pdf(
            b"BT /F1 14 Tf 72 700 Td (ALPHABRAVOCHARLIEDELTAECHO) Tj ET\n"
            b"0 0 0 rg 70 694 96 22 re f\n" + rawpdf.prose_lines()
        )
    )
    cfg = fast_config()
    cfg.safety.hidden_text = "warn"  # what --unsafe-publish-leaks sets
    out = tmp_path / "out"
    collection, outcomes = build_collection(folder, cfg, out, workers=1)
    assert any(o.hidden for o in outcomes), "the fixture did not leak"
    site_mod.attach_about(collection, cfg)
    build_site(collection, cfg, out)

    page = (out / "d" / "partial" / "p" / "1" / "index.html").read_text(encoding="utf-8")
    assert "ALPHABRAVO" not in page, (
        "the CLI promises the recovered text is kept out of the site even in warn mode"
    )

    # Not just the page: the JSON payload, the docs index and the search index
    # are all built from the same words, and any one of them publishes it.
    # files/ is excluded because the original is copied byte for byte by design.
    offenders = [
        str(f.relative_to(out))
        for f in out.rglob("*")
        if f.is_file()
        and f.relative_to(out).parts[:1] != ("files",)
        and b"ALPHABRAVO" in f.read_bytes()
    ]
    assert offenders == [], f"the recovered text was published in {offenders}"


def test_a_stopped_build_writes_no_file_containing_the_recovered_text(tmp_path):
    """The guarantee that does hold: in ``stop`` mode nothing is published.

    Belt and braces - the site is never written, and the recovered text is
    nowhere under the output directory even though rendered images already are.
    """
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "L"\n', encoding="utf-8")
    (folder / "leak.pdf").write_bytes(rawpdf.page_pdf(leaking_page("SOURCE NAME ALPHA")))

    out = tmp_path / "out"
    cfg = fast_config()
    _, outcomes = build_collection(folder, cfg, out, workers=1)
    with pytest.raises(SafetyStop):
        check_safety(outcomes, cfg)

    for path in out.rglob("*"):
        if path.is_file():
            assert b"SOURCE NAME ALPHA" not in path.read_bytes(), path


def test_the_leak_report_shows_a_shape_and_never_the_text():
    """What the operator sees, and what they can safely paste into a bug report."""
    from stackroom.model import HiddenText

    shape = HiddenText(box=Box(0, 0, 0.1, 0.1), text="Smith, Jonathan").redacted_repr()
    assert shape == "#####, ########"
    assert "Smith" not in shape and "Jonathan" not in shape


# --------------------------------------------------------------------------
# 1a. The leak report's arithmetic
# --------------------------------------------------------------------------
#
# This is the one report in the program that must not omit evidence quietly.
# An operator reads it to decide whether the files they are about to hand over
# are safe, so the number above the table and the rows below it are allowed to
# differ only when the report says by how much. It printed three rows per page
# and said nothing at all about the fourth.


def leak_findings(pages: int, per_page: int, text: str = "Smith, Jonathan"):
    """`pages` pages, each hiding `per_page` passages, shaped like real ones."""
    from stackroom.model import HiddenText

    return [
        (
            f"doc-{p:02d}",
            p + 1,
            [
                HiddenText(box=Box(0, 0, 0.1, 0.1), text=f"{text} {p}-{i}")
                for i in range(per_page)
            ],
        )
        for p in range(pages)
    ]


def leak_report(findings, *, full: bool = False, width: int = 120) -> str:
    """`cli._leak_report` rendered to a string, with the colours taken off."""
    import io

    from rich.console import Console

    buffer = io.StringIO()
    before = cli_mod.err
    cli_mod.err = Console(file=buffer, width=width, no_color=True, highlight=False, soft_wrap=False)
    try:
        passages = sum(len(hidden) for _, _, hidden in findings)
        stop = SafetyStop(
            f"{passages} passage(s) on {len(findings)} page(s) are covered by a black box "
            "but still readable in the file.",
            findings,
        )
        cli_mod._leak_report(stop, full=full)
    finally:
        cli_mod.err = before
    return buffer.getvalue()


def leak_rows(report: str, doc_id: str) -> list[str]:
    """The table rows the report printed for one document."""
    return [line for line in report.splitlines() if line.strip().startswith(doc_id)]


def test_the_leak_report_never_drops_a_passage_without_saying_so():
    """Four leaks on a page used to print three rows and no fourth anything.

    The header said "4 passage(s)", the table showed three, and the operator
    had no way to know which number to believe. Whatever the report chooses not
    to print, it has to count.
    """
    report = leak_report(leak_findings(pages=1, per_page=4))
    assert len(leak_rows(report, "doc-00")) == 4, "a passage went missing under the count"
    assert "4 passage(s)" in report
    assert "every one of the 4 passage(s)" in report
    assert "more passage(s)" not in report, "nothing was omitted; the report should not imply it"

    # And past the point where printing them all stops being a report, every
    # passage is still either a row or counted in one - on the page it is on,
    # because "which page have I not seen?" is the operator's next question.
    many = leak_report(leak_findings(pages=1, per_page=200))
    assert len(leak_rows(many, "doc-00")) == cli_mod.LEAK_ROWS_PER_PAGE + 1
    assert f"and {200 - cli_mod.LEAK_ROWS_PER_PAGE} more passage(s) on this page" in many
    assert f"Listed above: {cli_mod.LEAK_ROWS_PER_PAGE} of 200 passage(s)" in many
    assert "--debug" in many, "the operator is not told how to see the rest"


def test_the_leak_report_reconciles_its_own_count_with_its_own_rows():
    """Every passage found is either a row or is counted in one, on every page.

    Two hundred pages of four leaks each is 800 passages and nobody wants 800
    rows - but "listed 40 of 800, on 10 of 200 pages" is a true sentence and
    "… and 190 more pages" under a table of 30 rows is not.
    """
    findings = leak_findings(pages=200, per_page=4)
    report = leak_report(findings)

    listed = re.search(
        r"Listed above: ([\d,]+) of ([\d,]+) passage\(s\), on ([\d,]+) of ([\d,]+) page\(s\)",
        report,
    )
    assert listed, f"the report did not reconcile itself:\n{report}"
    shown_passages, passages, shown_pages, pages = (
        int(g.replace(",", "")) for g in listed.groups()
    )
    assert (passages, pages) == (800, 200)

    rows = [line for line in report.splitlines() if re.match(r"\s*doc-\d+\s", line)]
    counted = sum(
        int(found.group(1).replace(",", ""))
        for found in (re.search(r"and ([\d,]+) more passage\(s\)", line) for line in rows)
        if found
    )
    printed = len(rows) - sum(1 for line in rows if "more passage(s)" in line)
    assert printed == shown_passages, (
        f"the report says it listed {shown_passages} passages and printed {printed} rows"
    )
    assert len({line.split()[0] for line in rows}) == shown_pages
    # Nothing is lost between the two halves of the sentence: what was printed,
    # plus what was counted in place, plus what is on the pages it never
    # reached, is everything that was found.
    unreached = sum(len(hidden) for _, _, hidden in findings[shown_pages:])
    assert printed + counted + unreached == passages


def test_debug_prints_every_leak_and_says_when_it_did():
    """The unabridged list belongs behind the flag that already means "all of it"."""
    findings = leak_findings(pages=3, per_page=60)
    report = leak_report(findings, full=True)

    assert len(leak_rows(report, "doc-00")) == 60
    assert "more passage(s) on this page" not in report
    assert "Listed above:" not in report
    assert "every one of the 180 passage(s), on all 3 page(s)" in report


def test_the_leak_report_marks_a_shape_it_had_to_cut():
    """The length beside a shape is the passage's, so a cut shape has to show it.

    A 300-character passage rendered as 52 hashes next to the number 300 is two
    facts on one line that contradict each other.
    """
    report = leak_report(leak_findings(pages=1, per_page=1, text="Jonathan " * 40), width=200)
    row = leak_rows(report, "doc-00")[0]
    assert "…" in row, f"a shape was cut to the column width with nothing to show for it: {row}"
    assert "Jonathan" not in report and "#" in row


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("white-on-white", b"BT 1 1 1 rg /F1 14 Tf 72 700 Td (SOURCE NAME DELTA) Tj ET\n"),
        (
            "clipped-away",
            b"q 0 0 1 1 re W n\nBT /F1 14 Tf 72 700 Td (SOURCE NAME DELTA) Tj ET\nQ\n",
        ),
        (
            "thin-bar",
            b"BT /F1 3 Tf 72 700 Td (SOURCE NAME DELTA) Tj ET\n0 0 0 rg 70 698 180 4 re f\n",
        ),
        (
            "letterhead-band",
            b"BT /F1 10 Tf 72 770 Td (SOURCE NAME DELTA) Tj ET\n0 0 0 rg 70 766 180 16 re f\n",
        ),
    ],
)
def test_documented_limits_of_the_hidden_text_check(tmp_path, name, content):
    """SECURITY.md lists these. They are pinned so the list cannot go stale.

    Each hides text from a reader by a means the check is not anchored on, so no
    finding is produced - *and Stackroom then publishes that text as ordinary
    page text and indexes it for search*, which is a materially stronger
    statement than "the check misses it". That second half is what the threat
    model asks the documentation to start saying.
    """
    path = tmp_path / f"{name}.pdf"
    path.write_bytes(rawpdf.page_pdf(content + rawpdf.prose_lines()))
    outcome = process_page(one_page_job(path, tmp_path / "m"))
    assert outcome.hidden == [], f"{name} is caught after all; update SECURITY.md"
    assert "DELTA" in " ".join(w.text for w in outcome.page.words), (
        f"{name} no longer publishes the hidden text; update this test and SECURITY.md"
    )


def test_invisible_text_is_not_promoted_into_the_published_page(tmp_path):
    path = tmp_path / "invisible.pdf"
    path.write_bytes(
        rawpdf.page_pdf(
            b"BT 3 Tr /F1 14 Tf 72 700 Td (SOURCE NAME DELTA) Tj ET\n"
            b"BT /F1 14 Tf 72 700 Td ([REDACTED]) Tj ET\n" + rawpdf.prose_lines()
        )
    )
    outcome = process_page(one_page_job(path, tmp_path / "m"))
    published = "".join(w.text for w in outcome.page.words)
    assert "DELTA" not in published
    assert any("invisible" in w for w in outcome.warnings), (
        "text was withheld from the page and the operator was not told"
    )


def test_an_invisible_ocr_layer_over_a_scan_is_still_published(tmp_path):
    """The other half of F15, and the reason it cannot simply drop mode 3.

    Every searchable scan in existence is an image of the page with an
    invisible transcription behind it, and that transcription is the only text
    the page has. A rule that withheld it would leave the whole class of
    scanned collections unsearchable - which is most of what this tool is
    pointed at.
    """
    path = tmp_path / "scan.pdf"
    path.write_bytes(
        rawpdf.page_pdf(
            b"q 500 0 0 700 56 60 cm /Im0 Do Q\n"
            b"BT 3 Tr /F1 11 Tf 72 700 Td (" + rawpdf.PROSE[:60].encode() + b") Tj ET\n"
            b"BT 3 Tr /F1 11 Tf 72 680 Td (SOURCE NAME DELTA and more prose) Tj ET\n",
            extra_objects=(rawpdf.noise_image(500, 700),),
            xobjects="/XObject << /Im0 6 0 R >> ",
        )
    )
    outcome = process_page(one_page_job(path, tmp_path / "m"))
    published = " ".join(w.text for w in outcome.page.words)
    assert "DELTA" in published, "the OCR layer of a scanned page was thrown away"


# --------------------------------------------------------------------------
# 6. Metadata and earlier revisions
# --------------------------------------------------------------------------


def test_strip_metadata_removes_author_and_producer_from_the_published_file(tmp_path):
    """The sanitiser itself: ``/Info`` does not survive the rewrite."""
    source = tmp_path / "release.pdf"
    source.write_bytes(
        rawpdf.page_pdf(
            rawpdf.prose_lines(3),
            info={"Author": "Case Officer M. Petrova", "Producer": "Agency Redaction Suite"},
        )
    )
    result = pdf_mod.publish_pdf(source, tmp_path / "out" / "release.pdf", strip=True)
    assert result.stripped and not result.note
    published = (tmp_path / "out" / "release.pdf").read_bytes()
    assert b"Petrova" not in published
    assert b"Agency Redaction Suite" not in published
    assert b"%PDF" in published[:1024], "the published file is not a PDF any more"


def test_strip_metadata_removes_the_revision_history(tmp_path):
    """The case that actually burns somebody.

    An incremental save keeps every earlier revision of every object it
    replaced, so a "corrected" release routinely still contains the uncorrected
    text. ``pdftotext`` on it shows nothing wrong; anyone who truncates the file
    at the first ``%%EOF`` reads revision one. No amount of deleting dictionary
    entries removes that - only writing a new file from the page tree does.
    """
    first = rawpdf.page_pdf(
        b"BT /F1 14 Tf 72 700 Td (WITHHELD NAME: Jonathan Smith) Tj ET\n"
        + rawpdf.prose_lines()
    )
    corrected = rawpdf.incremental_update(
        first,
        obj_number=5,
        body=rawpdf.stream_obj(
            b"BT /F1 14 Tf 72 700 Td (WITHHELD NAME: [REDACTED]) Tj ET\n"
            + rawpdf.prose_lines()
        ),
    )
    assert b"Jonathan Smith" in corrected, "the fixture is not a real incremental save"
    source = tmp_path / "release.pdf"
    source.write_bytes(corrected)

    result = pdf_mod.publish_pdf(source, tmp_path / "out" / "release.pdf", strip=True)
    assert result.stripped
    assert b"Jonathan Smith" not in (tmp_path / "out" / "release.pdf").read_bytes()


def test_a_file_that_cannot_be_stripped_is_published_with_a_reason(tmp_path):
    """A *silently* unstripped original is how this option becomes a liability.

    Failing the build over a courtesy would be the wrong answer, so the file is
    published unchanged - and the caller is handed a sentence to print.
    """
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"%PDF-1.7\nnot really a pdf\n")
    result = pdf_mod.publish_pdf(source, tmp_path / "out" / "broken.pdf", strip=True)
    assert not result.stripped
    assert result.note, "the operator would never learn that nothing was stripped"
    assert (tmp_path / "out" / "broken.pdf").read_bytes() == source.read_bytes()


def test_the_published_digest_is_reported_because_stripping_changes_it(tmp_path):
    """A rewritten original no longer matches the SHA-256 in the manifest.

    Both numbers have to reach the reader, labelled, or an archive that strips
    is an archive nobody can check against what the agency sent.
    """
    import hashlib

    source = tmp_path / "release.pdf"
    source.write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(3), info={"Author": "A. Person"}))
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    plain = pdf_mod.publish_pdf(source, tmp_path / "a.pdf", strip=False)
    assert plain.sha256 == source_digest
    assert not plain.stripped

    stripped = pdf_mod.publish_pdf(source, tmp_path / "b.pdf", strip=True)
    assert stripped.sha256 != source_digest
    assert stripped.sha256 == hashlib.sha256((tmp_path / "b.pdf").read_bytes()).hexdigest()


def test_strip_metadata_removes_author_and_producer(tmp_path):
    """End to end: ``safety.strip_metadata = true`` reaches ``files/``."""
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text(
        'title = "M"\n[safety]\nstrip_metadata = true\n', encoding="utf-8"
    )
    (folder / "release.pdf").write_bytes(
        rawpdf.page_pdf(
            rawpdf.prose_lines(3),
            info={"Author": "Case Officer M. Petrova", "Producer": "Agency Redaction Suite"},
        )
    )
    cfg = fast_config()
    cfg.safety.strip_metadata = True
    out = tmp_path / "out"
    build_folder(folder, out, cfg)

    published = (out / "files" / "release.pdf").read_bytes()
    assert b"Petrova" not in published
    assert b"Agency Redaction Suite" not in published


def test_strip_metadata_removes_earlier_revisions(tmp_path):
    """The viewer shows "[REDACTED]"; the bytes still say the name.

    ``pdftotext`` on the published file reads the current revision and shows
    nothing wrong. Anyone who truncates the file at the first ``%%EOF``, or opens
    it in a repair tool, reads revision one.
    """
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text(
        'title = "M"\n[safety]\nstrip_metadata = true\n', encoding="utf-8"
    )
    first = rawpdf.page_pdf(
        b"BT /F1 14 Tf 72 700 Td (WITHHELD NAME: Jonathan Smith) Tj ET\n"
        + rawpdf.prose_lines()
    )
    corrected = rawpdf.incremental_update(
        first,
        obj_number=5,
        body=rawpdf.stream_obj(
            b"BT /F1 14 Tf 72 700 Td (WITHHELD NAME: [REDACTED]) Tj ET\n"
            + rawpdf.prose_lines()
        ),
    )
    (folder / "release.pdf").write_bytes(corrected)
    assert b"Jonathan Smith" in corrected, "the fixture is not a real incremental save"

    cfg = fast_config()
    cfg.safety.strip_metadata = True
    out = tmp_path / "out"
    build_folder(folder, out, cfg)
    assert b"Jonathan Smith" not in (out / "files" / "release.pdf").read_bytes()


def test_document_metadata_is_recorded_but_never_treated_as_a_fact():
    """``/Title`` is a claim by whoever produced the file, and is filtered."""
    from stackroom.pipeline import _title_for

    class Source:
        path = Path("/tmp/quarterly-report.pdf")
        slug = "quarterly-report"

    source = Source()
    assert _title_for(source, {"title": "Untitled"}) == "quarterly report"
    assert _title_for(source, {"title": "Microsoft Word - final(2).doc"}) == "quarterly report"
    assert _title_for(source, {"title": "a-b-c-d-e-f"}) == "quarterly report"
    assert _title_for(source, {"title": "Contracting authority correspondence"}) == (
        "Contracting authority correspondence"
    )


# --------------------------------------------------------------------------
# 7. Robustness of the whole build
# --------------------------------------------------------------------------


def test_a_filename_that_is_not_valid_utf8_does_not_crash_the_build(tmp_path):
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "B"\n', encoding="utf-8")
    name = os.fsdecode(b"report-\xff\xfe-final.pdf")
    try:
        (folder / name).write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(3)))
    except OSError:  # pragma: no cover - depends on the host
        # APFS refuses to create a name that is not valid UTF-8 (EILSEQ), so
        # on macOS this hazard cannot reach the build in the first place.
        pytest.skip("this filesystem refuses undecodable filenames")

    out = tmp_path / "out"
    build_folder(folder, out)  # must not raise
    assert (out / "index.html").is_file()


def test_the_build_makes_no_network_request(tmp_path, monkeypatch):
    """Guarantee 5, enforced rather than asserted.

    Every socket entry point is replaced with something that raises, and the
    whole ingest-and-build path is run. Pagefind is excluded because it is a
    separate process with its own network posture; it is covered by the
    third-party-origin test below, which looks at what the pages actually fetch.
    """

    def deny(*args, **kwargs):
        raise RuntimeError("the build tried to open a socket")

    for attr in (
        "socket", "create_connection", "socketpair",
        "getaddrinfo", "gethostbyname", "create_server",
    ):
        if hasattr(socket, attr):
            monkeypatch.setattr(socket, attr, deny)

    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "N"\n', encoding="utf-8")
    (folder / "release.pdf").write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(3)))
    (folder / "about.md").write_text("# About\n\nA release.\n", encoding="utf-8")

    out = tmp_path / "out"
    _, _, report = build_folder(folder, out)
    assert report.files_written > 0


def test_the_generated_site_requests_nothing_from_a_third_party(hostile_site):
    """No CDN, no font service, no analytics: not one external subresource.

    A link a reader can *click* is not a request, so ``<a href>`` is excluded and
    ``<link>``, ``<script src>``, ``<img src>``, ``@import`` and ``url()`` are
    not. An archive that fetches from a third party is a log of who read what.
    """
    from html.parser import HTMLParser

    SUBRESOURCE = {"link": "href", "script": "src", "img": "src", "source": "srcset",
                   "iframe": "src", "audio": "src", "video": "src", "embed": "src",
                   "object": "data", "track": "src"}
    external: set[str] = set()

    def note(url: str) -> None:
        match = re.match(r"\s*(?:https?:)?//([^/?#\s]+)", url or "")
        if match:
            external.add(match.group(1))

    class Reader(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attribute = SUBRESOURCE.get(tag)
            if not attribute:
                return
            for name, value in attrs:
                if name == attribute:
                    for candidate in (value or "").split(","):
                        note(candidate.strip().split(" ")[0])

    for path, text in generated_text_files(hostile_site):
        if path.suffix == ".html":
            Reader().feed(text)
        elif path.suffix in (".css", ".js"):
            for match in re.finditer(r"""url\(\s*['"]?([^)'"]+)|@import\s+['"]([^'"]+)""", text):
                note(match.group(1) or match.group(2))

    assert external == set(), f"the archive fetches from {sorted(external)}"


def test_every_page_carries_a_content_security_policy(hostile_site):
    """The CSP is what makes an injected tag inert, so it must be on every page."""
    pages = list(hostile_site.rglob("*.html"))
    assert pages
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert "Content-Security-Policy" in text, page
        assert "default-src 'none'" in text, page
        script_src = text.split("script-src")[1].split(";")[0]
        assert "'unsafe-inline'" not in script_src, page


def test_check_says_where_it_writes_and_can_be_pointed_somewhere_else(tmp_path):
    """``check`` renders every page, so it must say so and take direction.

    It writes every page of every document as an encoded image. Those images do
    not contain the recovered text - it is under an opaque box - but a tool
    people run on documents that must not touch disk cannot claim to write
    nothing, and has to be pointable at a ramdisk.
    """
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "stackroom.toml").write_text('title = "C"\n', encoding="utf-8")
    (folder / "release.pdf").write_bytes(rawpdf.page_pdf(rawpdf.prose_lines(3)))

    scratch = tmp_path / "scratch"
    ingest(folder, scratch)
    assert [p for p in scratch.rglob("*") if p.is_file()], (
        "if this fails, `check` really does write nothing - say so in the README"
    )

    from typer.testing import CliRunner

    elsewhere = tmp_path / "ramdisk"
    result = CliRunner().invoke(
        cli_mod.app, ["check", str(folder), "--scratch", str(elsewhere)]
    )
    assert result.exit_code == 0, result.output
    # Rich soft-wraps long paths across lines, and how long the path is
    # depends on where pytest puts tmp_path, so compare with the whitespace
    # squashed out.
    flat = "".join(result.output.split())
    assert str(elsewhere) in flat, "check did not say where it was writing"
    assert elsewhere.is_dir()


def test_the_documented_page_ceiling_is_enforced():
    """ARCHITECTURE.md and ``search.py`` both promise ``--i-know`` above 50,000.

    It is a behaviour test now: the number was stated in two places and
    implemented in none, and a limit that is documented and unenforced is worse
    than one that is neither, because the person who acted on the documentation
    is the one who gets the unusable archive.
    """
    from stackroom.build.search import DEGRADED_PAGES

    assert DEGRADED_PAGES == 50_000
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "--i-know" in literals, "the documented flag still does not exist"

    with pytest.raises(EXIT):
        cli_mod._page_ceiling(DEGRADED_PAGES + 1, i_know=False)
    cli_mod._page_ceiling(DEGRADED_PAGES + 1, i_know=True)  # must not raise
    cli_mod._page_ceiling(DEGRADED_PAGES, i_know=False)
