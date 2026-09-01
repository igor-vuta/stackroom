/* Stackroom reader preferences.
 *
 * The one script here that is not deferred, for one reason: the theme has to
 * be on <html> before the first pixel is painted, or the reader gets a flash
 * of the wrong one, and the content policy allows no inline script. So this
 * file is loaded synchronously from <head>, after the stylesheet the paint was
 * waiting for anyway. Everything else it does waits for the DOM.
 *
 * Running first is the one thing it is guaranteed to do, so the few helpers
 * all three of these files share live here. Deliberately not window.Stackroom,
 * which viewer.js owns and which loads later.
 *
 * Running first is also why the interface's own language is read here. The
 * build writes assets/i18n.js - one file, generated from the catalogue named
 * by `language` in stackroom.toml - and the page shell loads it in the head
 * immediately before this file. So by the time any deferred script runs,
 * sr.t(), sr.n() and sr.pct() exist and answer in the archive's language.
 * Nothing is fetched, nothing is translated in the browser: the sentences
 * arrived already written, and what happens here is choosing a plural form and
 * putting numbers into slots.
 */
(function () {
  'use strict';

  var doc = document, root = doc.documentElement, cache = {}, prefix = null;

  /* --------------------------------------------------------- the language */

  /* What assets/i18n.js left behind, or an empty catalogue. An empty one is
     not a failure worth an exception: every string comes back as its own key
     in brackets, which is ugly, obvious, and still a page a reader can use. */
  var CAT = window.stackroomMessages || {};
  var MSG = CAT.messages || {};
  var NUM = CAT.number || { group: ',', decimal: '.', min: 1, percent: '{n}%' };
  var PLURAL = CAT.plural || { c: ['other'], t: '', x: {} };

  /* One warning, on load, naming what a translator has not got to yet. Loud
     enough that a contributor working on a catalogue sees it the first time
     they open the site they built; quiet enough that a reader never does,
     because what is on the page is a sentence that reads - the English one,
     substituted at build time rather than here. */
  if (CAT.fell_back && CAT.fell_back.length && window.console && console.warn) {
    console.warn('Stackroom: ' + CAT.fell_back.length + ' interface string(s) are ' +
      'missing from the ' + CAT.locale + ' catalogue and were published in English: ' +
      CAT.fell_back.join(', ') + '\nRun: python -m stackroom.i18n check ' + CAT.locale);
  }

  /* The plural rule, run off the table the build generated from the Python
     one. Six lines and no second implementation to drift: `t` is the category
     for every value of i % 100 and `x` is the handful of exact small values
     that do not follow their own residue - English's 1, which is `one` where
     101 is not. Russian needs no exceptions at all, because 1, 21, 101 and
     1001 really are the same form.

     Counts here are whole numbers - files, pages, matches, words - and a
     fractional one is floored rather than guessed at. */
  function category(n) {
    var i = Math.floor(Math.abs(Number(n) || 0));
    var k = PLURAL.x ? PLURAL.x[i] : undefined;
    if (k === undefined) k = parseInt(PLURAL.t.charAt(i % 100), 10);
    return PLURAL.c[k] || 'other';
  }

  function esc(text) {
    return String(text).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }

  /* Intl decides where the groups fall; the catalogue decides which characters
     go in them and when grouping starts. Measured in Chromium 141, Intl and
     this project's catalogues disagree about both: Intl.NumberFormat('fr')
     separates with U+202F where locales/fr.json says U+00A0, and
     Intl.NumberFormat('pl') applies CLDR's own minimumGroupingDigits rather
     than the catalogue's, writing 1234 where a catalogue asking for grouping
     from four digits wants 1 234. An unknown code does not even raise: 'xx'
     resolves to en-US and groups with commas. So the page and the script would
     write the same number two ways, on the same page, silently. */
  var grouper = null;
  function intl() {
    if (grouper === null) {
      try {
        grouper = new Intl.NumberFormat(CAT.locale || 'en',
          { useGrouping: true, maximumFractionDigits: 20 });
      } catch (e) { grouper = false; }
    }
    return grouper;
  }

  function num(value, digits) {
    var v = Number(value);
    if (!isFinite(v)) return String(value);
    var text = digits === undefined || digits === null ? null : v.toFixed(digits);
    var whole, frac = '', sign = v < 0 ? '-' : '';
    var f = intl();
    if (f && text === null) {
      var parts;
      try { parts = f.formatToParts(Math.abs(v)); } catch (e) { parts = null; }
      if (parts) {
        whole = ''; 
        for (var i = 0; i < parts.length; i++) {
          if (parts[i].type === 'group') whole += NUM.group;
          else if (parts[i].type === 'decimal') whole += NUM.decimal;
          else whole += parts[i].value;
        }
        return sign + trimGroups(whole);
      }
    }
    /* No Intl, or a fixed number of decimals, which Intl would round for us
       but which toFixed has already done. Grouping a run of digits from the
       right is the whole of what is left. */
    if (text === null) text = String(Math.abs(v));
    else text = text.replace(/^-/, '');
    var dot = text.indexOf('.');
    if (dot >= 0) { frac = text.slice(dot + 1); text = text.slice(0, dot); }
    whole = text;
    if (whole.length >= 3 + (NUM.min || 1)) {
      var chunks = [];
      while (whole.length > 3) { chunks.unshift(whole.slice(-3)); whole = whole.slice(0, -3); }
      chunks.unshift(whole);
      whole = chunks.join(NUM.group);
    }
    return sign + whole + (frac ? NUM.decimal + frac : '');
  }

  /* Intl groups from four digits; a catalogue may ask for five, which is what
     Spanish wants - 1000, but 10.000. One separator in a four-digit number is
     the only case, so it is taken out again rather than asking Intl for an
     option not every engine has. */
  function trimGroups(text) {
    if ((NUM.min || 1) < 2) return text;
    var digits = text.replace(/\D/g, '').length;
    return digits <= 4 ? text.split(NUM.group).join('') : text;
  }

  var FIELD = /\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

  /* The same substitution the Python side does, and the same two rules: a key
     ending in _html carries markup and has its parameters escaped, every other
     key is plain text whose caller writes it with textContent; and a
     placeholder with no argument is left visible rather than silently dropped,
     because a hole in a sentence is a bug somebody has to be able to see. */
  function translate(key, params) {
    var message = MSG[key];
    if (message === undefined) return '[' + key + ']';
    var text = message;
    if (typeof message === 'object') {
      var count = params && params.count;
      if (count === undefined || count === null) return '[' + key + ': no count]';
      text = message[category(count)] || message.other || '';
    }
    var html = /_html$/.test(key);
    return String(text).replace(FIELD, function (whole, name) {
      if (whole === '{{') return '{';
      if (whole === '}}') return '}';
      if (!params || !(name in params)) return whole;
      var value = params[name];
      var out = typeof value === 'number' ? num(value) : String(value);
      return html ? esc(out) : out;
    });
  }

  /* localStorage throws in a private window, in a sandboxed frame, and where
     the reader has switched site data off. No preference is worth an
     exception: every touch is wrapped, and failure means the defaults. */
  function read(key) {
    try { return window.localStorage.getItem('stackroom.' + key); } catch (e) { return null; }
  }

  function write(key, value) {
    try {
      if (value === null) window.localStorage.removeItem('stackroom.' + key);
      else window.localStorage.setItem('stackroom.' + key, value);
    } catch (e) { /* nothing is lost the reader cannot set again */ }
  }

  /* Three steps and all of them up. The layout is in rem and clamp(), so the
     root size carries the page with it, and nobody reading a photocopy has
     ever wanted the transcription smaller. */
  var SIZES = { normal: '', large: '112.5%', largest: '125%' };

  function apply() {
    var theme = read('theme');
    if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
    else root.removeAttribute('data-theme');
    root.style.fontSize = SIZES[read('size')] || '';
  }

  apply();
  root.classList.add('js');   /* what must not exist until scripting does */

  var sr = window.stackroomReader = {
    read: read,
    write: write,

    /* The interface's language, for anything that needs to know rather than
       just to say something: the citation panel picks a date format with it,
       and the tests read it back. */
    locale: CAT.locale || 'en',
    dir: CAT.dir || 'ltr',

    t: translate,
    n: num,
    plural: category,
    esc: esc,

    /* A percentage, with the sign where this language puts it and the space
       before it that Russian, Ukrainian, French and Spanish all want. */
    pct: function (value, digits, ofOne) {
      var share = ofOne ? value * 100 : value;
      return (NUM.percent || '{n}%').replace('{n}', num(share, digits || 0));
    },

    el: function (tag, cls, text) {
      var node = doc.createElement(tag);
      if (cls) node.className = cls;
      if (text) node.textContent = text;
      return node;
    },

    /* Radios, not buttons pretending to be radios: arrow keys, a group label
       and the checked state all arrive with them. */
    radios: function (name, options) {
      var now = read(name) || options[0][0];
      return options.map(function (o) {
        return '<label class="opt"><input type="radio" name="sr-' + name + '" value="' +
          o[0] + '"' + (o[0] === now ? ' checked' : '') + '> ' + esc(o[1]) + '</label>';
      }).join('');
    },

    /* Every link here is relative, so the way back to the root is whatever the
       masthead's own link home is: nothing hard-coded, and the site keeps
       working in a subdirectory, in a zip, on a stick. */
    prefix: function () {
      if (prefix === null) {
        var home = doc.querySelector('.masthead__title a');
        prefix = home ? home.getAttribute('href').replace(/index\.html$/, '') : '';
      }
      return prefix;
    },

    json: function (path) {
      if (!cache[path]) {
        cache[path] = fetch(sr.prefix() + path).then(function (r) {
          if (!r.ok) throw new Error(path);
          return r.json();
        });
      }
      return cache[path];
    }
  };

  /* ----------------------------------------------------------- the panel */

  /* Two settings and a way out. A preferences panel is where superfluity
     hides, so there is no font switcher (the reading face is a decision of
     this design), no line height, no density and no motion toggle: the
     stylesheet honours prefers-reduced-motion and prefers-contrast from the
     system, which is where a reader sets those once, for everything. */
  function panel(nav) {
    var button = sr.el('button', 'mh-btn pref__open', translate('js.prefs.open'));
    button.type = 'button';
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', 'pref-panel');
    button.setAttribute('aria-label', translate('js.prefs.label'));

    var sheet = sr.el('div', 'pref');
    sheet.id = 'pref-panel';
    sheet.hidden = true;
    sheet.innerHTML =
      '<fieldset class="pref__set"><legend>' + esc(translate('js.prefs.theme')) +
      '</legend>' +
      sr.radios('theme', [['system', translate('js.prefs.theme_system')],
                          ['light', translate('js.prefs.theme_light')],
                          ['dark', translate('js.prefs.theme_dark')]]) +
      '</fieldset><fieldset class="pref__set"><legend>' +
      esc(translate('js.prefs.size')) + '</legend>' +
      sr.radios('size', [['normal', translate('js.prefs.size_normal')],
                         ['large', translate('js.prefs.size_large')],
                         ['largest', translate('js.prefs.size_largest')]]) +
      '</fieldset><p class="pref__note">' + esc(translate('js.prefs.note')) + ' ' +
      '<button type="button" class="pref__forget">' +
      esc(translate('js.prefs.forget')) + '</button></p>';

    nav.appendChild(button);
    nav.appendChild(sheet);

    function show(open) {
      sheet.hidden = !open;
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    button.addEventListener('click', function () {
      show(sheet.hidden);
      if (!sheet.hidden) sheet.querySelector('input').focus();
    });

    sheet.addEventListener('change', function (ev) {
      if (ev.target.name) { write(ev.target.name.slice(3), ev.target.value); apply(); }
    });

    sheet.addEventListener('click', function (ev) {
      if (!ev.target.classList.contains('pref__forget')) return;
      try {
        Object.keys(window.localStorage).forEach(function (k) {
          if (k.indexOf('stackroom.') === 0) window.localStorage.removeItem(k);
        });
      } catch (e) { /* there was nothing to forget */ }
      apply();
      sheet.querySelectorAll('input').forEach(function (i) {
        i.checked = i.value === 'system' || i.value === 'normal';
      });
      var note = doc.querySelector('.resume');
      if (note) note.remove();
    });

    /* Escape puts focus back on the control that opened it: a reader who
       closes a panel from the keyboard should not land at the top of the page. */
    doc.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !sheet.hidden) { show(false); button.focus(); }
    });
    doc.addEventListener('click', function (ev) {
      if (!sheet.hidden && !sheet.contains(ev.target) && ev.target !== button) show(false);
    });
  }

  /* ------------------------------------------------------ where you were */

  /* Per document, for a month. On a page, remember; on the document's own
     page, offer - in the flow, once. Not a banner that follows the reader, not
     a dialog that interrupts them, and not a live region: they will reach it
     on the way to the pages. */
  function position() {
    var page = /\/d\/([^/]+)\/p\/(\d+)\//.exec(window.location.pathname);
    if (page) { write('read.' + page[1], page[2] + ':' + Date.now()); return; }

    var grid = doc.querySelector('.thumbs');
    var open = /\/d\/([^/]+)\//.exec(window.location.pathname);
    if (!grid || !open) return;

    var saved = (read('read.' + open[1]) || '').split(':'), n = parseInt(saved[0], 10);
    if (!(n > 1) || Date.now() - parseInt(saved[1], 10) > 30 * 864e5) {
      if (saved[1]) write('read.' + open[1], null);
      return;
    }

    var note = sr.el('p', 'resume');
    note.innerHTML = translate('js.resume_html', { href: 'p/' + n + '/index.html', number: n }) +
      ' <button type="button" class="resume__forget">' +
      esc(translate('js.resume.forget')) + '</button>';
    note.querySelector('button').addEventListener('click', function () {
      write('read.' + open[1], null);
      note.remove();
    });
    var before = doc.querySelector('#main .section-title') || grid;
    before.parentNode.insertBefore(note, before);
  }

  /* ------------------------------------------------------------ wiring */

  function init() {
    var nav = doc.querySelector('.masthead__nav');
    if (nav) panel(nav);
    try { position(); } catch (e) { /* never fatal */ }
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
  else init();
})();
