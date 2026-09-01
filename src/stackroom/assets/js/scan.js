/* Stackroom: the scan, up close.
 *
 * Three enhancements, all of them about the same object seen from different
 * distances, all of them optional:
 *
 *   the lens      the scan at full size, panned and zoomed, in a <dialog>
 *   the scrubber  the ribbon's preview tile, showing the page under the pointer
 *   the turn      the scan carrying the movement from one page to the next
 *
 * This file is loaded on every page of the archive and each part looks for the
 * thing it enhances before doing anything. Without it the scan is still an
 * <img>, the ribbon is still a picture with a caption, and the pager is still
 * two links. Nothing here is required to read, cite or crawl a page.
 *
 * The premise it settles: the scan beside the transcription is about 480px
 * wide. That is enough to recognise a page and not enough to read one, which
 * made the largest object in the archive decorative. Now it has somewhere to
 * go, and the way in is a click on the thing itself.
 */
(function () {
  'use strict';

  /* Fetched once on every page, from one of two places. On a page of a
     document the shell requests it in the head, parser-blocking, because
     `pagereveal` fires before deferred scripts run and the page turn loses its
     direction without it - see the note above initTurns. Everywhere else it
     arrives deferred at the end of the body, where the lens and the scrubber
     are early enough and 41 KB in the head would be a cost paid before the
     first pixel for nothing.

     Which of the two is `HEAD_SCRIPTS` in build/site.py; the shell drops the
     names it took in the head from the deferred sweep, so the two lists cannot
     both claim this file. There was a time when they could, and the template
     asked for it twice on purpose - the guard below is what made the second
     run harmless, and this comment used to describe that arrangement.

     The guard stays. It no longer prevents anything the build can currently
     produce, but this is a 42 KB IIFE that binds listeners to `document`,
     appends a <dialog> and makes a live region: run twice it would double all
     three, and nothing in here is idempotent on its own. Two lines to make a
     second execution a no-op is the cheapest insurance in the file. */
  if (window.stackroomScan) return;
  window.stackroomScan = true;

  var doc = document;
  /* prefs.js is loaded from the head of every template and publishes the
     archive's own language; this file is loaded from the head of the page
     template, after it. The fallback is for a page that has neither. */
  var sr = window.stackroomReader || {
    t: function (k) { return '[' + k + ']'; },
    pct: function (v, d, ofOne) { return Math.round(ofOne ? v * 100 : v) + '%'; }
  };
  var html = doc.documentElement;

  /* ------------------------------------------------------------- shared */

  /* viewer.js owns the join between a token index and a box on the scan, and
     publishes it onto the namespace these files share. It is loaded only on
     page templates and after this file, so the namespace is read at use time
     and never at load time, and everything here that does not find what it
     wants there does slightly less rather than throwing. */
  function api() { return window.stackroomViewer || null; }

  function reducedMotion() {
    return !!(window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  /* A live region has to be in the document before its text changes, or the
     screen reader was not watching the element when it changed and says
     nothing at all. viewer.js makes one on page templates; the front page and
     the document pages have no viewer.js and the ribbon still has things to
     say, so one is made at init - which is early enough, and is why this is
     not done lazily on the first announcement. */
  var region = null;

  function makeRegion() {
    if (region || api()) return;
    region = el('p', 'visually-hidden');
    region.setAttribute('role', 'status');
    region.setAttribute('aria-live', 'polite');
    doc.body.appendChild(region);
  }

  function announce(text) {
    var shared = api();
    if (shared && shared.announce) { shared.announce(text); return; }
    if (region) region.textContent = text;
  }

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function abs(url) {
    try { return new URL(url, doc.baseURI).href; } catch (e) { return url; }
  }

  /* The archive's own easing curve, evaluated in script so that a movement
     driven by requestAnimationFrame and a movement driven by a CSS transition
     are the same movement. Bisection rather than Newton: fifteen frames of a
     zoom is not a place where twenty divisions cost anything, and bisection
     cannot fail to converge on a curve with a flat segment in it. */
  function ease(t) {
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    var x1 = 0.2, x2 = 0.0, y1 = 0.0, y2 = 1.0;
    var lo = 0, hi = 1, u = t, x;
    for (var i = 0; i < 20; i++) {
      u = (lo + hi) / 2;
      var v = 1 - u;
      x = 3 * v * v * u * x1 + 3 * v * u * u * x2 + u * u * u;
      if (x < t) lo = u; else hi = u;
    }
    var w = 1 - u;
    return 3 * w * w * u * y1 + 3 * w * u * u * y2 + u * u * u;
  }

  /* ==================================================================== */
  /* The lens                                                             */
  /* ==================================================================== */

  var STASH = 'stackroom:lens';

  function boxesFromOverlay(overlay) {
    if (!overlay) return [];
    var out = [];
    Array.prototype.forEach.call(overlay.querySelectorAll('.hit--void'), function (node) {
      var s = node.style;
      var box = {
        x: parseFloat(s.left) / 100,
        y: parseFloat(s.top) / 100,
        w: parseFloat(s.width) / 100,
        h: parseFloat(s.height) / 100,
        codes: node.getAttribute('data-codes') || ''
      };
      if (box.w > 0 && box.h > 0) out.push(box);
    });
    return out;
  }

  /* The widest rendering, in the format this browser actually chose.
     `currentSrc` is the only honest way to learn that choice - it is the URL
     the browser picked out of the <picture>, so its format is a format this
     browser can decode. We stay in that format and take the largest width
     offered in it. Before the image has loaded currentSrc is empty and the
     <img src> is right by construction: the builder puts the widest compatible
     rendering there. Either way nothing large is requested until this runs,
     which is when the dialog opens. */
  function widest(picture, img) {
    var chosen = img.currentSrc || '';
    var sources = picture ? picture.querySelectorAll('source[srcset]') : [];
    for (var i = 0; i < sources.length; i++) {
      var mine = false;
      var top = null;
      var cands = String(sources[i].getAttribute('srcset') || '').split(',');
      for (var j = 0; j < cands.length; j++) {
        var bits = cands[j].trim().split(/\s+/);
        if (!bits[0]) continue;
        var url = abs(bits[0]);
        var w = bits[1] && /w$/.test(bits[1]) ? parseInt(bits[1], 10) : 0;
        if (url === chosen) mine = true;
        if (!top || w > top.w) top = { url: url, w: w };
      }
      if (mine && top) return top;
    }
    return { url: abs(img.getAttribute('src') || ''), w: 0 };
  }

  function buildLens(page) {
    var lens = el('dialog', 'lens');
    lens.id = 'lens';
    lens.setAttribute('aria-label', page.label);

    var stage = el('div', 'lens__stage');
    stage.id = 'lens-stage';
    stage.tabIndex = 0;
    /* Focusable because it is a control - the arrows pan it and +/- zoom it,
       and a control a keyboard cannot reach is not a control. Given a role
       because a focusable element with no role is exposed as "generic", and a
       name on a generic is not reliably read; and given a name that says what
       the two gestures are, because they are not discoverable from looking at
       a rectangle. */
    stage.setAttribute('role', 'group');
    stage.setAttribute('aria-label', sr.t('js.lens.stage_label', { label: page.label }));

    var canvas = el('div', 'lens__canvas');
    var img = el('img', 'lens__img');
    img.alt = '';
    img.decoding = 'async';
    var overlay = el('div', 'lens__overlay');
    overlay.setAttribute('aria-hidden', 'true');
    canvas.appendChild(img);
    canvas.appendChild(overlay);
    stage.appendChild(canvas);

    var bar = el('div', 'lens__bar');
    var where = el('p', 'lens__where', page.where);
    var zoom = el('div', 'lens__group');
    var out = el('button', null, '−');
    out.type = 'button';
    out.setAttribute('aria-label', sr.t('js.lens.zoom_out'));
    var level = el('span', 'lens__level', sr.pct(100));
    level.id = 'lens-level';
    var into = el('button', null, '+');
    into.type = 'button';
    into.setAttribute('aria-label', sr.t('js.lens.zoom_in'));
    out.setAttribute('aria-describedby', 'lens-level');
    into.setAttribute('aria-describedby', 'lens-level');
    var fit = el('button', null, sr.t('js.lens.fit'));
    fit.type = 'button';
    zoom.appendChild(out);
    zoom.appendChild(level);
    zoom.appendChild(into);
    zoom.appendChild(fit);

    var voidBtn = null;
    if (page.voids.length) {
      voidBtn = el('button', null,
        sr.t('js.lens.withheld_button', { count: page.voids.length }));
      voidBtn.type = 'button';
      voidBtn.setAttribute('aria-label',
        sr.t('js.lens.withheld_button_label', { count: page.voids.length }));
    }

    var steps = el('div', 'lens__group');
    var prev = null;
    var next = null;
    if (page.prev) {
      prev = el('a', 'lens__step', sr.t('page.prev', { number: page.number - 1 }));
      prev.href = page.prev;
      prev.rel = 'prev';
    }
    if (page.next) {
      next = el('a', 'lens__step', sr.t('page.next', { number: page.number + 1 }));
      next.href = page.next;
      next.rel = 'next';
    }
    if (prev) steps.appendChild(prev);
    if (next) steps.appendChild(next);

    var close = el('button', 'lens__close', sr.t('js.lens.close'));
    close.type = 'button';

    bar.appendChild(where);
    bar.appendChild(zoom);
    if (voidBtn) bar.appendChild(voidBtn);
    bar.appendChild(el('div', 'lens__spacer'));
    if (prev || next) bar.appendChild(steps);
    bar.appendChild(close);

    lens.appendChild(stage);
    lens.appendChild(bar);
    doc.body.appendChild(lens);

    return {
      root: lens, stage: stage, canvas: canvas, img: img, overlay: overlay,
      level: level, zoomIn: into, zoomOut: out, fit: fit, close: close,
      voidBtn: voidBtn, prev: prev, next: next
    };
  }

  function wireLens(page) {
    var ui = null;
    var natW = page.width;
    var natH = page.height;
    var scale = 1, tx = 0, ty = 0, fitScale = 1;
    var maxScale = 1;
    var opener = null;
    var scrollAt = 0;
    var raf = 0;
    var voidAt = -1;
    var leaving = false;
    var loadedWide = false;

    function stageW() { return ui.stage.clientWidth || 1; }
    function stageH() { return ui.stage.clientHeight || 1; }

    function computeFit() {
      fitScale = Math.min(stageW() / natW, stageH() / natH);
      /* Six times the scanned resolution is past the point where anything new
         appears; it is there so a 200px-wide fragment of a page still has
         somewhere to go. */
      maxScale = Math.max(6, fitScale * 8);
      return fitScale;
    }

    function clampScale(s) {
      return Math.min(maxScale, Math.max(fitScale, s));
    }

    /* The page's own edges stop the pan. A sheet big enough to fill the window
       always does; one too small to sits centred in it.

       This is what stops a box in the outer margin from being centred, and it
       is deliberate. `frame()` below shows about 40% of the page width, so the
       scan is roughly 2.3 times the stage - and at that magnification a
       redaction at 80% of the width comes to rest about 60px right of centre
       on a 1400px stage, with the page's right edge against the stage's right
       edge. Centring it would mean scrolling that margin off in exchange for
       60px of nothing, and the margin is the part of the picture that answers
       "where on the page is this?". A viewer that shows pages rather than an
       infinite canvas stops here; `assert_framed` in tests/test_viewer_
       browser.py is the same rule written as an assertion. */
    function clampPan() {
      var w = stageW(), h = stageH();
      var sw = natW * scale, sh = natH * scale;
      tx = sw <= w ? (w - sw) / 2 : Math.min(0, Math.max(w - sw, tx));
      ty = sh <= h ? (h - sh) / 2 : Math.min(0, Math.max(h - sh, ty));
    }

    function apply() {
      ui.canvas.style.width = natW + 'px';
      ui.canvas.style.height = natH + 'px';
      ui.canvas.style.transform =
        'translate(' + tx.toFixed(2) + 'px,' + ty.toFixed(2) + 'px) scale(' +
        scale.toFixed(5) + ')';
      ui.canvas.classList.toggle('is-magnified', scale > 2.5);
      ui.level.textContent = sr.pct(scale, 0, true);
      var atFit = scale <= fitScale * 1.001;
      ui.zoomOut.disabled = atFit;
      ui.fit.disabled = atFit;
      ui.zoomIn.disabled = scale >= maxScale * 0.999;
    }

    function zoomAbout(px, py, factor) {
      var next = clampScale(scale * factor);
      var k = next / scale;
      tx = px - (px - tx) * k;
      ty = py - (py - ty) * k;
      scale = next;
      clampPan();
      apply();
    }

    /* The point of the image currently under the middle of the stage, in
       image pixels. Interpolating *this* rather than the translation is what
       keeps a fly-to from swinging away before it arrives: the thing the eye
       is tracking travels in a straight line. */
    function centre() {
      return { x: (stageW() / 2 - tx) / scale, y: (stageH() / 2 - ty) / scale };
    }

    function setCentre(c, s) {
      scale = clampScale(s);
      tx = stageW() / 2 - c.x * scale;
      ty = stageH() / 2 - c.y * scale;
      clampPan();
      apply();
    }

    function stop() {
      if (raf) { window.cancelAnimationFrame(raf); raf = 0; }
    }

    function glide(toCentre, toScale, ms) {
      stop();
      toScale = clampScale(toScale);
      if (reducedMotion() || !ms) { setCentre(toCentre, toScale); return; }
      var from = centre();
      var s0 = scale;
      var t0 = 0;
      /* Scale is interpolated in log space. Halfway through a 1x to 4x zoom is
         2x, not 2.5x - anything else accelerates towards the end and reads as
         the page falling towards you. */
      var l0 = Math.log(s0), l1 = Math.log(toScale);
      raf = window.requestAnimationFrame(function step(now) {
        if (!t0) t0 = now;
        var p = Math.min(1, (now - t0) / ms);
        var e = ease(p);
        setCentre({
          x: from.x + (toCentre.x - from.x) * e,
          y: from.y + (toCentre.y - from.y) * e
        }, Math.exp(l0 + (l1 - l0) * e));
        if (p < 1) raf = window.requestAnimationFrame(step);
        else raf = 0;
      });
    }

    /* Frame a box given in page fractions.

       Not the box: a *region* around it, at least a third of the page wide.
       Framing the box itself is the obvious thing and it is wrong - three
       words fill about 3% of a sheet, so filling the window with them lands
       the reader at 2,600% looking at four enormous letters with nothing
       around them and no idea where on the page they are. A found phrase
       means something because of the sentence it is in. The minimum region
       is what puts the sentence back. */
    var CONTEXT_W = 0.4;
    var CONTEXT_H = 0.15;

    function frame(box, ms) {
      var w = Math.max(box.w, CONTEXT_W) * natW;
      var h = Math.max(box.h, CONTEXT_H) * natH;
      var target = Math.min((stageW() * 0.92) / w, (stageH() * 0.92) / h);
      glide({
        x: (box.x + box.w / 2) * natW,
        y: (box.y + box.h / 2) * natH
      }, target, ms == null ? 340 : ms);
    }

    function toFit(ms) {
      glide({ x: natW / 2, y: natH / 2 }, computeFit(), ms);
    }

    /* What the lens opens at, which is deliberately not Fit.

       Fitting the whole sheet into a 1400x900 window puts the scan on screen
       about 690px wide - half again the 450px it had on the page, and still
       not a page anybody can read. Opening at fit would answer "bigger?" and
       leave "readable?" for a second gesture, which is the premise this whole
       view exists to settle.

       So it opens at the width of the window, capped at the scan's own
       resolution because upsampling on arrival is a lie about how good the
       rendering is, and anchored to the top left - which is where a document
       starts. On a phone, where the width of the window is already less than
       the whole sheet, that is the same number as Fit and the reader gets the
       whole page, which is the best that screen can do before they pinch.

       Fit is a button, and 0, and says what it means. */
    function toOpening() {
      computeFit();
      var byWidth = Math.min(stageW() / natW, 1);
      if (byWidth > fitScale * 1.02) {
        scale = clampScale(byWidth);
        tx = 0;
        ty = 0;
        clampPan();      /* centres it if the page is narrower than the stage */
        apply();
        return;
      }
      setCentre({ x: natW / 2, y: natH / 2 }, fitScale);
    }

    /* --- the wide rendering, fetched here and nowhere else --- */

    function loadWide() {
      if (loadedWide) return;
      loadedWide = true;
      var pick = widest(page.picture, page.img);
      if (pick.w) {
        natH = Math.round(pick.w * (page.height / page.width));
        natW = pick.w;
      }
      /* The small rendering is already decoded and in cache, so it paints on
         the frame the dialog opens and the reader never sees an empty stage.
         The wide one replaces it in place when it is ready: same picture, same
         geometry, more detail, no flash and nothing to cross-fade. */
      ui.img.src = page.img.currentSrc || page.img.src;
      if (!pick.url || pick.url === ui.img.src) return;
      var wide = new Image();
      wide.decoding = 'async';
      wide.onload = function () {
        if (wide.naturalWidth) {
          var c = centre();
          var s = scale;
          natW = wide.naturalWidth;
          natH = wide.naturalHeight;
          computeFit();
          ui.img.src = pick.url;
          setCentre(c, s);
        }
      };
      wide.src = pick.url;
    }

    /* --- pointers --------------------------------------------------- */

    var pointers = {};
    var lastMid = null;
    var lastGap = 0;
    var dragged = false;
    var downAt = null;

    function positions() {
      var list = [];
      for (var id in pointers) if (pointers.hasOwnProperty(id)) list.push(pointers[id]);
      return list;
    }

    function local(ev) {
      var r = ui.stage.getBoundingClientRect();
      return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    }

    function onDown(ev) {
      stop();
      var p = local(ev);
      pointers[ev.pointerId] = p;
      dragged = false;
      downAt = p;
      lastMid = null;
      try { ui.stage.setPointerCapture(ev.pointerId); } catch (e) { /* fine */ }
      ui.stage.classList.add('is-panning');
    }

    function onMove(ev) {
      if (!pointers[ev.pointerId]) return;
      var p = local(ev);
      var before = pointers[ev.pointerId];
      pointers[ev.pointerId] = p;
      var list = positions();

      if (list.length >= 2) {
        var a = list[0], b = list[1];
        var mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        var gap = Math.max(1, Math.hypot(b.x - a.x, b.y - a.y));
        if (lastMid) {
          tx += mid.x - lastMid.x;
          ty += mid.y - lastMid.y;
          zoomAbout(mid.x, mid.y, gap / lastGap);
        }
        lastMid = mid;
        lastGap = gap;
        dragged = true;
        return;
      }

      tx += p.x - before.x;
      ty += p.y - before.y;
      if (downAt && Math.hypot(p.x - downAt.x, p.y - downAt.y) > 3) dragged = true;
      clampPan();
      apply();
    }

    function onUp(ev) {
      delete pointers[ev.pointerId];
      lastMid = null;
      if (!positions().length) ui.stage.classList.remove('is-panning');
      try { ui.stage.releasePointerCapture(ev.pointerId); } catch (e) { /* fine */ }
      /* A click that did not move is a click on what is under it. */
      if (!dragged && ev.type === 'pointerup' && downAt) hitTest(downAt);
      downAt = null;
    }

    function hitTest(p) {
      if (!page.voids.length) return;
      var ix = (p.x - tx) / scale / natW;
      var iy = (p.y - ty) / scale / natH;
      for (var i = 0; i < page.voids.length; i++) {
        var b = page.voids[i];
        if (ix >= b.x && ix <= b.x + b.w && iy >= b.y && iy <= b.y + b.h) {
          voidAt = i;
          frame(b);
          announce(voidLabel(i));
          return;
        }
      }
    }

    function voidLabel(i) {
      var b = page.voids[i];
      var text = sr.t('js.lens.void', { index: i + 1, count: page.voids.length });
      if (b.codes) {
        text = sr.t('js.lens.void_cited', {
          area: text,
          codes: b.codes.split(/\s+/).join(sr.t('ribbon.join'))
        });
      }
      return text;
    }

    function nextVoid() {
      if (!page.voids.length) return;
      voidAt = (voidAt + 1) % page.voids.length;
      frame(page.voids[voidAt]);
      announce(voidLabel(voidAt));
    }

    /* --- opening and closing ---------------------------------------- */

    function sync() {
      /* Cloned rather than recomputed. One arithmetic for the boxes means a
         highlight cannot be in one place on the page and another in here. */
      if (page.overlay) ui.overlay.innerHTML = page.overlay.innerHTML;
    }

    function target() {
      /* Arriving on a search hit, or on a link somebody sent: the reader has
         already said which words they came for. */
      var shared = api();
      if (!shared || !shared.positions || !shared.boxFor) return null;
      var pos = shared.positions();
      if (!pos.length) return null;
      var boxes = shared.merge(pos.map(shared.boxFor).filter(Boolean));
      return boxes.length ? boxes[0] : null;
    }

    function open(opts) {
      opts = opts || {};
      if (!ui) {
        ui = buildLens(page);
        bind();
      }
      if (ui.root.open) return;
      opener = opts.opener || doc.activeElement;
      scrollAt = window.pageYOffset;
      sync();
      loadWide();

      var restore = opts.restore || null;
      var box = opts.box || (restore ? null : target());

      var show = function () {
        html.classList.add('is-lensed');
        ui.root.showModal();
        computeFit();
        if (restore) {
          scale = clampScale(restore.r * fitScale);
          tx = stageW() / 2 - restore.cx * natW * scale;
          ty = stageH() / 2 - restore.cy * natH * scale;
          clampPan();
          apply();
        } else if (box && reducedMotion()) {
          frame(box, 0);
        } else {
          toOpening();
        }
        ui.stage.focus({ preventScroll: true });
      };

      if (restore) {
        /* Carried in from the page before this one. There is nothing on this
           page for the scan to grow out of - the reader never saw it small -
           so it simply arrives, and the fade is the whole of it. */
        show();
        if (!reducedMotion()) ui.canvas.classList.add('is-arriving');
      } else if (!reducedMotion() && doc.startViewTransition) {
        /* The scan grows out of its own place on the page rather than being
           replaced by a dialog that happens to contain it. Both boxes are the
           same picture, so there is one object on the screen throughout and
           the reader never has to find it again. */
        var vt = doc.startViewTransition(show);
        if (box) vt.finished.then(function () { frame(box); }, function () {});
      } else {
        show();
        if (box) frame(box, 0);
      }

      announce(sr.t('js.lens.opened', { label: page.label }));
    }

    function shut() {
      if (!ui || !ui.root.open) return;
      var run = function () {
        html.classList.remove('is-lensed');
        ui.root.close();
      };
      if (!reducedMotion() && doc.startViewTransition && !leaving) {
        doc.startViewTransition(run);
      } else {
        run();
      }
    }

    /* Carried across the navigation so that reading a scan page by page at
       400% does not mean re-finding the same paragraph on every sheet. Kept in
       session storage rather than in the URL: a link somebody sends should
       open the page, not force the room the sender happened to be reading in.
       Relative to fit rather than absolute, so a page of a different size
       arrives at the same closeness rather than the same number. */
    function stash() {
      leaving = true;
      try {
        var c = centre();
        window.sessionStorage.setItem(STASH, JSON.stringify({
          r: scale / (fitScale || 1),
          cx: c.x / natW,
          cy: c.y / natH
        }));
      } catch (e) { /* a private window has no session storage; carry on */ }
    }

    function step(link) {
      if (!link) return;
      stash();
      window.location.href = link.href;
    }

    function bind() {
      var stage = ui.stage;

      stage.addEventListener('pointerdown', onDown);
      stage.addEventListener('pointermove', onMove);
      stage.addEventListener('pointerup', onUp);
      stage.addEventListener('pointercancel', onUp);

      stage.addEventListener('wheel', function (ev) {
        ev.preventDefault();
        var d = ev.deltaY;
        if (ev.deltaMode === 1) d *= 16;
        else if (ev.deltaMode === 2) d *= stageH();
        /* A trackpad pinch arrives as a wheel event with ctrlKey set, and it
           should feel like a pinch: more travel per unit. A plain wheel zooms
           too, because in a window that contains one picture and nothing else
           the wheel means "closer", and panning has the drag, the arrows and
           two fingers already. */
        var p = local(ev);
        stop();
        zoomAbout(p.x, p.y, Math.exp(-d * (ev.ctrlKey ? 0.01 : 0.0022)));
      }, { passive: false });

      stage.addEventListener('dblclick', function (ev) {
        var p = local(ev);
        if (scale > fitScale * 1.001) {
          toFit(240);
        } else {
          var c = { x: (p.x - tx) / scale, y: (p.y - ty) / scale };
          glide(c, 1, 240);
        }
      });

      ui.zoomIn.addEventListener('click', function () {
        var c = centre(); stop(); setCentre(c, scale * 1.6);
      });
      ui.zoomOut.addEventListener('click', function () {
        var c = centre(); stop(); setCentre(c, scale / 1.6);
      });
      ui.fit.addEventListener('click', function () {
        toFit(240); announce(sr.t('js.lens.whole_page'));
      });
      ui.close.addEventListener('click', shut);
      if (ui.voidBtn) ui.voidBtn.addEventListener('click', nextVoid);
      if (ui.prev) ui.prev.addEventListener('click', function (ev) {
        ev.preventDefault(); step(ui.prev);
      });
      if (ui.next) ui.next.addEventListener('click', function (ev) {
        ev.preventDefault(); step(ui.next);
      });

      ui.root.addEventListener('close', function () {
        html.classList.remove('is-lensed');
        stop();
        /* <dialog> returns focus itself, and does it wrong often enough that
           the archive says so out loud rather than hoping. */
        if (opener && opener.isConnected) {
          try { opener.focus({ preventScroll: true }); } catch (e) { opener.focus(); }
        }
        if (window.pageYOffset !== scrollAt) window.scrollTo(0, scrollAt);
        opener = null;
      });

      /* Escape is the platform's. Everything else below is claimed here and
         stopped from reaching the page underneath, which uses the arrows for
         paging.

         Nothing with a modifier on it is claimed, and that is not tidiness:
         the browser's own zoom is Ctrl and the same three keys this view uses,
         so catching them would have taken a reader's page zoom away and given
         them a picture zoom instead. Ctrl R would have jumped to a redaction
         rather than reloading. */
      ui.root.addEventListener('keydown', function (ev) {
        if (ev.metaKey || ev.altKey || ev.ctrlKey) return;
        var pan = ev.shiftKey ? stageH() * 0.6 : 80;
        var used = true;
        switch (ev.key) {
          case '+': case '=': {
            var a = centre(); stop(); setCentre(a, scale * 1.6); break;
          }
          case '-': case '_': {
            var b = centre(); stop(); setCentre(b, scale / 1.6); break;
          }
          case '0': case 'Home': toFit(240); announce(sr.t('js.lens.whole_page')); break;
          case '1': { var c = centre(); stop(); setCentre(c, 1); break; }
          case 'ArrowLeft': stop(); tx += pan; clampPan(); apply(); break;
          case 'ArrowRight': stop(); tx -= pan; clampPan(); apply(); break;
          case 'ArrowUp': stop(); ty += pan; clampPan(); apply(); break;
          case 'ArrowDown': stop(); ty -= pan; clampPan(); apply(); break;
          case 'r': nextVoid(); break;
          case 'j': case 'PageDown': step(ui.next); break;
          case 'k': case 'PageUp': step(ui.prev); break;
          default: used = false;
        }
        if (used) {
          ev.preventDefault();
          ev.stopPropagation();
        }
      });

      window.addEventListener('resize', function () {
        if (!ui.root.open) return;
        var c = centre();
        var was = scale / (fitScale || 1);
        computeFit();
        setCentre(c, was * fitScale);
      });

      window.addEventListener('hashchange', function () {
        if (!ui.root.open) return;
        /* viewer.js has just redrawn the page's boxes; the lens shows the same
           ones or it is lying about where the match is. */
        window.setTimeout(function () {
          sync();
          var box = target();
          if (box) frame(box);
        }, 0);
      });
    }

    return { open: open, close: shut };
  }

  /* --- the page's own description, and the two ways in ---------------- */

  function initLens() {
    var figure = doc.getElementById('scan');
    var img = figure && figure.querySelector('.scan__img');
    var meta = doc.querySelector('.page-view');
    if (!figure || !img || !meta) return;

    var number = parseInt(meta.getAttribute('data-page'), 10) || 1;
    var count = parseInt(meta.getAttribute('data-pages'), 10) || number;
    var title = meta.getAttribute('data-doc') || '';
    var prevLink = doc.getElementById('prev-page');
    var nextLink = doc.getElementById('next-page');

    var page = {
      number: number,
      count: count,
      title: title,
      label: sr.t('js.scan.page_of_document',
        { number: number, title: title || sr.t('js.scan.this_document') }),
      where: title
        ? sr.t('page.where', { title: title, number: number, total: count })
        : sr.t('js.scan.where_untitled', { number: number, total: count }),
      picture: figure.querySelector('picture'),
      img: img,
      overlay: doc.getElementById('overlay'),
      voids: boxesFromOverlay(doc.getElementById('overlay')),
      width: parseInt(img.getAttribute('width'), 10) || img.naturalWidth || 1000,
      height: parseInt(img.getAttribute('height'), 10) || img.naturalHeight || 1294,
      prev: prevLink ? prevLink.getAttribute('href') : null,
      next: nextLink ? nextLink.getAttribute('href') : null
    };

    var lens = wireLens(page);

    /* The route for a keyboard, a screen reader and a touch screen, sitting in
       the line of captions under the mount. Built here rather than in the
       template so that a page without script never shows a control that would
       do nothing. */
    var button = el('button', 'scan__open', sr.t('js.scan.open_full'));
    button.type = 'button';
    button.setAttribute('aria-haspopup', 'dialog');
    var caption = doc.querySelector('.scan__caption');
    if (caption) caption.insertBefore(button, caption.firstChild);
    else figure.parentNode.appendChild(button);
    button.addEventListener('click', function () { lens.open({ opener: button }); });

    /* The route for a pointer. `cursor: zoom-in` is the whole affordance and
       it costs no pixels on top of the document, which is the rule this design
       is built on. The click is hit-tested against the redaction boxes first,
       so clicking a black block opens on that block - the one question a
       reader has about a page with a hole in it. */
    figure.classList.add('is-zoomable');
    figure.addEventListener('click', function (ev) {
      var r = figure.getBoundingClientRect();
      if (!r.width || !r.height) return;
      var nx = (ev.clientX - r.left) / r.width;
      var ny = (ev.clientY - r.top) / r.height;
      var box = null;
      for (var i = 0; i < page.voids.length; i++) {
        var b = page.voids[i];
        if (nx >= b.x && nx <= b.x + b.w && ny >= b.y && ny <= b.y + b.h) { box = b; break; }
      }
      lens.open({ opener: button, box: box });
    });

    /* Came here from inside the lens on the page before this one. Read once
       and thrown away, so a reload is a plain page and only the reader's own
       next-page press reopens the room they were reading in. */
    var carried = null;
    try {
      var raw = window.sessionStorage.getItem(STASH);
      if (raw) {
        window.sessionStorage.removeItem(STASH);
        carried = JSON.parse(raw);
      }
    } catch (e) { carried = null; }
    if (carried && typeof carried.r === 'number') {
      lens.open({ opener: button, restore: carried });
    }
  }

  /* ==================================================================== */
  /* The ribbon, scrubbed                                                 */
  /* ==================================================================== */

  /* The class the builder drew a run of ticks with, and the key that names
     what it means. Keys rather than words: the tile used to compare the word
     it had just written against the literal 'unreadable' to decide whether to
     invert it, which stops being true the moment the word is Russian. */
  var STATES = {
    'r-part': 'js.strip.part',
    'r-full': 'key.full',
    'r-dark': 'js.strip.dark',
    'r-void': 'key.void'
  };

  function pad4(n) {
    var s = String(n);
    while (s.length < 4) s = '0' + s;
    return s;
  }

  function scrubTile() {
    var tile = doc.getElementById('scrub');
    if (tile) return tile;
    tile = el('div', 'scrub');
    tile.id = 'scrub';
    tile.setAttribute('aria-hidden', 'true');
    var img = el('img', 'scrub__img');
    img.alt = '';
    img.decoding = 'async';
    img.hidden = true;
    var meta = el('span', 'scrub__meta');
    var num = el('span', 'scrub__num');
    var state = el('span', 'scrub__state');
    meta.appendChild(num);
    meta.appendChild(state);
    tile.appendChild(img);
    tile.appendChild(meta);
    doc.body.appendChild(tile);
    return tile;
  }

  function wireRibbon(svg) {
    var total = parseInt(svg.getAttribute('data-pages'), 10);
    var base = svg.getAttribute('data-base');
    if (!total || !base) return;

    /* The media directory is derived from the link the builder already wrote,
       resolved as a URL rather than assembled as a string, so this works at a
       domain root, in a subdirectory, from a USB stick and inside a zip. */
    var media = '';
    var first = abs(base.replace('{n}', '1'));
    var m = /^(.*\/)d\/([^/]+)\/p\/\d+\/[^/]*$/.exec(first);
    if (m) media = m[1] + 'media/' + m[2] + '/';

    /* The state of every page is already in this graphic: the builder writes
       one rect per run of identical pages and names the run in its class. */
    var runs = [];
    Array.prototype.forEach.call(svg.querySelectorAll('rect'), function (r) {
      runs.push({ x: parseFloat(r.getAttribute('x')) || 0, cls: r.getAttribute('class') || '' });
    });

    function stateOf(n) {
      var at = ((n - 0.5) / total) * 1000;
      var found = '';
      for (var i = 0; i < runs.length; i++) {
        if (runs[i].x <= at + 0.0001) found = runs[i].cls; else break;
      }
      return STATES[found] || '';
    }

    function pageAt(clientX) {
      var r = svg.getBoundingClientRect();
      if (!r.width) return 0;
      var ratio = (clientX - r.left) / r.width;
      return Math.min(total, Math.max(1, Math.ceil(ratio * total)));
    }

    svg.classList.add('is-live');
    /* The tooltip this replaces said "Page 7" after a second of stillness and
       could not show the page. It is gone. */
    svg.removeAttribute('title');

    var tile = null;
    var img = null;
    var num = null;
    var state = null;
    var shown = 0;
    var timer = 0;
    var cache = {};
    var scrubbing = false;

    function place(clientX) {
      var r = svg.getBoundingClientRect();
      var w = tile.offsetWidth || 136;
      var x = clientX - w / 2;
      /* Kept inside the strip it belongs to, so it never hangs off the side of
         a phone and never covers the masthead. */
      x = Math.min(r.right - w, Math.max(r.left, x));
      var top = r.top + window.pageYOffset - (tile.offsetHeight || 160) - 8;
      if (top < window.pageYOffset + 4) top = r.bottom + window.pageYOffset + 8;
      tile.style.transform =
        'translate(' + Math.round(x + window.pageXOffset) + 'px,' + Math.round(top) + 'px)';
    }

    function blank() {
      img.hidden = true;
      img.removeAttribute('src');
    }

    function paint(n) {
      num.textContent = sr.t('page.n', { number: n });
      var key = stateOf(n);
      state.textContent = key ? sr.t(key) : '';
      state.classList.toggle('is-dark', key === 'js.strip.dark');
      if (!media) { blank(); return; }
      var url = media + 'p' + pad4(n) + '@thumb.webp';
      if (cache[url] === false) { blank(); return; }
      /* The frame is held open while we do not yet know, so the tile does not
         change height under the pointer when the picture arrives. It collapses
         only once we have learned there is no picture for this page. */
      img.hidden = false;
      if (cache[url]) { img.src = url; return; }
      img.removeAttribute('src');
      /* Nothing is fetched while the pointer is travelling. A sweep across a
         2,000-page strip would otherwise ask for 2,000 thumbnails to show one.
         What is already cached appears at once, so a drag stays responsive. */
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        if (shown !== n) return;
        var probe = new Image();
        probe.onload = function () {
          cache[url] = true;
          if (shown === n) { img.hidden = false; img.src = url; }
        };
        /* A missing thumbnail is a page that could not be rendered, which is
           a fact about the archive and not an error. The tile keeps the
           number and the state and says nothing about the picture. */
        probe.onerror = function () {
          cache[url] = false;
          if (shown === n) blank();
        };
        probe.src = url;
      }, 90);
    }

    function show(clientX) {
      if (!tile) {
        tile = scrubTile();
        img = tile.querySelector('.scrub__img');
        num = tile.querySelector('.scrub__num');
        state = tile.querySelector('.scrub__state');
      }
      var n = pageAt(clientX);
      if (n !== shown) { shown = n; paint(n); }
      place(clientX);
      tile.classList.add('is-open');
    }

    function hide() {
      window.clearTimeout(timer);
      shown = 0;
      if (tile) tile.classList.remove('is-open');
    }

    svg.addEventListener('pointerenter', function (ev) {
      if (ev.pointerType === 'touch') return;
      show(ev.clientX);
    });
    svg.addEventListener('pointermove', function (ev) {
      if (ev.pointerType === 'touch' && !scrubbing) return;
      show(ev.clientX);
      if (scrubbing) ev.preventDefault();
    });
    svg.addEventListener('pointerleave', function () { if (!scrubbing) hide(); });

    /* Touch: press and drag scrubs, lifting navigates. The strip only claims
       horizontal gestures (touch-action: pan-y in the stylesheet), so a
       vertical swipe that happens to start on it still scrolls the page. */
    svg.addEventListener('pointerdown', function (ev) {
      if (ev.pointerType !== 'touch') return;
      scrubbing = true;
      try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* fine */ }
      show(ev.clientX);
    });
    svg.addEventListener('pointerup', function (ev) {
      if (ev.pointerType !== 'touch') return;
      scrubbing = false;
      var n = pageAt(ev.clientX);
      hide();
      window.location.href = base.replace('{n}', String(n));
    });
    svg.addEventListener('pointercancel', function () { scrubbing = false; hide(); });

    svg.addEventListener('click', function (ev) {
      window.location.href = base.replace('{n}', String(pageAt(ev.clientX)));
    });

    window.addEventListener('blur', hide);
    window.addEventListener('scroll', function () { if (!scrubbing) hide(); }, { passive: true });
  }

  /* ==================================================================== */
  /* Page turns                                                           */
  /* ==================================================================== */

  /* Direction follows the document, not the history. Going from page 4 to
     page 3 moves the same way whether the reader used the "previous page"
     link or the browser's back button, because they mean the same thing when
     what you are holding is a document. */
  function pageOf(url) {
    var m = /\/p\/(\d+)\/(?:index\.html)?(?:[?#].*)?$/.exec(String(url || ''));
    return m ? parseInt(m[1], 10) : 0;
  }

  function docOf(url) {
    var m = /\/d\/([^/]+)\/p\/\d+\//.exec(String(url || ''));
    return m ? m[1] : '';
  }

  function direction(from, to) {
    var a = pageOf(from), b = pageOf(to);
    if (!a || !b || a === b || docOf(from) !== docOf(to)) return '';
    return b > a ? 'forward' : 'back';
  }

  /* Everything below happens on the *arriving* document, and only there.

     A cross-document transition runs in the new document: its
     ::view-transition pseudo-elements are children of the new root, so the
     selectors that give the movement a direction are matched against the new
     root as well. The outgoing page can set view-transition-types in its
     `pageswap` handler, and they do not travel - measured: added on the way
     out, empty on the way in - so a `pageswap` listener here would be a
     listener that does nothing, and there is not one.

     `pagereveal` fires at the browser's first rendering opportunity, which
     comes before deferred scripts run: 464ms against 489ms on a warm cache,
     and the direction was silently lost about four times in five. That is why
     the page template also asks for this file in the head. Registered from
     there it is 100ms ahead of the reveal rather than 25ms behind it. */
  function initTurns() {
    if (!('onpagereveal' in window)) return;

    window.addEventListener('pagereveal', function (ev) {
      if (!ev.viewTransition || reducedMotion()) return;
      var nav = window.navigation;
      var from = nav && nav.activation && nav.activation.from ? nav.activation.from.url : '';
      var dir = direction(from, window.location.href);
      if (!dir) return;
      html.setAttribute('data-turn', dir);
      if (ev.viewTransition.types && ev.viewTransition.types.add) {
        try { ev.viewTransition.types.add(dir); } catch (e) { /* fine */ }
      }
      var done = function () { html.removeAttribute('data-turn'); };
      ev.viewTransition.finished.then(done, done);
    });
  }

  /* ---------------------------------------------------------------- init */

  initTurns();

  function init() {
    makeRegion();
    try { initLens(); } catch (e) { /* never fatal: the scan is still an img */ }
    Array.prototype.forEach.call(
      doc.querySelectorAll('svg.ribbon[data-base]'),
      function (svg) { try { wireRibbon(svg); } catch (e) { /* leave it a picture */ } }
    );
  }

  /* Two reasons this waits. Read from the head there is no document yet; read
     from the deferred sweep there is one, but viewer.js - which publishes the
     token-to-box join the lens flies by - has not run, because it is loaded
     after this file. Waiting for the document covers both. */
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
  else window.setTimeout(init, 0);
})();
