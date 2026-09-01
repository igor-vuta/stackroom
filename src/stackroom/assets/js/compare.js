/* Comparing two releases: one filter, over a page that is already complete.
 *
 * The comparison page arrives from the site builder with every finding written
 * out, every confidence spelled as a word beside it, and the diagrams drawn as
 * inline SVG. If this file never loads, nothing is lost. What it adds is one
 * thing a static page cannot do: letting a reader put the suspected passages
 * away and look only at what the archive is willing to stand behind - and,
 * with them away, letting the pages that then have nothing on them collapse,
 * so a document with three real findings among ninety scans reads as three.
 *
 * Two rules it keeps, because a filter on a page about disclosure can lie in a
 * way a filter on a photo gallery cannot:
 *
 *   1. Nothing is ever hidden without the page saying so. The count of what is
 *      out of sight is announced, politely, on every change.
 *   2. The stricter view is not the default. A reader arrives at everything the
 *      comparison found, including the parts it doubts, and chooses to narrow.
 *      A page that quietly starts filtered is a page that quietly omits.
 */
(function () {
  'use strict';

  var doc = document;
  /* prefs.js runs from the head of every template and publishes the archive's
     own language; this deferred file always runs after it. */
  var sr = window.stackroomReader || { t: function (k) { return '[' + k + ']'; } };
  var pages = doc.querySelectorAll('.cmp-page');
  if (!pages.length) return;

  var anchor = doc.querySelector('.cmp-page');
  if (!anchor || !anchor.parentNode) return;

  var NAME = 'cmp-filter-mode';
  var modes = [
    { value: 'all', label: sr.t('js.compare.all') },
    { value: 'corroborated', label: sr.t('js.compare.corroborated') }
  ];

  /* ------------------------------------------------------------ the control */

  var box = doc.createElement('fieldset');
  box.className = 'cmp-filter';
  box.hidden = true;

  var legend = doc.createElement('legend');
  legend.className = 'visually-hidden';
  legend.textContent = sr.t('js.compare.legend');
  box.appendChild(legend);

  var title = doc.createElement('span');
  title.textContent = sr.t('js.compare.show');
  box.appendChild(title);

  var inputs = [];
  for (var i = 0; i < modes.length; i++) {
    var label = doc.createElement('label');
    var input = doc.createElement('input');
    input.type = 'radio';
    input.name = NAME;
    input.value = modes[i].value;
    input.checked = i === 0;
    input.addEventListener('change', apply);
    var text = doc.createElement('span');
    text.textContent = modes[i].label;
    label.appendChild(input);
    label.appendChild(text);
    box.appendChild(label);
    inputs.push(input);
  }

  /* Polite, not assertive: this is the result of something the reader just did
     deliberately, and interrupting them to say so is the wrong manners. */
  var said = doc.createElement('p');
  said.className = 'visually-hidden';
  said.setAttribute('role', 'status');
  said.setAttribute('aria-live', 'polite');
  box.appendChild(said);

  var seen = doc.createElement('span');
  seen.className = 'cmp-filter__count';
  box.appendChild(seen);

  anchor.parentNode.insertBefore(box, anchor);
  box.hidden = false;

  /* ------------------------------------------------------------ applying it */

  function chosen() {
    for (var i = 0; i < inputs.length; i++) {
      if (inputs[i].checked) return inputs[i].value;
    }
    return 'all';
  }

  /* A finding is kept when it is corroborated, or when everything is being
     shown. A <details> block of suspected passages is a finding container in
     its own right and goes with them; a page keeps its diagram and its
     geometry line whatever is filtered, because those are not claims and a
     reader narrowing the claims should still see the shape of the page. */
  function apply() {
    var strict = chosen() === 'corroborated';
    var hiddenFindings = 0;
    var hiddenPages = 0;

    for (var p = 0; p < pages.length; p++) {
      var page = pages[p];
      var findings = page.querySelectorAll('.cmp-finding');
      var shown = 0;

      for (var f = 0; f < findings.length; f++) {
        var kind = findings[f].getAttribute('data-confidence');
        var keep = !strict || kind === 'corroborated';
        findings[f].hidden = !keep;
        if (keep) shown++; else hiddenFindings++;
      }

      var groups = page.querySelectorAll('.cmp-details');
      for (var g = 0; g < groups.length; g++) groups[g].hidden = strict;

      /* Headings whose whole list is gone would otherwise stand over nothing. */
      var lists = page.querySelectorAll('.cmp-findings');
      for (var l = 0; l < lists.length; l++) {
        var alive = lists[l].querySelectorAll('.cmp-finding:not([hidden])').length;
        lists[l].hidden = alive === 0;
        var head = lists[l].previousElementSibling;
        while (head && (head.className.indexOf('cmp-finding-note') === 0 ||
                        head.className.indexOf('cmp-finding-title') === 0)) {
          head.hidden = alive === 0;
          head = head.previousElementSibling;
        }
      }

      var empty = strict && shown === 0;
      page.hidden = empty;
      if (empty) hiddenPages++;
    }

    var parts = [];
    if (hiddenPages) {
      parts.push(sr.t('js.compare.hidden_pages', { count: hiddenPages }));
    }
    if (hiddenFindings) {
      parts.push(sr.t('js.compare.hidden_findings', { count: hiddenFindings }));
    }
    var message = parts.length
      ? sr.t('js.compare.hidden', { what: parts.join(sr.t('ribbon.join')) })
      : sr.t('js.compare.showing_all');
    seen.textContent = message;
    said.textContent = message;
  }

  apply();
})();
