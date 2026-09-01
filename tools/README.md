# tools/

Maintenance scripts that are run by hand, occasionally, and whose output is
committed to the repository. Nothing here runs during `stackroom build`.

| Script | What it does |
|---|---|
| `build-fonts.sh` | Rebuilds the self-hosted web fonts in `src/stackroom/assets/fonts/`. |

---

## `build-fonts.sh`

```sh
sh tools/build-fonts.sh              # rebuild in place
KEEP_WORK=1 sh tools/build-fonts.sh  # keep the scratch dir for inspection
```

Needs `curl`, `python3`, and:

```sh
pip install "fonttools[woff]" brotli zopfli
```

It downloads three upstream families pinned by commit SHA, checks every
download against a recorded size and SHA-256, trims the variable sans's weight
axis, subsets each face into five script groups, renames the faces as the
licence requires, and then verifies what it produced and prints a coverage
table. A changed byte upstream stops the build rather than silently shipping a
different design.

The build is **deterministic** - `docs/ARCHITECTURE.md` guarantee 6. fontTools
stamps `head.modified` with the current time on every save, which would make
each run produce different bytes, so the script pins `SOURCE_DATE_EPOCH` and
sets the head timestamps explicitly. Two consecutive runs produce byte-identical
files; that is worth re-checking if you change the script.

### The one thing that will bite you

The `unicode-range` values in `src/stackroom/assets/fonts/fonts.css` are a
hand-maintained copy of the ranges in this script. **If they drift apart,
nothing errors** - the browser simply never asks for the file that holds the
character, and those pages render in the fallback font. Change one, change the
other. There is a checker at the bottom of this file.

---

## What is shipped, and why

Three families, all SIL OFL 1.1, all self-hosted because an archive that phones
home is a log of who read what:

| Family | Weights | Used for |
|---|---|---|
| Stackroom Sans (Source Sans 3) | variable 400-600 | interface, labels, metadata |
| Stackroom Serif (Source Serif 4) | 400, 600, 400 italic | reading text, notes, OCR |
| Stackroom Mono (IBM Plex Mono) | 400 | checksums, Bates numbers, paths |

They are renamed because the OFL reserves the names `Source` and `Plex` and a
subset is a Modified Version under that licence. See
`src/stackroom/assets/fonts/LICENSE-FONTS.md`; this is a licence obligation, not
a style choice.

Public Sans was rejected before this: it has **zero** Cyrillic glyphs.

---

## Variable or static: measured, per family

The trade-off is total bytes for the weights a page actually uses against the
flexibility of an axis. It does not resolve the same way for all three, so it
was measured rather than assumed. All figures are the Latin `core` subset,
identical subsetting options on both sides.

### Sans - **variable wins**

| What loads | Static | Variable 400-600 |
|---|---:|---:|
| 400 only | **20.8 KiB** | 28.6 KiB |
| 400 + 600 | 41.6 KiB | **28.6 KiB** |
| 400 + 500 + 600 | 62.4 KiB | **28.6 KiB** |
| every script group | 210.0 KiB | **88.9 KiB** |

Static wins only if a page uses exactly one weight, and the interface never
does - labels sit at 400 and headings and active states at 600 on every page.
At two weights the variable file is 13.0 KiB smaller, at three 33.8 KiB, and it
gives 500 for free. Shipped variable.

This depended entirely on trimming the axis first: Source Sans 3 VF carries
wght 200-900 and defaults to 200. `--variations` is **not** a pyftsubset flag,
so the trim has to happen in `fontTools.varLib.instancer` before subsetting.

### Serif - **static wins**

| What loads | Static | Variable (opsz=11, wght 400-700) |
|---|---:|---:|
| 400 only | **29.2 KiB** | 48.6 KiB |
| 400 + 600 | 60.2 KiB | **48.6 KiB** |
| 400 + 600 + italic | **88.1 KiB** | 93.8 KiB |
| every script group | **168.9 KiB** | 179.0 KiB |

The serif is the reading text, and the common page - a document page showing
scan plus OCR transcription - needs 400 and nothing else. Static serves that in
29.2 KiB against 48.6 KiB, and statics let the browser fetch only the weight in
use instead of one file covering weights the page never asks for. Italic is a
second variable file, not an axis, which is what sinks the variable option
whenever italic appears. Shipped static.

For the record, the axis trim matters here too: the variable Roman subsets to
211.7 KiB with all axes intact and 81.3 KiB after pinning `opsz=11` and limiting
`wght` to 400-700.

### Mono - static, trivially

One weight is used. A variable file would be strictly larger for the same
result.

---

## Splitting by script

Each face is cut into five groups. A browser downloads a file only when a
character on the page falls inside its `unicode-range`, so an English page never
touches Cyrillic or Greek outlines.

| Group | Ranges | Serves |
|---|---|---|
| `core` | Latin-1 + shared punctuation, currency, symbols | en, de, fr, es, pt, it, nl |
| `ext` | Latin Extended-A/B + combining marks | pl, cs, hu, ro, tr, Baltic |
| `ext-rare` | IPA, Latin Extended Additional/C/D | romanised names (`Muḥammad`) |
| `cyrillic` | Cyrillic + supplements | ru, uk |
| `greek` | Greek, monotonic and polytonic | el |

Splitting `core` from `ext` is the single biggest win and was not obvious:
Source Sans 3's Latin Extended coverage is *larger than its core*, so the naive
one-Latin-file layout cost 50.4 KiB where core alone costs 20.8 KiB. Splitting
`ext-rare` out of `ext` saved a further 20.0 KiB on the sans for languages like
Polish, which need Latin Extended-A but no IPA.

`cyrillic` is declared **last** in `fonts.css` for every family. It claims
U+0301, the combining acute, which `ext` also claims inside U+0300-036F. CSS
Fonts 4 checks overlapping ranges in reverse declaration order, so the last rule
wins and a stressed Russian vowel gets its base letter and its accent from the
same file - the only way the accent lands correctly. Russian and Ukrainian have
no precomposed stressed vowels; Latin does, which is why the overlap is resolved
in Cyrillic's favour.

---

## Payload

Budget was under 120 KiB for a first page load. Measured, from the committed
files:

| Page | Files fetched | Total |
|---|---|---:|
| English document page | sans core, serif 400 core, mono core | **69.9 KiB** |
| + serif 600 (essay headings) | above + serif 600 core | 101.3 KiB |
| Greek page | above minus 600, + sans/serif greek | 93.9 KiB |
| Russian / Ukrainian page | + sans/serif/mono cyrillic | 112.4 KiB |
| Polish page | + sans/serif ext | 115.5 KiB |

The Polish row does not include `stackroom-mono-400-ext.woff2`: Bates numbers,
checksums and paths are ASCII, so no character on the page ever selects the mono
`ext` file. That is the split doing its job.

Total committed to the repository, all 24 files: **366.7 KiB**. That is a
one-time repository cost, not a per-page one.

---

## Known coverage gaps - stated, not hidden

**Stackroom Mono has no Greek.** IBM Plex Mono contains exactly one codepoint in
the Greek blocks, U+03C0 (the mathematical pi) - 1 of the 69 letters monotonic
Greek needs. `fonts.css` therefore omits Greek from the mono `unicode-range`
entirely and ships no mono Greek file, so Greek in a monospaced context falls
through to the reader's own monospace font, which can draw it. Declaring the
range and shipping a font with no Greek in it would have been the silent hole.

**No family covers Vietnamese.** Measured against U+1EA0-1EF9, all three carry
8 of the 90 precomposed Vietnamese letters (only the Y forms, which exist for
other reasons). Vietnamese text falls back to a system font in the sans, the
serif *and* the mono. `ext-rare` is not Vietnamese support; it is IPA and the
dotted and underlined letters of transliterated Arabic and South Asian names.

**Source Serif has no polytonic Greek.** It covers monotonic Greek completely
(69/69 of the letters modern Greek needs, 80 codepoints in U+0370-03FF) but
nothing in U+1F00-1FFF. Ancient Greek quotations set in the serif will fall
back per character. Source Sans covers polytonic fully (321 codepoints).

**No sans italic is shipped.** The sans sets interface labels, which are not
italicised. If one is ever needed the browser will synthesise an oblique; add
`SourceSans3-It` to the build rather than living with that.

---

## Fallback metrics

`fonts.css` defines three fallback faces carrying `ascent-override`,
`descent-override` and `line-gap-override`, so the line box the page paints
before the web font arrives is exactly the line box it paints after. Those
values are the shipped fonts' own metrics, read with fontTools - in all three
families the OS/2 typo, OS/2 win and hhea metrics agree, so one value is correct
on every platform:

| Face | ascent | descent | line gap |
|---|---:|---:|---:|
| Stackroom Sans | 100.0% | 32.6% | 0% |
| Stackroom Serif | 103.6% | 33.5% | 0% |
| Stackroom Mono | 102.5% | 27.5% | 0% |

(Stackroom Mono's typo metrics differ at 0.780/0.220, but its
`USE_TYPO_METRICS` bit is clear, so browsers use hhea/win. That is the pair
above.)

**`size-adjust` is deliberately not set.** It corrects x-height mismatch, which
needs the metrics of the font that actually renders first - and that differs per
platform. What could be measured on the build machine:

| Web font | x-height | Fallback measured | x-height | ratio |
|---|---:|---|---:|---:|
| Source Sans 3 | 0.478 em | Arial (via Liberation Sans) | 0.528 em | 90.5% |
| Source Sans 3 | 0.478 em | Calibri (via Carlito) | 0.478 em | 100.1% |
| Source Serif 4 | 0.475 em | Times New Roman (via Liberation Serif) | 0.459 em | 103.5% |
| IBM Plex Mono | 0.516 em | Courier New (via Liberation Mono) | 0.528 em | 97.7% |

Segoe UI, SF and Roboto - which is what most readers will actually see - are
proprietary and were not installed on the build machine, so their ratios are
unknown. A single `size-adjust` tuned to Arial would be right for one fallback
and wrong for the rest, and the spread above (90.5% to 100.1% for the same web
font) shows how wrong. The line-box overrides fix the layout shift that actually
moves content; the residual x-height difference changes apparent size without
moving the page. If you want to revisit this, measure the real fallbacks on the
real platforms first.

Note also that `size-adjust` on the mono would be actively harmful: IBM Plex
Mono's advance is 0.600 em and Courier New's is 0.600 em, so columns already
line up. Scaling by 97.7% to match x-heights would break that.

---

## Checking `fonts.css` against the fonts

Run this from the repository root after any change to either. It confirms every
referenced file exists, that each declared `unicode-range` matches what is
actually inside that file, that nothing on disk is unreferenced, and that
`cyrillic` is still declared last.

```sh
python3 - <<'EOF'
import re, os
from fontTools.ttLib import TTFont
d = "src/stackroom/assets/fonts"
css = open(f"{d}/fonts.css", encoding="utf-8").read()
body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
assert "@import" not in body and "http" not in body, "external reference in fonts.css"
bad, seen = [], set()
for blk in re.findall(r"@font-face\s*\{(.*?)\}", body, re.S):
    u = re.search(r'url\("([^"]+)"\)', blk)
    r = re.search(r"unicode-range:\s*([^;]+);", blk, re.S)
    if not u:
        continue
    seen.add(u.group(1))
    rs = []
    for p in r.group(1).split(","):
        p = p.strip().removeprefix("U+")
        lo, _, hi = p.partition("-")
        rs.append((int(lo, 16), int(hi or lo, 16)))
    f = TTFont(f"{d}/{u.group(1)}", lazy=True)
    cps = set(f.getBestCmap()); f.close()
    out = {c for c in cps if c > 0x20 and not any(a <= c <= b for a, b in rs)}
    if out:
        bad.append(f"{u.group(1)}: {len(out)} codepoints outside its unicode-range")
    if not (cps & {c for a, b in rs for c in range(a, min(b, a + 4096) + 1)}):
        bad.append(f"{u.group(1)}: unicode-range matches nothing in the file")
for f in sorted({f for f in os.listdir(d) if f.endswith('.woff2')} - seen):
    bad.append(f"{f}: on disk but not referenced by fonts.css")
print("\n".join(bad) if bad else "fonts.css and the .woff2 files agree")
EOF
```
