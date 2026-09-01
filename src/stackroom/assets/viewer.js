/* Stackroom page viewer.
 *
 * Everything here is an enhancement. The page it runs on is already complete
 * without it: the scan is an <img>, the text is real text, the links work. If
 * this file fails to load, or throws, or is blocked, nothing is lost except
 * the highlighting and the keyboard shortcuts.
 *
 * Its one real job is the join between two representations of the same page.
 * A search hit arrives as a list of token positions; the text layer numbers
 * its tokens with the same positions; the box data numbers them again. Draw
 * the boxes, mark the words, and the reader sees the phrase they searched for
 * sitting on the scan where it was actually printed.
 *
 * That join is also published on `window.Stackroom`, because the full-size
 * view in assets/js/scan.js needs the same three numbers to fly to the same
 * rectangle. Two implementations of one arithmetic is how a highlight ends up
 * in one place on the page and a different place in the lens.
 */
(function () {
  'use strict';

  var doc = document;
  /* prefs.js is loaded synchronously from the head of every template, ahead of
     this deferred file, and it publishes the archive's own language. The
     fallback keeps this file from throwing on a page that has no prefs.js and
     puts the key on screen, where somebody will notice. */
  var sr = window.stackroomReader || { t: function (k) { return '[' + k + ']'; } };

  function readJSON(id) {
    var el = doc.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  /* ---------------------------------------------------------------- boxes */

  var data = readJSON('page-data');

  /* Boxes arrive as a flat array of integers, four per token, in ten-
     thousandths of the page. Integers because a 400-word page is 1,600
     numbers and floats cost roughly twice the bytes for precision finer than
     a pixel. */
  function boxAt(i) {
    if (!data || !data.b || i < 0) return null;
    var o = i * 4;
    if (o + 3 >= data.b.length) return null;
    return {
      x: data.b[o] / 10000,
      y: data.b[o + 1] / 10000,
      w: data.b[o + 2] / 10000,
      h: data.b[o + 3] / 10000
    };
  }

  /* Adjacent tokens of one phrase are merged into a single box. Three separate
     rectangles around "office of the director" reads as three findings; one
     rectangle reads as the phrase, which is what was searched for. */
  function merge(boxes) {
    var out = [];
    boxes.forEach(function (b) {
      if (!b) return;
      var last = out[out.length - 1];
      if (last &&
          Math.abs(b.y - last.y) < last.h * 0.6 &&
          b.x >= last.x && b.x - (last.x + last.w) < 0.02) {
        var right = Math.max(last.x + last.w, b.x + b.w);
        last.y = Math.min(last.y, b.y);
        last.h = Math.max(last.h, b.h);
        last.w = right - last.x;
      } else {
        out.push({ x: b.x, y: b.y, w: b.w, h: b.h });
      }
    });
    return out;
  }

  function positionsFromLocation() {
    var out = [];
    var hash = window.location.hash || '';
    var m = /(?:^|[#&])w=([0-9,]+)/.exec(hash);
    if (m) {
      m[1].split(',').forEach(function (n) {
        var i = parseInt(n, 10);
        if (!isNaN(i)) out.push(i);
      });
    }
    return out;
  }

  function highlight(positions) {
    var overlay = doc.getElementById('overlay');
    var layer = doc.querySelector('.text-layer');
    if (!positions.length) return;

    if (layer) {
      positions.forEach(function (i) {
        var w = layer.querySelector('.w[data-i="' + i + '"]');
        if (w) w.classList.add('is-hit');
      });
    }

    if (overlay) {
      var boxes = merge(positions.map(boxAt).filter(Boolean));
      boxes.forEach(function (b) {
        var el = doc.createElement('span');
        el.className = 'hit';
        /* A hair of padding around the ink: a box drawn exactly on the glyph
           bounds looks like a rendering error rather than a highlight. */
        el.style.left = ((b.x - 0.002) * 100).toFixed(3) + '%';
        el.style.top = ((b.y - 0.004) * 100).toFixed(3) + '%';
        el.style.width = ((b.w + 0.004) * 100).toFixed(3) + '%';
        el.style.height = ((b.h + 0.008) * 100).toFixed(3) + '%';
        overlay.appendChild(el);
      });
    }

    var first = layer && layer.querySelector('.w.is-hit');
    if (first && !prefersReducedMotion()) {
      first.scrollIntoView({ block: 'center', behavior: 'smooth' });
    } else if (first) {
      first.scrollIntoView({ block: 'center' });
    }

    announce(sr.t('js.viewer.matches', { count: positions.length }));
  }

  function prefersReducedMotion() {
    return window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /* The live region is put in place at load and written to later. Created and
     filled in the same tick it usually says nothing at all: a screen reader
     has to have been watching the element before its text changes, and an
     element that did not exist a moment ago was not being watched. */
  var statusRegion = null;

  function makeStatusRegion() {
    if (statusRegion) return statusRegion;
    statusRegion = doc.createElement('p');
    statusRegion.id = 'viewer-status';
    statusRegion.className = 'visually-hidden';
    statusRegion.setAttribute('role', 'status');
    statusRegion.setAttribute('aria-live', 'polite');
    doc.body.appendChild(statusRegion);
    return statusRegion;
  }

  function announce(text) {
    makeStatusRegion().textContent = text;
  }

  /* ------------------------------------------------------------ keyboard */

  /* Every one of these has a visible control on the page as well. A shortcut
     that is the only way to do something is a shortcut most people never
     find.

     The list is built from what is actually on this page rather than written
     out in advance, because the sheet used to advertise "/  search" on pages
     where pressing / did nothing at all: it looked for a search URL in the
     page's JSON, and the builder does not put one there. A keyboard sheet
     that lists a key which does nothing is worse than no sheet. */
  function searchTarget() {
    /* The same link the reader can see in the masthead, so the shortcut and
       the visible control can never disagree about where search lives. */
    return doc.querySelector('.masthead__nav a[href$="search/index.html"]');
  }

  function lensOpen() {
    var lens = doc.getElementById('lens');
    return !!(lens && lens.open);
  }

  function shortcuts() {
    var list = [];
    /* Inside the lens the same keys mean different things, so the sheet says
       what they mean in there. A list that describes the page while the
       reader is looking at the scan is a list that is wrong. */
    /* The keys are keys and are not translated - a reader presses j whatever
       their language - so only the right-hand column comes from the
       catalogue. */
    if (lensOpen()) {
      list.push(['+  \u2013', sr.t('js.keys.zoom')]);
      list.push(['0', sr.t('js.keys.whole_page')]);
      list.push(['1', sr.t('js.keys.actual_size')]);
      list.push(['\u2190 \u2191 \u2193 \u2192', sr.t('js.keys.pan')]);
      if (doc.querySelector('.lens__step')) list.push(['j  k', sr.t('js.keys.step_page')]);
      if (doc.querySelector('.hit--void')) list.push(['r', sr.t('js.keys.next_withheld')]);
      list.push(['Esc', sr.t('js.keys.close')]);
      list.push(['?', sr.t('js.keys.this_list')]);
      return list;
    }
    /* "or" is the one word in the left-hand column, and it is a word. */
    var either = '  ' + sr.t('js.keys.or') + '  ';
    if (doc.getElementById('next-page')) list.push(['j' + either + '\u2192', sr.t('js.keys.next_page')]);
    if (doc.getElementById('prev-page')) list.push(['k' + either + '\u2190', sr.t('js.keys.prev_page')]);
    if (doc.getElementById('q') || searchTarget()) list.push(['/', sr.t('js.keys.search')]);
    if (doc.querySelector('.scan__open')) list.push(['z', sr.t('js.keys.scan_full')]);
    list.push(['?', sr.t('js.keys.this_list')]);
    return list;
  }

  function go(id) {
    var link = doc.getElementById(id);
    if (!link) return;
    window.location.href = link.href;
  }

  function typingInto(el) {
    if (!el) return false;
    var tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  doc.addEventListener('keydown', function (ev) {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (typingInto(doc.activeElement)) {
      if (ev.key === 'Escape') doc.activeElement.blur();
      return;
    }
    /* A modal dialog has its own keys - the lens pans with the arrows - and
       the page's shortcuts must not fire underneath one. The dialogs stop
       what they use from propagating; this stops everything else, so `j`
       inside the lens cannot navigate the document out from under it. */
    var modal = doc.querySelector('dialog[open]');
    if (modal && ev.key !== '?' && ev.key !== 'Escape') return;

    switch (ev.key) {
      case 'j': case 'ArrowRight':
        go('next-page'); break;
      case 'k': case 'ArrowLeft':
        go('prev-page'); break;
      case '/': {
        var q = doc.getElementById('q');
        if (q) { ev.preventDefault(); q.focus(); q.select(); return; }
        var link = searchTarget();
        if (link) { ev.preventDefault(); window.location.href = link.href; }
        break;
      }
      case 'z': {
        /* The button, not a second implementation of it: the shortcut and the
           visible control can never disagree about what they do. */
        var zoom = doc.querySelector('.scan__open');
        if (zoom) { ev.preventDefault(); zoom.click(); }
        break;
      }
      case '?':
        ev.preventDefault(); toggleShortcuts(); break;
    }
    /* There is no case for Escape, and there used to be: it closed the
       shortcut sheet, which <dialog> already does for itself. Harmless while
       the sheet was the only dialog on the page. Once the sheet could be
       opened from inside the full-size view it was not: this handler runs
       before the browser acts on the key, closed the sheet out from under it,
       and left the browser's close request to land on the dialog underneath -
       so one Escape shut the sheet *and* threw the reader out of the scan
       they were reading. The platform gets the key. */
  });

  function toggleShortcuts() {
    var dialog = doc.getElementById('shortcuts');
    if (!dialog) {
      dialog = doc.createElement('dialog');
      dialog.id = 'shortcuts';
      dialog.className = 'shortcuts';
      /* Without this the dialog announces as "dialog" and nothing else. The
         heading is already the right words; point at it rather than repeat
         them somewhere a sighted reader cannot check. */
      dialog.setAttribute('aria-labelledby', 'shortcuts-title');
      doc.body.appendChild(dialog);
    }
    if (!dialog.open) {
      /* Rebuilt on every opening rather than once. What the keys do depends on
         what is open, and a sheet cached the first time it was asked for
         would go on describing a page the reader has since left. */
      /* The descriptions come out of the catalogue, which is trusted the same
         way the templates are - but they are plain-text messages, so they are
         escaped on the way into markup like any other plain-text message. */
      var esc = sr.esc || function (x) { return String(x); };
      var html = '<h2 class="shortcuts__title" id="shortcuts-title">' +
        esc(sr.t('js.viewer.keyboard')) + '</h2><dl>';
      shortcuts().forEach(function (pair) {
        html += '<dt><kbd>' + pair[0] + '</kbd></dt><dd>' + esc(pair[1]) + '</dd>';
      });
      html += '</dl>';
      dialog.innerHTML = html;
    }
    /* <dialog> handles the rest itself: Escape closes it, focus is trapped
       inside it while it is open and returned to whatever had it before. */
    if (dialog.open) dialog.close();
    else if (dialog.showModal) dialog.showModal();
  }

  /* -------------------------------------------------------------- ribbon */

  /* The ribbon used to be wired from here, and it never once worked in a
     published archive: this file is loaded by the page template alone, and no
     page template contains a ribbon. The strip lives on the front page, the
     browse list and the document page, none of which load this file. It is now
     in assets/js/scan.js, which is loaded on every page, and it does rather
     more than accept a click. */

  /* ---------------------------------------------------------------- init */

  function init() {
    makeStatusRegion();
    try { highlight(positionsFromLocation()); } catch (e) { /* never fatal */ }
  }

  if (doc.readyState === 'loading') {
    doc.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  /* The one arithmetic, published. assets/js/scan.js reads it to frame the
     same rectangle in the full-size view; nothing else may recompute it.

     Under its own name and not on the namespace the assets/js files share.
     That namespace is a bag of helpers those files hand each other, and a
     script that finds an object there assumes it holds their keys; putting
     five unrelated ones in it turns a missing-feature check into a
     TypeError. This is the page viewer's surface, it is named for the page
     viewer, and it exists only on pages that have one. */
  var shared = window.stackroomViewer = window.stackroomViewer || {};
  shared.boxFor = boxAt;
  shared.merge = merge;
  shared.positions = positionsFromLocation;
  shared.announce = announce;
  shared.reducedMotion = prefersReducedMotion;

  window.addEventListener('hashchange', function () {
    var overlay = doc.getElementById('overlay');
    if (overlay) {
      /* Only the boxes this file drew. The test used to be "has no inline
         outline", which is true of the template's redaction boxes as well -
         so following a second search hit on the same page silently deleted
         every redaction from the scan and did not put them back. */
      Array.prototype.forEach.call(
        overlay.querySelectorAll('.hit:not(.hit--void)'),
        function (el) { el.remove(); }
      );
    }
    Array.prototype.forEach.call(doc.querySelectorAll('.w.is-hit'), function (el) {
      el.classList.remove('is-hit');
    });
    highlight(positionsFromLocation());
  });
})();
