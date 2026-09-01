"""Walk the input folder and decide what is in the collection.

Discovery is the first thing that runs, and the only thing that ever sees the
operator's real input directory: nested exports, a ``__MACOSX`` sidecar left by
a zip, three copies of the same PDF under three different names, filenames in
Cyrillic, and ``doc10.pdf`` sorting before ``doc2.pdf`` under every naive sort.

Everything here is deterministic on purpose. The build promises that the same
input bytes produce the same output bytes (ARCHITECTURE.md, guarantee 6), and
document order and slugs - which become URLs, and therefore citations - are the
first place that promise can break. So nothing in this module depends on
filesystem enumeration order, on the locale, or on the platform's idea of case
folding: the walk collects, then we sort with our own key, then we assign slugs.
Move the folder to another machine and every URL stays where it was.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

__all__ = ["SourceFile", "discover", "natural_key", "printable", "slugify"]

CHUNK = 1 << 20
"""Bytes per read while hashing. Collections contain 500 MB scans; reading one
of those into memory to hash it would be a bug on a laptop."""

MAX_SLUG = 60
"""Longest slug we will emit. Long enough to stay recognisable in a URL, short
enough that ``d/<slug>/p/1234/index.html`` survives Windows path limits."""

# Files that are noise in every collection: they carry no document, and
# reporting them as "skipped" would bury the one line the operator needs to
# read. Dotfiles and dot-directories are dropped by the same rule.
JUNK_NAMES = frozenset(
    {".ds_store", "thumbs.db", "desktop.ini", "icon\r", ".localized"}
)
JUNK_DIRS = frozenset({"__macosx", "__pycache__", ".git", ".svn", ".hg"})

# Magic numbers, checked before extensions. A file called `report.pdf` that
# begins with a PNG header is a PNG; trusting the extension here would hand a
# non-PDF to pdfminer and turn a clear message into a traceback.
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),
    (b"GIF87a", "image"),
    (b"GIF89a", "image"),
    (b"II*\x00", "image"),  # little-endian TIFF
    (b"MM\x00*", "image"),  # big-endian TIFF
    (b"BM", "image"),
)

TEXT_SUFFIXES = frozenset({".txt", ".text", ".md", ".markdown", ".csv", ".tsv"})

# Cyrillic transliteration, applied after Unicode decomposition has stripped
# the accents off everything else. Without it a Russian filename folds to the
# empty string and every Russian document in a collection gets the same
# `doc-<digest>` slug: legal, unique, and unreadable. It is a fixed table
# rather than a dependency so that the same name yields the same URL on every
# machine, forever.
#
# Letters that decompose - yo, short i, yi, short u - are handled by the NFKD
# pass stripping their diacritic, so they are deliberately absent. The linter
# is silenced per line because a transliteration table is, by construction,
# nothing but characters confusable with ASCII.
_TRANSLIT = (
    "а=a б=b в=v г=g д=d е=e ж=zh з=z и=i к=k л=l м=m "  # noqa: RUF001
    "н=n о=o п=p р=r с=s т=t у=u ф=f х=kh ц=ts ч=ch "  # noqa: RUF001
    "ш=sh щ=shch ъ= ы=y ь= э=e ю=yu я=ya "
    "є=ye і=i ґ=g ђ=dj ј=j љ=lj њ=nj ћ=c џ=dz"  # noqa: RUF001
)
CYRILLIC: dict[str, str] = {
    src: dst for src, _, dst in (pair.partition("=") for pair in _TRANSLIT.split())
}


@dataclass(slots=True)
class SourceFile:
    """One file on disk, and what we decided about it.

    ``usable`` entries are the collection; ``skipped`` entries exist so the CLI
    can tell the operator what it did *not* ingest and why. An archive that
    quietly drops files is worse than one that refuses to build.
    """

    path: Path
    sha256: str
    """Empty for files we skipped before hashing - there is no reason to read
    600 MB of video we are not going to ingest."""

    size: int
    slug: str
    """URL segment, unique across the collection. Empty on skipped files: they
    never get a URL, so giving them a slug would only invite one to be used."""

    kind: str
    """``"pdf"`` | ``"text"`` | ``"image"`` | ``"unsupported"``."""

    duplicate_of: Path | None = None
    """Set on a skipped file whose bytes are identical to an ingested one."""

    aliases: list[Path] = field(default_factory=list)
    """Set on the survivor: every other path that held these exact bytes. Worth
    surfacing - in a FOIA release, the same memo appearing in two productions
    under two names is itself a fact about the release."""

    reason: str = ""
    """Why this file was skipped. Empty on usable files."""

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------

_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key that reads digit runs as numbers, so ``doc2`` precedes ``doc10``.

    Every element is the same shape - ``(kind, number, text)`` - because Python
    compares tuples element by element and would raise on ``int < str``. Digit
    runs sort before letters, which only matters for names that differ in that
    position. The original string is appended so that two names that fold to the
    same key still have a stable, reproducible order.
    """
    parts: list[tuple[int, int, str]] = []
    for i, chunk in enumerate(_DIGITS.split(unicodedata.normalize("NFC", name))):
        if i % 2:  # re.split with one group puts the digit runs at odd indices
            parts.append((0, int(chunk), ""))
        elif chunk:
            parts.append((1, 0, chunk.casefold()))
    parts.append((2, 0, name))
    return tuple(parts)


def _path_key(rel: Path) -> tuple[tuple[tuple[int, int, str], ...], ...]:
    """Order by directory, then by name, naturally at every level."""
    return tuple(natural_key(part) for part in rel.parts)


# --------------------------------------------------------------------------
# slugs
# --------------------------------------------------------------------------


RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
"""MS-DOS device names, still reserved by every Windows filesystem.

They are reserved with any extension and in any directory, so a document that
slugifies to one of these cannot be written on Windows and cannot be re-hosted
from a Windows machine. ``com0``/``lpt0`` are not reserved; the rest of the
range is."""


def printable(name: str) -> str:
    """A name that can be written into a UTF-8 file.

    ``os.walk`` hands back undecodable bytes as surrogate escapes - routine for
    a zip made on Windows with a cp1251 filename - and every string operation
    in Python accepts them happily while ``str.encode("utf-8")`` refuses. So
    the failure lands three modules and one whole ingest later, in
    ``SiteBuilder.write``, *after* the output directory has been emptied.
    Replacing them here, where names enter the model, is what keeps that from
    being a crash.

    The path on disk is untouched: this is for the name we *show*, never for
    the name we open.
    """
    return name.encode("utf-8", "replace").decode("utf-8")


def slugify(name: str, digest: str = "") -> str:
    """Turn a filename into a URL segment.

    Lowercase, Unicode-decomposed so accents fall off, Cyrillic transliterated,
    everything else that is not ``[a-z0-9]`` collapsed to single hyphens. When
    nothing survives - a name made only of CJK, emoji or typographic punctuation
    - fall back to ``doc-<first 8 of the digest>``, which is stable for the file
    rather than for its position in the walk.
    """
    folded = unicodedata.normalize("NFKD", name).casefold()
    out: list[str] = []
    for ch in folded:
        if unicodedata.combining(ch):
            continue  # an accent whose base letter we just kept
        if ch in CYRILLIC:
            out.append(CYRILLIC[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(out)).strip("-")
    slug = slug[:MAX_SLUG].rstrip("-")
    if not slug:
        return f"doc-{digest[:8]}" if digest else "doc"
    if slug in RESERVED_STEMS:
        # A slug becomes a directory. On Windows these names are devices, not
        # files, so `d/con/index.html` cannot be created - the archive is
        # unbuildable there and un-rehostable by anyone on Windows. Suffixed
        # rather than digest-keyed so the URL is still readable.
        slug = f"{slug[: MAX_SLUG - 4]}-doc"
    return slug


def _unique(slug: str, taken: set[str]) -> str:
    """First free variant of *slug*, keeping the result within ``MAX_SLUG``."""
    if slug not in taken:
        taken.add(slug)
        return slug
    n = 2
    while True:
        suffix = f"-{n}"
        base = slug[: MAX_SLUG - len(suffix)].rstrip("-")
        candidate = f"{base}{suffix}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        n += 1


# --------------------------------------------------------------------------
# file inspection
# --------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _looks_like_text(head: bytes, complete: bool) -> bool:
    """True when *head* decodes as UTF-8 and holds no NUL bytes."""
    if b"\x00" in head:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        # final=False tolerates a multi-byte character cut in half by the read.
        decoder.decode(head, final=complete)
    except UnicodeDecodeError:
        return False
    return True


def _classify(path: Path, size: int) -> tuple[str, str]:
    """Return ``(kind, reason)`` for one file, reading only its first bytes."""
    if size == 0:
        return "unsupported", "empty file"
    try:
        with path.open("rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        return "unsupported", f"unreadable: {exc.strerror or exc}"

    for prefix, kind in MAGIC:
        if head.startswith(prefix):
            return kind, ""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ""
    # Some producers stick a byte-order mark or a mail header in front of the
    # PDF header. Acrobat accepts it, so we do too, but only near the start.
    if b"%PDF-" in head[:1024]:
        return "pdf", ""

    suffix = path.suffix.casefold()
    if suffix in TEXT_SUFFIXES and _looks_like_text(head, complete=size <= 4096):
        return "text", ""
    if suffix == ".pdf":
        return "unsupported", "named .pdf but has no PDF header"
    return "unsupported", f"unsupported type '{suffix or path.name}'"


# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------


def _matches(patterns: list[str], rel: Path, name: str) -> bool:
    """Glob match against both the relative path and the bare filename.

    Case is folded explicitly rather than left to :func:`fnmatch.fnmatch`, which
    normalises according to the *host* filesystem and would therefore give two
    different collections on macOS and Linux from the same input.
    """
    subject = rel.as_posix().casefold()
    lowered = name.casefold()
    return any(
        fnmatchcase(subject, p.casefold()) or fnmatchcase(lowered, p.casefold())
        for p in patterns
    )


# --------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------


def discover(
    root: Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[list[SourceFile], list[SourceFile]]:
    """Return ``(usable, skipped)`` for every file under *root*.

    ``usable`` is in the order the collection will be published in: directory by
    directory, naturally sorted within each, with slugs already made unique.
    ``skipped`` carries everything that was deliberately left out, each with a
    ``reason`` - unsupported types, files excluded by pattern, and byte-identical
    duplicates, which point at the copy that survived.

    ``include`` and ``exclude`` are glob patterns matched against both the path
    relative to *root* and the bare filename, case-insensitively. ``include``,
    when given, is a whitelist: a file must match one of its patterns.

    Symlinked directories are not followed, because a link back up the tree
    would make the walk infinite and the output non-deterministic. A *root* that
    is not a directory raises rather than returning nothing: silently publishing
    an empty collection because of a typo in a path is the worst answer here.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"{root}: not a directory")
    real_root = root.resolve()
    include = include or []
    exclude = exclude or []

    candidates: list[tuple[Path, Path]] = []  # (relative, absolute)
    escaping: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune in place so os.walk never descends. Sorting dirnames does not
        # affect our output order - we sort at the end - but it keeps the walk
        # itself reproducible, which makes progress output make sense.
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d.casefold() not in JUNK_DIRS
        )
        here = Path(dirpath)
        for filename in filenames:
            if filename.startswith(".") or filename.casefold() in JUNK_NAMES:
                continue
            absolute = here / filename
            if absolute.is_symlink():
                # A link is not a document. os.walk(followlinks=False) prunes
                # symlinked *directories* and says nothing about files, so a
                # link in a folder somebody else assembled is followed, and the
                # file it points at - chosen by them, not by the operator - is
                # hashed, ingested and republished byte for byte.
                try:
                    target = absolute.resolve(strict=True)
                except OSError:
                    continue  # a broken link is not a document
                if not target.is_relative_to(real_root):
                    escaping.append(absolute)
                    continue
            if not absolute.is_file():
                continue
            candidates.append((absolute.relative_to(root), absolute))

    candidates.sort(key=lambda pair: _path_key(pair[0]))

    usable: list[SourceFile] = []
    # Reported rather than dropped in silence: an archive that quietly leaves a
    # file out is worse than one that refuses to build, and "why is appendix B
    # missing" has to have an answer.
    skipped: list[SourceFile] = [
        SourceFile(
            link,
            "",
            0,
            "",
            "unsupported",
            reason="a symbolic link pointing outside the folder",
        )
        for link in sorted(escaping)
    ]
    taken: set[str] = set()
    by_digest: dict[str, SourceFile] = {}

    for rel, absolute in candidates:
        name = rel.name
        size = _size(absolute)
        # Classify before filtering. It costs one 4 KB read and it means the
        # skipped list can say "you excluded 40 PDFs" rather than "40 files".
        kind, reason = _classify(absolute, size)

        if exclude and _matches(exclude, rel, name):
            skipped.append(SourceFile(absolute, "", size, "", kind, reason="excluded by pattern"))
            continue
        if include and not _matches(include, rel, name):
            skipped.append(
                SourceFile(absolute, "", size, "", kind, reason="did not match --include")
            )
            continue
        if kind == "unsupported":
            skipped.append(SourceFile(absolute, "", size, "", kind, reason=reason))
            continue

        try:
            digest = _sha256(absolute)
        except OSError as exc:
            skipped.append(
                SourceFile(absolute, "", size, "", "unsupported", reason=f"unreadable: {exc.strerror or exc}")
            )
            continue

        first = by_digest.get(digest)
        if first is not None:
            # Same bytes, different name. One document, and the survivor is the
            # one that came first in the collection order - never whichever the
            # filesystem happened to hand back first.
            first.aliases.append(absolute)
            skipped.append(
                SourceFile(
                    absolute,
                    digest,
                    size,
                    "",
                    kind,
                    duplicate_of=first.path,
                    reason=f"duplicate of {first.slug}",
                )
            )
            continue

        source = SourceFile(
            path=absolute,
            sha256=digest,
            size=size,
            slug=_unique(slugify(Path(name).stem, digest), taken),
            kind=kind,
        )
        by_digest[digest] = source
        usable.append(source)

    return usable, skipped


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
