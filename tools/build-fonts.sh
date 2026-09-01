#!/bin/sh
#
# build-fonts.sh - rebuild the self-hosted web fonts in
# src/stackroom/assets/fonts/ from pinned upstream sources.
#
# Stackroom sites must load nothing from a third party (ARCHITECTURE.md,
# guarantee 5), so the fonts are subset, renamed and committed to the repo.
# This script is the record of how those .woff2 files were made. Running it on
# a clean checkout must reproduce them byte for byte.
#
# Usage:   sh tools/build-fonts.sh
#          KEEP_WORK=1 sh tools/build-fonts.sh     # keep the scratch dir
#
# Requires: curl, python3, and  pip install "fonttools[woff]" brotli zopfli
#
# ---------------------------------------------------------------------------
# Why the sources are pinned by commit SHA
# ---------------------------------------------------------------------------
# `release` and `master` are moving branches: a rebuild six months from now
# would silently pick up a different design. Every URL below is addressed by
# the commit that the upstream version tag pointed at, and every download is
# checked against a recorded SHA-256. A changed byte stops the build.
#
# ---------------------------------------------------------------------------
# Why the output fonts are renamed
# ---------------------------------------------------------------------------
# All three families are OFL 1.1 *with a Reserved Font Name* - "Source" for the
# two Adobe families, "Plex" for IBM's. Subsetting deletes glyphs and changes
# the format, which makes these Modified Versions under the OFL's definition,
# and OFL clause 3 forbids a Modified Version from carrying the Reserved Font
# Name. So the shipped faces are renamed to "Stackroom Sans/Serif/Mono" and the
# original copyright, licence and designer strings are preserved in the name
# table. See src/stackroom/assets/fonts/LICENSE-FONTS.md.

set -eu

# Reproducibility. fontTools stamps head.modified with the current time on every
# save, which would make each rebuild produce different bytes and break
# ARCHITECTURE.md guarantee 6 ("same input bytes, same output bytes"). fontTools
# honours SOURCE_DATE_EPOCH, so pin it. The instant is arbitrary - 2023-01-01Z -
# it only has to be the same on every machine.
SOURCE_DATE_EPOCH=1672531200
export SOURCE_DATE_EPOCH

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUT_DIR="$REPO_ROOT/src/stackroom/assets/fonts"

if [ "${KEEP_WORK:-0}" = "1" ]; then
    WORK="$REPO_ROOT/.font-build"
    mkdir -p "$WORK"
else
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/stackroom-fonts.XXXXXX")
    trap 'rm -rf "$WORK"' EXIT INT TERM
fi

SRC="$WORK/src"
STAGE="$WORK/stage"
mkdir -p "$SRC" "$STAGE" "$OUT_DIR"

die() { printf 'build-fonts: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 0. dependencies
# ---------------------------------------------------------------------------
printf '\n== checking tools ==\n'
command -v curl >/dev/null 2>&1 || die "curl not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v pyftsubset >/dev/null 2>&1 || \
    die 'pyftsubset not found - pip install "fonttools[woff]" brotli zopfli'
python3 -c 'import fontTools, brotli' 2>/dev/null || \
    die 'python3 needs fontTools and brotli - pip install "fonttools[woff]" brotli zopfli'
note "fontTools $(python3 -c 'import fontTools; print(fontTools.version)')"

# sha256sum (GNU) or shasum -a 256 (macOS)
if command -v sha256sum >/dev/null 2>&1; then
    sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
    die "no sha256sum or shasum available"
fi

# ---------------------------------------------------------------------------
# 1. upstream sources, pinned
# ---------------------------------------------------------------------------
# Source Serif 4, version 4.005 - tag 4.005R
SERIF_COMMIT=2823e993c53fca27c5c8749f529b56a5a7c77b6b
SERIF_BASE="https://raw.githubusercontent.com/adobe-fonts/source-serif/$SERIF_COMMIT/WOFF2/OTF"

# Source Sans 3, version 3.052 - tag 3.052R (commit ed1808970eb3c7301c9a523bee26473ba0bb62fa).
# The variable font is NOT on the release branch - it ships only as a release
# asset. Verified: the branch has WOFF2/OTF/... and OTF/... but no VAR/ at all.
SANS_VF_URL="https://github.com/adobe-fonts/source-sans/releases/download/3.052R/VF-source-sans-3.052R.zip"

# IBM Plex Mono, package version 2.5.0 - tag @ibm/plex-mono@2.5.0
PLEX_COMMIT=2f9ba1b25957d958db71a849e85d72e3ecfb845a
PLEX_URL="https://raw.githubusercontent.com/IBM/plex/$PLEX_COMMIT/packages/plex-mono/fonts/complete/woff2/IBMPlexMono-Regular.woff2"

fetch() {
    # fetch <url> <dest> <expected-sha256> <expected-bytes>
    _url=$1; _dest=$2; _sha=$3; _size=$4
    curl -sSfL --retry 3 --retry-delay 2 -o "$_dest" "$_url" \
        || die "download failed: $_url"
    _got_size=$(wc -c < "$_dest" | tr -d ' ')
    [ "$_got_size" = "$_size" ] || \
        die "size mismatch for $_url: expected $_size bytes, got $_got_size"
    _got_sha=$(sha256_of "$_dest")
    [ "$_got_sha" = "$_sha" ] || \
        die "SHA-256 mismatch for $_url
     expected $_sha
     got      $_got_sha
   Upstream has changed. Do not just update the hash: check what moved."
    note "ok  $(basename "$_dest")  $_got_size bytes"
}

printf '\n== downloading pinned upstream sources ==\n'
fetch "$SERIF_BASE/SourceSerif4-Regular.otf.woff2"  "$SRC/serif-400.woff2" \
      42aa010dbb82d90764a28f6cc7d809a9395999b7390eb3b212028c6975e97402 107408
fetch "$SERIF_BASE/SourceSerif4-Semibold.otf.woff2" "$SRC/serif-600.woff2" \
      7eff2d2fde32c42992e723eb24dcc6dc5b640ef0da97ce373cd38e0604202e30 112092
fetch "$SERIF_BASE/SourceSerif4-It.otf.woff2"       "$SRC/serif-400i.woff2" \
      e65464583be3cb56b9fee2bff0f2f7d11706aba31435144f87f393e62ed316c8 79940
fetch "$PLEX_URL" "$SRC/mono-400.woff2" \
      ba204497f16b6d334cee9d1e963a831b73e3a56e1d6300a8489d18df7214b350 49248
fetch "$SANS_VF_URL" "$SRC/sans-vf.zip" \
      d8e2ac355e06e6a0f0e0a0b1ac0c2451afa707584d7bb9d6b11ef9e4b749904c 795927

python3 -c "
import zipfile, sys
z = zipfile.ZipFile('$SRC/sans-vf.zip')
with open('$SRC/sans-vf-upright.otf','wb') as fh:
    fh.write(z.read('VF/SourceSans3VF-Upright.otf'))
" || die "could not extract VF/SourceSans3VF-Upright.otf"
_sha=$(sha256_of "$SRC/sans-vf-upright.otf")
[ "$_sha" = 3d0dfd6a3a644ab3d462a737923ffac41fb0ae007ce9ba83c24e6bfa76aa56c7 ] \
    || die "SHA-256 mismatch for extracted SourceSans3VF-Upright.otf: got $_sha"
note "ok  sans-vf-upright.otf  $(wc -c < "$SRC/sans-vf-upright.otf" | tr -d ' ') bytes"

# ---------------------------------------------------------------------------
# 2. instance the variable sans down to the weights we ship
# ---------------------------------------------------------------------------
# Source Sans 3 VF carries wght 200-900 and defaults to 200. The UI uses
# 400-600, and `--variations` is not a pyftsubset flag, so the axis has to be
# trimmed here, before subsetting. This is what makes the variable option
# competitive: untrimmed it is far larger than three static instances.
printf '\n== instancing variable sans (wght 400-600) ==\n'
python3 -m fontTools.varLib.instancer "$SRC/sans-vf-upright.otf" \
    "wght=400:600" -o "$STAGE/sans-var.otf" >/dev/null 2>&1 \
    || die "varLib.instancer failed on the sans variable font"
note "sans-var.otf  $(wc -c < "$STAGE/sans-var.otf" | tr -d ' ') bytes (pre-subset)"

# ---------------------------------------------------------------------------
# 3. unicode ranges
# ---------------------------------------------------------------------------
# Four non-overlapping groups. These strings are duplicated in fonts.css as
# `unicode-range` descriptors; if you change one, change the other or pages
# will render in the fallback font. See tools/README.md.
#
# core - Latin-1 plus the punctuation, currency and symbols every page uses.
#        Covers English, German, French, Spanish, Portuguese, Italian, Dutch.
#        U+0152-0153 (OE/oe) is pulled forward out of Latin Extended-A because
#        French is a first-class language here and "oeuvre" is not exotic.
R_CORE='U+0000-00FF,U+0152-0153,U+2000-206F,U+2070,U+2074-209C,U+20A0-20C0,U+2113,U+2122,U+2190-2193,U+2212,U+2215,U+FEFF,U+FFFD'
# ext  - Latin Extended-A and -B plus combining marks: the accented letters of
#        the other European languages this project indexes. Polish needs it, so
#        do Czech, Hungarian, Romanian, Turkish and the Baltic languages.
R_EXT='U+0100-0151,U+0154-024F,U+0300-036F'
# ext-rare - IPA extensions, Latin Extended Additional and Latin Extended-C/D.
#        Split out of `ext` after measuring: 20 KB of the sans on its own, and no
#        language in lang.py uses it. What it is really for is the romanisation
#        diacritics - the dotted and underlined letters of transliterated Arabic
#        and South Asian names ("Muhammad" with the dot under the h), which do
#        appear in released documents. Kept, because a file that only downloads
#        when such a character appears costs a normal page nothing.
#
#        NOTE: this is *not* Vietnamese support. Measured against U+1EA0-1EF9,
#        all three families carry only 8 of the 90 precomposed Vietnamese
#        letters (the Y forms, which exist for other reasons). Vietnamese text
#        falls back to a system font in every one of these families. See
#        tools/README.md.
R_RARE='U+0250-02AF,U+1E00-1E9F,U+1EF2-1EFF,U+2C60-2C7F,U+A720-A7FF'
# cyr  - Cyrillic and its supplements. U+0301 (combining acute) is duplicated
#        here because Russian and Ukrainian stress marks have no precomposed
#        form, and a mark that comes from a different font file than its base
#        letter does not attach correctly.
R_CYR='U+0301,U+0400-045F,U+0460-052F,U+1C80-1C8A,U+2116,U+2DE0-2DFF,U+A640-A69F,U+FE2E-FE2F'
# greek - monotonic and polytonic Greek.
R_GRK='U+0370-03FF,U+1F00-1FFF'

# Layout features kept. tnum/lnum matter: Bates numbers and checksums have to
# line up in columns. onum/frac/sups/case are cheap and used by the essay text.
FEATURES='kern,liga,clig,calt,ccmp,mark,mkmk,locl,rlig,onum,tnum,lnum,pnum,frac,sups,case'

subset() {
    # subset <input> <output> <unicode-ranges>
    pyftsubset "$1" --unicodes="$3" \
        --layout-features="$FEATURES" \
        --flavor=woff2 --no-hinting --desubroutinize \
        --name-IDs='*' --name-legacy --notdef-outline \
        --output-file="$2" || die "pyftsubset failed for $2"
}

# ---------------------------------------------------------------------------
# 4. subset every face into every script group
# ---------------------------------------------------------------------------
# Mono gets no Greek file on purpose: IBM Plex Mono contains exactly one
# codepoint in the Greek blocks (U+03C0, the maths pi) and cannot set Greek
# text. fonts.css therefore leaves Greek out of the mono unicode-range so it
# falls through to the system monospace font instead of rendering a hole.
printf '\n== subsetting ==\n'
build_face() {
    # build_face <input> <output-stem> <groups...>
    _in=$1; _stem=$2; shift 2
    for _g in "$@"; do
        case $_g in
            core)     _r=$R_CORE ;;
            ext)      _r=$R_EXT ;;
            ext-rare) _r=$R_RARE ;;
            cyrillic) _r=$R_CYR ;;
            greek)    _r=$R_GRK ;;
            *) die "unknown group $_g" ;;
        esac
        subset "$_in" "$STAGE/$_stem-$_g.woff2" "$_r"
    done
    note "$_stem"
}

build_face "$STAGE/sans-var.otf"   stackroom-sans-var   core ext ext-rare cyrillic greek
build_face "$SRC/serif-400.woff2"  stackroom-serif-400  core ext ext-rare cyrillic greek
build_face "$SRC/serif-600.woff2"  stackroom-serif-600  core ext ext-rare cyrillic greek
build_face "$SRC/serif-400i.woff2" stackroom-serif-400i core ext ext-rare cyrillic greek
build_face "$SRC/mono-400.woff2"   stackroom-mono-400   core ext ext-rare cyrillic

# ---------------------------------------------------------------------------
# 5. rename (OFL clause 3) and move into place
# ---------------------------------------------------------------------------
printf '\n== renaming name tables (OFL clause 3) ==\n'
python3 - "$STAGE" "$OUT_DIR" <<'PYRENAME'
import sys, os, re
from fontTools.ttLib import TTFont
from fontTools.misc.timeTools import timestampSinceEpoch

stage, out_dir = sys.argv[1], sys.argv[2]
EPOCH = int(os.environ["SOURCE_DATE_EPOCH"])

# family, style, weight-or-range, italic-flag, PostScript stem
FACES = {
    "stackroom-sans-var":   ("Stackroom Sans",  "Regular",  0, "StackroomSans-Regular"),
    "stackroom-serif-400":  ("Stackroom Serif", "Regular",  0, "StackroomSerif-Regular"),
    "stackroom-serif-600":  ("Stackroom Serif", "SemiBold", 0, "StackroomSerif-Semibold"),
    "stackroom-serif-400i": ("Stackroom Serif", "Italic",   1, "StackroomSerif-Italic"),
    "stackroom-mono-400":   ("Stackroom Mono",  "Regular",  0, "StackroomMono-Regular"),
}
ORIGIN = {
    "stackroom-sans-var":   "Source Sans 3 3.052",
    "stackroom-serif-400":  "Source Serif 4 4.005",
    "stackroom-serif-600":  "Source Serif 4 4.005",
    "stackroom-serif-400i": "Source Serif 4 4.005",
    "stackroom-mono-400":   "IBM Plex Mono 2.005",
}

# Names that identify the face. Everything else - copyright (0), trademark (7),
# manufacturer (8), designer (9), licence (13) and licence URL (14) - is left
# exactly as upstream wrote it, which is what the OFL requires us to carry.
IDENTITY_IDS = (1, 2, 3, 4, 6, 16, 17, 18, 20, 21, 22, 25)
WIN, MAC = (3, 1, 0x409), (1, 0, 0)

count = 0
for fn in sorted(os.listdir(stage)):
    if not fn.endswith(".woff2"):
        continue
    stem = re.sub(r"-(core|ext-rare|ext|cyrillic|greek)\.woff2$", "", fn)
    if stem not in FACES:
        continue
    family, style, italic, ps = FACES[stem]
    group = fn[len(stem) + 1:-len(".woff2")]

    font = TTFont(os.path.join(stage, fn))
    name = font["name"]
    for nid in IDENTITY_IDS:
        name.removeNames(nameID=nid)

    full = family if style == "Regular" else f"{family} {style}"
    unique = f"{full}; subset {group}; derived from {ORIGIN[stem]}"
    provenance = (
        f"Subset of {ORIGIN[stem]} for the Stackroom static-site builder: "
        f"{group} codepoints only. Renamed as SIL OFL 1.1 clause 3 requires "
        f"for a Modified Version. Not the original font."
    )
    for (pid, eid, lid) in (WIN, MAC):
        name.setName(family, 1, pid, eid, lid)
        name.setName(style if style in ("Regular", "Italic") else "Regular", 2, pid, eid, lid)
        name.setName(unique, 3, pid, eid, lid)
        name.setName(full, 4, pid, eid, lid)
        name.setName(ps, 6, pid, eid, lid)
        name.setName(provenance, 10, pid, eid, lid)
        # Typographic family/subfamily keep SemiBold out of the RIBBI slots.
        name.setName(family, 16, pid, eid, lid)
        name.setName(style, 17, pid, eid, lid)
    if "fvar" in font:
        # Variable PostScript name prefix.
        for (pid, eid, lid) in (WIN, MAC):
            name.setName(ps.split("-")[0], 25, pid, eid, lid)

    head = font["head"]
    head.macStyle = (head.macStyle & ~0b11) | (0b10 if italic else 0)
    # Belt and braces alongside SOURCE_DATE_EPOCH: a fixed date keeps rebuilds
    # byte-identical no matter which fontTools version runs them.
    head.created = head.modified = timestampSinceEpoch(EPOCH)
    font.flavor = "woff2"
    font.save(os.path.join(out_dir, fn))
    font.close()
    count += 1
print(f"  renamed and wrote {count} files")
PYRENAME

# ---------------------------------------------------------------------------
# 6. verify
# ---------------------------------------------------------------------------
printf '\n== verifying output ==\n'
python3 - "$OUT_DIR" <<'PYVERIFY'
import sys, os, re
from fontTools.ttLib import TTFont

out_dir = sys.argv[1]

# Groups of codepoints each file must actually be able to draw. These are
# real letters, not range endpoints: a font can declare a range and still have
# nothing in it.
PROBES = {
    "Latin":    "AaZz09",
    "Lat-ext":  "ąłżćș",       # Polish, Romanian
    "Lat-rare": "ḍḥỳə",        # romanisation diacritics + schwa
    "Cyrillic": "АяЁєґ",       # А я Ё є ґ
    "Greek":    "ΑωάςΈ",       # Α ω ά ς Έ
}
EXPECT = {  # group -> probe set that must be non-empty
    "core": "Latin", "ext": "Lat-ext", "ext-rare": "Lat-rare",
    "cyrillic": "Cyrillic", "greek": "Greek",
}
EXPECTED_WEIGHT = {
    "stackroom-sans-var":   ("variable", 400, 600),
    "stackroom-serif-400":  ("static", 400, 400),
    "stackroom-serif-600":  ("static", 600, 600),
    "stackroom-serif-400i": ("static", 400, 400),
    "stackroom-mono-400":   ("static", 400, 400),
}

files = sorted(f for f in os.listdir(out_dir) if f.endswith(".woff2"))
if not files:
    sys.exit("verify: no .woff2 files in " + out_dir)

failures, total = [], 0
rows = []
for fn in files:
    stem = re.sub(r"-(core|ext-rare|ext|cyrillic|greek)\.woff2$", "", fn)
    group = fn[len(stem) + 1:-len(".woff2")]
    path = os.path.join(out_dir, fn)
    size = os.path.getsize(path)
    total += size
    try:
        font = TTFont(path, lazy=True)
        cmap = set(font.getBestCmap())
    except Exception as exc:                       # noqa: BLE001
        failures.append(f"{fn}: does not parse ({exc})")
        continue

    counts = {k: sum(1 for ch in v if ord(ch) in cmap) for k, v in PROBES.items()}

    kind, lo, hi = EXPECTED_WEIGHT[stem]
    if kind == "variable":
        if "fvar" not in font:
            failures.append(f"{fn}: expected a variable font, found no fvar table")
            weight = "?"
        else:
            ax = {a.axisTag: a for a in font["fvar"].axes}
            w = ax.get("wght")
            if not w or (w.minValue, w.maxValue) != (lo, hi):
                failures.append(f"{fn}: wght axis is {w and (w.minValue, w.maxValue)}, expected ({lo}, {hi})")
            weight = f"{int(w.minValue)}-{int(w.maxValue)} var" if w else "?"
    else:
        got = font["OS/2"].usWeightClass
        if got != lo:
            failures.append(f"{fn}: usWeightClass is {got}, expected {lo}")
        weight = str(got)

    fam = font["name"].getDebugName(16) or font["name"].getDebugName(1) or ""
    if "Source" in fam or "Plex" in fam:
        failures.append(f"{fn}: family name {fam!r} still carries a Reserved Font Name")
    if not font["name"].getDebugName(0):
        failures.append(f"{fn}: upstream copyright string was lost")

    want = EXPECT[group]
    if counts[want] == 0:
        failures.append(f"{fn}: group '{group}' has no {want} coverage at all")

    rows.append((fn, size, weight, counts, len(cmap)))
    font.close()

w = max(len(r[0]) for r in rows)
print(f"\n  {'file'.ljust(w)}  {'bytes':>7}  {'weight':>10}  {'cps':>5}   Latin Lat-ext Lat-rare Cyril Greek")
print(f"  {'-' * w}  {'-' * 7}  {'-' * 10}  {'-' * 5}   ----- ------- -------- ----- -----")
for fn, size, weight, counts, ncp in rows:
    c = counts
    print(f"  {fn.ljust(w)}  {size:7d}  {weight:>10}  {ncp:5d}   "
          f"{c['Latin']:5d} {c['Lat-ext']:7d} {c['Lat-rare']:8d} {c['Cyrillic']:5d} {c['Greek']:5d}")
print(f"  {'-' * w}  {'-' * 7}")
print(f"  {'total committed'.ljust(w)}  {total:7d}")

# Per-family rollup: Latin, Cyrillic and Greek must each be covered somewhere.
print("\n  per-family script coverage (union across that family's files):")
fams = {}
for fn, size, weight, counts, ncp in rows:
    stem = re.sub(r"-(core|ext-rare|ext|cyrillic|greek)\.woff2$", "", fn)
    agg = fams.setdefault(stem, {k: 0 for k in PROBES})
    for k, v in counts.items():
        agg[k] = max(agg[k], v)
KNOWN_GAPS = {("stackroom-mono-400", "Greek"):
              "expected: IBM Plex Mono has only U+03C0; fonts.css excludes Greek "
              "from the mono unicode-range so it falls back to system monospace"}
for stem in sorted(fams):
    agg = fams[stem]
    bits = []
    for k in ("Latin", "Lat-ext", "Lat-rare", "Cyrillic", "Greek"):
        ok = agg[k] > 0
        bits.append(f"{k}={'yes' if ok else 'NO '}")
        if not ok:
            gap = KNOWN_GAPS.get((stem, k))
            if gap is None:
                failures.append(f"{stem}: no {k} coverage in any subset file")
    print(f"    {stem:<22} {'  '.join(bits)}")
    for k in ("Latin", "Lat-ext", "Lat-rare", "Cyrillic", "Greek"):
        if agg[k] == 0 and (stem, k) in KNOWN_GAPS:
            print(f"      note: no {k} - {KNOWN_GAPS[(stem, k)]}")

if failures:
    print("\n  FAILED:")
    for f in failures:
        print("    - " + f)
    sys.exit(1)
print("\n  all checks passed")
PYVERIFY

printf '\n== done ==\n'
note "output: $OUT_DIR"
