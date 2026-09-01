"""The page viewer, driven in a real browser.

Everything the viewer does is an enhancement, so none of it can be tested by
looking at the built HTML: the scan is an ``<img>`` until a click turns it into
a pannable full-size view, the boxes on it are drawn from a JSON blob at
runtime, and the affordance that turns a text selection into a permalink does
not exist until there is a selection. This file exercises the parts that only
exist once a browser has run them.

It needs two things that are not part of the package - a built site and
Playwright with Chromium - and skips cleanly without either, because a
contributor working on the PDF reader should not be made to install a browser.

To run it::

    stackroom build ./demo/release -o ./demo/site
    STACKROOM_TEST_SITE=./demo/site pytest tests/test_viewer_browser.py

Without those two things every test in the module skips, so ``pytest`` on a
checkout with neither is quiet and green.

The site is served over HTTP rather than opened from ``file://``. Three things
here need a real origin: the content policy the pages carry, ``navigator
.clipboard`` (which does not exist outside a secure context, and 127.0.0.1
counts as one), and cross-document view transitions.

What is asserted, and what is deliberately not: these tests check behaviour a
reader would notice - a dialog that opens, focus that comes back, the right
words highlighted, boxes in the right place on the scan, a preview naming the
right page. They avoid asserting on exact colours, durations or wording, which
belong to the design and are expected to change. The one piece of structure
they do pin down is the search contract, because breaking it is silent and
every highlight in a published archive depends on it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# The same marker tests/test_offline.py and tests/test_qol_browser.py use, so
# one CI job can deselect everything that needs a browser. Registered in
# pyproject.toml, under [tool.pytest.ini_options] markers.
pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------
# the site, the server and the browser
# --------------------------------------------------------------------------

# All three come from tests/conftest.py: `site` finds a built demo, `base_url`
# serves it on a port the OS picks, and `browser` is one Chromium for the whole
# session. This file used to define its own of each, which is how three test
# modules ended up each starting a Playwright driver and only one of them ever
# running - a synchronous driver owns an event loop and a process gets one.


@pytest.fixture(scope="module", autouse=True)
def _needs_the_viewer(site: Path) -> None:
    """Skip rather than fail on a site built before this layer existed.

    conftest's `site` only asks for an index and an assets directory, which is
    all the other suites need. Everything here is about viewer.js, so a site
    without one is not a failure of the viewer - it is the wrong site.
    """
    if not (site / "assets" / "viewer.js").is_file():
        pytest.skip(f"{site} was built without viewer.js; rebuild it")


class Watched:
    """A page plus everything the console complained about while it was open.

    Two lists, because two different things go wrong. `problems` is script
    misbehaving - an uncaught exception, a policy violation, anything the
    viewer itself logged - and no test tolerates any of it. `missing` is a
    request that came back with nothing, which is a fact about what was built
    rather than about what the scripts did, and which one test here causes on
    purpose to check that a missing thumbnail degrades rather than throws.
    """

    A_FAILED_FETCH = re.compile(r"Failed to load resource")

    def __init__(self, page):
        self.page = page
        self.problems: list[str] = []
        self.missing: list[str] = []
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.problems.append(f"uncaught: {e}"))
        page.on("requestfailed", lambda r: self.missing.append(r.url))
        page.on(
            "response",
            lambda r: self.missing.append(r.url) if r.status >= 400 else None,
        )

    def _console(self, message) -> None:
        if message.type != "error":
            return
        if self.A_FAILED_FETCH.search(message.text):
            return
        self.problems.append(f"console error: {message.text}")

    def clear(self) -> None:
        self.problems.clear()
        self.missing.clear()


@pytest.fixture
def watched(browser):
    """A fresh context per test, with the service worker kept out of it.

    `service_workers="block"` is the load-bearing argument. The built site
    registers one and it calls `clients.claim()`, so from a second or so into
    the first page load every same-origin GET is answered by the worker's own
    `fetch()` - and a fetch made by a service worker does not pass through
    Playwright's interception, on `page.route` or on `context.route`. Measured
    on the demo, with the worker allowed and `**/media/**@thumb.*` routed to a
    404: the four thumbnails on the page itself were served the 404 (they load
    before the worker has claimed the page) and the one the scrubber asked for
    afterwards arrived intact, straight from the server, so the test that
    proves a missing thumbnail degrades was proving nothing.

    Blocking is the right answer rather than working around it, and not only
    for that one test. Nothing in this file is about the offline layer;
    tests/test_offline.py owns it and drives a real worker against a site built
    for the purpose. Leaving one running here would make every assertion about
    what was and was not fetched depend on how much of the archive a context
    had already cached, which is a fact about test order.
    """

    def make(**context_args):
        context_args.setdefault("service_workers", "block")
        context = browser.new_context(
            viewport={"width": 1400, "height": 950}, **context_args
        )
        made.append(context)
        return Watched(context.new_page())

    made: list = []
    try:
        yield make
    finally:
        for context in made:
            context.close()


@pytest.fixture
def viewer(watched):
    return watched()


# --------------------------------------------------------------------------
# working out what is in this particular archive
# --------------------------------------------------------------------------

# Nothing below names a document, a page or a word. The demo collection is
# rebuilt from real PDFs and its contents move; a test that hard-codes
# "page 3 of the contract memo" fails for a reason that is not a bug.


def _pages(site: Path) -> list[Path]:
    return sorted(site.glob("d/*/p/*/index.html"))


@pytest.fixture(scope="session")
def a_page(site: Path) -> str:
    """A page with a scan, some words and a neighbour on each side."""
    for path in _pages(site):
        html = path.read_text(encoding="utf-8")
        if 'class="scan__img"' in html and 'id="next-page"' in html and 'id="prev-page"' in html:
            return _url_of(site, path)
    pytest.skip("no page in this site has a scan and two neighbours")


@pytest.fixture(scope="session")
def a_redacted_page(site: Path) -> str:
    for path in _pages(site):
        html = path.read_text(encoding="utf-8")
        if "hit--void" in html and 'class="scan__img"' in html:
            return _url_of(site, path)
    pytest.skip("no page in this site carries a redaction")


@pytest.fixture(scope="session")
def a_ribbon_page(site: Path) -> str:
    for path in sorted(site.glob("d/*/index.html")):
        if "data-base" in path.read_text(encoding="utf-8"):
            return _url_of(site, path)
    pytest.skip("no document page in this site carries a ribbon")


def _url_of(site: Path, path: Path) -> str:
    return "/" + path.relative_to(site).as_posix()


def _every_html(site: Path) -> list[str]:
    return [_url_of(site, p) for p in sorted(site.rglob("*.html")) if "_pagefind" not in p.parts]


# --------------------------------------------------------------------------
# helpers the tests share
# --------------------------------------------------------------------------

TRANSFORM = re.compile(r"translate\(([-\d.]+)px,\s*([-\d.]+)px\)\s*scale\(([\d.]+)\)")


def canvas_state(page) -> dict:
    """Where the full-size view currently is, in numbers rather than pixels."""
    return page.evaluate(
        """() => {
          var c = document.querySelector('.lens__canvas');
          var s = document.querySelector('.lens__stage');
          if (!c || !s) return null;
          var m = /translate\\(([-\\d.]+)px,\\s*([-\\d.]+)px\\)\\s*scale\\(([\\d.]+)\\)/
            .exec(c.style.transform);
          if (!m) return null;
          return {
            tx: +m[1], ty: +m[2], scale: +m[3],
            natW: parseFloat(c.style.width), natH: parseFloat(c.style.height),
            stageW: s.clientWidth, stageH: s.clientHeight
          };
        }"""
    )


def framed_centre(state: dict, box: dict) -> tuple[float, float]:
    """Where the centre of a page-fraction box currently sits on the stage."""
    return (
        (box["x"] + box["w"] / 2) * state["natW"] * state["scale"] + state["tx"],
        (box["y"] + box["h"] / 2) * state["natH"] * state["scale"] + state["ty"],
    )


CENTRED = 12
"""How far off centre a framed box may sit, in stage pixels, when it can be
centred at all."""

CONTEXT_W, CONTEXT_H = 0.4, 0.15
"""The region `frame()` in scan.js puts around a box, as a share of the page.

Mirrored here from a documented contract rather than read out of the code: the
lens deliberately frames a *region* around a box rather than the box, and how
big that region is is the difference between a lens that went and found the box
and one that opened where it always opens.
"""

CLOSER = 2.0
"""How much nearer than Fit a framed box has to be.

That region is at most 40% of the page wide with 8% of the stage to spare, so
framing anything smaller than the region lands at 2.3x Fit or nearer, whichever
axis binds. The lens's *opening* view is the window's own width capped at the
scan's resolution, which on the demo is 1.95x Fit - so this is the number that
separates "it went there" from "it opened and stayed".
"""


def _pan_is_spent(offset: float, natural: float, stage: float, scale: float) -> bool:
    """Has the lens run out of page to pan on this axis?

    Three ways for that to be true, all of them `clampPan` in scan.js refusing
    to show blank space beside the sheet: the near edge of the page is against
    the near edge of the stage, the far edge is against the far edge, or the
    page is smaller than the stage and sits centred in it.
    """
    spread = natural * scale
    if spread <= stage:
        return abs(offset - (stage - spread) / 2) < 1.5
    return abs(offset) < 1.5 or abs(offset - (stage - spread)) < 1.5


def assert_framed(state: dict, box: dict, what: str = "the box") -> None:
    """The thing the reader asked for is under their eye - as far as the page allows.

    *Framed* is not *centred*, and near a margin it cannot be. The lens will
    not show blank space beside a page that is big enough to fill the window:
    `clampPan` stops the pan at the sheet's own edge, so a box in the outer
    margin is carried as close to the middle as there is page to carry it and
    no further. Measured on the demo's first redacted page, whose first
    redaction sits at 77-83% of the width: the lens frames it at 2.5x with the
    page's right edge flush against the right of a 1400px stage, which leaves
    the box spanning x 663-858 - wholly on screen, 61px right of centre.

    That 61px is the whole of the disagreement, and this suite used to call it
    a failure. It is not one. Buying those 61px means showing 61px of nothing
    to the right of the page, and the margin that would be scrolled away is
    itself the answer to "where on this page is the hole?" - a reader can see
    the redaction is in the right margin only while the right margin is in
    shot. Every document viewer that shows pages rather than an infinite canvas
    stops the same way at the same place.

    So what is asserted here is what a reader would notice, and not the
    arithmetic that produces it:

    * the lens went closer - the box is nearer than the whole sheet at Fit;
    * the box is wholly on the stage, so there is nothing to hunt for; and
    * it is centred, unless the pan is spent on that axis, in which case the
      lens went as far as there was page to go.

    All three, because no two of them are enough. Panning alone cannot be
    asserted: a lens that never zoomed shows the whole sheet, and on the whole
    sheet a box in the margin is as central as panning can make it - which is
    why the pan test excuses it and the closeness test does not. Position alone
    is not enough either; and closeness alone would pass a lens that magnified
    the middle of a page the reader never asked about.
    """
    sw = state["natW"] * state["scale"]
    sh = state["natH"] * state["scale"]
    fit = min(state["stageW"] / state["natW"], state["stageH"] / state["natH"])
    if box["w"] < CONTEXT_W and box["h"] < CONTEXT_H:
        assert state["scale"] > fit * CLOSER, (
            f"{what} was not closed in on: {state['scale'] / fit:.1f}x Fit, which is about "
            "what the lens opens at before anybody asks it for anything"
        )

    left, right = box["x"] * sw + state["tx"], (box["x"] + box["w"]) * sw + state["tx"]
    top, bottom = box["y"] * sh + state["ty"], (box["y"] + box["h"]) * sh + state["ty"]
    assert left >= -0.5 and right <= state["stageW"] + 0.5, (
        f"{what} is off the side of the view: x {left:.0f}..{right:.0f} of {state['stageW']}"
    )
    assert top >= -0.5 and bottom <= state["stageH"] + 0.5, (
        f"{what} is off the top or bottom: y {top:.0f}..{bottom:.0f} of {state['stageH']}"
    )

    cx, cy = framed_centre(state, box)
    assert abs(cx - state["stageW"] / 2) < CENTRED or _pan_is_spent(
        state["tx"], state["natW"], state["stageW"], state["scale"]
    ), (
        f"{what} is {abs(cx - state['stageW'] / 2):.0f}px off centre horizontally "
        "and the page had more room to give"
    )
    assert abs(cy - state["stageH"] / 2) < CENTRED or _pan_is_spent(
        state["ty"], state["natH"], state["stageH"], state["scale"]
    ), (
        f"{what} is {abs(cy - state['stageH'] / 2):.0f}px off centre vertically "
        "and the page had more room to give"
    )


def select_words(page, first: int, last: int) -> None:
    """Select tokens `first`..`last` inclusive, the way a reader would drag."""
    page.evaluate(
        """([a, b]) => {
          var layer = document.querySelector('.text-layer');
          var from = layer.querySelector('.w[data-i="' + a + '"]');
          var to = layer.querySelector('.w[data-i="' + b + '"]');
          var r = document.createRange();
          r.setStart(from.firstChild, 0);
          r.setEnd(to.firstChild, to.textContent.length);
          var s = window.getSelection();
          s.removeAllRanges();
          s.addRange(r);
        }""",
        [first, last],
    )


# --------------------------------------------------------------------------
# the full-size view
# --------------------------------------------------------------------------


def test_the_lens_opens_closes_and_gives_focus_back(viewer, base_url, a_page):
    """The dialog is the platform's, and so is the focus it returns."""
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")

    opener = page.locator(".scan__open")
    assert opener.count() == 1, "the page offers no way in that a keyboard can reach"
    assert page.evaluate("!!document.getElementById('lens')") is False, (
        "the dialog exists before anyone has asked for it"
    )

    was = page.evaluate("window.scrollY")
    opener.click()
    page.wait_for_timeout(700)

    assert page.evaluate("document.getElementById('lens').open") is True
    assert page.evaluate("document.activeElement.className") == "lens__stage", (
        "focus should land on the thing the arrow keys operate"
    )

    page.keyboard.press("Escape")
    page.wait_for_timeout(600)

    assert page.evaluate("document.getElementById('lens').open") is False
    assert page.evaluate("document.activeElement.className").startswith("scan__open"), (
        "focus did not come back to what opened it"
    )
    assert page.evaluate("window.scrollY") == was, "the page moved while the lens was over it"
    assert viewer.problems == []


def test_the_lens_asks_for_the_wide_rendering_only_when_it_opens(viewer, base_url, a_page):
    """The page view is a thumbnail. It must not pay for the full-size one."""
    page = viewer.page
    wanted: list[str] = []
    page.on("request", lambda r: wanted.append(r.url) if "/media/" in r.url else None)

    page.goto(base_url + a_page, wait_until="networkidle")
    page.wait_for_timeout(300)
    widest = page.evaluate(
        """() => {
          var out = [];
          document.querySelectorAll('#scan source[srcset]').forEach(function (s) {
            s.getAttribute('srcset').split(',').forEach(function (c) {
              var bits = c.trim().split(/\\s+/);
              out.push({ url: new URL(bits[0], document.baseURI).href,
                         w: parseInt(bits[1], 10) || 0 });
            });
          });
          out.sort(function (a, b) { return b.w - a.w; });
          return out.length ? out[0].url : null;
        }"""
    )
    if not widest:
        pytest.skip("this page offers only one rendering")

    before = [u for u in wanted if u == widest]
    page.click(".scan__open")
    page.wait_for_timeout(1200)
    after = [u for u in wanted if u == widest]

    assert before == [], "the widest rendering was fetched before anybody opened it"
    assert after, "opening the lens did not fetch the widest rendering"
    assert viewer.problems == []


def test_the_lens_frames_the_words_the_reader_arrived_for(viewer, base_url, a_page):
    """`#w=` means "these words". Opening the scan should go and find them.

    What "framed" is allowed to mean is in `assert_framed`.
    """
    page = viewer.page
    page.goto(base_url + a_page + "#w=10,11,12", wait_until="networkidle")

    page.click(".scan__open")
    page.wait_for_timeout(1500)

    state = canvas_state(page)
    box = page.evaluate(
        "window.stackroomViewer.merge([10,11,12].map(window.stackroomViewer.boxFor))[0]"
    )
    if not box:
        pytest.skip("this page has no box data for those tokens")

    assert_framed(state, box, "the words the reader arrived for")
    assert viewer.problems == []


def test_clicking_a_redaction_opens_the_lens_on_it(viewer, base_url, a_redacted_page):
    """The one question a reader has about a page with a hole in it.

    The demo's first redaction sits in the right margin, at 77-83% of the page
    width, which is the case this used to get wrong: it asked for the box dead
    centre, the lens will not pan past the edge of the sheet to put it there,
    and the 61px between the two was reported as a broken lens. It is not - see
    `assert_framed`, which is where "framed" is defined and why.
    """
    page = viewer.page
    page.goto(base_url + a_redacted_page, wait_until="networkidle")

    box = page.evaluate(
        """() => {
          var v = document.querySelector('#overlay .hit--void');
          if (!v) return null;
          return { x: parseFloat(v.style.left) / 100, y: parseFloat(v.style.top) / 100,
                   w: parseFloat(v.style.width) / 100, h: parseFloat(v.style.height) / 100 };
        }"""
    )
    figure = page.locator("#scan").bounding_box()
    page.mouse.click(
        figure["x"] + (box["x"] + box["w"] / 2) * figure["width"],
        figure["y"] + (box["y"] + box["h"] / 2) * figure["height"],
    )
    page.wait_for_timeout(1500)

    assert page.evaluate("document.getElementById('lens').open") is True
    state = canvas_state(page)
    assert_framed(state, box, "the redaction the reader clicked")
    assert viewer.problems == []


def test_the_lens_keyboard_does_not_leak_to_the_page(viewer, base_url, a_page):
    """Inside the dialog the arrows pan. They must not also turn the page."""
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")
    page.click(".scan__open")
    page.wait_for_timeout(700)
    here = page.url

    page.keyboard.press("+")
    page.wait_for_timeout(200)
    zoomed = canvas_state(page)["scale"]

    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(200)
    panned = canvas_state(page)
    assert panned["scale"] == pytest.approx(zoomed), "panning changed the zoom"
    assert panned["ty"] < 0, "the arrow key did not pan"

    page.keyboard.press("0")
    page.wait_for_timeout(500)
    fitted = canvas_state(page)
    fit = min(fitted["stageW"] / fitted["natW"], fitted["stageH"] / fitted["natH"])
    assert fitted["scale"] == pytest.approx(fit, rel=0.02), "0 did not fit the page"

    assert page.url == here, "a key inside the dialog navigated the document"
    assert page.evaluate("document.getElementById('lens').open") is True
    assert viewer.problems == []


def test_paging_inside_the_lens_keeps_the_reader_where_they_were(
    viewer, base_url, a_page
):
    """Reading a scan page by page at 250% should not mean finding the same
    paragraph again on every sheet. The zoom is carried across the navigation
    - and only once, so a reload is a plain page again."""
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")
    page.click(".scan__open")
    page.wait_for_timeout(700)
    page.keyboard.press("+")
    page.keyboard.press("+")
    page.wait_for_timeout(300)
    before = canvas_state(page)

    with page.expect_navigation():
        page.locator(".lens__step[rel=next]").click()
    page.wait_for_timeout(1200)

    assert page.evaluate("!!(document.getElementById('lens') || {}).open"), (
        "the lens did not come with the reader"
    )
    after = canvas_state(page)
    assert after["scale"] == pytest.approx(before["scale"], rel=0.05)
    assert after["ty"] == pytest.approx(before["ty"], abs=40)

    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)
    assert not page.evaluate("!!(document.getElementById('lens') || {}).open"), (
        "a reload reopened it; the carried state was not consumed"
    )
    assert viewer.problems == []


def test_escape_closes_the_keyboard_sheet_and_not_the_scan(viewer, base_url, a_page):
    """A reader who asks what the keys do, from inside the scan, should get an
    answer and then their page back."""
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")
    page.click(".scan__open")
    page.wait_for_timeout(700)

    page.keyboard.press("?")
    page.wait_for_timeout(300)
    listed = page.eval_on_selector_all("#shortcuts dd", "els => els.map(e => e.textContent)")
    assert "the whole page" in listed, listed

    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.evaluate("!!(document.getElementById('shortcuts') || {}).open") is False
    assert page.evaluate("document.getElementById('lens').open") is True
    assert page.evaluate("document.activeElement.className") == "lens__stage"

    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    assert page.evaluate("document.getElementById('lens').open") is False
    assert viewer.problems == []


# --------------------------------------------------------------------------
# passage permalinks
# --------------------------------------------------------------------------


def test_a_selection_offers_a_link_to_exactly_those_words(viewer, base_url, a_page):
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")

    select_words(page, 4, 7)
    page.wait_for_timeout(400)

    bar = page.locator(".passage")
    assert bar.is_visible(), "selecting words offered nothing"

    box = bar.bounding_box()
    selection = page.evaluate(
        """() => {
          var rs = window.getSelection().getRangeAt(0).getClientRects();
          return { top: rs[0].top, bottom: rs[rs.length - 1].bottom };
        }"""
    )
    assert (
        box["y"] + box["height"] <= selection["top"] + 1
        or box["y"] >= selection["bottom"] - 1
    ), "the affordance is sitting on top of the words it is about"

    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.locator(".passage button").first.click()
    page.wait_for_timeout(400)

    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied.endswith("#w=4,5,6,7"), copied
    assert copied.split("#")[0] == base_url + a_page

    page.wait_for_timeout(1400)
    assert not bar.is_visible(), "the confirmation is still there a second and a half later"
    assert viewer.problems == []


def test_a_selection_with_no_words_in_it_offers_nothing(viewer, base_url, a_page):
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")

    # Text on the page, but not the page's text: nothing here is citable.
    page.evaluate(
        """() => {
          var target = document.querySelector('.crumbs') || document.querySelector('h1');
          var r = document.createRange();
          r.selectNodeContents(target);
          var s = window.getSelection();
          s.removeAllRanges();
          s.addRange(r);
        }"""
    )
    page.wait_for_timeout(400)
    assert not page.locator(".passage").is_visible()

    select_words(page, 2, 3)
    page.wait_for_timeout(400)
    assert page.locator(".passage").is_visible()

    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    assert not page.locator(".passage").is_visible(), "Escape did not dismiss it"
    assert viewer.problems == []


def test_a_quote_says_where_the_document_was_cut(viewer, base_url, a_redacted_page):
    """A quote assembled from the words alone closes over the redactions and
    reads as a sentence the document does not contain."""
    page = viewer.page
    page.goto(base_url + a_redacted_page, wait_until="networkidle")
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])

    span = page.evaluate(
        """() => {
          var layer = document.querySelector('.text-layer');
          var bar = layer.querySelector('.withheld');
          if (!bar) return null;
          var before = null, after = null, seen = false;
          layer.querySelectorAll('.w[data-i], .withheld').forEach(function (n) {
            if (n === bar) { seen = true; return; }
            if (!n.dataset.i) return;
            if (!seen) before = +n.dataset.i;
            else if (after === null) after = +n.dataset.i;
          });
          return before !== null && after !== null ? [before, after] : null;
        }"""
    )
    if not span:
        pytest.skip("no redaction sits between two words in this transcription")

    select_words(page, span[0], span[1])
    page.wait_for_timeout(400)
    page.locator(".passage button").nth(1).click()
    page.wait_for_timeout(400)

    quote = page.evaluate("navigator.clipboard.readText()")
    assert "[withheld" in quote, quote
    assert "#w=" in quote, "the quote does not carry the link back to the words"
    assert viewer.problems == []


# --------------------------------------------------------------------------
# highlights arriving from a link
# --------------------------------------------------------------------------


def test_arriving_on_a_link_marks_the_words_and_boxes_the_scan(viewer, base_url, a_page):
    """The join guarantee 3 exists to protect: a token index, the same word in
    the transcription, and the same rectangle on the scan."""
    page = viewer.page
    page.goto(base_url + a_page + "#w=10,11,12", wait_until="networkidle")
    page.wait_for_timeout(300)

    marked = page.eval_on_selector_all(".text-layer .w.is-hit", "els => els.map(e => e.dataset.i)")
    assert marked == ["10", "11", "12"]

    drawn = page.eval_on_selector_all(
        "#overlay .hit:not(.hit--void)",
        """els => els.map(e => ({
             x: parseFloat(e.style.left) / 100, y: parseFloat(e.style.top) / 100,
             w: parseFloat(e.style.width) / 100, h: parseFloat(e.style.height) / 100 }))""",
    )
    assert drawn, "the words were marked in the text and nowhere on the scan"

    data = json.loads(page.eval_on_selector("#page-data", "e => e.textContent"))
    for i in (10, 11, 12):
        x, y, w, h = data["b"][i * 4 : i * 4 + 4]
        box = {"x": x / 10000, "y": y / 10000, "w": w / 10000, "h": h / 10000}
        inside = any(
            d["x"] - 0.005 <= box["x"]
            and d["y"] - 0.008 <= box["y"]
            and d["x"] + d["w"] + 0.005 >= box["x"] + box["w"]
            and d["y"] + d["h"] + 0.008 >= box["y"] + box["h"]
            for d in drawn
        )
        assert inside, f"token {i} is not inside any box drawn on the scan"
    assert viewer.problems == []


def test_the_indexed_body_is_never_touched(viewer, base_url, a_page):
    """The search contract. The element carrying data-pagefind-body holds this
    page's tokens and nothing else, and it has to still be true after the
    viewer has drawn boxes, opened a dialog and put an affordance on screen -
    one inserted element inside it moves every highlight in the archive."""
    page = viewer.page
    page.goto(base_url + a_page + "#w=10,11,12", wait_until="networkidle")

    select_words(page, 4, 7)
    page.wait_for_timeout(400)
    page.click(".scan__open")
    page.wait_for_timeout(900)
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    kinds = page.eval_on_selector_all(
        "[data-pagefind-body] *", "els => els.map(e => e.className.split(' ')[0])"
    )
    assert set(kinds) <= {"ln", "w", "withheld"}, sorted(set(kinds))

    tokens = page.eval_on_selector(
        "[data-pagefind-body]", "e => e.textContent.trim().split(/\\s+/).length"
    )
    expected = json.loads(page.eval_on_selector("#page-data", "e => e.textContent"))["n"]
    assert tokens == expected, "the number of whitespace-separated tokens changed"
    assert viewer.problems == []


# --------------------------------------------------------------------------
# the ribbon
# --------------------------------------------------------------------------


def test_the_ribbon_previews_the_page_under_the_pointer(viewer, base_url, a_ribbon_page, site):
    page = viewer.page
    page.goto(base_url + a_ribbon_page, wait_until="networkidle")

    strip = page.locator("svg.ribbon[data-base]").first
    assert page.evaluate(
        "document.querySelector('svg.ribbon[data-base]').classList.contains('is-live')"
    ), "the strip was never wired up"

    total = int(page.evaluate("document.querySelector('svg.ribbon[data-base]').dataset.pages"))
    box = strip.bounding_box()
    wanted = max(1, total // 2)
    x = box["x"] + box["width"] * ((wanted - 0.5) / total)
    y = box["y"] + box["height"] / 2

    page.mouse.move(x - 40, y)
    page.mouse.move(x, y)
    page.wait_for_timeout(500)

    tile = page.locator("#scrub")
    assert page.evaluate("document.getElementById('scrub').classList.contains('is-open')")
    assert f"Page {wanted}" in tile.inner_text()

    thumb = page.evaluate(
        "() => { var i = document.querySelector('#scrub .scrub__img'); "
        "return i.hidden ? null : i.getAttribute('src'); }"
    )
    if thumb:
        assert f"p{wanted:04d}@thumb" in thumb, thumb

    # It never leaves the strip it belongs to.
    tile_box = tile.bounding_box()
    assert tile_box["x"] >= box["x"] - 1
    assert tile_box["x"] + tile_box["width"] <= box["x"] + box["width"] + 1

    page.mouse.move(x, y + 300)
    page.wait_for_timeout(400)
    assert not page.evaluate("document.getElementById('scrub').classList.contains('is-open')")
    assert viewer.problems == []


def test_a_missing_thumbnail_degrades_to_the_number(viewer, base_url, a_ribbon_page):
    """The one test here that takes a file away, and the reason `watched`
    blocks service workers: see that fixture for what a worker in front of this
    route does to it. The assertion below is what stops this quietly reverting
    to a test of nothing if anybody ever lets one back in."""
    page = viewer.page
    page.route("**/media/**@thumb.*", lambda route: route.fulfill(status=404))
    page.goto(base_url + a_ribbon_page, wait_until="networkidle")
    assert page.evaluate(
        "() => !(navigator.serviceWorker && navigator.serviceWorker.controller)"
    ), "a service worker is in control, so the route below is not being applied"

    strip = page.locator("svg.ribbon[data-base]").first
    total = int(page.evaluate("document.querySelector('svg.ribbon[data-base]').dataset.pages"))
    box = strip.bounding_box()
    wanted = max(1, total // 2)
    x = box["x"] + box["width"] * ((wanted - 0.5) / total)
    page.mouse.move(x - 40, box["y"] + box["height"] / 2)
    page.mouse.move(x, box["y"] + box["height"] / 2)
    page.wait_for_timeout(700)

    assert f"Page {wanted}" in page.locator("#scrub").inner_text()
    assert page.evaluate("document.querySelector('#scrub .scrub__img').hidden") is True
    assert viewer.problems == [], "a page with no thumbnail is not an error"
    assert viewer.missing, "the test did not actually take the thumbnails away"


def test_the_ribbon_still_navigates(viewer, base_url, a_ribbon_page):
    page = viewer.page
    page.goto(base_url + a_ribbon_page, wait_until="networkidle")
    strip = page.locator("svg.ribbon[data-base]").first
    total = int(page.evaluate("document.querySelector('svg.ribbon[data-base]').dataset.pages"))
    box = strip.bounding_box()
    wanted = max(1, total // 2)
    with page.expect_navigation():
        page.mouse.click(
            box["x"] + box["width"] * ((wanted - 0.5) / total),
            box["y"] + box["height"] / 2,
        )
    assert page.url.endswith(f"/p/{wanted}/index.html")


def test_touch_can_pinch_the_lens_and_scrub_the_ribbon(watched, base_url, a_page, a_ribbon_page):
    """Both gestures are built on pointer events rather than touch events, so
    they can be exercised the way the browser delivers them."""
    view = watched(has_touch=True)
    page = view.page
    page.goto(base_url + a_page, wait_until="networkidle")
    page.click(".scan__open")
    page.wait_for_timeout(700)

    before = canvas_state(page)["scale"]
    page.evaluate(
        """() => {
          var stage = document.querySelector('.lens__stage');
          var r = stage.getBoundingClientRect();
          function send(type, id, x, y) {
            stage.dispatchEvent(new PointerEvent(type, {
              pointerId: id, pointerType: 'touch', bubbles: true, cancelable: true,
              clientX: r.left + x, clientY: r.top + y
            }));
          }
          var cx = r.width / 2, cy = r.height / 2;
          send('pointerdown', 1, cx - 40, cy);
          send('pointerdown', 2, cx + 40, cy);
          for (var i = 1; i <= 6; i++) {
            send('pointermove', 1, cx - 40 - i * 20, cy);
            send('pointermove', 2, cx + 40 + i * 20, cy);
          }
          send('pointerup', 1, cx - 160, cy);
          send('pointerup', 2, cx + 160, cy);
        }"""
    )
    page.wait_for_timeout(200)
    assert canvas_state(page)["scale"] > before * 1.5, "two fingers did not zoom"

    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    page.goto(base_url + a_ribbon_page, wait_until="networkidle")
    total = int(page.evaluate("document.querySelector('svg.ribbon[data-base]').dataset.pages"))
    wanted = max(1, total // 2)
    with page.expect_navigation():
        page.evaluate(
            """(wanted) => {
              var svg = document.querySelector('svg.ribbon[data-base]');
              var r = svg.getBoundingClientRect();
              var total = +svg.dataset.pages;
              function at(n) { return r.left + r.width * ((n - 0.5) / total); }
              function send(type, x) {
                svg.dispatchEvent(new PointerEvent(type, {
                  pointerId: 7, pointerType: 'touch', bubbles: true, cancelable: true,
                  clientX: x, clientY: r.top + r.height / 2
                }));
              }
              send('pointerdown', at(1));
              send('pointermove', at(wanted));
              send('pointerup', at(wanted));
            }""",
            wanted,
        )
    assert page.url.endswith(f"/p/{wanted}/index.html"), page.url
    assert view.problems == []


def test_a_refused_clipboard_falls_back_to_a_field_the_reader_can_copy(
    viewer, base_url, a_page
):
    """An archive on a USB stick is opened from file:// and has no clipboard at
    all. The honest answer to that is not an error message."""
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")
    page.evaluate(
        """() => {
          Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText: function () { return Promise.reject(new Error('no')); } }
          });
          document.execCommand = function () { return false; };
        }"""
    )
    select_words(page, 5, 6)
    page.wait_for_timeout(400)
    page.locator(".passage button").first.click()
    page.wait_for_timeout(400)

    field = page.locator(".passage__field")
    assert field.is_visible(), "the reader was told nothing and given nothing"
    assert field.input_value().endswith("#w=5,6")
    assert page.evaluate("document.activeElement.className") == "passage__field"
    assert page.evaluate("document.activeElement.selectionEnd") == len(field.input_value())
    assert viewer.problems == []


def test_ordinary_copying_is_left_alone(viewer, base_url, a_page):
    """No copy event is intercepted and no selection is changed, including by
    the fallback above - which works by selecting something else and has to put
    the reader's own selection back."""
    page = viewer.page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(base_url + a_page, wait_until="networkidle")

    select_words(page, 8, 10)
    page.wait_for_timeout(400)
    wanted = page.evaluate("window.getSelection().toString()")

    page.keyboard.press("Control+c")
    page.wait_for_timeout(300)
    assert page.evaluate("navigator.clipboard.readText()") == wanted

    # And the affordance's own copy leaves the selection where it was.
    page.locator(".passage button").first.click()
    page.wait_for_timeout(400)
    assert page.evaluate("window.getSelection().toString()") == wanted
    assert viewer.problems == []


def test_the_lens_can_walk_the_redactions(viewer, base_url, a_redacted_page):
    """"Withheld" steps through the holes, framing each one and saying so.

    The first stop is the same right-margin box `test_clicking_a_redaction_
    opens_the_lens_on_it` lands on, so "framed" means here what it means there:
    see `assert_framed`.
    """
    page = viewer.page
    page.goto(base_url + a_redacted_page, wait_until="networkidle")
    page.click(".scan__open")
    page.wait_for_timeout(900)

    button = page.locator(".lens__bar button", has_text="Withheld")
    assert button.count() == 1, "a page with redactions offers no way to find them"

    boxes = page.evaluate(
        """() => [].map.call(document.querySelectorAll('#overlay .hit--void'), function (v) {
             return { x: parseFloat(v.style.left) / 100, y: parseFloat(v.style.top) / 100,
                      w: parseFloat(v.style.width) / 100, h: parseFloat(v.style.height) / 100 };
           })"""
    )
    button.click()
    page.wait_for_timeout(900)
    state = canvas_state(page)
    assert_framed(state, boxes[0], "the first withheld area")
    # The viewer's own live region, by id: several parts of a page carry
    # role="status" and the first one in the document is not this one.
    assert "withheld area 1" in page.evaluate(
        "document.getElementById('viewer-status').textContent"
    ).lower()
    assert viewer.problems == []


def test_a_keyboard_can_reach_the_passage_affordance(viewer, base_url, a_page):
    """The bar is at the end of <body>, where Tab would meet it last. A reader
    who selected with the keyboard should not have to cross the whole page."""
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")
    select_words(page, 2, 4)
    page.wait_for_timeout(400)

    page.keyboard.press("Tab")
    page.wait_for_timeout(150)
    assert page.evaluate("!!document.querySelector('.passage').contains(document.activeElement)")

    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert not page.locator(".passage").is_visible()
    assert page.evaluate("document.activeElement.id") == "main", (
        "focus was left nowhere after the bar it was in went away"
    )
    assert viewer.problems == []


# --------------------------------------------------------------------------
# page turns
# --------------------------------------------------------------------------

# The direction is recorded on <html> before the outgoing page is captured and
# again on the incoming one, then removed when the transition ends - so the
# only way to see it is to be watching from before the document had scripts.
WATCH_TURNS = """
  window.__turns = [];
  new MutationObserver(function () {
    window.__turns.push(document.documentElement.getAttribute('data-turn'));
  }).observe(document, { attributes: true, subtree: true, attributeFilter: ['data-turn'] });
"""


def _turns(page) -> list:
    return [t for t in page.evaluate("window.__turns || []") if t]


def test_forward_and_back_move_in_opposite_directions(watched, base_url, a_page):
    view = watched()
    page = view.page
    if not page.evaluate("'onpagereveal' in window"):
        pytest.skip("this browser has no cross-document view transitions")

    page.context.add_init_script(WATCH_TURNS)
    page.goto(base_url + a_page, wait_until="networkidle")

    with page.expect_navigation():
        page.click("#next-page")
    page.wait_for_timeout(600)
    assert _turns(page) == ["forward"], _turns(page)

    with page.expect_navigation():
        page.click("#prev-page")
    page.wait_for_timeout(600)
    assert _turns(page) == ["back"], _turns(page)

    assert view.problems == []


def test_the_scan_is_the_element_that_carries_the_turn(viewer, base_url, a_page):
    page = viewer.page
    page.goto(base_url + a_page, wait_until="networkidle")
    if not page.evaluate("CSS.supports('view-transition-name', 'scan')"):
        pytest.skip("this browser has no view transitions")
    assert page.evaluate("getComputedStyle(document.getElementById('scan')).viewTransitionName") == (
        "scan"
    )


# --------------------------------------------------------------------------
# reduced motion
# --------------------------------------------------------------------------


def test_reduced_motion_removes_the_movement_and_keeps_the_state(
    watched, base_url, a_page
):
    """Asked not to move, nothing moves - but everything still happens."""
    view = watched(reduced_motion="reduce")
    page = view.page
    page.context.add_init_script(WATCH_TURNS)
    page.goto(base_url + a_page + "#w=10,11,12", wait_until="networkidle")

    page.click(".scan__open")
    page.wait_for_timeout(150)
    early = canvas_state(page)
    page.wait_for_timeout(1200)
    settled = canvas_state(page)

    assert early is not None, "the lens did not open"
    assert early["scale"] == pytest.approx(settled["scale"], rel=0.001), (
        "the view was still travelling towards the words"
    )
    assert early["tx"] == pytest.approx(settled["tx"], abs=1)

    box = page.evaluate(
        "window.stackroomViewer.merge([10,11,12].map(window.stackroomViewer.boxFor))[0]"
    )
    if box:
        cx, cy = framed_centre(settled, box)
        assert abs(cx - settled["stageW"] / 2) < 12, "it did not arrive framed"
        assert abs(cy - settled["stageH"] / 2) < 12

    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    with page.expect_navigation():
        page.click("#next-page")
    page.wait_for_timeout(500)
    assert _turns(page) == [], "a directional page turn was started anyway"
    assert view.problems == []


# --------------------------------------------------------------------------
# and nothing anywhere may throw
# --------------------------------------------------------------------------


def test_no_page_in_the_archive_logs_an_error(viewer, base_url, site):
    """These files are loaded on every page of the archive, including the ones
    that have no scan, no ribbon and no transcription. Each has to look for the
    thing it enhances and do nothing at all when it is not there."""
    page = viewer.page
    failures: dict[str, list[str]] = {}
    absent: dict[str, list[str]] = {}
    for url in _every_html(site):
        viewer.clear()
        page.goto(base_url + url, wait_until="networkidle")
        page.wait_for_timeout(120)
        if viewer.problems:
            failures[url] = list(viewer.problems)
        # Whatever else the archive is missing belongs to whoever built it;
        # what the viewer asks for has to be there.
        gone = [u for u in viewer.missing if "/assets/" in u or "/media/" in u]
        if gone:
            absent[url] = gone
    assert failures == {}
    assert absent == {}
