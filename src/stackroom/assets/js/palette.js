/* Stackroom command palette.
 *
 * The archive has a search box and this is not it. Search asks what a page
 * says; this asks where a thing is - a document, a numbered page, a control
 * number, an exemption code, one of the standing pages.
 *
 * It is never the only way to reach anything: every row is a link that exists
 * on this site anyway, and the standing pages are read out of the masthead
 * rather than listed here, so the palette and the navigation cannot disagree
 * about where Search lives or whether this build has one.
 */
(function () {
  'use strict';

  var doc = document;
  /* prefs.js is loaded synchronously from <head> - it has to be, or the theme
     flashes - so its helpers are here before this deferred file runs. */
  var sr = window.stackroomReader;
  if (!sr) return;

  var MAX = 30;   /* rows drawn; wanting more than this is a search, not a jump */
  var KEEP = 6;   /* recent destinations remembered */
  var ROOT = sr.prefix();

  var dialog, input, list, note;
  var all = null, rows = [], active = -1, loading = null;
  var numbered = {};   /* docs.json, kept out of `all` - see stamps() */

  /* ------------------------------------------------------------ matching */

  /* Every character you typed is in the row, in that order, and the marks show
     where. Six tiers rather than a weighted score, because a reader can hold
     six rules in their head and check them against what is on screen, and
     nobody has ever been able to do that with a number.

        0  the row starts with what you typed
        1  you typed the initials of its words, from the first
        2  what you typed starts a word inside it
        3  what you typed appears inside it
        4  initials, starting part-way in
        5  the characters are all there, in order, but scattered

     Ties go to the shorter row, then the earlier match. Several words must all
     match and take the worst tier, so a longer query only ever narrows. */
  var BREAK = /[^a-z0-9]/;

  function match(q, text) {
    var low = text.toLowerCase(), marks = [], i = low.indexOf(q), n, k;
    if (i >= 0) {
      for (n = 0; n < q.length; n++) marks.push(i + n);
      return { tier: i ? (BREAK.test(low.charAt(i - 1)) ? 2 : 3) : 0, at: i, marks: marks };
    }
    var at = [], letters = '';
    for (n = 0; n < low.length; n++) {
      if (!BREAK.test(low.charAt(n)) && (!n || BREAK.test(low.charAt(n - 1)))) {
        at.push(n);
        letters += low.charAt(n);
      }
    }
    i = letters.indexOf(q);
    if (i >= 0) return { tier: i ? 4 : 1, at: at[i], marks: at.slice(i, i + q.length) };
    for (n = 0, k = 0; n < low.length && k < q.length; n++) {
      if (low.charAt(n) === q.charAt(k)) { marks.push(n); k++; }
    }
    return k < q.length ? null : { tier: 5, at: marks[0], marks: marks };
  }

  function score(query, text) {
    var words = query.split(/\s+/), tier = 0, at = 1e9, marks = [];
    for (var i = 0; i < words.length; i++) {
      var hit = match(words[i], text);
      if (!hit) return null;
      tier = Math.max(tier, hit.tier);
      at = Math.min(at, hit.at);
      marks = marks.concat(hit.marks);
    }
    return { tier: tier, at: at, len: text.length, marks: marks };
  }

  /* ------------------------------------------------------------- sources */

  /* Which document we are in, read from the shape of the path, so it works
     from a domain root, a subdirectory or a folder on a disk. */
  var opened = /\/d\/([^/]+)\//.exec(window.location.pathname);
  var here = opened ? opened[1] : '';

  function standing() {
    var out = [], home = doc.querySelector('.masthead__title a');
    if (home) out.push({ t: home.textContent.trim(), s: sr.t('js.palette.front'), u: home.getAttribute('href') });
    doc.querySelectorAll('.masthead__nav a[href]').forEach(function (a) {
      var href = a.getAttribute('href');
      if (href.charAt(0) !== '#') out.push({ t: a.textContent.trim(), s: sr.t('js.palette.section'), u: href });
    });
    return out;
  }

  /* Two files, because neither is a superset of the other. manifest.json has
     the gaps in a document's numbering and the pages counted behind each
     exemption code; data/docs.json has the control number of *every* page and
     the plain English for each code. Both are wanted here and neither list can
     be derived from the other.

     The cost, on the demo: 1,949 bytes of manifest (763 gzipped) plus 672 of
     docs.json (406). Both are fetched once, lazily, on the reader's first
     Ctrl-K rather than on page load, and in parallel, so the second is not a
     second round trip; cite.js shares the manifest through `sr.json`'s cache
     and the service worker precaches docs.json. Taking docs.json alone would
     save the larger file and cost the gap rows and the per-code counts - a
     working feature for 763 gzipped bytes that are on nobody's critical
     path. */
  function sources(manifest, docs) {
    var out = standing();
    var legend = (docs && docs._legend) || {};
    (manifest.documents || []).forEach(function (d) {
      var url = ROOT + 'd/' + d.id + '/index.html';
      out.push({ t: d.title, s: sr.t('count.pages', { count: d.pages }),
        u: url, id: d.id, n: d.pages });
      if (d.bates_prefix) {
        out.push({ t: d.bates_prefix + '…',
          s: sr.t('js.palette.control_numbers', { title: d.title }), u: url });
      }
      /* A gap in the numbering is pages withheld in full - often the thing a
         reader came for - and the document page is where it is explained. */
      (d.bates_gaps || []).forEach(function (g) {
        out.push({ t: g[0] === g[1] ? g[0] : g[0] + '–' + g[1],
          s: sr.t('js.palette.gap', { title: d.title }), u: url });
      });
    });
    /* "b(5)" is not an answer to anybody's question, so the gloss is shown
       beside it rather than left on the section this row links to. The count
       stays in front because it is short and so never falls off the end of a
       sub-line that ellipsises; the whole string reaches a screen reader
       through the row's aria-label regardless. A code the build does not know
       is published with a gloss saying so, so none ever stands alone. */
    var codes = (manifest.stats && manifest.stats.exemption_counts) || {};
    Object.keys(codes).forEach(function (code) {
      var pages = sr.t('count.pages', { count: codes[code] });
      out.push({ t: code,
        s: legend[code]
          ? sr.t('js.palette.exemption_glossed', { pages: pages, label: legend[code] })
          : sr.t('js.palette.exemption', { pages: pages }),
        u: ROOT + 'withheld/index.html' });
    });
    return out;
  }

  function load() {
    if (!loading) {
      /* The manifest is required, docs.json is not: a site built before it
         carried stamps still gets every row this file drew before. */
      loading = Promise.all([
        sr.json('manifest.json'),
        sr.json('data/docs.json')['catch'](function () { return null; })
      ]).then(function (both) {
        numbered = both[1] || {};
        all = sources(both[0], both[1]);
      }, function () { all = standing(); });
    }
    return loading;
  }

  /* Resolving a control number is a lookup, not a search: nobody types "oca7"
     meaning OCA-2018-04412-000007, and the scattered-characters tier would
     answer four keystrokes with forty pages. So the stamps stay out of `all`.
     That also keeps the list the scorer walks on every keystroke at one row
     per document rather than one per page - twenty thousand of them, of
     near-identical digits, at the ceiling docs.json is sized for. Measured on
     a synthetic 500-document, 20,000-page collection: scoring them all costs
     28-34 ms per keystroke, this costs 0.04-0.10 ms.

     `bp` is why this can be cheap: the informative half of a stamp is the
     counter after a prefix every page of a production shares, so the prefix is
     tested once per document and the counters are read only for a document the
     query has already typed its way into.

     Until docs.json carried these, only the open document's numbers resolved -
     they were read off the thumbnails on screen - so a number copied out of a
     footnote found its page only if the reader had already guessed which
     document it was in. */
  function stamps(q) {
    var out = [];
    if (q.length < 3) return out;
    Object.keys(numbered).forEach(function (id) {
      var entry = numbered[id], list = entry.b;
      if (id.charAt(0) === '_' || !list || out.length >= 6) return;
      var prefix = String(entry.bp || '').toLowerCase();
      if (q.indexOf(prefix) !== 0 || q.length <= prefix.length) return;
      var rest = q.slice(prefix.length);
      for (var i = 0; i < list.length && out.length < 6; i++) {
        if (list[i] && list[i].toLowerCase().indexOf(rest) === 0) {
          out.push({ t: (entry.bp || '') + list[i],
            s: sr.t('js.palette.stamp_page', { number: i + 1, title: entry.t }),
            u: ROOT + 'd/' + id + '/p/' + (i + 1) + '/index.html' });
        }
      }
    });
    return out;
  }

  /* "47", "p 47" and "page 47" mean page 47 of the document you are in;
     "memo 47" means page 47 of the document called memo. Two things this is
     careful not to do: offer a page past the end of a document, because it is
     not there, and read a control number as a page reference - OCA000004 is
     not page 4 of everything whose title happens to contain an o, a c and an
     a, so the leading zero rules it out and the words in front of the number
     have to match a title properly rather than merely be scattered in it. */
  function pages(query) {
    var m = /^(.*?)\s*(?:p|pg|page)?\s*\.?\s*([1-9]\d{0,5})$/i.exec(query);
    var want = m ? parseInt(m[2], 10) : 0;
    if (!want) return [];
    var rest = m[1].trim(), out = [];
    (all || []).forEach(function (d) {
      if (!d.id || want > d.n) return;
      if (rest) {
        var hit = score(rest, d.t);
        if (!hit || hit.tier > 3) return;
      } else if (here && d.id !== here) return;
      out.push({ t: sr.t('page.n', { number: want }), s: d.t,
        u: ROOT + 'd/' + d.id + '/p/' + want + '/index.html' });
    });
    return out.slice(0, 6);
  }

  /* ------------------------------------------------------------ the list */

  function recents() {
    try { return JSON.parse(sr.read('recent') || '[]').slice(0, KEEP); } catch (e) { return []; }
  }

  /* Every URL this file builds is relative to the page it was built on, which
     is what lets the archive work in a subdirectory, in a zip, on a stick. A
     recent destination outlives that page, so it is stored with the way back
     to the root taken off and put on again wherever it is next shown -
     otherwise a document remembered from the front page becomes
     d/x/p/3/d/y/index.html the next time it is offered from inside one. */
  function keep(url) {
    return url.indexOf(ROOT) === 0 ? url.slice(ROOT.length) : url;
  }

  function remember(entry) {
    if (!entry) return;
    var url = keep(entry.u);
    var kept = recents().filter(function (e) { return e.u !== url; });
    kept.unshift({ t: entry.t, s: entry.s || '', u: url });
    sr.write('recent', JSON.stringify(kept.slice(0, KEEP)));
  }

  /* Titles come out of somebody's PDF, so they are treated as text: escaped,
     then the marks put back - the rule search.js follows for an excerpt. */
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function marked(text, marks) {
    var on = {}, out = '', at = 0, from;
    (marks || []).forEach(function (m) { on[m] = 1; });
    while (at < text.length) {
      for (from = at; at < text.length && !on[at]; at++);
      out += esc(text.slice(from, at));
      for (from = at; at < text.length && on[at]; at++);
      if (at > from) out += '<mark>' + esc(text.slice(from, at)) + '</mark>';
    }
    return out;
  }

  function render(query) {
    var q = query.trim().toLowerCase(), found;

    if (!q) {
      /* An empty box is a wasted question: it offers what this reader came
         back to, then the pages every collection has. */
      var seen = {};
      found = recents().map(function (e) {
        seen[ROOT + e.u] = 1;
        return { t: e.t,
          s: e.s ? sr.t('js.palette.recent_of', { what: e.s }) : sr.t('js.palette.recent'),
          u: ROOT + e.u };
      });
      standing().forEach(function (e) { if (!seen[e.u]) found.push(e); });
    } else {
      found = [];
      (all || standing()).forEach(function (e) {
        var s = score(q, e.t);
        if (s) found.push({ t: e.t, s: e.s, u: e.u, marks: s.marks, r: s });
      });
      found.sort(function (a, b) {
        return (a.r.tier - b.r.tier) || (a.r.len - b.r.len) || (a.r.at - b.r.at) ||
               (a.t < b.t ? -1 : 1);
      });
      found = stamps(q).concat(pages(q), found);
    }

    rows = found.slice(0, MAX);
    list.innerHTML = rows.map(function (e, i) {
      /* Marks explain the match to a reader who can see them; to everyone else
         they are noise inside a name, so the row is named in plain words. */
      return '<a class="pal__row" id="pal-r' + i + '" role="option" tabindex="-1" href="' +
        esc(e.u) + '" aria-label="' + esc(e.t + (e.s ? ', ' + e.s : '')) + '">' +
        '<span class="pal__name">' + marked(e.t, e.marks) + '</span>' +
        (e.s ? '<span class="pal__sub">' + esc(e.s) + '</span>' : '') + '</a>';
    }).join('');

    /* The count is not announced: it would fire on every keystroke and read the
       list back each time, and the active row already announces itself through
       aria-activedescendant. What a listbox cannot say for itself is that it is
       empty, so this line does - and only when the words change, so it is never
       said twice. */
    var said = !rows.length ? sr.t('js.palette.nothing')
      : found.length > MAX
        ? sr.t('js.palette.capped', { shown: MAX, count: found.length })
        : '';
    if (note.textContent !== said) note.textContent = said;
    select(rows.length ? 0 : -1);
  }

  function select(i) {
    var was = list.querySelector('[aria-selected="true"]');
    if (was) was.removeAttribute('aria-selected');
    active = i;
    var row = i < 0 ? null : doc.getElementById('pal-r' + i);
    if (!row) { input.removeAttribute('aria-activedescendant'); return; }
    row.setAttribute('aria-selected', 'true');
    input.setAttribute('aria-activedescendant', row.id);
    row.scrollIntoView({ block: 'nearest' });
  }

  /* --------------------------------------------------------------- shell */

  function build() {
    dialog = sr.el('dialog', 'pal');
    dialog.id = 'palette';
    dialog.setAttribute('aria-label', sr.t('js.palette.label'));
    dialog.innerHTML =
      '<div class="pal__box"><input class="pal__input" id="pal-q" type="text"' +
      ' autocomplete="off" spellcheck="false" role="combobox" aria-expanded="true"' +
      ' aria-controls="pal-list" aria-autocomplete="list"' +
      ' placeholder="' + esc(sr.t('js.palette.placeholder')) + '">' +
      '<div class="pal__list" id="pal-list" role="listbox" aria-label="' +
      esc(sr.t('js.palette.destinations')) + '"></div>' +
      '<p class="pal__note" id="pal-note" role="status" aria-live="polite"></p>' +
      '<p class="pal__keys" aria-hidden="true">' + sr.t('js.palette.keys_html') + '</p></div>';
    doc.body.appendChild(dialog);
    input = doc.getElementById('pal-q');
    list = doc.getElementById('pal-list');
    note = doc.getElementById('pal-note');

    input.addEventListener('input', function () { render(input.value); });
    list.addEventListener('click', function (ev) {
      var row = ev.target.closest('.pal__row');
      if (row) remember(rows[parseInt(row.id.slice(5), 10)]);   /* then the link does its job */
    });

    /* Bound to the dialog, not the document, so every key pressed in here stops
       here: the viewer is also listening for j, k and Escape on the document
       and must not hear them while this is open. */
    dialog.addEventListener('keydown', function (ev) {
      ev.stopPropagation();
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (rows.length) select((active + (ev.key === 'ArrowUp' ? -1 : 1) + rows.length) % rows.length);
      } else if (ev.key === 'Enter' && active >= 0) {
        ev.preventDefault();
        remember(rows[active]);
        window.location.href = doc.getElementById('pal-r' + active).href;
      }
    });

    dialog.addEventListener('click', function (ev) {
      if (ev.target === dialog) dialog.close();     /* the backdrop is the dialog */
    });
    dialog.addEventListener('close', function () {
      doc.documentElement.classList.remove('is-modal');
    });
  }

  function open() {
    if (!dialog) build();
    if (!all) load().then(function () { if (dialog.open) render(input.value); });
    /* The page behind a modal dialog still scrolls, and an archive moving under
       a list of destinations is disorienting. The lock is in the stylesheet,
       where scrollbar-gutter keeps it from jumping sideways. */
    doc.documentElement.classList.add('is-modal');
    input.value = '';
    dialog.showModal();     /* focus, Escape and returning focus are the browser's */
    render('');
  }

  /* ------------------------------------------------------------ shortcut */

  var LABEL = /Mac|iPhone|iPad/.test(window.navigator.platform || '') ? '⌘K' : 'Ctrl K';

  /* The viewer owns "/" wherever it is loaded and sends it to the search page.
     Two answers to one key is worse than one, so the palette takes "/" only on
     the pages where nothing has claimed it. */
  var slashFree = !doc.querySelector('script[src$="viewer.js"]') && !doc.getElementById('q');

  doc.addEventListener('keydown', function (ev) {
    if (doc.querySelector('dialog[open]')) return;
    var el = doc.activeElement;
    var typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ||
                        el.tagName === 'SELECT' || el.isContentEditable);
    if ((ev.metaKey || ev.ctrlKey) && !ev.altKey && (ev.key === 'k' || ev.key === 'K')) {
      ev.preventDefault();          /* otherwise both are the browser's address bar */
      open();
    } else if (ev.key === '/' && slashFree && !typing && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      ev.preventDefault();
      open();
    }
  });

  /* A shortcut nobody can see is one most people never find, so the palette has
     a control in the masthead and the control is labelled with the key. */
  var nav = doc.querySelector('.masthead__nav');
  if (nav) {
    var button = sr.el('button', 'mh-btn', LABEL);
    button.type = 'button';
    button.setAttribute('aria-label', sr.t('js.palette.button_label'));
    button.addEventListener('click', open);
    nav.insertBefore(button, nav.querySelector('.mh-btn'));
  }
})();
