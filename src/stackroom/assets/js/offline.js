/* Stackroom: keeping the archive readable with the network off.
 *
 * This file registers the service worker the build generated and puts one
 * quiet line in the colophon saying where the reader stands. Like everything
 * else in assets/js it is an enhancement: with JavaScript off, nothing here
 * runs, nothing appears, and every page of the archive still reads.
 *
 * Two things it refuses to do:
 *
 *   - Download the archive without being asked. A collection can be gigabytes.
 *     The size is stated on the button before a single byte is fetched.
 *   - Pretend. On file://, over plain http, or in a browser with no service
 *     workers, it says so in one sentence rather than showing a control that
 *     silently does nothing.
 */
(function () {
  'use strict';

  var doc = document;
  /* prefs.js runs from the head of every template and publishes the archive's
     own language; this deferred file always runs after it. */
  var sr = window.stackroomReader || {
    t: function (k) { return '[' + k + ']'; },
    n: function (v) { return String(v); },
    pct: function (v, d, ofOne) { return Math.round(ofOne ? v * 100 : v) + '%'; }
  };

  /* ---------------------------------------------------------------- root */

  /* Every URL in a Stackroom archive is relative, so this file has to work out
     where the site root is rather than being told. Its own src is the exact
     answer; the stylesheet link is the fallback for a browser that has not set
     currentScript by the time a deferred script runs. */
  function siteRoot() {
    var self = doc.currentScript;
    if (self && self.src) {
      var i = self.src.lastIndexOf('assets/js/');
      if (i >= 0) return self.src.slice(0, i);
    }
    var css = doc.querySelector('link[rel="stylesheet"][href$="assets/stackroom.css"]');
    if (css) {
      var href = css.getAttribute('href') || '';
      return href.slice(0, href.length - 'assets/stackroom.css'.length);
    }
    return './';
  }

  var ROOT = siteRoot();
  var KILL_FLAG = 'stackroom-offline-off';
  var DAY = 24 * 3600 * 1000;

  /* ------------------------------------------------------------- the line */

  var box = null;
  var text = null;
  var actions = null;
  var meter = null;

  function mount() {
    if (box) return box;
    var host = doc.querySelector('.colophon .wrap') || doc.querySelector('.colophon') || doc.body;
    box = doc.createElement('div');
    box.className = 'offline';
    box.hidden = true;
    text = doc.createElement('p');
    text.className = 'offline__say';
    /* Polite, not assertive: this line changes while a reader is reading and
       must never interrupt them mid-sentence. */
    text.setAttribute('role', 'status');
    text.setAttribute('aria-live', 'polite');
    actions = doc.createElement('p');
    actions.className = 'offline__do';
    box.appendChild(text);
    box.appendChild(actions);
    host.appendChild(box);
    return box;
  }

  function say(message, controls) {
    mount();
    box.hidden = false;
    text.textContent = message;
    actions.textContent = '';
    (controls || []).forEach(function (control) { actions.appendChild(control); });
  }

  function button(label, onClick, className) {
    var el = doc.createElement('button');
    el.type = 'button';
    el.className = 'offline__btn' + (className ? ' ' + className : '');
    el.textContent = label;
    el.addEventListener('click', onClick);
    return el;
  }

  function progress(fraction) {
    if (!meter || !meter.isConnected) {
      meter = doc.createElement('span');
      meter.className = 'offline__bar';
      meter.setAttribute('role', 'progressbar');
      meter.setAttribute('aria-valuemin', '0');
      meter.setAttribute('aria-valuemax', '100');
      var fill = doc.createElement('span');
      fill.className = 'offline__fill';
      meter.appendChild(fill);
    }
    meter.setAttribute('aria-valuenow', String(Math.round(fraction * 100)));
    meter.firstChild.style.width = (fraction * 100).toFixed(1) + '%';
    return meter;
  }

  /* Binary units, because that is what a browser's storage estimate is in and
     a reader comparing the two should not have to convert. The unit itself is
     a message - bytes.kb and its neighbours, the same keys the colophon uses -
     so a Russian archive says КБ in both places. */
  var UNITS = ['bytes.kb', 'bytes.mb', 'bytes.gb', 'bytes.tb'];

  function human(n) {
    if (n === null || n === undefined) return '';
    if (n < 1024) return sr.t('bytes.b', { count: n, size: sr.n(n) });
    var i = -1;
    do { n /= 1024; i += 1; } while (n >= 1024 && i < UNITS.length - 1);
    var rounded = n < 10 ? Math.round(n * 10) / 10 : Math.round(n);
    return sr.t(UNITS[i], {
      count: rounded,
      size: sr.n(rounded, n < 10 ? 1 : 0)
    });
  }

  /* A count of files, said the way the language says a count of files. */
  function files(n) {
    return sr.t('js.offline.files', { count: n });
  }

  /* --------------------------------------------------- the honest refusals */

  function unsupported() {
    if (location.protocol === 'file:') return sr.t('js.offline.file');
    if (typeof window.isSecureContext === 'boolean' && !window.isSecureContext) {
      return sr.t('js.offline.insecure');
    }
    if (!('serviceWorker' in navigator) || !navigator.serviceWorker ||
        !('caches' in window) || !window.caches) {
      return sr.t('js.offline.unsupported');
    }
    return null;
  }

  /* ------------------------------------------------------------ the worker */

  var registration = null;

  function ask(message, timeout) {
    /* One question, one answer, over a private channel: several pages of this
       archive can be open at once and none of them should see another's
       replies. */
    return new Promise(function (resolve, reject) {
      var worker = (registration && (registration.active || registration.waiting)) ||
                   navigator.serviceWorker.controller;
      if (!worker) { reject(new Error('no worker')); return; }
      var channel = new MessageChannel();
      var timer = setTimeout(function () { reject(new Error('timeout')); }, timeout || 15000);
      channel.port1.onmessage = function (event) {
        clearTimeout(timer);
        resolve(event.data);
      };
      try {
        worker.postMessage(message, [channel.port2]);
      } catch (err) {
        clearTimeout(timer);
        reject(err);
      }
    });
  }

  function refresh() {
    return ask({ type: 'stats' }).then(showState, function () {
      say(sr.t('js.offline.preparing'));
    });
  }

  function showState(stats) {
    if (!stats || stats.error) {
      say(sr.t('js.offline.preparing'));
      return;
    }
    if (registration && registration.waiting && navigator.serviceWorker.controller) {
      say(sr.t('js.offline.newer'), [
        button(sr.t('js.offline.load_new'), function () {
          ask({ type: 'skip-waiting' }).catch(function () {});
          if (registration.waiting) registration.waiting.postMessage({ type: 'skip-waiting' });
          setTimeout(function () { location.reload(); }, 250);
        })
      ]);
      return;
    }

    var count = stats.files || 0;
    var held = stats.held || 0;
    var share = count ? Math.min(1, held / count) : 0;
    var everything = share >= 0.999;

    if (everything) {
      say(sr.t('js.offline.whole', { size: human(stats.bytes), files: files(count) }),
          [remove()]);
      return;
    }

    var line = sr.t('js.offline.partial', {
      held: held, files: files(count), percent: sr.pct(share, 0, true)
    });
    var store = button(sr.t('js.offline.store_all', { size: human(stats.bytes) }),
      function () { storeAll(stats); }, 'offline__btn--go');
    if (stats.quota && stats.quota.quota && stats.bytes > stats.quota.quota - (stats.quota.usage || 0)) {
      line = sr.t('js.offline.tight', {
        line: line,
        size: human(stats.quota.quota - (stats.quota.usage || 0))
      });
    }
    say(line, [store, remove()]);
  }

  function remove() {
    return button(sr.t('js.offline.remove'), function () {
      say(sr.t('js.offline.removing'));
      ask({ type: 'clear' }, 30000).then(refresh, refresh);
    }, 'offline__btn--quiet');
  }

  function storeAll(stats) {
    say(sr.t('js.offline.storing', { percent: sr.pct(0) }), [progress(0), stop()]);
    /* No timeout that means anything: storing a gigabyte over a phone
       connection is a legitimate hour. The Stop button is the way out. */
    ask({ type: 'store-all' }, DAY).then(function (result) {
      if (result && result.quota) {
        say(sr.t('js.offline.full', {
          done: result.done || 0, total: files(result.total || 0)
        }), [remove()]);
        return;
      }
      if (result && result.stopped) {
        say(sr.t('js.offline.stopped', { count: result.done || 0 }), []);
        return refresh();
      }
      if (result && result.failed) {
        say(sr.t('js.offline.failed', { count: result.failed }), []);
        return refresh();
      }
      return refresh();
    }, function () {
      say(sr.t('js.offline.interrupted'), []);
      setTimeout(refresh, 500);
    });
  }

  /* The worker reports every eighth file. The final report is ignored here
     because storeAll's own reply says what actually happened, including the
     failure cases a percentage cannot express. */
  if ('serviceWorker' in navigator && navigator.serviceWorker) {
    navigator.serviceWorker.addEventListener('message', function (event) {
      var data = event.data || {};
      /* The operator published a `sw-kill` file. A worker that is still
         controlling this page cannot be told to stop from inside another
         worker, so the clearing happens here instead. */
      if (data.type === 'killed') {
        sweep();
        setTimeout(sweep, 1500);
        say(sr.t('js.offline.killed_by_publisher'), []);
        return;
      }
      if (data.type !== 'progress' || data.finished) return;
      var share = data.total ? data.done / data.total : 0;
      say(sr.t('js.offline.storing_progress', {
        percent: sr.pct(share, 0, true), size: human(data.bytes)
      }), [progress(share), stop()]);
    });
  }

  function stop() {
    return button(sr.t('js.offline.stop'), function () {
      ask({ type: 'stop' }, 2000).catch(function () {});
      if (navigator.serviceWorker.controller) {
        navigator.serviceWorker.controller.postMessage({ type: 'stop' });
      }
    }, 'offline__btn--quiet');
  }

  /* ---------------------------------------------------------- kill switch */

  /* Two ways out, because a broken worker is the one failure that a reader
     cannot route around by reloading:
       - a reader: open any page of the archive with ?stackroom-offline=off
       - the operator: publish a file called `sw-kill` beside sw.js, which the
         worker checks whenever a new build activates (see sw.js).
     A third is always available and needs nobody's cooperation: the browser's
     own "clear site data". */
  function killRequested() {
    if (location.search.indexOf('stackroom-offline=off') >= 0) return true;
    if ((location.hash || '').indexOf('stackroom-offline=off') >= 0) return true;
    try {
      return window.localStorage.getItem(KILL_FLAG) === '1';
    } catch (err) {
      return false;
    }
  }

  function kill() {
    try { window.localStorage.setItem(KILL_FLAG, '1'); } catch (err) { /* private mode */ }
    var done = function () {
      say(sr.t('js.offline.killed'),
          [button(sr.t('js.offline.turn_back_on'), function () {
            try { window.localStorage.removeItem(KILL_FLAG); } catch (err) { /* ignore */ }
            location.href = location.pathname;
          }, 'offline__btn--quiet')]);
    };
    if (!('serviceWorker' in navigator) || !navigator.serviceWorker) { done(); return; }
    /* Tell the worker first. A worker that is still controlling this page
       will happily re-create its runtime cache from the next subresource it
       sees, so it has to be made inert before anything is deleted - otherwise
       clearing races the thing doing the caching, and loses. */
    var told = navigator.serviceWorker.controller
      ? ask({ type: 'kill' }, 10000).catch(function () {})
      : Promise.resolve();
    told.then(function () {
      return navigator.serviceWorker.getRegistrations();
    }).then(function (all) {
      return Promise.all(all.map(function (r) { return r.unregister(); }));
    }).catch(function () {}).then(function () {
      if (!('caches' in window)) return null;
      return caches.keys().then(function (names) {
        return Promise.all(names.map(function (n) {
          return /^stackroom-/.test(n) ? caches.delete(n) : null;
        }));
      });
    }).catch(function () {}).then(function () {
      /* One more pass, once the requests that were already in flight when the
         worker was disarmed have landed. Without it a font or a thumbnail
         arriving a moment late puts an entry back into a cache that is
         supposed to be gone. */
      setTimeout(sweep, 1500);
      done();
    });
  }

  function sweep() {
    if (!('caches' in window) || !window.caches) return;
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (n) {
        return /^stackroom-/.test(n) ? caches.delete(n) : null;
      }));
    }).catch(function () {});
  }

  /* ---------------------------------------------------------------- start */

  function start() {
    if (killRequested()) { kill(); return; }

    var refusal = unsupported();
    if (refusal) { say(refusal, []); return; }

    say(sr.t('js.offline.starting'));
    navigator.serviceWorker.register(ROOT + 'sw.js', { scope: ROOT }).then(function (reg) {
      registration = reg;
      reg.addEventListener('updatefound', function () {
        var worker = reg.installing;
        if (!worker) return;
        worker.addEventListener('statechange', function () {
          if (worker.state === 'installed' || worker.state === 'activated') refresh();
        });
      });
      /* The first install has no controller yet, so wait for one rather than
         reporting 0% to a reader whose archive is in fact being stored. */
      if (!navigator.serviceWorker.controller) {
        navigator.serviceWorker.addEventListener('controllerchange', refresh, { once: true });
      }
      return refresh();
    }).catch(function (err) {
      /* Registration can fail for reasons that are nobody's fault: a private
         window, an enterprise policy, a browser with storage switched off.
         The archive is unaffected, so this is a statement, not an error. */
      say(sr.t('js.offline.no_store'), []);
      void err;
    });
  }

  /* Nothing this file does is worth an unhandled exception on a page whose
     only job is to be readable. Whatever went wrong, the archive did not. */
  function begin() {
    try {
      start();
    } catch (err) {
      say(sr.t('js.offline.no_store'), []);
    }
  }

  if (doc.readyState === 'loading') {
    doc.addEventListener('DOMContentLoaded', begin);
  } else {
    begin();
  }
})();
