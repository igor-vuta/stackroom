/* Stackroom search.
 *
 * The index is ~200 KB of WebAssembly and metadata. It is not loaded when the
 * page loads; it is loaded the first time someone puts the cursor in the box.
 * A reader who came to browse never pays for it.
 *
 * Two honesty rules are enforced here rather than left to the reader:
 *
 *   - Very short queries are refused. Latency tracks the number of *hits*, not
 *     the size of the archive - 3 ms at 59 hits, 3.2 s at 20,000 - so "a"
 *     would not be a fast search of everything, it would be a frozen tab.
 *
 *   - When some pages could not be read, the result count says so. A search
 *     that quietly omits 200 unreadable pages tells the reader the phrase is
 *     not in the archive, which may be false and is the worst thing a search
 *     over public records can do.
 */
(function () {
  'use strict';

  var doc = document;
  /* Every sentence below arrives already written, in the archive's own
     language, from assets/i18n.js by way of prefs.js - which is loaded in the
     head of every page and so has always run by the time this deferred file
     does. The fallback is only for a page that somehow has no prefs.js: it
     keeps this file from throwing, and puts the key on screen where somebody
     will notice it. */
  var sr = window.stackroomReader || {
    t: function (k) { return '[' + k + ']'; },
    n: function (v) { return String(v); }
  };
  var cfg = {};
  try { cfg = JSON.parse(doc.getElementById('search-config').textContent); } catch (e) { cfg = {}; }

  var root = cfg.root || '';
  var minQuery = cfg.minQuery || 2;
  var input = doc.getElementById('q');
  var status = doc.getElementById('status');
  var list = doc.getElementById('results');
  var caveats = doc.getElementById('caveats');
  var caveatText = doc.getElementById('caveat-text');

  if (!input || !status || !list) return;

  var pagefind = null;
  var loading = null;
  var docs = null;
  var lastQuery = '';
  var run = 0;

  /* ------------------------------------------------------------ loading */

  function loadIndex() {
    if (loading) return loading;
    status.textContent = sr.t('js.search.loading');
    loading = Promise.all([
      import(root + '_pagefind/pagefind.js').then(function (mod) {
        pagefind = mod;
        return mod.options ? mod.options({ excerptLength: 24 }) : null;
      }),
      fetch(root + 'data/docs.json')
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { docs = j || {}; })
        .catch(function () { docs = {}; })
    ]).then(function () {
      status.textContent = '';
    }).catch(function (err) {
      status.textContent = sr.t('js.search.unavailable');
      loading = null;
      throw err;
    });
    return loading;
  }

  input.addEventListener('focus', function () { loadIndex().catch(function () {}); }, { once: true });

  /* ------------------------------------------------------------ querying */

  var timer = null;
  input.addEventListener('input', function () {
    window.clearTimeout(timer);
    timer = window.setTimeout(search, 140);
  });

  input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { window.clearTimeout(timer); search(); }
  });

  function search() {
    var q = input.value.trim();
    if (q === lastQuery) return;
    lastQuery = q;
    setHash(q);

    if (!q) { list.innerHTML = ''; status.textContent = ''; hideCaveats(); return; }
    if (q.length < minQuery) {
      list.innerHTML = '';
      status.textContent = sr.t('js.search.too_short', { count: minQuery });
      return;
    }

    var mine = ++run;
    loadIndex().then(function () {
      if (mine !== run) return;
      status.textContent = sr.t('js.search.searching');
      return pagefind.search(q);
    }).then(function (result) {
      if (!result || mine !== run) return;
      return render(result, q, mine);
    }).catch(function () {
      if (mine === run) status.textContent = sr.t('js.search.failed');
    });
  }

  var PAGE_SIZE = 20;

  function render(result, q, mine) {
    var total = result.results.length;
    list.innerHTML = '';

    if (!total) {
      status.textContent = '';
      list.innerHTML = '';
      var p = doc.createElement('li');
      p.className = 'empty';
      p.textContent = emptyMessage(q);
      list.appendChild(p);
      showCaveats();
      return;
    }

    /* aria-live goes on the count and nothing else. Put it on the list and a
       screen reader re-reads every result on every keystroke. */
    status.textContent = sr.t('js.search.hits', { count: total, query: q });
    showCaveats();

    return Promise.all(result.results.slice(0, PAGE_SIZE).map(function (r) {
      return r.data();
    })).then(function (items) {
      if (mine !== run) return;
      items.forEach(function (item, i) {
        list.appendChild(row(item, result.results[i]));
      });
      if (total > PAGE_SIZE) {
        var more = doc.createElement('li');
        more.className = 'empty';
        more.textContent = sr.t('js.search.more', { shown: PAGE_SIZE, count: total });
        list.appendChild(more);
      }
    });
  }

  /* Pagefind reports the URL of the file it indexed, rooted at the site. We
     rebuild it relative to wherever this page happens to be, so the archive
     works in a subdirectory and from a folder on disk. */
  function locate(url) {
    return root + String(url).replace(/^\/+/, '');
  }

  function parse(url) {
    var m = /d\/([^/]+)\/p\/(\d+)\//.exec(url);
    return m ? { slug: m[1], page: parseInt(m[2], 10) } : null;
  }

  function row(item, stub) {
    var li = doc.createElement('li');
    li.className = 'result';

    var where = parse(item.url);
    var positions = (stub && stub.words) || item.locations || [];
    var href = locate(item.url);
    if (positions.length) href += '#w=' + positions.slice(0, 12).join(',');

    if (where) {
      var img = doc.createElement('img');
      img.className = 'result__thumb';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = '';
      img.src = root + 'media/' + where.slug + '/p' + pad(where.page) + '@thumb.webp';
      img.addEventListener('error', function () { img.style.visibility = 'hidden'; });
      li.appendChild(img);
    } else {
      li.appendChild(doc.createElement('span'));
    }

    var body = doc.createElement('div');

    var head = doc.createElement('p');
    head.className = 'result__where';
    var link = doc.createElement('a');
    link.href = href;
    link.textContent = where
      ? sr.t('js.search.result_where', { title: title(where.slug), number: where.page })
      : (item.meta && item.meta.title) || item.url;
    head.appendChild(link);
    body.appendChild(head);

    var excerpt = doc.createElement('p');
    excerpt.className = 'result__excerpt';
    excerpt.innerHTML = sanitise(item.excerpt || '');
    body.appendChild(excerpt);

    li.appendChild(body);
    return li;
  }

  function title(slug) {
    return (docs && docs[slug] && docs[slug].t) || slug;
  }

  function pad(n) {
    return String(n).padStart ? String(n).padStart(4, '0') : ('0000' + n).slice(-4);
  }

  /* Pagefind's excerpt is HTML containing <mark> and the document's own text.
     That text came out of somebody's PDF, so it is treated as text: everything
     is escaped, and the marks are put back afterwards. */
  function sanitise(html) {
    var escaped = String(html)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return escaped
      .replace(/&lt;mark&gt;/g, '<mark>')
      .replace(/&lt;\/mark&gt;/g, '</mark>');
  }

  /* -------------------------------------------------------------- honesty */

  /* Three sentences, each of which frames the one before it. They are nested
     rather than concatenated so that a language which puts the caveat first
     can: the frame owns the join, not this file. */
  function emptyMessage(q) {
    var base = sr.t('js.search.none', { count: cfg.pages || 0, query: q });
    if (cfg.unreadablePages) {
      base = sr.t('js.search.none_unreadable', { none: base, count: cfg.unreadablePages });
    }
    if (/["“”]/.test(q)) base = sr.t('js.search.none_quoted', { none: base });
    return base;
  }

  /* `indexedPages` is what pagefind reported it took, written by
     `build_search`. Deriving the same number as `pages - unreadablePages`
     worked, but it made this file responsible for a subtraction the build has
     already done - and the count above the results is the one sentence on this
     page whose whole job is not to flatter the archive. */
  function showCaveats() {
    if (!caveats || !caveatText || !cfg.unreadablePages) return;
    caveatText.textContent = sr.t('js.search.caveat', {
      count: cfg.unreadablePages, covered: cfg.indexedPages, total: cfg.pages
    });
    caveats.hidden = false;
  }

  function hideCaveats() { if (caveats) caveats.hidden = true; }

  /* --------------------------------------------------------------- state */

  function setHash(q) {
    var next = q ? '#q=' + encodeURIComponent(q) : ' ';
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, '', window.location.pathname + next);
    }
  }

  function fromHash() {
    var m = /[#&]q=([^&]*)/.exec(window.location.hash || '');
    return m ? decodeURIComponent(m[1].replace(/\+/g, ' ')) : '';
  }

  var initial = fromHash();
  if (initial) { input.value = initial; search(); }
  input.focus({ preventScroll: true });
})();
