/* Stackroom offline service worker.
 *
 * An archive that only exists while a server is up is not an archive. This
 * file makes a published Stackroom site readable with the network switched
 * off, after one visit.
 *
 * It is generated at build time: `stackroom.build.offline` substitutes the
 * three constants below and writes the result to the site root. The file in
 * the source tree is valid JavaScript exactly as it stands, so it can be
 * parsed and linted without running a build.
 *
 * Rules this file will not break
 * ------------------------------
 *
 * 1. FAIL OPEN. A service worker sits in front of every request a reader
 *    makes. If this one throws, the site must still load. Every handler is
 *    wrapped, and every failure path ends in a plain `fetch()`.
 *
 * 2. NEVER TOUCH ANOTHER ORIGIN. There is nothing cross-origin in a Stackroom
 *    site (ARCHITECTURE.md guarantee 5) and this worker enforces it: a request
 *    to any other origin is not inspected, not cached and not answered.
 *
 * 3. NEVER ANSWER A RANGE REQUEST. `caches.match` ignores the Range header and
 *    hands back the whole body with a 200, which is a lie to anything that
 *    asked for bytes 4096-8191 - a PDF viewer paging through an original, most
 *    of all. Ranged requests go straight to the network.
 *
 * 4. ONE BUILD, ONE CACHE. The cache name carries a digest of the build. A
 *    rebuilt archive lands in a new cache and every older cache is deleted on
 *    activation, so a reader is never served half of one build and half of
 *    another. The new worker waits rather than seizing control mid-session:
 *    the pages already open keep the build they started with.
 *
 * 5. NOTHING LARGE WITHOUT ASKING. Page HTML, thumbnails and page images are
 *    cached as the reader actually visits them. The original documents are
 *    not - an archive can be gigabytes, and spending a reader's disk and data
 *    without asking is hostile. Storing all of it is an explicit action.
 */
'use strict';

/* ---- generated at build time ------------------------------------------ */

/* A digest over the source documents, the generator version and every file in
   the precache. Any of those changing gives a new cache and drops the old. */
const BUILD = '__STACKROOM_BUILD__';

/* The shell: enough to render any of the standing pages with no network. */
const PRECACHE = ['__STACKROOM_PRECACHE__'];

/* Where the full inventory lives, relative to this file. Fetched only when a
   reader asks to store the whole archive, because on a large collection it is
   the one file that is proportional to the size of the site: 120,000 entries
   for a 20,000-page archive. Nothing on the ordinary path touches it. */
const INVENTORY = '__STACKROOM_INVENTORY__';

/* How big the archive is, counted at build time. The indicator has to state
   the size before a reader agrees to store it, and it must not have to
   download a megabyte-long file list to do that. */
const TOTALS = {"__STACKROOM_TOTALS__": 0};

/* Set the moment a kill is seen. A disarmed worker answers nothing and
   stores nothing: every request goes to the network as if it were not here.
   Unregistering alone is not enough, because the page will register again on
   the next load and the new worker has to be just as inert until the operator
   takes the kill file down. */
let disarmed = false;
let killChecked = false;

/* ---- names ------------------------------------------------------------ */

const SHELL = 'stackroom-shell-' + BUILD;
const RUNTIME = 'stackroom-runtime-' + BUILD;
const MINE = /^stackroom-(shell|runtime)-/;

/* Everything is resolved against this worker's own URL rather than against
   the registered scope, so the site works in a subdirectory and keeps working
   if it is ever registered with a narrower scope than the directory it sits
   in. Every URL in this file is relative, like everything else in a Stackroom
   archive. */
const ROOT = new URL('./', self.location.href);

/* An operator who published a broken worker needs a way to disarm it in every
   browser that already has it, without being able to set a header. Uploading a
   file called `sw-kill` next to `sw.js` does that: the next time a worker for
   this site starts up it sees the file, deletes every cache it owns, tells
   every open page to do the same, stops answering anything and unregisters.
   The check runs on install, on activation, and once per worker start-up when
   a page asks for its storage figures - not once per request. Chrome spins an
   idle worker down after about thirty seconds, so a kill lands within a page
   or two of the file appearing, with no new build and nothing asked of the
   reader. A normal build does not contain the file, so the whole mechanism
   costs one 404 per worker start-up.

   Two other ways out exist and need nothing from the operator: a reader can
   open any page with `?stackroom-offline=off` (see assets/js/offline.js), and
   the browser's own "clear site data" always works. */
const KILL = 'sw-kill';

/* Runtime caching is for the things a reader is looking at: page HTML,
   thumbnails, page images, word boxes, the search index. Original documents
   are deliberately excluded - they are the biggest thing in the archive by a
   wide margin, and a reader who clicked "download the original" wants the file,
   not a copy of it on their disk forever. They are stored only when someone
   asks for the whole archive. */
const RUNTIME_SKIP = /^files\//;

/* ---- helpers ---------------------------------------------------------- */

function url(path) {
  return new URL(path, ROOT).href;
}

/* The path of a same-origin request relative to the site root, or null if the
   request is outside this archive. Used for every decision below, so a site
   sharing an origin with something else never has its neighbour cached. */
function inScope(request) {
  let u;
  try {
    u = new URL(request.url);
  } catch (e) {
    return null;
  }
  if (u.origin !== self.location.origin) return null;
  if (!u.pathname.startsWith(ROOT.pathname)) return null;
  return u.pathname.slice(ROOT.pathname.length);
}

/* Cache keys drop the query string. Pagefind asks for its entry file with a
   `?ts=` cache-buster, and storing that under the busted URL would mean it is
   never found again. Nothing this project generates distinguishes two files by
   query string, so dropping it is safe here and nowhere near safe in general. */
function key(request) {
  const u = new URL(request.url);
  u.search = '';
  u.hash = '';
  return u.href;
}

function cacheable(response) {
  /* `basic` is same-origin. An opaque response has an unknown status and a
     body we are not allowed to read, so caching one would put a possible
     error page in front of a real file forever. */
  return response && response.ok && response.type === 'basic';
}

async function putSafely(cacheName, request, response) {
  try {
    const cache = await caches.open(cacheName);
    await cache.put(key(request), response);
    return true;
  } catch (err) {
    /* Out of quota, or the browser evicted the cache under us. Serving the
       response we already have is more useful than throwing. */
    return false;
  }
}

/* ---- install ---------------------------------------------------------- */

self.addEventListener('install', function (event) {
  event.waitUntil((async function () {
    /* Checked here as well as on activation. A new worker with no
       `skipWaiting` sits in `waiting` until every tab closes, which can be
       days - and a kill switch that takes days is not a kill switch. */
    if (await killed()) return;
    const cache = await caches.open(SHELL);
    /* Not `addAll`: it rejects the whole install if any one file 404s, and a
       shell that is 39 files out of 40 is worth having. Whatever is missing is
       fetched from the network on demand like anything else. */
    await Promise.allSettled(PRECACHE.map(async function (path) {
      const request = new Request(url(path), { cache: 'reload' });
      const response = await fetch(request);
      if (!cacheable(response)) throw new Error(path);
      await cache.put(url(path), response);
    }));
  })());
});

/* ---- activate --------------------------------------------------------- */

self.addEventListener('activate', function (event) {
  event.waitUntil((async function () {
    if (await killed()) return;
    const names = await caches.keys();
    await Promise.all(names.map(function (name) {
      /* Only ever delete our own, and only ever from an older build. Another
         tool sharing this origin keeps its caches. */
      if (!MINE.test(name)) return null;
      if (name === SHELL || name === RUNTIME) return null;
      return caches.delete(name);
    }));
    await self.clients.claim();
  })());
});

async function killed() {
  if (disarmed) return true;
  try {
    const response = await fetch(url(KILL), { cache: 'no-store' });
    if (!response || !response.ok) return false;
  } catch (err) {
    return false;                       /* offline, or no such file: normal */
  }
  disarmed = true;
  await clearAll();
  /* The worker that finds the kill file is usually the *new* one, installing
     alongside an older one that is still controlling every open tab and still
     re-filling its runtime cache from every image those tabs request. It
     cannot be reached from here, so the pages are told instead and they do the
     clearing from their own side, where the caches are equally reachable. */
  try {
    const clients = await self.clients.matchAll({ includeUncontrolled: true, type: 'window' });
    for (const client of clients) client.postMessage({ type: 'killed' });
  } catch (err) {
    /* Best effort; `disarmed` still holds for this worker. */
  }
  /* Unregistering is the tidy half; `disarmed` is the half that matters,
     because a page that loads a second later will register a worker again and
     that one has to do nothing too. */
  try {
    await self.registration.unregister();
  } catch (err) {
    /* Nothing to do about it, and being inert is already the whole effect. */
  }
  return true;
}

/* ---- fetch ------------------------------------------------------------ */

self.addEventListener('fetch', function (event) {
  const request = event.request;

  /* Everything below this line is a reason to get out of the way entirely.
     Not answering is always safe; answering wrongly is not. */
  if (disarmed) return;                              /* the kill switch */
  if (request.method !== 'GET') return;
  if (request.headers.has('range')) return;          /* rule 3 */
  if (request.cache === 'only-if-cached' && request.mode !== 'same-origin') return;
  const path = inScope(request);
  if (path === null) return;                         /* rule 2 */

  event.respondWith(handle(request, path));
});

async function handle(request, path) {
  try {
    /* The archive is immutable for the life of a build - the cache name says
       which build - so a hit is always the right answer and never needs
       revalidating. That is what makes cache-first correct here rather than
       merely fast. */
    const hit = await caches.match(key(request), { ignoreSearch: true });
    if (hit) return hit;

    /* A directory URL a reader typed by hand, or a link from outside. */
    if (request.mode === 'navigate' && path.endsWith('/')) {
      const index = await caches.match(url(path + 'index.html'));
      if (index) return index;
    }

    const response = await fetch(request);
    /* Checked again on this side of the await. A request that was in flight
       when the kill arrived would otherwise re-create the cache that was just
       deleted, which is exactly how a kill switch ends up not working. */
    if (!disarmed && cacheable(response) && !RUNTIME_SKIP.test(path)) {
      /* Cache a clone and hand the original to the page: a response body can
         only be read once. Deliberately not awaited - the reader should not
         wait on our bookkeeping. */
      putSafely(RUNTIME, request, response.clone());
    }
    return response;
  } catch (err) {
    /* The network failed and nothing is stored. For a page, say so in a
       readable way rather than letting the browser's error screen imply the
       archive is gone. For anything else, let the failure be a failure. */
    if (request.mode === 'navigate') {
      const shell = await caches.match(url('index.html'));
      return new Response(offlinePage(Boolean(shell)), {
        status: 503,
        headers: { 'Content-Type': 'text/html; charset=utf-8' }
      });
    }
    throw err;
  }
}

function offlinePage(haveIndex) {
  const home = haveIndex
    ? '<p><a href="' + url('index.html') + '">Go to the front of the archive</a>, which is stored on this device.</p>'
    : '<p>Nothing from this archive is stored on this device yet.</p>';
  return '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width, initial-scale=1">'
    + '<title>Not stored offline</title>'
    + '<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:3rem 1.5rem;max-width:34rem;'
    + 'color:#1c1a17;background:#fbfaf7}h1{font-size:1.25rem;margin:0 0 .75rem}'
    + 'a{color:inherit}@media(prefers-color-scheme:dark){body{color:#e8e4dc;background:#171614}}</style>'
    + '</head><body><h1>This page is not stored on this device.</h1>'
    + '<p>You are offline, and this page was not among the ones saved here. '
    + 'Reconnect and reload, or open it again once you are online and it will '
    + 'be kept.</p>' + home + '</body></html>';
}

/* ---- messages from the page ------------------------------------------- */

/* Set by a `stop` message while `store-all` is running; read by its workers
   between files, so stopping is immediate but never leaves a half-written
   entry in the cache. */
let stopped = false;

self.addEventListener('message', function (event) {
  const data = event.data || {};
  const reply = function (payload) {
    if (event.ports && event.ports[0]) event.ports[0].postMessage(payload);
  };
  if (data.type === 'skip-waiting') {
    self.skipWaiting();
    return;
  }
  if (data.type === 'stats') {
    /* Every page asks for this once, on load, and a postMessage wakes a worker
       that has been spun down - which makes this the one reliable moment to
       look for the operator's kill file. Once per worker start-up, so a reader
       pays one 404 every half-minute of active reading at the very most, and
       nothing at all while offline. It is not awaited: the indicator does not
       wait on it and neither does the page. */
    if (!killChecked) {
      killChecked = true;
      event.waitUntil(killed().catch(function () { return false; }));
    }
    event.waitUntil(stats().then(reply, function (e) { reply({ error: String(e) }); }));
    return;
  }
  if (data.type === 'store-all') {
    event.waitUntil(storeAll(event.source && event.source.id).then(reply, function (e) {
      reply({ error: String(e) });
    }));
    return;
  }
  if (data.type === 'stop') {
    stopped = true;
    return;
  }
  if (data.type === 'clear') {
    event.waitUntil(clearAll().then(reply, function (e) { reply({ error: String(e) }); }));
    return;
  }
  if (data.type === 'kill') {
    /* Inert first, then empty. The other order races the fetch handler, which
       re-creates the runtime cache from the next image the page asks for. */
    disarmed = true;
    stopped = true;
    event.waitUntil((async function () {
      await clearAll();
      try { await self.registration.unregister(); } catch (err) { /* already gone */ }
      reply({ ok: true });
    })());
  }
});

async function inventory() {
  const response = await fetch(url(INVENTORY), { cache: 'no-store' });
  if (!response.ok) throw new Error('no inventory');
  return response.json();
}

async function stats() {
  const runtime = await caches.open(RUNTIME);
  const shell = await caches.open(SHELL);
  const held = new Set();
  for (const cache of [shell, runtime]) {
    for (const request of await cache.keys()) held.add(request.url);
  }
  let quota = null;
  if (navigator.storage && navigator.storage.estimate) {
    try { quota = await navigator.storage.estimate(); } catch (e) { quota = null; }
  }
  return {
    build: BUILD,
    /* Cache entries this build holds. Every one of them is a file in TOTALS,
       so held/files is a true share of the archive - which is the number the
       indicator shows, and it costs no network to compute. */
    held: held.size,
    files: TOTALS.files,
    bytes: TOTALS.bytes,
    originals: TOTALS.originals,
    quota: quota ? { usage: quota.usage, quota: quota.quota } : null
  };
}

async function report(clientId, payload) {
  const client = clientId ? await self.clients.get(clientId) : null;
  const targets = client ? [client] : await self.clients.matchAll();
  for (const target of targets) target.postMessage(Object.assign({ type: 'progress' }, payload));
}

async function storeAll(clientId) {
  stopped = false;
  const inv = await inventory();
  const cache = await caches.open(RUNTIME);
  const shell = await caches.open(SHELL);
  const held = new Set();
  for (const c of [shell, cache]) {
    for (const request of await c.keys()) held.add(request.url);
  }

  const wanted = inv.files.filter(function (entry) { return !held.has(url(entry[0])); });
  let done = 0;
  let bytes = 0;
  let failed = 0;
  let quotaHit = false;

  /* Four at a time. Enough to keep a connection busy, few enough that the
     reader can still use the site while it runs and that a phone on a slow
     connection is not holding twenty sockets open. */
  const WIDTH = 4;
  const queue = wanted.slice();

  async function worker() {
    while (queue.length && !stopped && !quotaHit) {
      const entry = queue.shift();
      try {
        const response = await fetch(url(entry[0]), { cache: 'no-store' });
        if (!cacheable(response)) { failed += 1; continue; }
        await cache.put(url(entry[0]), response);
        bytes += entry[1];
      } catch (err) {
        if (err && (err.name === 'QuotaExceededError' || String(err).indexOf('Quota') >= 0)) {
          quotaHit = true;
          break;
        }
        failed += 1;
      }
      done += 1;
      if (done % 8 === 0 || !queue.length) {
        await report(clientId, {
          done: done, total: wanted.length, bytes: bytes, failed: failed,
          stopped: stopped, quota: quotaHit
        });
      }
    }
  }

  await Promise.all(Array.from({ length: WIDTH }, worker));
  await report(clientId, {
    done: done, total: wanted.length, bytes: bytes, failed: failed,
    stopped: stopped, quota: quotaHit, finished: true
  });
  return { done: done, total: wanted.length, failed: failed, quota: quotaHit, stopped: stopped };
}

async function clearAll() {
  const names = await caches.keys();
  await Promise.all(names.map(function (n) {
    return MINE.test(n) ? caches.delete(n) : null;
  }));
  return { ok: true };
}
