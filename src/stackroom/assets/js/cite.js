/* Stackroom citation.
 *
 * A journalist quoting a page has to say where they got it in a form an editor
 * will accept, and be able to show they did not alter it. So every citation
 * here carries a SHA-256 beside the collection, the document, the page, the
 * control number the agency stamped on it, and the day it was read.
 *
 * Which SHA-256 is the whole difficulty, and the test is what an editor
 * actually does: they open the link, download the file, run `shasum -a 256`
 * on it, and expect the number they get to be the number in the citation.
 *
 * With `safety.strip_metadata` on, the file published here is not the file
 * that arrived - it was rewritten from its page tree to drop its author, its
 * producer and every earlier revision an incremental save left behind - so
 * `sha256` and `published_sha256` in manifest.json are two different numbers.
 * A citation that carried `sha256` alone would fail that check on every
 * document in the archive, and the conclusion an editor would draw from a
 * digest that does not match is that the journalist altered the document.
 * That is the worst failure this file could have, and it is silent.
 *
 * So: the digest a citation leads with is always the digest of the file a
 * reader can download from here, and the citation says so. Where the two
 * differ, both are carried and both are labelled - the published one because
 * it is checkable, the source one because it is what the archive can be held
 * to against the agency's own copy, a mirror, or another archive. Where
 * nothing was published (`safety.publish_originals = false`) there is no
 * download to check, and the citation says the digest is the source file's.
 *
 * The labels are messages, not decoration. An unlabelled hexadecimal number in
 * a citation is an assertion nobody can act on.
 */
(function () {
  'use strict';

  var doc = document;
  var sr = window.stackroomReader;
  var at = /\/d\/([^/]+)\/p\/(\d+)\//.exec(window.location.pathname);
  /* The page's own metadata line, not the breadcrumb, which shares its class. */
  var meta = doc.querySelector('#main > .wrap > .doc__meta');
  if (!sr || !at || !meta) return;

  var mac = /Mac|iPhone|iPad/.test(window.navigator.platform || '');
  var facts = null, said, panel, area, note;

  /* The reader's own address, minus the fragment - unless the build was given a
     base_url, in which case the canonical is an absolute URL and is worth more
     than wherever this copy of the archive happens to be mounted right now.
     The test is on the attribute, not on link.href: a canonical is relative
     when there is no base_url, and .href would resolve it against this page
     and hand back the path twice over. */
  function address() {
    var link = doc.querySelector('link[rel="canonical"]');
    return link && /^https?:/i.test(link.getAttribute('href') || '')
      ? link.href.split('#')[0]
      : window.location.origin + window.location.pathname;
  }

  /* Sixteen hex characters, which is what the About page and the comparison
     pages show and what `about.digests_html` tells a reader to compare. A
     prefix of a SHA-256 is checked by looking at the front of what `shasum`
     printed, so a citation and the page it came from must truncate alike. */
  var HEX = 16;

  /* Which digest goes in the citation, and whether there are two of them.

     `published_sha256` is the file in `files/` - the bytes a download hands
     over - and it is null when the build published no originals at all.
     `sha256` is the file as it arrived. `metadata_stripped` says the two were
     always going to differ; the inequality is checked as well, because a
     manifest that says one thing and shows another should not produce a
     citation with the same number printed twice. */
  function digests(record) {
    var published = (record && record.published_sha256) || '';
    var source = (record && record.sha256) || '';
    var split = !!(record && record.metadata_stripped) && !!published && published !== source;
    return {
      sha: (published || source).slice(0, HEX),
      source: split ? source.slice(0, HEX) : ''
    };
  }

  function gather(manifest) {
    var record = null;
    ((manifest && manifest.documents) || []).forEach(function (d) {
      if (d.id === at[1]) record = d;
    });
    var stamp = meta.querySelector('.mono');
    var title = doc.querySelector('.masthead__title a');
    var now = new Date();
    var sums = digests(record);
    return {
      collection: (manifest && manifest.title) || (title ? title.textContent.trim() : ''),
      title: (record && record.title) || at[1],
      n: at[2],
      stamp: stamp ? stamp.textContent.trim() : '',
      sha: sums.sha,
      source: sums.source,
      url: address(),
      /* Fixed locales on purpose: a date that reads differently on the
         reader's machine than in the citation they pasted is a small lie. The
         ISO day is deliberately not localised at all - it is a machine-readable
         date and every citation style that takes one takes it in that order -
         while the spelt-out one follows the archive's own language, because it
         is being read as prose in a sentence written in that language. A
         browser that does not know the code falls back to English rather than
         to something unreadable. */
      day: now.toLocaleDateString('en-CA'),
      spelt: spellDate(now)
    };
  }

  function spellDate(now) {
    var opts = { year: 'numeric', month: 'long', day: 'numeric' };
    try { return now.toLocaleDateString(sr.locale || 'en', opts); }
    catch (e) { return now.toLocaleDateString('en-US', opts); }
  }

  /* Three, because three is what people are asked for: something to paste into
     copy, something to paste into a document written in Markdown, and
     something shaped like a note in a piece of scholarship.

     Each is one message with every slot always filled. The clauses that are
     only sometimes there - the control number, the digest - are messages of
     their own that come back empty when there is nothing to say, so the
     punctuation around them belongs to a translator rather than to this file,
     and the frame stays one sentence they can reorder. */
  function clause(key, name, value) {
    if (!value) return '';
    var params = {};
    params[name] = value;
    return sr.t(key, params);
  }

  /* One clause or the other, never one glued to the other: a sentence built
     out of two half-sentences can only come out in English word order, and a
     translator has to be able to put "as published" and "as it arrived"
     wherever their language wants them. */
  function digestClause(kind, f) {
    if (!f.sha) return '';
    if (f.source) {
      return sr.t(kind === 'markdown' ? 'js.cite.digest_both_markdown' : 'js.cite.digest_both',
                  { sha: f.sha, source: f.source });
    }
    return clause(kind === 'markdown' ? 'js.cite.digest_markdown' : 'js.cite.digest',
                  'sha', f.sha);
  }

  function format(kind, f) {
    if (kind === 'markdown') {
      return sr.t('js.cite.markdown', {
        title: f.title, number: f.n, url: f.url, collection: f.collection,
        control: clause('js.cite.control_semi', 'control', f.stamp),
        digest: digestClause('markdown', f),
        day: f.day
      });
    }
    if (kind === 'note') {
      return sr.t('js.cite.note', {
        title: f.title, number: f.n, url: f.url, collection: f.collection,
        control: clause('js.cite.control_comma', 'control', f.stamp),
        digest: digestClause('note', f),
        spelt: f.spelt
      });
    }
    return sr.t('js.cite.plain', {
      title: f.title, number: f.n, url: f.url, collection: f.collection,
      control: clause('js.cite.control_paren', 'control', f.stamp),
      digest: digestClause('plain', f),
      day: f.day
    });
  }

  function load() {
    if (!facts) {
      facts = sr.json('manifest.json').then(gather, function () { return gather(null); });
    }
    return facts;
  }

  function say(words) {
    if (said.textContent !== words) said.textContent = words;
  }

  function show(open) {
    panel.hidden = !open;
    opener.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function fill() {
    return load().then(function (f) {
      var picked = panel.querySelector('input:checked');
      var kind = picked ? picked.value : 'plain';
      sr.write('cite', kind);
      area.value = format(kind, f);
      /* Said here rather than in the panel's markup because it is a fact about
         this document, and the manifest that carries it has not been read when
         the panel is built. A citation with two numbers in it and no sentence
         saying why is worse than one number. */
      note.textContent = sr.t(f.source ? 'js.cite.foot_stripped' : 'js.cite.foot');
    });
  }

  /* Refused permission, or an insecure origin - an archive read off a stick is
     served from file:// and has no clipboard at all. Selecting the text is not
     a consolation prize, it is what the reader would have done anyway. */
  function manual(text) {
    show(true);
    area.value = text;
    area.focus();
    area.select();
    say(sr.t('js.cite.manual', { keys: mac ? '⌘C' : 'Ctrl C' }));
  }

  function copy(text) {
    var clip = window.navigator.clipboard;
    if (clip && clip.writeText) {
      clip.writeText(text).then(function () { say(sr.t('js.cite.copied')); },
                                function () { manual(text); });
    } else {
      manual(text);
    }
  }

  var opener = sr.el('button', 'cite__open', sr.t('js.cite.open'));
  opener.type = 'button';
  opener.setAttribute('aria-expanded', 'false');
  opener.setAttribute('aria-controls', 'cite-panel');

  var linker = sr.el('button', 'cite__link');
  linker.type = 'button';

  /* The label is the promise. If the reader arrived on a highlighted passage
     the link keeps it, because that is what they are looking at and what they
     mean to send; if there is no highlight there is nothing to keep. Either
     way the button says which before it is pressed - and the citation, which
     names a page rather than a passage, never carries one. */
  function relabel() {
    linker.textContent = sr.t(/[#&]w=/.test(window.location.hash || '')
      ? 'js.cite.link_highlight' : 'js.cite.link');
  }

  relabel();
  window.addEventListener('hashchange', relabel);

  said = sr.el('span', 'cite__said');
  said.setAttribute('role', 'status');
  said.setAttribute('aria-live', 'polite');

  panel = sr.el('div', 'cite');
  panel.id = 'cite-panel';
  panel.hidden = true;
  panel.innerHTML =
    '<div class="cite__formats" role="group" aria-label="' +
    sr.esc(sr.t('js.cite.formats')) + '">' +
    sr.radios('cite', [['plain', sr.t('js.cite.plain_name')],
                       ['markdown', sr.t('js.cite.markdown_name')],
                       ['note', sr.t('js.cite.note_name')]]) +
    '</div><textarea class="cite__text" readonly rows="3" aria-label="' +
    sr.esc(sr.t('js.cite.text_label')) + '"></textarea>' +
    '<p class="cite__foot"><button type="button" class="cite__copy">' +
    sr.esc(sr.t('js.cite.copy')) + '</button> <span class="cite__note">' +
    sr.esc(sr.t('js.cite.foot')) + '</span></p>';

  meta.appendChild(opener);
  meta.appendChild(linker);
  meta.appendChild(said);
  meta.parentNode.insertBefore(panel, meta.nextSibling);
  area = panel.querySelector('textarea');
  note = panel.querySelector('.cite__note');

  opener.addEventListener('click', function () {
    show(panel.hidden);
    say('');
    if (!panel.hidden) fill().then(function () { area.focus(); area.select(); });
  });

  linker.addEventListener('click', function () {
    copy(address() + (/[#&]w=/.test(window.location.hash) ? window.location.hash : ''));
  });

  panel.addEventListener('change', function () { say(''); fill(); });
  panel.addEventListener('click', function (ev) {
    if (ev.target.classList.contains('cite__copy')) fill().then(function () { copy(area.value); });
  });
})();
