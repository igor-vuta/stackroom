/* The negative: pointer, keyboard and filter.
 *
 * Everything here is an enhancement over a page that is already complete. The
 * field is inline SVG written by the site builder: the rectangles are correct,
 * every one of them is inside a link to the page it came from, the three
 * arrangements are radio buttons and CSS, and the index under the field lists
 * the same pages in the same order. If this file never loads, nothing is lost
 * but a tooltip, a keyboard route through the picture and a filter - and every
 * fact each of those shows is written out somewhere on the page in words.
 *
 * Three things it does:
 *
 *   1. A tooltip, so pointing at a rectangle says what it is.
 *   2. One tab stop into the field, with the arrow keys moving between pages.
 *      A field of four thousand rectangles must not be four thousand tab
 *      stops, and the builder ships them all with tabindex="-1" for that
 *      reason; this promotes exactly one at a time - the roving-tabindex
 *      pattern - so the whole picture is reachable at the cost of one stop.
 *   3. A filter that dims every rectangle not withheld under one code, done by
 *      rewriting a single CSS rule rather than by touching four thousand
 *      elements, because the second one takes a quarter of a second on a
 *      phone and the first takes none.
 */
(function () {
  'use strict';

  var doc = document;
  /* prefs.js runs from the head of every template and publishes the archive's
     own language; this deferred file always runs after it. */
  var sr = window.stackroomReader || {
    t: function (k) { return '[' + k + ']'; },
    pct: function (v, d, ofOne) { return (ofOne ? v * 100 : v).toFixed(d || 0) + '%'; }
  };
  var root = doc.querySelector('.negative');
  if (!root) return;

  var tip = null;
  var styleTag = null;
  var current = null;      /* the <a> that holds the roving tab stop */
  var marker = null;       /* [halo, ring] drawn over the focused cell */

  /* ------------------------------------------------------------ reading it */

  function fieldOf(el) {
    return el && el.closest ? el.closest('.negative__field') : null;
  }

  function visibleField() {
    var fields = root.querySelectorAll('.negative__field');
    for (var i = 0; i < fields.length; i++) {
      /* offsetParent is null for anything display:none, which is how the
         arrangement toggle hides the other two. */
      if (fields[i].parentNode.offsetParent !== null) return fields[i];
    }
    return null;
  }

  function cellsOf(field) {
    return field ? field.querySelectorAll('a') : [];
  }

  /* The page a link points at, said the way the index list says it. The text
     is read back off the page rather than shipped again as JSON: the index
     under the field already carries every one of these sentences, and two
     copies of the same fact are two facts that can disagree. */
  var indexByUrl = null;

  function describe(link) {
    if (indexByUrl === null) {
      indexByUrl = {};
      var rows = doc.querySelectorAll('.negative__index .doc a[href]');
      for (var i = 0; i < rows.length; i++) {
        var row = rows[i].closest('.doc');
        var head = rows[i].closest('li');
        var title = head ? previousDocTitle(head) : '';
        indexByUrl[rows[i].getAttribute('href')] = {
          title: title,
          page: rows[i].textContent.trim(),
          meta: metaOf(row)
        };
      }
    }
    return indexByUrl[link.getAttribute('href')] || null;
  }

  /* The metadata line, read back as a sentence. Its parts are separate spans
     with the separator between them drawn by the stylesheet, so joining the
     text nodes runs the last two words together; the parts are collected and
     rejoined instead. */
  function metaOf(row) {
    var meta = row ? row.querySelector('.doc__meta') : null;
    if (!meta) return '';
    var parts = [];
    var spans = meta.children;
    for (var i = 0; i < spans.length; i++) {
      if (spans[i].classList.contains('sep')) continue;
      var text = spans[i].textContent.replace(/\s+/g, ' ').trim();
      if (text) parts.push(text);
    }
    return parts.join(' · ');
  }

  function previousDocTitle(li) {
    var node = li.previousElementSibling;
    while (node) {
      if (node.classList.contains('negative__index-doc')) return node.textContent.trim();
      node = node.previousElementSibling;
    }
    return '';
  }

  /* A rectangle's own size, as a share of the page it was cut out of. Every
     page in the field is drawn at the same *area*, whatever shape it is, so a
     rectangle's area divided by that one number is its share of its page -
     which is the claim the whole picture rests on, and it is recoverable here
     without shipping every box's measurements a second time. */
  function shareOf(shape, field) {
    var area = parseFloat(field.getAttribute('data-area'));
    if (!area || shape.tagName.toLowerCase() !== 'rect') return null;
    var share = (shape.width.baseVal.value * shape.height.baseVal.value) / area;
    if (share <= 0) return null;
    /* The same four messages build/negative.py uses for the same four bands,
       so the tooltip and the page say a share of a page in the same words. */
    if (share < 0.001) return sr.t('negative.share_tiny');
    if (share < 0.01) return sr.t('negative.share_small', { percent: sr.pct(share, 1, true) });
    return sr.t('negative.share_large', { percent: sr.pct(share, 0, true) });
  }

  /* Which law a rectangle was withheld under, and what that law says in
     words. Both are read off the table further down the page - the one a
     reader can check - rather than shipped a second time as data. */
  var codeNames = null;

  function codeOf(shape) {
    if (codeNames === null) {
      codeNames = {};
      var rows = doc.querySelectorAll('tr[data-code]');
      for (var i = 0; i < rows.length; i++) {
        var cells = rows[i].cells;
        if (cells.length < 2) continue;
        codeNames[rows[i].getAttribute('data-code')] =
          cells[0].textContent.trim() + ' — ' + cells[1].textContent.trim();
      }
    }
    var classes = (shape.getAttribute('class') || '').split(/\s+/);
    for (var j = 0; j < classes.length; j++) {
      if (codeNames[classes[j]]) return codeNames[classes[j]];
    }
    return null;
  }

  /* ---------------------------------------------------------- the tooltip */

  function tipElement() {
    if (tip) return tip;
    tip = doc.createElement('div');
    tip.className = 'negative__tip';
    tip.hidden = true;
    doc.body.appendChild(tip);
    return tip;
  }

  function showTip(shape, link, x, y) {
    var field = fieldOf(shape);
    var about = describe(link);
    if (!about) return;
    var el = tipElement();
    /* Document, page, how much of the page this one box took, and the law it
       was taken under, in that order. Nothing else: everything a tooltip says
       is also in the index list below, and a tooltip that repeats the whole
       row is a tooltip nobody reads to the end of. */
    var lines = '<p>' + sr.t('js.negative.tip_where_html', {
      title: about.title, page: about.page.toLowerCase()
    }) + '</p>';
    var share = shareOf(shape, field);
    if (share) lines += '<p>' + escapeHTML(share) + '</p>';
    var code = codeOf(shape);
    lines += '<p>' + escapeHTML(code || sr.t('js.negative.no_code_here')) + '</p>';
    el.innerHTML = lines;
    el.hidden = false;
    place(el, x, y);
  }

  function place(el, x, y) {
    var width = el.offsetWidth;
    var left = x + 14;
    if (left + width > window.innerWidth - 8) left = Math.max(8, x - width - 14);
    el.style.left = (left + window.scrollX) + 'px';
    el.style.top = (y + 18 + window.scrollY) + 'px';
  }

  function hideTip() {
    if (tip) tip.hidden = true;
  }

  function escapeHTML(text) {
    return String(text).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }

  /* ----------------------------------------------------------- the marker */

  /* A ring around whatever is being pointed at or focused, drawn in two
     strokes: the archive's focus ring is two-tone for the same reason, because
     one colour cannot clear 3:1 against paper and against a solid black cell
     at the same time. */
  function markerFor(field) {
    if (marker && marker[0].ownerSVGElement === field) return marker;
    if (marker) removeMarker();
    var halo = svgEl('rect', 'negative__marker negative__marker--halo');
    var ring = svgEl('rect', 'negative__marker');
    field.appendChild(halo);
    field.appendChild(ring);
    marker = [halo, ring];
    return marker;
  }

  function svgEl(name, cls) {
    var el = doc.createElementNS('http://www.w3.org/2000/svg', name);
    el.setAttribute('class', cls);
    return el;
  }

  function removeMarker() {
    if (!marker) return;
    marker[0].remove();
    marker[1].remove();
    marker = null;
  }

  function markCell(link) {
    var field = fieldOf(link);
    if (!field) return;
    var box;
    try { box = link.getBBox(); } catch (e) { return; }
    var pair = markerFor(field);
    for (var i = 0; i < pair.length; i++) {
      pair[i].setAttribute('x', box.x - 2);
      pair[i].setAttribute('y', box.y - 2);
      pair[i].setAttribute('width', box.width + 4);
      pair[i].setAttribute('height', box.height + 4);
    }
  }

  /* ---------------------------------------------------------- the keyboard */

  /* The field becomes one tab stop. Once inside, the arrow keys walk the
     pages in the order they are drawn, Home and End go to the ends, and Enter
     follows the link - which is what the reader would have got by clicking a
     rectangle. Tab leaves. Everything reachable this way is also in the index
     list below, so this is a shortcut through the picture and not the only
     road. */
  function arm(field) {
    var cells = cellsOf(field);
    if (!cells.length) return;
    /* role="img" hides a graphic's contents from assistive technology, which
       is right for a picture and wrong for one with a focusable link in it:
       hidden and focusable is the worst of both. So with a script here the
       field becomes a group - and then every link in it but one is hidden
       again, by hand. A thousand nameless links in the reading order is not an
       improvement on a picture; one link, named, holding the tab stop and
       moving under the arrow keys, is. */
    field.setAttribute('role', 'group');
    for (var i = 0; i < cells.length; i++) {
      cells[i].setAttribute('aria-hidden', 'true');
    }
    focusable(cells[0]);
  }

  function focusable(link) {
    if (current === link) return;
    if (current) {
      current.setAttribute('tabindex', '-1');
      current.setAttribute('aria-hidden', 'true');
      current.removeAttribute('aria-label');
    }
    current = link;
    /* Named before it is shown, and shown before anything focuses it: an
       element that is aria-hidden at the moment it takes focus is a bug
       report waiting to happen. */
    var about = describe(link);
    if (about) {
      link.setAttribute('aria-label', sr.t('js.negative.cell_label', {
        title: about.title, page: about.page, meta: about.meta
      }));
    }
    link.removeAttribute('aria-hidden');
    link.setAttribute('tabindex', '0');
  }

  function step(delta) {
    var field = visibleField();
    if (!field) return;
    var cells = cellsOf(field);
    var at = -1;
    for (var i = 0; i < cells.length; i++) {
      if (cells[i] === current) { at = i; break; }
    }
    var next = Math.min(cells.length - 1, Math.max(0, (at < 0 ? 0 : at) + delta));
    focusable(cells[next]);
    cells[next].focus();
  }

  /* A row is however many cells fit across, which the builder does not write
     down anywhere: it is read off the drawing by looking for the first cell
     that starts back at the left. */
  function columns(field) {
    var cells = cellsOf(field);
    if (cells.length < 2) return 1;
    var first = left(cells[0]);
    for (var i = 1; i < cells.length; i++) {
      if (left(cells[i]) <= first) return i;
    }
    return cells.length;
  }

  function left(link) {
    try { return link.getBBox().x; } catch (e) { return 0; }
  }

  root.addEventListener('keydown', function (ev) {
    if (!current || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    var field = visibleField();
    if (!field || doc.activeElement !== current) return;
    var wide = columns(field);
    switch (ev.key) {
      case 'ArrowRight': ev.preventDefault(); step(1); break;
      case 'ArrowLeft': ev.preventDefault(); step(-1); break;
      case 'ArrowDown': ev.preventDefault(); step(wide); break;
      case 'ArrowUp': ev.preventDefault(); step(-wide); break;
      case 'Home': ev.preventDefault(); step(-cellsOf(field).length); break;
      case 'End': ev.preventDefault(); step(cellsOf(field).length); break;
      default: return;
    }
  });

  root.addEventListener('focusin', function (ev) {
    var link = ev.target.closest ? ev.target.closest('a') : null;
    if (!link || !fieldOf(link)) return;
    focusable(link);
    markCell(link);
    var box = link.getBoundingClientRect();
    var shape = link.querySelector('rect') || link;
    showTip(shape, link, box.left + box.width / 2, box.bottom);
  });

  root.addEventListener('focusout', function (ev) {
    if (ev.target.closest && ev.target.closest('.negative__field')) {
      hideTip();
      removeMarker();
    }
  });

  /* ------------------------------------------------------------- pointing */

  root.addEventListener('pointermove', function (ev) {
    var shape = ev.target;
    var name = shape.tagName ? shape.tagName.toLowerCase() : '';
    if (name !== 'rect' && name !== 'path') { hideTip(); removeMarker(); return; }
    if (shape.classList.contains('negative__paper') ||
        shape.classList.contains('negative__marker')) {
      hideTip(); removeMarker(); return;
    }
    var link = shape.closest('a');
    if (!link) { hideTip(); return; }
    markCell(link);
    showTip(shape, link, ev.clientX, ev.clientY);
  });

  root.addEventListener('pointerleave', function () {
    hideTip();
    removeMarker();
  });

  /* --------------------------------------------------------------- filter */

  /* One rule, rewritten, rather than four thousand class changes. The name of
     whatever is picked out is written into the live region as well, so the
     state of this control is never carried by lightness alone. */
  function filter(key, label) {
    if (!styleTag) {
      styleTag = doc.createElement('style');
      doc.head.appendChild(styleTag);
    }
    styleTag.textContent = key
      ? '.negative__field rect:not(.' + key + '):not(.negative__marker),' +
        '.negative__field path:not(.' + key + '):not(.negative__paper)' +
        ' { fill: var(--rule-strong); }'
      : '';
    var chips = root.querySelectorAll('.negative__chip');
    for (var i = 0; i < chips.length; i++) {
      var on = chips[i].getAttribute('data-code') === key;
      chips[i].setAttribute('aria-pressed', on ? 'true' : 'false');
      chips[i].classList.toggle('is-on', on);
    }
    announce(key
      ? sr.t('js.negative.filtered', { label: label })
      : sr.t('js.negative.unfiltered'));
  }

  var status = null;

  function announce(text) {
    if (!status) {
      status = doc.createElement('p');
      status.className = 'visually-hidden';
      status.setAttribute('role', 'status');
      status.setAttribute('aria-live', 'polite');
      root.appendChild(status);
    }
    status.textContent = text;
  }

  /* ------------------------------------------------------------------ init */

  function init() {
    var controls = root.querySelector('[data-negative-filter]');
    if (controls) {
      controls.hidden = false;
      controls.addEventListener('click', function (ev) {
        var chip = ev.target.closest('.negative__chip');
        if (!chip) return;
        filter(chip.getAttribute('data-code'), chip.textContent.trim());
      });
      /* The live region has to be in the document before it is written to: a
         screen reader announces a change to an element it was already
         watching, and one created a moment ago was not being watched. */
      announce('');
    }

    var showing = visibleField();
    if (showing) arm(showing);

    /* Switching arrangement moves the tab stop into the field that is now on
       screen, so Tab does not land on a link nobody can see. */
    var radios = root.querySelectorAll('.negative__radio');
    for (var j = 0; j < radios.length; j++) {
      radios[j].addEventListener('change', function () {
        hideTip();
        removeMarker();
        current = null;
        var field = visibleField();
        if (field) arm(field);
      });
    }
  }

  if (doc.readyState === 'loading') {
    doc.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
