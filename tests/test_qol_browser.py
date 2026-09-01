"""The quality-of-life layer, driven in a real browser.

Everything under test here is an enhancement over a site that already works
without it, so these tests are about two things: that the enhancement does what
it says, and that it leaves the archive intact when it is switched off.

They run against a *built* demo site rather than a fixture, because the things
that break in this layer break between files - a relative path that resolves
one way from ``/index.html`` and another from ``/d/x/p/3/index.html``, a
stylesheet part that is concatenated after the base, a script that has to be in
the head rather than at the end of the body. None of that is visible to a unit
test of a template.

Point ``STACKROOM_DEMO_SITE`` at a built site, or build one where
``tests/conftest.py`` looks for it. With no site and no Playwright, every test
in the module skips.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

# Every test in this file drives a real Chromium, so every one of them carries
# the marker the CI job without a browser deselects on. It skipped cleanly
# there before this line existed - `site` and `browser` both skip when there is
# no built demo and no Playwright - but skipping is what happens when a suite
# is *collected* on a machine it cannot run on, and green-because-it-skipped is
# indistinguishable from green-because-it-passed in a summary line. The marker
# is the difference between the job not running these and the job not knowing
# it did not run these.
pytestmark = pytest.mark.browser

# --------------------------------------------------------------------------
# the site, the server and the browser
# --------------------------------------------------------------------------

# All from tests/conftest.py now: `site` finds a built demo, `base` serves it
# over HTTP with a trailing slash, `manifest` reads what was built, and
# `browser` is one Chromium for the whole session. This file used to define its
# own of each, and its `browser` was module-scoped with a comment explaining
# that a second Playwright driver in the same process cannot start - which was
# a description of the problem rather than a fix. The shared fixtures are the
# fix; nothing here shadows them any more.


@pytest.fixture(scope="module", autouse=True)
def _needs_this_layer(site: Path) -> None:
    """Skip rather than fail on a site built before this layer existed."""
    if not (site / "assets" / "js" / "palette.js").is_file():
        pytest.skip(f"{site} was built before this layer; rebuild it")


def context_for(browser, **options):
    """A context that this suite's assumptions hold in.

    `service_workers="block"` is not tidiness. The built site registers one and
    it calls `clients.claim()`, so a second into the first page load it answers
    every same-origin GET from its own cache or its own `fetch()` - and a fetch
    a service worker makes does not pass through Playwright's interception, on
    `page.route` or on `context.route`. Nothing in this file is about the
    offline layer, tests/test_offline.py owns it and drives a real worker
    against a site built for that, and a worker running here would only make
    what reaches the network depend on how many pages a context had already
    visited.

    It also settles the console sweep at the end of this file. That sweep used
    to carry an allowance for one foreign error - the registration failing on a
    build with no sw.js - which stopped matching anything the moment the
    builder started writing one, and would have gone on quietly excusing a
    genuinely broken registration. With no worker to register there is nothing
    foreign left to allow, so the sweep now asserts on every error it sees.
    """
    options.setdefault("service_workers", "block")
    return browser.new_context(**options)


class Session:
    """A page, plus every console error it produced."""

    def __init__(self, context):
        self.context = context
        self.page = context.new_page()
        self.errors: list[str] = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.text} [{(m.location or {}).get('url', '')}]")
            if m.type == "error"
            else None,
        )
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))


@pytest.fixture
def session(browser):
    context = context_for(browser)
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    made = Session(context)
    try:
        yield made
    finally:
        context.close()


@pytest.fixture
def page(session: Session):
    return session.page


def longest(manifest: dict) -> dict:
    """The demo's biggest document - the one worth paging around in."""
    return max(manifest["documents"], key=lambda d: d["pages"])


def built(site: Path, paths: list[str]) -> list[str]:
    """Only the pages this build actually wrote.

    A collection with search switched off has no search page, and asserting
    against one would be asserting against the builder's configuration rather
    than against this layer.
    """
    return [p for p in paths if (site / p).is_file()]


def every_kind_of_page(site: Path, each: int = 2) -> list[str]:
    """One URL of every shape the builder writes, up to `each` of each shape.

    A sweep that names its pages tests the pages somebody thought of. The
    ledger overflowed sideways on a phone for as long as it did because the
    list in the one test that would have caught it stopped one line above
    `withheld/negative/index.html` - the negative is a page type, it was not in
    the list, and nothing else looked at it.

    Every shape rather than every page, because `site` may point at a real
    archive: a 3,000-page collection has four page *kinds* and 3,000 pages, and
    walking all of them would turn one assertion into a ten-minute browser
    session for no more coverage than the first two.
    """
    shapes: dict[str, list[str]] = {}
    for path in sorted(site.rglob("index.html")):
        if "_pagefind" in path.parts:
            continue
        url = path.relative_to(site).as_posix()
        shapes.setdefault(_shape(url), []).append(url)
    return [url for group in shapes.values() for url in group[:each]]


def _shape(url: str) -> str:
    """A URL with its identifiers starred, so two of a kind group together.

    `d/01-award-memorandum/p/4/` and `d/02-correspondence/p/11/` are one shape;
    `withheld/` and `withheld/negative/` are two, which is the distinction that
    matters here.
    """
    parts: list[str] = []
    identifier = False
    for part in url.split("/"):
        parts.append("*" if identifier else part)
        identifier = part in ("d", "p", "compare")
    return "/".join(parts)


# --------------------------------------------------------------------------
# the palette
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chord", ["Control+k", "Meta+k"])
def test_the_palette_opens_on_either_shortcut(page, base, chord):
    page.goto(base + "index.html")
    page.wait_for_selector(".mh-btn")
    page.keyboard.press(chord)
    page.wait_for_selector("#palette[open]", timeout=2000)
    assert page.eval_on_selector("#pal-q", "i => document.activeElement === i")


def test_the_palette_filters_and_shows_why(page, base, manifest):
    page.goto(base + "index.html")
    page.wait_for_selector(".mh-btn")
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")

    # Every row with no query at all: the standing pages, never an empty box.
    assert page.eval_on_selector_all(".pal__row", "rs => rs.length") >= 4

    doc = longest(manifest)
    page.keyboard.type(doc["title"][:4].lower())
    page.wait_for_timeout(150)
    labels = page.eval_on_selector_all(".pal__row", "rs => rs.map(r => r.textContent)")
    assert any(doc["title"] in label for label in labels)
    # The matched characters are marked, which is the whole claim about the
    # matching being explainable.
    assert page.eval_on_selector_all(".pal__row mark", "ms => ms.length") > 0
    # One row is active, and it is named on the input rather than announced.
    active = page.get_attribute("#pal-q", "aria-activedescendant")
    assert active and page.get_attribute(f"#{active}", "aria-selected") == "true"

    page.keyboard.type("zzzqq")
    page.wait_for_timeout(150)
    assert page.eval_on_selector_all(".pal__row", "rs => rs.length") == 0
    assert "Nothing" in page.text_content(".pal__note")


def test_the_palette_navigates_and_remembers_where_you_went(page, base, manifest):
    doc = longest(manifest)
    page.goto(base + "index.html")
    page.wait_for_selector(".mh-btn")
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    page.keyboard.type(doc["title"][:5].lower())
    page.wait_for_timeout(200)
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert f"/d/{doc['id']}/" in page.url

    recent = json.loads(page.evaluate("localStorage.getItem('stackroom.recent')"))
    assert recent[0]["t"] == doc["title"]

    # And the next time it opens with an empty query, that is the first offer.
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    assert page.eval_on_selector(".pal__row", "r => r.textContent").startswith(doc["title"])
    page.keyboard.press("Escape")

    # Offered again from four directories down, it still points at the same
    # place: what is stored is the path from the root, not from the page that
    # happened to be open when the reader went there.
    page.goto(base + f"d/{doc['id']}/p/1/index.html")
    page.wait_for_selector(".mh-btn")
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert page.url == base + f"d/{doc['id']}/index.html"


def test_the_palette_knows_pages_codes_and_control_numbers(page, base, manifest):
    doc = longest(manifest)
    page.goto(base + f"d/{doc['id']}/index.html")
    page.wait_for_selector(".mh-btn")

    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    page.keyboard.type("p 3")
    page.wait_for_timeout(200)
    first = page.eval_on_selector(".pal__row", "r => [r.textContent, r.getAttribute('href')]")
    assert first[0].startswith("Page 3")
    assert first[1].endswith("/p/3/index.html")
    page.keyboard.press("Escape")

    codes = manifest["stats"].get("exemption_counts") or {}
    if codes:
        code = next(iter(codes))
        page.keyboard.press("Control+k")
        page.wait_for_selector("#palette[open]")
        page.keyboard.type(code)
        page.wait_for_timeout(200)
        rows = page.eval_on_selector_all(
            ".pal__row", "rs => rs.map(r => [r.textContent, r.getAttribute('href')])"
        )
        assert any(r[0].startswith(code) and "withheld" in r[1] for r in rows)
        page.keyboard.press("Escape")

    # A control number is not a page number: OCA000004 must not be read as
    # "page 4 of everything whose title contains those letters".
    stamp = page.eval_on_selector_all(
        ".thumbs a .mono", "ms => ms.length ? ms[ms.length - 1].textContent.trim() : ''"
    )
    if stamp:
        page.keyboard.press("Control+k")
        page.wait_for_selector("#palette[open]")
        page.keyboard.type(stamp)
        page.wait_for_timeout(200)
        rows = page.eval_on_selector_all(".pal__row", "rs => rs.map(r => r.textContent)")
        assert rows and rows[0].startswith(stamp)


def test_the_palette_closes_restores_focus_and_holds_the_page_still(page, base):
    page.goto(base + "index.html")
    trigger = page.wait_for_selector(".masthead__nav .mh-btn")
    trigger.click()
    page.wait_for_selector("#palette[open]")

    assert page.evaluate("document.documentElement.classList.contains('is-modal')")
    assert page.evaluate("getComputedStyle(document.documentElement).overflow") == "hidden"
    # Locking the page must not shift it sideways by the width of a scrollbar.
    width = page.evaluate("document.documentElement.clientWidth")

    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert page.eval_on_selector("#palette", "d => !d.open")
    assert not page.evaluate("document.documentElement.classList.contains('is-modal')")
    assert page.evaluate("document.documentElement.clientWidth") == width
    assert page.evaluate("document.activeElement.classList.contains('mh-btn')")


def test_slash_belongs_to_whoever_claimed_it_first(page, base, manifest):
    """The viewer owns "/" on page views; the palette takes it only elsewhere."""
    doc = longest(manifest)
    page.goto(base + f"d/{doc['id']}/index.html")
    page.wait_for_selector(".mh-btn")
    page.keyboard.press("/")
    page.wait_for_selector("#palette[open]", timeout=2000)
    page.keyboard.press("Escape")

    page.goto(base + f"d/{doc['id']}/p/1/index.html")
    page.wait_for_selector(".mh-btn")
    with page.expect_navigation():
        page.keyboard.press("/")
    assert "search" in page.url
    assert page.evaluate("!document.querySelector('#palette')")


# --------------------------------------------------------------------------
# preferences
# --------------------------------------------------------------------------


def test_a_chosen_theme_survives_a_reload_with_no_flash(page, base):
    page.goto(base + "index.html")
    page.wait_for_selector(".pref__open")
    page.click(".pref__open")
    page.check("input[name='sr-theme'][value='dark']")
    page.wait_for_timeout(150)
    assert page.get_attribute("html", "data-theme") == "dark"
    dark = page.evaluate("getComputedStyle(document.body).backgroundColor")

    # The attribute has to be on <html> before anything is painted. The first
    # animation frame runs before the first paint, so what it can see is what
    # the reader would have seen.
    page.add_init_script(
        "window.__atFirstFrame = null;"
        "requestAnimationFrame(function () {"
        "  window.__atFirstFrame = document.documentElement.getAttribute('data-theme');"
        "});"
    )
    page.reload()
    page.wait_for_function("window.__atFirstFrame !== null")
    assert page.evaluate("window.__atFirstFrame") == "dark"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == dark

    # And it is one file, fetched once, that is not deferred.
    assert page.eval_on_selector(
        "head script[src$='prefs.js']", "s => !s.defer && !s.async"
    )
    assert page.eval_on_selector_all("script[src$='prefs.js']", "s => s.length") == 1


OVERFLOW = """() => {
  const doc = document.documentElement;
  const out = { wide: doc.scrollWidth, seen: doc.clientWidth, blame: [] };
  if (out.wide <= out.seen) return out;
  /* Anything inside a scroller of its own - a wide table in `.table-scroll`,
     the negative's field - is doing what it was built to do and is not what
     pushed the page sideways. */
  const scrolls = (el) => {
    for (let p = el.parentElement; p && p !== doc; p = p.parentElement) {
      const x = getComputedStyle(p).overflowX;
      if (x === 'auto' || x === 'scroll' || x === 'hidden') return true;
    }
    return false;
  };
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    if (r.right <= out.seen + 0.5) continue;
    if (scrolls(el)) continue;
    out.blame.push(
      (el.className && typeof el.className === 'string'
        ? el.tagName.toLowerCase() + '.' + el.className.trim().split(/\\s+/).join('.')
        : el.tagName.toLowerCase()) +
      ' to ' + Math.round(r.right) + 'px: ' + (el.textContent || '').trim().slice(0, 48)
    );
  }
  out.blame = out.blame.slice(0, 6);
  return out;
}"""


def _sweep_for_sideways_scroll(page, base: str, paths: list[str]) -> None:
    for path in paths:
        page.goto(base + path)
        page.wait_for_timeout(250)
        got = page.evaluate(OVERFLOW)
        assert got["wide"] <= got["seen"], (
            f"{path} scrolls sideways: {got['wide']}px of content in a {got['seen']}px "
            "viewport\n  " + "\n  ".join(got["blame"] or ["(nothing named it)"])
        )


def test_text_size_changes_and_nothing_overflows_at_the_largest_step(browser, base, site):
    """A phone at the largest text step, over every kind of page in the site.

    The narrowest thing a reader will hold, with the text as large as the
    archive will set it: the state most likely to overflow, and the one nobody
    is testing by hand. A horizontal scrollbar here is not cosmetic - it is a
    column of a document row sliding under the edge of the screen on the page
    that says what was withheld.

    Every *kind* of page, not a list of seven. The list this used to carry
    stopped one line short of `withheld/negative/index.html`, which overflowed
    for the same reason `withheld/index.html` did and went unmeasured because
    nobody had added it. `every_kind_of_page` finds the page types instead of
    naming them, so the next page type this project grows arrives already
    covered.
    """
    context = context_for(browser, viewport={"width": 380, "height": 700})
    page = context.new_page()
    try:
        page.goto(base + "index.html")
        page.wait_for_selector(".pref__open")
        before = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
        page.click(".pref__open")
        page.check("input[name='sr-size'][value='largest']")
        page.wait_for_timeout(150)
        after = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
        assert after > before

        _sweep_for_sideways_scroll(page, base, every_kind_of_page(site))
    finally:
        context.close()


def test_nothing_overflows_on_a_phone_at_the_default_text_size(browser, base, site):
    """The same sweep with nothing switched on, which is what most people see.

    The largest step is the hard case and this is the common one; a fix that
    only holds at one of the two sizes has not been made.
    """
    context = context_for(browser, viewport={"width": 380, "height": 700})
    page = context.new_page()
    try:
        _sweep_for_sideways_scroll(page, base, every_kind_of_page(site))
    finally:
        context.close()


def test_a_page_citing_several_exemptions_wraps_between_the_codes(browser, base, site):
    """One span per code, so the list breaks between codes and never inside one.

    Both of the ledger's pages print a page's whole exemption list, and both
    used to print it as one span: `b(4), b(5), b(6), b(7)(C), b(7)(E), k(2)` is
    a 40-character run, `white-space: nowrap` is on it because `b(7)(C)` must
    not break in half, and the two together took `withheld/index.html` to 500px
    of content in a 380px viewport. The codes are separate spans now. This
    checks the arrangement rather than only the symptom, because the symptom is
    a measurement on one collection's worth of codes and the arrangement is the
    thing that has to survive a collection with more of them.
    """
    context = context_for(browser, viewport={"width": 380, "height": 700})
    page = context.new_page()
    try:
        most = 0
        for path in built(site, ["withheld/index.html", "withheld/negative/index.html"]):
            page.goto(base + path)
            page.wait_for_timeout(200)
            rows = page.evaluate(
                """() => [].map.call(document.querySelectorAll('.doc__codes'), function (g) {
                     var codes = g.querySelectorAll('.mono');
                     var meta = g.closest('.doc__meta');
                     return {
                       codes: [].map.call(codes, function (c) { return c.textContent.trim(); }),
                       nowrap: [].every.call(codes, function (c) {
                         return getComputedStyle(c).whiteSpace === 'nowrap';
                       }),
                       over: Math.round(
                         g.getBoundingClientRect().right - meta.getBoundingClientRect().right
                       )
                     };
                   })"""
            )
            assert rows, f"{path} lists no exemption codes at all"
            for row in rows:
                assert all(row["codes"]), f"{path} has an empty code span"
                assert row["nowrap"], (
                    f"{path} lets a single code break in half - b(7)(C) would read as two"
                )
                assert row["over"] <= 1, (
                    f"{path}: {', '.join(row['codes'])} sticks {row['over']}px out of the "
                    "row it belongs to"
                )
                most = max(most, len(row["codes"]))
        if most < 2:
            pytest.skip("no page in this collection cites more than one code")
    finally:
        context.close()


def test_forgetting_clears_what_was_stored(page, base):
    page.goto(base + "index.html")
    page.wait_for_selector(".pref__open")
    page.click(".pref__open")
    page.check("input[name='sr-theme'][value='dark']")
    page.wait_for_timeout(100)
    page.click(".pref__forget")
    page.wait_for_timeout(150)
    assert page.evaluate("localStorage.getItem('stackroom.theme')") is None
    assert page.get_attribute("html", "data-theme") is None


# --------------------------------------------------------------------------
# citation
# --------------------------------------------------------------------------


def test_a_citation_carries_everything_needed_to_check_it(page, base, manifest):
    doc = longest(manifest)
    page.goto(base + f"d/{doc['id']}/p/2/index.html")
    page.wait_for_selector(".cite__open")
    page.click(".cite__open")
    page.wait_for_timeout(400)
    text = page.input_value(".cite__text")

    assert manifest["title"] in text
    assert doc["title"] in text
    assert "page 2" in text
    assert doc["sha256"][:16] in text
    assert date.today().isoformat() in text
    assert page.url in text
    stamp = page.eval_on_selector_all(
        "#main > .wrap > .doc__meta > .mono", "ms => ms.length ? ms[0].textContent.trim() : ''"
    )
    if stamp:
        assert stamp in text

    page.click(".cite__copy")
    page.wait_for_timeout(300)
    assert page.evaluate("navigator.clipboard.readText()") == text
    assert "Copied" in page.text_content(".cite__said")

    # The format is remembered, and it is a different string.
    page.check("input[name='sr-cite'][value='markdown']")
    page.wait_for_timeout(200)
    markdown = page.input_value(".cite__text")
    assert markdown.startswith("[") and markdown != text
    page.reload()
    page.wait_for_selector(".cite__open")
    page.click(".cite__open")
    page.wait_for_timeout(400)
    assert page.input_value(".cite__text") == markdown


def test_copy_link_says_which_link_it_is_copying(page, base, manifest):
    doc = longest(manifest)
    plain = base + f"d/{doc['id']}/p/2/index.html"
    page.goto(plain)
    page.wait_for_selector(".cite__link")
    assert "keeping the highlight" not in page.text_content(".cite__link")
    page.click(".cite__link")
    page.wait_for_timeout(300)
    assert page.evaluate("navigator.clipboard.readText()") == plain

    page.goto(plain + "#w=0,1")
    page.wait_for_selector(".cite__link")
    page.wait_for_timeout(200)
    assert "keeping the highlight" in page.text_content(".cite__link")
    page.click(".cite__link")
    page.wait_for_timeout(300)
    assert page.evaluate("navigator.clipboard.readText()") == plain + "#w=0,1"

    # The citation names the page and never the passage, whatever the link does.
    page.click(".cite__open")
    page.wait_for_timeout(400)
    assert "#w=" not in page.input_value(".cite__text")


# --------------------------------------------------------------------------
# reading position, and the rest of the room
# --------------------------------------------------------------------------


def test_where_you_were_is_remembered_offered_and_forgettable(page, base, manifest):
    doc = longest(manifest)
    assert doc["pages"] >= 3, "this test needs a document with pages to be lost in"
    page.goto(base + f"d/{doc['id']}/p/3/index.html")
    page.wait_for_timeout(300)
    assert page.evaluate(f"localStorage.getItem('stackroom.read.{doc['id']}')").startswith("3:")

    page.goto(base + f"d/{doc['id']}/index.html")
    note = page.wait_for_selector(".resume", timeout=2000)
    assert "page 3" in note.text_content()
    assert page.get_attribute(".resume a", "href") == "p/3/index.html"
    # In the flow of the page, not floating over it and not a dialog.
    assert page.evaluate("getComputedStyle(document.querySelector('.resume')).position") == "static"
    assert page.evaluate("!document.querySelector('.resume').closest('dialog')")

    page.click(".resume__forget")
    page.wait_for_timeout(150)
    assert page.evaluate("!document.querySelector('.resume')")
    assert page.evaluate(f"localStorage.getItem('stackroom.read.{doc['id']}')") is None


def test_back_to_top_arrives_only_after_real_scrolling(browser, base, manifest):
    doc = longest(manifest)
    context = context_for(browser, viewport={"width": 900, "height": 500})
    page = context.new_page()
    try:
        page.goto(base + f"d/{doc['id']}/p/1/index.html")
        page.wait_for_selector(".to-top", state="attached")
        if not page.evaluate("CSS.supports('animation-timeline: scroll()')"):
            pytest.skip("this browser has no scroll-driven animations")
        assert page.evaluate("getComputedStyle(document.querySelector('.to-top')).visibility") == "hidden"
        page.evaluate("window.scrollTo({top: 99999, behavior: 'instant'})")
        page.wait_for_timeout(400)
        assert page.evaluate("window.scrollY") > 350, "the page is too short to test this on"
        assert page.evaluate("getComputedStyle(document.querySelector('.to-top')).visibility") == "visible"
    finally:
        context.close()


def test_a_digest_is_selectable_as_one_thing(page, base):
    page.goto(base + "index.html")
    page.wait_for_selector(".colophon .mono")
    assert page.eval_on_selector(".colophon .mono", "e => getComputedStyle(e).userSelect") == "all"


# --------------------------------------------------------------------------
# the two things that must always be true
# --------------------------------------------------------------------------


def test_nothing_here_exists_without_javascript(browser, base, manifest):
    doc = longest(manifest)
    context = context_for(browser, java_script_enabled=False)
    page = context.new_page()
    try:
        for path in ("index.html", f"d/{doc['id']}/index.html", f"d/{doc['id']}/p/1/index.html"):
            page.goto(base + path)
            for absent in (".mh-btn", ".pref", ".cite__open", ".cite", ".resume", "#palette"):
                assert page.query_selector(absent) is None, f"{absent} appears with no script"
            # What is left is the archive, and the one control that is a link.
            assert page.query_selector("#main")
            assert page.eval_on_selector(".to-top", "e => getComputedStyle(e).visibility") == "visible"
    finally:
        context.close()


def test_the_indexed_body_is_never_touched(page, base, manifest):
    """Guarantee 3, from the side this layer could break it.

    The search index reports matches as positions in the whitespace-separated
    tokens of the element carrying ``data-pagefind-body``. One node inserted
    into it moves every highlight in the archive. This layer puts controls on
    page views, so it is worth proving that none of them land in there.
    """
    doc = longest(manifest)
    page.goto(base + f"d/{doc['id']}/p/1/index.html")
    page.wait_for_selector(".cite__open")
    before = page.evaluate(
        "() => { const b = document.querySelector('[data-pagefind-body]');"
        " return b ? [b.childElementCount, b.textContent] : null; }"
    )
    assert before, "this page has no indexed body to protect"

    page.click(".cite__open")
    page.wait_for_timeout(400)
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    page.keyboard.type("page 1")
    page.wait_for_timeout(200)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    after = page.evaluate(
        "() => { const b = document.querySelector('[data-pagefind-body]');"
        " return [b.childElementCount, b.textContent]; }"
    )
    assert after == before


def test_the_room_holds_up_in_forced_colours_and_high_contrast(browser, base):
    """Neither mode may lose a control or push the page sideways."""
    for options in ({"forced_colors": "active"}, {"contrast": "more"}):
        context = context_for(browser, viewport={"width": 900, "height": 700}, **options)
        page = context.new_page()
        try:
            page.goto(base + "index.html")
            page.wait_for_selector(".mh-btn")
            page.keyboard.press("Control+k")
            page.wait_for_selector("#palette[open]")
            page.keyboard.type("doc")
            page.wait_for_timeout(200)
            assert page.eval_on_selector_all(".pal__row", "rs => rs.length") > 0
            assert page.eval_on_selector(
                ".pal__row[aria-selected='true']",
                "r => r.getBoundingClientRect().width > 0",
            )
            page.keyboard.press("Escape")
            wide, seen = page.evaluate(
                "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
            )
            assert wide <= seen
        finally:
            context.close()


def test_no_console_errors_anywhere(session, base, site, manifest):
    page = session.page
    paths = ["index.html", "browse/index.html", "about/index.html", "withheld/index.html",
             "search/index.html"]
    for doc in manifest["documents"]:
        paths.append(f"d/{doc['id']}/index.html")
        paths.append(f"d/{doc['id']}/p/1/index.html")
        paths.append(f"d/{doc['id']}/p/{doc['pages']}/index.html")

    for path in built(site, paths):
        page.goto(base + path)
        page.wait_for_timeout(350)
        page.keyboard.press("Control+k")
        page.wait_for_timeout(200)
        page.keyboard.type("o")
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
        if page.query_selector(".pref__open"):
            page.click(".pref__open")
            page.wait_for_timeout(100)
            page.keyboard.press("Escape")
        if page.query_selector(".cite__open"):
            page.click(".cite__open")
            page.wait_for_timeout(300)
    page.wait_for_timeout(300)
    assert session.errors == []
