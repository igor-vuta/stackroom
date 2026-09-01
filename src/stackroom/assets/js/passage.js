/* Stackroom: passage permalinks.
 *
 * Select a run of words in the transcription and get a URL that highlights
 * exactly those words, on this page, boxed on the scan. It is the same URL a
 * search hit produces - `…/p/3/index.html#w=12,13,14` - because there is only
 * one way to point at words in this archive and a reader should be able to
 * make one by hand.
 *
 * Two rules govern every line below.
 *
 * The first is the search contract. The element carrying data-pagefind-body
 * holds this page's tokens and nothing else, each in its own span, separated by
 * whitespace; the index splits on that whitespace and reports matches as
 * positions in the list. One inserted element inside it moves every highlight
 * in the archive by however many words it contains. So nothing here is ever
 * added to the text layer: the affordance is a child of <body>, positioned over
 * the page, and the only thing it reads out of the layer is the numbering the
 * builder already put there.
 *
 * The second is that ordinary copying still has to work. No copy event is
 * intercepted, no selection is changed, no key is swallowed. The one place a
 * selection has to be touched - the execCommand fallback, which works by
 * selecting something else - saves the reader's ranges and puts them back.
 */
(function () {
  'use strict';

  var doc = document;
  /* prefs.js runs from the head of every template and publishes the archive's
     own language; this deferred file always runs after it. */
  var sr = window.stackroomReader || { t: function (k) { return '[' + k + ']'; } };

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function reducedMotion() {
    return !!(window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function announce(text) {
    var sr = window.stackroomViewer;
    if (sr && sr.announce) sr.announce(text);
  }

  function init() {
    var layer = doc.querySelector('.text-layer');
    if (!layer || !window.getSelection) return;

    var meta = doc.querySelector('.page-view');
    var cite = {
      doc: meta ? meta.getAttribute('data-doc') || '' : '',
      page: meta ? meta.getAttribute('data-page') || '' : '',
      bates: meta ? meta.getAttribute('data-bates') || '' : '',
      collection: meta ? meta.getAttribute('data-collection') || '' : ''
    };

    /* ------------------------------------------------------ the reading */

    /* Everything the affordance knows about a selection comes out of a clone
       of it. A word the reader clipped in half is still that word's index -
       they pointed at it - so a partially contained span counts. A span the
       selection merely touched at a boundary clones with no text in it, and
       does not. */
    function readSelection() {
      var sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
      var range = sel.getRangeAt(0);
      /* Inside the transcription, not merely overlapping it. Select-all takes
         in the masthead and the colophon and is not a citation of a passage. */
      if (!layer.contains(range.commonAncestorContainer)) return null;

      var frag = range.cloneContents();
      var parts = frag.querySelectorAll('.w[data-i], .withheld');
      var indices = [];
      var quote = [];
      for (var i = 0; i < parts.length; i++) {
        var node = parts[i];
        if (node.classList.contains('withheld')) {
          /* A redaction has no text, so a quote assembled from text alone
             closes over the hole and reads as a continuous sentence the
             document does not contain. Absence is content: it is written out.
             This is the one place this file adds words the reader did not
             select, and it adds them because leaving them out would be a
             claim about the document that is false. */
          var code = node.getAttribute('data-code');
          quote.push(code
            ? sr.t('js.passage.withheld_code', { code: code })
            : sr.t('js.passage.withheld'));
          continue;
        }
        var text = node.textContent.replace(/\s+/g, ' ').trim();
        if (!text) continue;
        var n = parseInt(node.getAttribute('data-i'), 10);
        if (!isNaN(n) && indices.indexOf(n) === -1) indices.push(n);
        quote.push(text);
      }
      if (!indices.length) return null;
      indices.sort(function (a, b) { return a - b; });

      var rects = range.getClientRects();
      if (!rects.length) return null;
      return {
        indices: indices,
        quote: quote.join(' '),
        first: rects[0],
        last: rects[rects.length - 1]
      };
    }

    function linkFor(indices) {
      return window.location.href.replace(/#.*$/, '') + '#w=' + indices.join(',');
    }

    function quoteFor(read) {
      var where = sr.t('js.passage.cite', {
        title: cite.doc || sr.t('js.passage.this_document'),
        number: cite.page,
        control: cite.bates ? sr.t('js.cite.control_paren', { control: cite.bates }) : '',
        collection: cite.collection
      });
      return sr.t('js.passage.quote', {
        quote: read.quote, cite: where, link: linkFor(read.indices)
      });
    }

    /* ---------------------------------------------------------- the bar */

    /* Two actions and no more.

       "Copy link" is the one this URL scheme was built for and it comes first.

       "Copy quote" is here because of what this archive is. The act that
       follows selecting a sentence in a released record is almost always
       quoting it in something being written - a story, a brief, a filing - and
       a link on its own makes the writer retype the words, which is where
       transcription errors enter a public record. The quote carries its own
       citation and its own permalink, so the sentence and the proof of the
       sentence travel together and either can be checked against the scan.
       It costs one button and introduces no new concept: same selection, same
       URL, plus the words and the two facts already printed above them.

       There is no third button. "Cite as BibTeX", "share to", "download as" is
       where this kind of affordance stops being an affordance and becomes a
       menu nobody reads. */
    var bar = el('div', 'passage');
    bar.hidden = true;
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', sr.t('js.passage.label'));
    var copyLink = el('button', null, sr.t('js.passage.copy_link'));
    copyLink.type = 'button';
    var sep = el('span', 'passage__sep');
    sep.setAttribute('aria-hidden', 'true');
    var copyQuote = el('button', null, sr.t('js.passage.copy_quote'));
    copyQuote.type = 'button';
    bar.appendChild(copyLink);
    bar.appendChild(sep);
    bar.appendChild(copyQuote);
    doc.body.appendChild(bar);

    var current = null;
    var hideTimer = 0;
    var restoring = false;

    function place(read) {
      bar.hidden = false;
      var w = bar.offsetWidth;
      var h = bar.offsetHeight;
      /* Above the first line of the selection, and below the last if there is
         no room above. Either way it sits on a line the reader did not select,
         because an affordance that covers the thing it is about makes you move
         the mouse to see what you are citing. */
      var top = read.first.top - h - 8;
      if (top < 4) top = read.last.bottom + 8;
      var left = read.first.left;
      left = Math.min(left, window.innerWidth - w - 8);
      left = Math.max(8, left);
      bar.style.transform =
        'translate(' + Math.round(left + window.pageXOffset) + 'px,' +
        Math.round(top + window.pageYOffset) + 'px)';
    }

    function reset() {
      copyLink.textContent = sr.t('js.passage.copy_link');
      copyQuote.textContent = sr.t('js.passage.copy_quote');
      copyLink.classList.remove('is-done');
      copyQuote.classList.remove('is-done');
      var field = bar.querySelector('.passage__field');
      if (field) {
        bar.removeChild(field);
        copyLink.hidden = false;
        sep.hidden = false;
        copyQuote.hidden = false;
      }
    }

    function show(read) {
      window.clearTimeout(hideTimer);
      current = read;
      reset();
      place(read);
      /* The class is added on the next frame so the transition has a state to
         come from; set in the same frame as `hidden = false` it would not
         run at all. */
      window.requestAnimationFrame(function () { bar.classList.add('is-open'); });
      announce(sr.t('js.passage.selected', { count: read.indices.length }));
    }

    function hide() {
      window.clearTimeout(hideTimer);
      current = null;
      bar.classList.remove('is-open');
      if (reducedMotion()) { bar.hidden = true; return; }
      hideTimer = window.setTimeout(function () { bar.hidden = true; }, 200);
    }

    /* ------------------------------------------------------- the copying */

    function legacyCopy(text) {
      /* execCommand copies the selection, so it has to make one. The reader's
         own ranges are saved and put back, and the selectionchange that
         restoring fires is ignored - otherwise the affordance would treat the
         reader's selection as a new one and dismiss itself the moment it
         worked. */
      var sel = window.getSelection();
      var saved = [];
      for (var i = 0; i < sel.rangeCount; i++) saved.push(sel.getRangeAt(i).cloneRange());
      var pad = doc.createElement('textarea');
      pad.value = text;
      pad.setAttribute('readonly', '');
      pad.setAttribute('aria-hidden', 'true');
      pad.style.cssText = 'position:fixed;top:0;left:-9999px;opacity:0';
      doc.body.appendChild(pad);
      var ok = false;
      try {
        pad.select();
        ok = doc.execCommand('copy');
      } catch (e) {
        ok = false;
      }
      doc.body.removeChild(pad);
      restoring = true;
      try {
        sel.removeAllRanges();
        for (var j = 0; j < saved.length; j++) sel.addRange(saved[j]);
      } catch (e) { /* the selection is gone; the copy still happened */ }
      window.setTimeout(function () { restoring = false; }, 0);
      return ok;
    }

    /* When there is no clipboard at all - a mirror served over plain http, or
       an archive opened from a USB stick as file:// - the honest answer is not
       a failure message. It is the URL, in a field, already selected, so the
       reader can copy it the way they would copy anything else. */
    function offerField(text) {
      copyLink.hidden = true;
      sep.hidden = true;
      copyQuote.hidden = true;
      var field = el('input', 'passage__field');
      field.type = 'text';
      field.readOnly = true;
      field.value = text;
      field.setAttribute('aria-label', sr.t('js.passage.field_label'));
      bar.appendChild(field);
      if (current) place(current);
      field.focus();
      field.select();
      announce(sr.t('js.passage.no_clipboard'));
    }

    function done(button, label) {
      button.textContent = label;
      button.classList.add('is-done');
      announce(label);
      /* Feedback that leaves on its own. A confirmation that outlives the act
         it confirms becomes furniture, and then it becomes something to
         dismiss. */
      window.clearTimeout(hideTimer);
      hideTimer = window.setTimeout(hide, 1000);
    }

    function copy(text, button, label) {
      var later = null;
      if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
        try { later = navigator.clipboard.writeText(text); } catch (e) { later = null; }
      }
      if (later && later.then) {
        later.then(function () { done(button, label); }, function () {
          if (legacyCopy(text)) done(button, label);
          else offerField(text);
        });
        return;
      }
      if (legacyCopy(text)) done(button, label);
      else offerField(text);
    }

    /* ------------------------------------------------------------ wiring */

    /* Prevents the button from taking focus, which in several browsers
       collapses the document selection - the selection this is about. The
       click still fires, and the keyboard route below still reaches it. */
    bar.addEventListener('mousedown', function (ev) {
      if (ev.target.tagName === 'BUTTON') ev.preventDefault();
    });

    copyLink.addEventListener('click', function () {
      if (current) copy(linkFor(current.indices), copyLink, sr.t('js.passage.link_copied'));
    });
    copyQuote.addEventListener('click', function () {
      if (current) copy(quoteFor(current), copyQuote, sr.t('js.passage.quote_copied'));
    });

    var pending = 0;
    doc.addEventListener('selectionchange', function () {
      if (restoring) return;
      /* The reader is working inside the bar - dragging across the fallback
         field, most likely - and that is not them starting a new selection in
         the document. Without this, offering the field dismissed the thing
         offering it: focusing an input moves the document selection, which
         landed here, found no tokens, and hid the bar mid-copy. */
      if (!bar.hidden && bar.contains(doc.activeElement)) return;
      window.clearTimeout(pending);
      /* Debounced, because a drag fires this on every frame and because a
         selection is not finished until the reader stops moving. */
      pending = window.setTimeout(function () {
        var read = readSelection();
        if (read) show(read); else hide();
      }, 120);
    });

    doc.addEventListener('pointerdown', function (ev) {
      if (!bar.hidden && !bar.contains(ev.target)) hide();
    }, true);

    doc.addEventListener('keydown', function (ev) {
      if (bar.hidden) return;
      if (ev.key === 'Escape') {
        var inside = bar.contains(doc.activeElement);
        hide();
        /* Focus was inside something that just went away. It goes to the
           document landmark rather than to <body>, so the next Tab continues
           from the page instead of from the top of the browser. */
        if (inside) {
          var main = doc.getElementById('main');
          if (main) main.focus();
        }
        return;
      }
      /* The selection was very likely made with the keyboard, and the bar is
         at the end of <body> where Tab would reach it last. One press puts the
         reader in it; from there Tab and Enter are the platform's. */
      if (ev.key === 'Tab' && !ev.shiftKey && !bar.contains(doc.activeElement)) {
        ev.preventDefault();
        var target = bar.querySelector('.passage__field') || copyLink;
        target.focus();
      }
    });

    window.addEventListener('resize', hide);
    window.addEventListener('hashchange', hide);
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
  else window.setTimeout(init, 0);
})();
