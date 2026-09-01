# Publishing an archive

You have run `stackroom build` and you have a `site/` directory. This is how to
get it in front of people, what each option costs, and what to do when the
archive is bigger than the place you wanted to put it.

The short version: it is a folder of ordinary files with no server-side
anything, so almost any host will do. The details below are the handful of
places where "almost" matters.

---

## Before you put it anywhere

```sh
stackroom check ./release     # failed redactions, without building a site
stackroom serve site          # look at it yourself first
```

`check` writes no site. It does rasterise every page to look at the pixels, and
those images go to a temporary folder whose path it prints and which it deletes
when it finishes; `--scratch` puts that folder wherever you say, which is what
to use if these documents must not touch a disk. It never uses the page cache,
in either direction, for the same reason.

`stackroom serve` binds to loopback and exists to be run for ninety seconds.
Open the front page, run a search, click through to a scan, download an
original. Five minutes here is cheaper than a correction.

Three things to look at while you are in there:

- **The about page.** If `about.md` is empty, the front page says so, and a
  reader has no way to judge what they are looking at. Who released these, under
  what request, and what is missing — that last one especially.
- **The withheld ledger.** It is often the story, and it is also where a
  mis-detected scan artefact shows up as an implausible percentage.
- **Any page marked unreadable.** Stackroom says where recognition failed; a
  reader searching those pages will find nothing and has to be told why.

## What you are about to upload

```
site/
  index.html  browse/  search/  withheld/  about/
  d/<doc>/p/<n>/index.html      one HTML file per page
  data/<doc>/<n>.json           word boxes for that page
  media/<doc>/p<n>@…            renderings: two widths and a thumbnail
  files/<doc>.pdf               the original, byte for byte
  manifest.json                 SHA-256 of every source file
  _pagefind/                    the search index
  sw.js  offline.json           the service worker and its inventory
  .nojekyll
```

With the default settings that is **eight files per page** — the HTML, the JSON,
and six images (two widths and a thumbnail, each as AVIF and WebP) — plus one
original PDF per document. Five files per page if AVIF encoding was not
available when you built.

Size is dominated by `media/`, then by `files/`. A 150 dpi scan of a typed page
encodes to roughly 100 KB as WebP at 1600 px, and rather less as AVIF, so a page
of scanned text across all its variants tends to land in the low hundreds of
kilobytes. Do not take that as a promise — measure yours:

```sh
du -sh site
du -sh site/*
find site -type f | wc -l
```

Those two numbers, total bytes and total files, are what every host below is
going to have an opinion about.

---

## GitHub Pages

The common case, and a good one: free, versioned, HTTPS, and the archive sits
beside the documents that produced it.

### Two ways to do it

**Build in Actions from the documents.** Put the PDFs in a repository, and let
a workflow install Stackroom, build, and deploy. This is the arrangement worth
having: the site is a function of the documents, anyone can see how it was made,
and rebuilding after the agency sends the next tranche is a `git push`.

[`docs/gh-pages-workflow.yml`](gh-pages-workflow.yml) in this repository is a
complete workflow to copy into your own. Three lines to change, all marked.

**Or commit the built site.** Build locally, push `site/` to a branch, and point
Pages at it. Simpler, and it works, but the repository no longer explains
itself, and anyone who wants to check your work has to take the HTML on trust.

Either way, once: **Settings → Pages → Build and deployment → Source: GitHub
Actions** (or "Deploy from a branch", for the second route).

### The limits, which are real

| | |
|---|---|
| Published site | **1 GB.** Hard. |
| Any single file **in the repository** | **100 MB.** Git refuses the push; a warning at 50 MB. |
| Deployment | **10 minutes.** Not the build — the deploy step. A large site can exceed it. |
| Bandwidth | **100 GB/month**, soft. |
| Builds | 10/hour, soft — and it does not apply to your own Actions workflow. |
| Source repository | 1 GB recommended. |

The 1 GB is the one that bites, and at a few hundred kilobytes a page it stops
being theoretical somewhere in the low thousands of pages. See [when it does not
fit](#when-the-archive-is-bigger-than-the-host-allows).

The 100 MB per file applies to what you commit, not to what Stackroom generates.
A single 140 MB PDF from an agency cannot go in the repository at all — that is
a Git limit, not a Pages one, and Git LFS is the usual answer (with `lfs: true`
on the checkout step, or the workflow will deploy pointer files).

### GitHub Pages cannot set custom HTTP headers

There is no `_headers` file, no `.htaccess`, no configuration of any kind. You
get the headers GitHub sends and no others. Concretely, on Pages you cannot:

- set `Cache-Control`, so you cannot mark the content-hashed search fragments as
  immutable;
- send a `Content-Security-Policy`, `Permissions-Policy` or any other security
  header;
- set CORS headers, so another site cannot fetch your `manifest.json`;
- override a content type, or control what gets compressed.

None of that stops a Stackroom archive from working, and it is worth being
precise about why. Every link in the site is relative, so nothing needs
rewriting. The search index compresses its own files and decompresses them in
the client, so it needs no `Content-Encoding` and no server gzip at all —
Pagefind's own hosting documentation says as much, and the configuration in
[the section on plain servers](#the-one-thing-to-get-right-on-any-server) below
is about stopping a server from *helping*, which Pages will not do. There is
nothing left for a header to configure.

If you need headers — a CSP, cross-origin access to the manifest so other
people can build on it, or long-lived caching — that is the reason to move to
Netlify, Cloudflare, or a server of your own.

### Two more Pages details

**Keep `.nojekyll`.** Stackroom writes it into the site root. Without it, Pages
runs the site through Jekyll, Jekyll deletes any directory whose name starts
with an underscore, and `_pagefind/` disappears — search then fails completely,
with no error anywhere. If you use `actions/upload-pages-artifact`, set
`include-hidden-files: true`, because from v4 it drops dotfiles by default and
`.nojekyll` is a dotfile.

**Project sites need no configuration.** An archive published at
`https://you.github.io/repo/` works unchanged: every link in the site is
relative. You do not have to set `base_url` in `stackroom.toml` for this, and
setting it wrongly is a common way to break every link at once. It is for
citation URLs and the manifest, nothing else.

---

## Netlify

Worth it mainly for one thing: you can set headers. Drag the `site/` folder onto
the Netlify dashboard, or `netlify deploy --dir=site --prod`, and add a
`_headers` file at the root of the site:

```
/_pagefind/*.pf_meta
  Content-Type: application/octet-stream
  Cache-Control: public, max-age=31536000, immutable

/_pagefind/*.pf_index
  Content-Type: application/octet-stream
  Cache-Control: public, max-age=31536000, immutable

/_pagefind/*.pf_fragment
  Content-Type: application/octet-stream
  Cache-Control: public, max-age=31536000, immutable

/media/*
  Cache-Control: public, max-age=31536000

/files/*
  Cache-Control: public, max-age=31536000
```

The fragment and index filenames contain a content hash, so caching them
forever is safe. Do not cache `pagefind-entry.json`; the client appends its own
cache-buster to it anyway.

Netlify's free-tier bandwidth and build allowances have changed more than once
and are metered per account — check the current numbers before you point a
newsroom's traffic at it. The technical limits that matter to an archive are the
per-file size cap and the deploy's file count; both are generous compared with
Cloudflare's, and neither is documented as stably as GitHub's.

## Cloudflare Pages

Fast, free, and it has a **file-count limit that a document archive hits long
before it hits a size limit**:

| | |
|---|---|
| Files per site | **20,000** on the free plan; 100,000 on paid plans. |
| Any single file | **25 MiB**. |
| Headers | `_headers` file, up to 100 rules. |

Eight files per page means the free plan runs out at roughly **2,500 pages**,
and the 25 MiB per-file cap will reject a large scanned PDF outright — the
original stays in `files/`, so a 60 MB production is a problem here in a way it
is not on GitHub Pages.

If your archive is small enough, it is an excellent host, and the `_headers`
syntax is the same shape as Netlify's. If it is not, use R2 (object storage,
no file-count limit) with a Worker or a custom domain in front, which is the
Cloudflare answer for large archives.

---

## Any plain static server

nginx, Caddy, Apache, a VPS, an intranet box, a shared host. Nothing special is
required: point the document root at `site/` and serve files.

### The other thing, on every host: do not cache `sw.js`

The archive ships a service worker at the site root, and a reader's browser
looks for a new one against the ordinary HTTP cache rules. A `Cache-Control:
max-age` of hours or days on that one file means readers keep an old build's
worker after you republish. None of the configurations below matches it — they
name extensions and `sw.js` matches none of them — but a blanket rule of your
own will, and GitHub Pages, which sets no caching headers you can influence, is
safe here by accident.

If you ever publish a broken worker, put an empty file called `sw-kill` beside
`sw.js`. Every worker checks for it on install, on activation, and once per
start-up; on finding it, it deletes its caches, tells every open page to do the
same, stops answering anything, and unregisters. One 404 per worker start-up,
no rebuild required.

### The one thing to get right on any server

`.pf_meta`, `.pf_index`, `.pf_fragment` and `wasm.*.pagefind` **are already
compressed**. Gzipping them again makes them bigger — measured: a 103-byte
`.pf_meta` becomes 124 bytes, and the 72,209-byte wasm becomes 72,252 — and
costs CPU on every request to do it. Serve them as they are.

Do compress the JavaScript: `pagefind.js` is 45,555 bytes raw and 12,859
gzipped, and every reader downloads it before they can type.

Most servers decide what to compress by MIME type, so the simplest correct
configuration is to give those four extensions a type nothing compresses.

**nginx**

```nginx
server {
    root /srv/archive;
    index index.html;

    gzip on;
    gzip_vary on;
    # text/html is always compressed and must not be listed here.
    gzip_types text/css application/javascript application/json image/svg+xml;

    # The search index shards and the WebAssembly are gzip streams already.
    # An empty types block plus default_type is nginx's way of saying
    # "everything matched by this location is exactly this type".
    location ~* \.(pf_meta|pf_index|pf_fragment|pagefind)$ {
        types { }
        default_type application/octet-stream;
        gzip off;
        add_header Cache-Control "public, max-age=31536000, immutable";
    }

    # Self-hosted fonts and page renderings: content that never changes for a
    # given URL. media/ paths carry a page number rather than a hash, so if you
    # re-render at a different dpi, readers need a hard reload.
    location ~* \.(woff2|avif|webp|pdf)$ {
        add_header Cache-Control "public, max-age=2592000";
    }
}
```

**Caddy**

```caddy
archive.example.org {
    root * /srv/archive
    file_server

    encode {
        gzip
        match {
            header Content-Type text/*
            header Content-Type application/javascript*
            header Content-Type application/json*
        }
    }

    @pagefind path *.pf_meta *.pf_index *.pf_fragment *.pagefind
    header @pagefind {
        Content-Type "application/octet-stream"
        Cache-Control "public, max-age=31536000, immutable"
    }
}
```

**Apache** (`.htaccess` in the site root)

```apache
AddType application/octet-stream .pf_meta .pf_index .pf_fragment .pagefind
AddType application/wasm .wasm
AddType font/woff2 .woff2
AddType image/avif .avif

# Listing types explicitly means everything else - including the pf_* files
# above - is left alone.
<IfModule mod_deflate.c>
  AddOutputFilterByType DEFLATE text/html text/css application/javascript application/json image/svg+xml
</IfModule>
```

If you put a **Content-Security-Policy** in front of the archive — which you
can do on every host in this section, and cannot on GitHub Pages — the search
client needs two allowances: `script-src 'wasm-unsafe-eval'` for the
WebAssembly, and `worker-src 'self' blob:` for the web worker it runs the index
in. Without them the pages still read perfectly and the search box silently
returns nothing.

`.wasm` is in the Apache list above for completeness — a browser refuses to
instantiate WebAssembly from a response of the wrong type. Pagefind's own
WebAssembly is not affected: it arrives under the `.pagefind` extension as a
gzip stream that the client decompresses itself, which is why
`application/octet-stream` is the correct type for it and why leaving
compression off matters more than the type does.

### Object storage

S3, Cloudflare R2, Backblaze B2 and their equivalents all have static-website
modes, no file-count limit worth worrying about, and a per-request price rather
than a cap. This is the sane answer for an archive of tens of thousands of
pages. Two things to set when you upload:

- content types for `.pf_meta`, `.pf_index`, `.pf_fragment` and `.pagefind`
  (`application/octet-stream`), and for `.woff2`, `.avif` and `.webp`;
- no `Content-Encoding: gzip` on those four, however clever your sync tool
  thinks it is being.

Put a CDN in front if you expect a crowd. Both of those are one-line flags in
`aws s3 sync`, `rclone` or `wrangler`; getting them wrong is silent, and the
symptom is a search box that never returns anything.

---

## A USB stick, or a folder on a laptop

Copy `site/` onto the stick. That is the whole procedure, and it is one of the
reasons the archive is shaped the way it is: no server, no database, no account,
and the original PDFs are right there in `files/`.

One honest caveat. Opening `index.html` by double-clicking gives you a `file://`
page, and browsers refuse to load ES modules, web workers and WebAssembly from
`file://`. So **reading works and searching does not**: every page, every scan,
every original PDF and every link between them behaves normally, and the search
box does not come back. That is a browser rule, not a broken archive.

If search matters, put a two-line note on the stick beside the folder:

```
To read this: open site/index.html in a browser.

To search it as well, open a terminal in this folder and run
    python3 -m http.server
then visit http://localhost:8000/site/ - any Python 3 will do,
nothing needs installing.
```

`stackroom serve site` does the same thing with the right MIME types already
set, for anyone who has Stackroom installed.

For an intranet, a shared drive or a courtroom laptop, the same folder behind
any of the servers above works exactly as it does on the public web.

---

## When the archive is bigger than the host allows

First find out where it went:

```sh
du -sh site/*
```

It will be `media/` and `files/`, in that order, and by a wide margin. The
search index is not your problem: it is about 5.5 bytes per page of metadata
plus the fragments, so `--no-search` saves you very little and costs you the
thing readers use most. Do not start there.

**1. Render less.** In `stackroom.toml`:

```toml
[render]
dpi = 150          # 150 is right for typed pages; 300 only for small print
widths = [1600]    # dropping the 900px variant saves roughly a third
formats = ["webp"] # or ["avif"] alone, which is smaller still
```

Dropping to one width and one format cuts `media/` by more than half. Keep the
dpi: below 150 the scans get hard to read, which defeats the point, and the
redaction pass has less to work with.

**2. Split the collection.** Several archives — by year, by tranche, by agency
— each under the limit, each with its own front page and its own ledger. Cross
-archive search is the thing you give up. For a release that arrives in
instalments this is often the honest structure anyway.

**3. Move to a host that does not have the limit.** Object storage, or a small
VPS. An archive of 20,000 pages is a few gigabytes and a few dollars a month;
it is not a hard hosting problem, it is only a hard *free* hosting problem.

**4. Last resort: `safety.publish_originals = false`.** This drops `files/` and
usually a third of the total. It also means nobody can check your renderings
against the source, which contradicts the reason the originals are there in the
first place — the build will tell you so, and it is right. If you do it, say so
prominently in `about.md` and put the originals somewhere a reader can still
reach them.

Splitting is nearly always better than any of the others.

---

## After it is up

- **Tell people how to verify it.** `manifest.json` holds the SHA-256 of every
  source file. A reader who downloads a PDF from `files/` can check it against
  the digest and know they have what you were given.
- **Encourage mirrors.** These are static files under a licence you chose. If
  the archive matters, somebody else having a copy is the only real insurance,
  and an archive that can be mirrored without asking anyone is the whole design.
- **Rebuild rather than patch, and pin the date.** `stackroom build` rewrites
  its output folder every time, and the build is reproducible: the same
  documents produce the same bytes. The one thing that moves is the build
  timestamp, in `manifest.json` and in every page footer — so export
  `SOURCE_DATE_EPOCH` (whole seconds since 1970, e.g.
  `SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)`) and say in `about.md` what
  value you used. A reader can then rebuild the archive from your documents and
  get a byte-identical tree, which is a much stronger claim than a checksum of
  the originals. Editing the generated HTML by hand is a change nobody can
  reproduce and the next build erases.
- **Run the build somewhere you would be willing to run a stranger's file.**
  poppler and Tesseract are C and C++ parsers, invoked on bytes an agency or a
  source chose. Stackroom passes them argv lists with timeouts and never a
  shell, and a crash is contained by the process boundary — but a memory-safety
  bug in either of them is a memory-safety bug in your build. A container, or a
  machine you can throw away, is a reasonable precaution for a release you did
  not produce. [`docs/THREAT-MODEL.md`](THREAT-MODEL.md) §7 says the same thing
  at more length.
