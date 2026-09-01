"""Making a rebuild cost only what actually changed.

A 5,000-page collection takes hours, and 61% of that is encoding images, 24%
recognising text and 11% rasterising. Fixing a typo in one page's source and
running the build again does all of it a second time. This module is the reason
it does not have to.

The whole design rests on one thing that was already true:
:func:`stackroom.pipeline.process_page` is a pure function of a picklable
:class:`~stackroom.pipeline.PageJob`. That is what makes the pool of workers
possible, and it is what makes a cache possible: if the answer depends only on
the job and the bytes the job reads, then the job and those bytes are a key, and
the answer can be looked up instead of computed.

So the cache is content-addressed and lives outside the output directory, which
the build empties on every run. An entry holds the serialised
:class:`~stackroom.pipeline.PageOutcome` and the encoded page images, keyed by a
digest over the source file's bytes, the page number, every field of the job
that changes the output, and the versions of everything that produced it -
Stackroom, poppler, Tesseract and its language data, Pillow and its codecs. A
Tesseract upgrade changes the text on a scanned page; a cache that ignored that
would be worse than no cache at all.

Three rules the rest of this file exists to keep:

**Never a wrong answer.** A full disk, a read-only cache directory, a truncated
entry, a blob somebody deleted, a model that grew a field since the entry was
written: every one of them degrades to *do the work*, never to a crash and never
to stale output. Where a check is cheap enough to run on every hit, it runs on
every hit.

**Never the recovered text.** Text found underneath a black box lives in memory
for exactly as long as it takes to tell the operator about it, and a cache is a
file on disk. A page that leaked is recorded as having leaked, with the boxes
and the count and nothing else, and is then **re-read from the file on every
build** rather than restored - which is also the only way the warning the
operator sees can be identical warm and cold. ``docs/CACHING.md`` argues that
out; the short version is that ``HiddenText.redacted_repr()`` preserves word
lengths and punctuation, that is a fingerprint of a name, and we do not need it.

**Bounded.** A cache that grows for ever is a bug. There is a size limit,
eviction is least-recently-used over whole entries, and ``stackroom cache
clear`` exists because an operator about to publish a source's documents needs
to be able to remove every trace of them - including the page images this
directory holds.

Watch mode lives here too, at the bottom. It is a polling watcher over size and
mtime rather than a dependency on an OS notification API, it debounces, and it
sits on top of the cache: rebuilding only what changed is the entire point.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .model import (
    Box,
    ImageVariant,
    OcrQuality,
    Page,
    PageVerdict,
    Redaction,
    RedactionKind,
    TextSource,
    Word,
)

if TYPE_CHECKING:  # pragma: no cover - imports for type checking only
    from .pipeline import PageJob, PageOutcome

# `pipeline` imports this module, so this module must not import `pipeline` at
# import time. The one place a PageOutcome has to be *constructed* does the
# import inside the function, by which point pipeline is fully loaded.

__all__ = [
    "CacheStats",
    "Change",
    "PageCache",
    "PruneReport",
    "Watcher",
    "base_dir",
    "cache_root",
    "default_cache_dir",
    "human_bytes",
    "key_for",
    "key_inputs",
    "open_cache",
    "parse_size",
    "watch",
]

_LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# constants and environment variables
# --------------------------------------------------------------------------

FORMAT = 1
"""Version of the on-disk entry format.

It is part of the key, not merely written into the file, so a change here does
not invalidate entries - it makes them unreachable, which is the same thing
without a migration. Bump it whenever the meaning of anything stored changes.
"""

LAYOUT = "v1"
"""Sub-directory of the cache root. Entries written by an incompatible layout
live in a sibling directory, so ``rm -rf`` on one of them is safe."""

DEFAULT_MAX_BYTES = 5 * 1024**3
"""5 GiB. The demo collection writes about 350 KB of page images per page, so
this is roughly three 5,000-page collections. Override with ``--cache-max`` or
``STACKROOM_CACHE_MAX``."""

BLOB_GRACE_SECONDS = 3600.0
"""How long an unreferenced blob is left alone before a sweep may remove it.

A build writes blobs before the entry that references them, so a concurrent
sweep could otherwise delete a blob a second later. The grace period makes the
window an hour wide; a blob deleted anyway is a cache miss, not a wrong answer.
"""

ENV_STAMP = "environment.json"
"""What the last build that wrote here was built with. **Never part of a key.**

A cold miss on a cache that is not empty is almost always one moved version, and
until this file existed the cache could only name the whole fingerprint and let
the operator guess which field it was. It holds one :meth:`Environment.as_dict`
and the time it was written, in the layout directory beside ``entries/``.

It is *read* once, when the cache is opened, and *written* once, by the first
:meth:`PageCache.put` that succeeds. Both halves of that are load-bearing.
Writing on open would mean ``stackroom cache show`` erased the record of what
the entries already there were written with; reading lazily would mean the
build asking *why did I miss* got back its own stamp, because by the time it
asks it has stored four thousand pages and overwritten the answer.

It cannot drift into being a second cache key, and not by promise:
:func:`key_for` is a pure function of ``(job, source_sha256, env, salt)`` and
reads no files at all, so nothing on disk can reach it. The only reader is
:meth:`PageCache.miss_reason`, which is called by the build report and by
nothing that decides a hit. ``test_cache.py`` pins both halves of that.
"""

ENV_DIR = "STACKROOM_CACHE_DIR"
ENV_MAX = "STACKROOM_CACHE_MAX"
ENV_ENABLED = "STACKROOM_CACHE"
ENV_SALT = "STACKROOM_CACHE_SALT"
ENV_COPY = "STACKROOM_CACHE_COPY"
ENV_VERIFY = "STACKROOM_CACHE_VERIFY"

CACHEDIR_TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n"
"""The `Cache Directory Tagging Standard <https://bford.info/cachedir/>`_ marker.

Written into the cache root so that borg, restic, tar --exclude-caches and
GNOME's backup tool skip it. This directory holds rendered page images of
documents the operator may be handling carefully; keeping it out of a backup
that goes somewhere else is worth eight lines."""

README = """\
This directory is Stackroom's page cache.

It holds rendered page images and the analysis of pages from documents that
have been built or previewed on this machine. Some of those documents may not
be public. Treat this directory exactly as carefully as you treat them.

It is safe to delete at any time: the next build simply does the work again.

    stackroom cache          show what is here
    stackroom cache clear    delete all of it

What is NOT here: text that Stackroom found underneath a black box. That never
reaches any file on disk, cache included, and pages where it was found are
re-read from the original on every build.
"""


# --------------------------------------------------------------------------
# where the cache lives
# --------------------------------------------------------------------------


def default_cache_dir() -> Path:
    """The per-user cache directory, by each platform's own convention.

    ``XDG_CACHE_HOME`` wins everywhere it is set - including on macOS and
    Windows, where it is not the native convention but is an explicit
    instruction from the operator and there is no reason to ignore it.
    """
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "stackroom"
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        # No home directory to speak of: a daemon, a container with no passwd
        # entry, a CI runner. A cache under the temporary directory is worth
        # more than no cache, and is exactly as safe to delete.
        return Path(tempfile.gettempdir()) / "stackroom-cache"
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "stackroom"
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local) / "stackroom" / "Cache"
    return home / ".cache" / "stackroom"


def base_dir(explicit: Path | str | None = None) -> Path:
    """The cache directory itself.

    Precedence: the argument (``--cache-dir``), then ``STACKROOM_CACHE_DIR``,
    then the platform default.
    """
    chosen = explicit or os.environ.get(ENV_DIR, "").strip() or None
    return Path(chosen).expanduser() if chosen else default_cache_dir()


def cache_root(explicit: Path | str | None = None) -> Path:
    """Where entries for this layout version go, inside :func:`base_dir`.

    The ``pages/<layout>`` tail is appended in every case, so that pointing
    ``--cache-dir`` at a directory holding other things cannot make ``clear``
    delete them, and so that an incompatible layout lands in a sibling
    directory rather than among entries it cannot read.
    """
    return base_dir(explicit) / "pages" / LAYOUT


def parse_size(text: str | int | None) -> int | None:
    """``"2GB"``, ``"512M"``, ``"1.5 GiB"``, ``"0"`` - into bytes.

    Returns None for empty input so a caller can tell "not set" from "set to
    zero", which means *keep nothing* and is a legitimate way to turn the store
    into a no-op without changing any other behaviour.
    """
    if text is None:
        return None
    if isinstance(text, int):
        return max(0, text)
    raw = str(text).strip().lower().replace(" ", "")
    if not raw:
        return None
    units = {
        "": 1, "b": 1,
        "k": 1000, "kb": 1000, "kib": 1024,
        "m": 1000**2, "mb": 1000**2, "mib": 1024**2,
        "g": 1000**3, "gb": 1000**3, "gib": 1024**3,
        "t": 1000**4, "tb": 1000**4, "tib": 1024**4,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if suffix and raw.endswith(suffix):
            number, unit = raw[: -len(suffix)], suffix
            break
    else:
        number, unit = raw, ""
    try:
        value = float(number)
    except ValueError as exc:
        raise ValueError(
            f"{text!r} is not a size. Write it like 2GB, 500MB or 1.5GiB."
        ) from exc
    if value < 0:
        raise ValueError(f"{text!r} is negative; a cache cannot be smaller than nothing.")
    return int(value * units[unit])


def human_bytes(n: float) -> str:
    """Bytes as a person would say them. Local copy: this module is imported by
    ``stackroom cache``, which has no business importing the site builder."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < step or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= step
    return f"{n:,.1f} TB"  # pragma: no cover - unreachable, the loop returns


def human_seconds(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 90:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# --------------------------------------------------------------------------
# what produced the answer: the environment fingerprint
# --------------------------------------------------------------------------
#
# Everything in this section answers one question: if I run this page again on
# this machine tomorrow, will I get the same bytes back? A field belongs here
# when the answer changes if it changes, and nowhere near here when it does not.


def _probe(argv: Sequence[str], *, timeout: float = 5.0) -> str:
    """First line of a version banner, or ``"absent"``.

    Version banners go to stdout on some builds and stderr on others, so both
    are read. Anything at all going wrong is reported as ``"absent"`` rather
    than raising: a version string is never worth failing a build over, and
    ``"absent"`` is itself a perfectly good key component - a machine without
    poppler produces different outcomes from a machine with it, and should not
    share their entries.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return "absent"
    text = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    for line in text.decode("utf-8", "replace").splitlines():
        if line.strip():
            return line.strip()[:120]
    return "absent"


def _poppler_version() -> str:
    """``pdftoppm``'s own banner: ``pdftoppm version 24.02.0``.

    Rasterising is 11% of a build and everything downstream reads its pixels, so
    a poppler upgrade can move a word box, change what OCR sees, and change
    whether the redaction check believes a box is uniform. It is not optional in
    the key.
    """
    return _probe(["pdftoppm", "-v"])


def _pillow_fingerprint() -> dict[str, str]:
    """Pillow and the image libraries it was built against.

    Pillow's version alone is not enough. The bytes of a WebP or AVIF file are
    decided by libwebp and libavif, which are separately versioned shared
    libraries; two Pillow 12.2 installs can encode the same page to different
    bytes. Since the cache hands those exact bytes back to be published, both
    have to be in the key.
    """
    out: dict[str, str] = {}
    try:
        import PIL
        from PIL import features

        out["pillow"] = PIL.__version__
        for name in sorted(features.get_supported_modules()):
            with contextlib.suppress(Exception):
                out[f"module:{name}"] = str(features.version_module(name))
        for name in sorted(features.get_supported_codecs()):
            with contextlib.suppress(Exception):
                out[f"codec:{name}"] = str(features.version_codec(name))
    except Exception:  # pragma: no cover - a Pillow this broken fails elsewhere
        out["pillow"] = "unknown"
    return out


@functools.lru_cache(maxsize=8)
def _tessdata_fingerprint(languages: tuple[str, ...]) -> str:
    """Size and mtime of the ``.traineddata`` for each language asked for.

    Tesseract's version is not the whole story: the language models are
    separate files that an operator can and does replace - swapping the fast
    ``eng.traineddata`` for the ``best`` one changes the text on every scanned
    page without changing a single version number. Size and mtime rather than a
    content hash because these files run to 15 MB each and this is computed on
    every build; it is a fingerprint, not a proof, and it is precise enough for
    a cache that is machine-local anyway.

    Anything unreadable yields ``"unknown"``, a stable value: a machine where
    this cannot be worked out still gets a usable cache, and ``docs/CACHING.md``
    says what that trades away.
    """
    directory = _tessdata_dir()
    if directory is None:
        return "unknown"
    parts: list[str] = [str(directory)]
    for lang in sorted({*languages, "osd"}):
        # osd is included because ocr.auto_rotate loads it, and a page turned
        # the wrong way up reads as garbage.
        path = directory / f"{lang}.traineddata"
        try:
            st = path.stat()
            parts.append(f"{lang}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{lang}:missing")
    return "|".join(parts)


def _tessdata_dir() -> Path | None:
    """Where Tesseract will look for language data.

    ``TESSDATA_PREFIX`` first, because that is what Tesseract itself honours;
    then the directory ``tesseract --list-langs`` names, which is the only
    answer that is right on a machine with several installs.
    """
    prefix = os.environ.get("TESSDATA_PREFIX", "").strip()
    if prefix:
        candidate = Path(prefix)
        # Tesseract accepts both the tessdata directory and its parent.
        if (candidate / "eng.traineddata").exists() or candidate.name == "tessdata":
            return candidate
        if (candidate / "tessdata").is_dir():
            return candidate / "tessdata"
        return candidate
    banner = _probe(["tesseract", "--list-langs"])
    # 'List of available languages in "/usr/share/tesseract-ocr/5/tessdata/" (2):'
    if '"' in banner:
        named = banner.split('"')[1]
        if named:
            return Path(named)
    for guess in ("/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tessdata",
                  "/usr/local/share/tessdata", "/opt/homebrew/share/tessdata"):
        if Path(guess).is_dir():
            return Path(guess)
    return None


def _font_fingerprint() -> str:
    """A digest over the fonts fontconfig can see, or ``"unknown"``.

    This one is easy to miss. A PDF that does not embed its fonts is rendered
    by poppler with whatever substitute the machine has, so the *pixels* of the
    page - and therefore the OCR text, the ink coverage, the redaction
    analysis and the published image - depend on which fonts are installed.
    ``docs/PERFORMANCE.md`` already says the fallback font set matters; it
    matters to the cache too.

    Sorted before hashing, because ``fc-list`` does not promise an order and an
    unsorted digest would change on every build and never hit.
    """
    try:
        proc = subprocess.run(
            ["fc-list", ":", "file"], capture_output=True, timeout=10.0, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    lines = sorted(proc.stdout.decode("utf-8", "replace").splitlines())
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return f"fc:{len(lines)}:{digest[:32]}"


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _is_source_checkout() -> bool:
    """Are we running from a working tree rather than an installed wheel?

    Both are ordinary: contributors run from ``src/``, everybody else installs.
    It matters because a version number does not change when somebody edits
    ``ingest/ocr.py``, and in a working tree that happens hourly.
    """
    package = _package_dir()
    if package.parent.name == "src":
        return True
    root = package.parent.parent
    return (root / ".git").exists() or (root / "pyproject.toml").is_file()


def _source_fingerprint() -> str | None:
    """A digest over every ``.py`` file in the package, for working trees.

    ``__version__`` is the right key component for an installed Stackroom and
    completely wrong for a checkout, where the code under a version number
    changes all day. Hashing the source is a few milliseconds once per process
    and turns "my cache is serving results from the code I just changed" from a
    bug report into an impossibility.

    Returns None when the tree cannot be read. The caller treats that as *do not
    use the cache*, because at that point we cannot say what code an entry was
    written by, and a cache that cannot say that is the dangerous kind.
    """
    package = _package_dir()
    digest = hashlib.sha256()
    try:
        for path in sorted(package.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            digest.update(str(path.relative_to(package)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return None
    return digest.hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Environment:
    """Every version that can change what a page comes out as.

    Probed once per process - each of these costs a subprocess - and then passed
    around. Frozen because a key built from a mutable environment is not a key.
    """

    stackroom: str
    source: str
    """Digest of the package's own source in a working tree, ``"release"`` in an
    installed one. See :func:`_source_fingerprint`."""

    poppler: str
    tesseract: str
    pillow: dict[str, str]
    pdfplumber: str
    pdfminer: str
    numpy: str
    formats: tuple[str, ...]
    """What ``raster.supported_formats()`` will actually write here. A machine
    without an AVIF encoder publishes different files from one with it."""

    fonts: str
    omp_threads: str
    """``OMP_THREAD_LIMIT``. ``ingest/ocr.py`` pins it to 1 with a
    ``setdefault``, so an operator who exports something else gets a Tesseract
    running a different number of threads - and OpenMP reductions are not
    obliged to be bit-identical across thread counts."""

    platform: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "stackroom": self.stackroom,
            "source": self.source,
            "poppler": self.poppler,
            "tesseract": self.tesseract,
            "pillow": dict(self.pillow),
            "pdfplumber": self.pdfplumber,
            "pdfminer": self.pdfminer,
            "numpy": self.numpy,
            "formats": list(self.formats),
            "fonts": self.fonts,
            "omp_threads": self.omp_threads,
            "platform": self.platform,
        }

    @property
    def usable(self) -> bool:
        """False when we could not establish what code is running."""
        return self.source != ""


_ENV_LABELS = {
    "omp_threads": "OMP_THREAD_LIMIT",
    "platform": "the platform",
}
"""Fields whose attribute name is not what a person calls them."""


def _short(value: Any, limit: int = 32) -> str:
    """One environment value, printable. Long digests are cut, never wrapped."""
    text = str(value)
    if not text:
        return "unset"
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _read_env_stamp(root: Path) -> dict[str, Any]:
    """:data:`ENV_STAMP` in *root*, or an empty dict. Never raises.

    A cache that cannot explain itself is still a cache, so every way this can
    fail - no file, a truncated one, a directory where the file should be, one
    written by a version that stored something else - is the same answer.
    """
    try:
        raw = json.loads((root / ENV_STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = raw.get("environment") if isinstance(raw, dict) else None
    return env if isinstance(env, dict) else {}


def _describe_move(name: str, before: Any, after: Any) -> str:
    """One field of :class:`Environment`, and what it moved from and to.

    Reads back as a clause in a sentence - "tesseract moved from 5.3.3 to
    5.3.4" - because that is the whole point of storing the environment: an
    operator looking at a cold cache wants the field, not the fingerprint.
    Three of the fields are not version numbers and are described instead of
    quoted; a 64-character digest printed at somebody tells them nothing.
    """
    if name == "source":
        return "stackroom's own source changed - in a working tree, one edited .py file does it"
    if name == "fonts":
        return "the fonts installed on this machine changed"
    if name == "pillow" and isinstance(before, dict) and isinstance(after, dict):
        inner = [
            f"{lib} {_short(before.get(lib, 'absent'))} to {_short(after.get(lib, 'absent'))}"
            for lib in sorted(set(before) | set(after))
            if before.get(lib) != after.get(lib)
        ]
        return f"Pillow's imaging libraries moved: {', '.join(inner)}" if inner else ""
    if name == "formats":
        return (
            f"the image formats this machine can write went from "
            f"{_short(', '.join(map(str, before or ())))} to "
            f"{_short(', '.join(map(str, after or ())))}"
        )
    return f"{_ENV_LABELS.get(name, name)} moved from {_short(before)} to {_short(after)}"


_ENVIRONMENT: Environment | None = None


def probe_environment(*, refresh: bool = False) -> Environment:
    """Read every version this machine will build with. Memoised per process."""
    global _ENVIRONMENT
    if _ENVIRONMENT is not None and not refresh:
        return _ENVIRONMENT

    from . import __version__ as pkg_version

    if _is_source_checkout():
        fingerprint = _source_fingerprint()
        source = "" if fingerprint is None else f"src:{fingerprint}"
    else:
        source = "release"

    tesseract = "absent"
    with contextlib.suppress(Exception):
        from .ingest import ocr as ocr_mod

        tesseract = str(ocr_mod.tesseract_version())

    formats: tuple[str, ...] = ()
    with contextlib.suppress(Exception):
        from .ingest import raster as raster_mod

        formats = tuple(raster_mod.supported_formats())

    def _version_of(module: str) -> str:
        try:
            import importlib

            mod = importlib.import_module(module)
            return str(getattr(mod, "__version__", "unknown"))
        except Exception:
            return "absent"

    _ENVIRONMENT = Environment(
        stackroom=str(pkg_version),
        source=source,
        poppler=_poppler_version(),
        tesseract=tesseract,
        pillow=_pillow_fingerprint(),
        pdfplumber=_version_of("pdfplumber"),
        pdfminer=_version_of("pdfminer"),
        numpy=_version_of("numpy"),
        formats=formats,
        fonts=_font_fingerprint(),
        omp_threads=os.environ.get("OMP_THREAD_LIMIT", "1"),
        platform=f"{sys.platform}/{os.name}",
    )
    return _ENVIRONMENT


# --------------------------------------------------------------------------
# the key
# --------------------------------------------------------------------------
#
# A key that is too narrow silently serves a stale result. A key that is too
# broad never hits and the cache is an expensive no-op. Every field of PageJob
# is therefore accounted for below, in one of three sets, and the sets are
# checked against the dataclass at import time - so a field added to PageJob by
# somebody who has never read this file turns into a loud refusal to cache
# rather than a quiet wrong answer.

KEYED_JOB_FIELDS = frozenset({
    "doc_id",
    "number",
    "media_prefix",
    "dpi",
    "widths",
    "thumb_width",
    "formats",
    "max_megapixels",
    "ocr_mode",
    "ocr_languages",
    "psm",
    "auto_rotate",
    "is_image",
})
"""Fields that change what comes out, and are therefore in the key.

- ``number``, ``is_image``: which pixels are read at all.
- ``dpi``, ``max_megapixels``: the resolution, and therefore every word box,
  the ink coverage, and what recognition can resolve.
- ``widths``, ``thumb_width``, ``formats``: which image files exist, at what
  size, in what encoding - the cache hands those files back, so they are the
  answer, not a detail of it.
- ``ocr_mode``, ``ocr_languages``, ``psm``, ``auto_rotate``: the text. Language
  *order* matters as well as membership; ``-l eng+fra`` is not ``-l fra+eng``.
- ``doc_id`` and ``media_prefix``: they are written into the outcome.
  ``PageOutcome.doc_id`` and every ``ImageVariant.path`` carry them, and a path
  is published in the HTML. Renaming a source file changes its slug, changes
  every URL it owns, and costs a rebuild of that document. That is the honest
  answer; ``docs/CACHING.md`` explains the normalisation that would avoid it and
  why it was not worth the risk.
"""

UNKEYED_JOB_FIELDS = frozenset({"pdf", "media_dir", "ocr_timeout"})
"""Fields deliberately left out, each for its own reason.

- ``pdf``: the *path*. Replaced by the SHA-256 of the file's bytes, which is
  strictly better: moving a collection, or building the same document from two
  directories, keeps every hit. The path is where the bytes came from; the bytes
  are what the answer depends on.
- ``media_dir``: where the images are written. It is a destination, not an
  input. Leaving it out is what lets ``--out`` change without re-encoding
  anything: the same blobs are simply linked somewhere else.
- ``ocr_timeout``: a resource bound, not an output determinant. It cannot change
  a successful result, only turn one into a failure - and failures are never
  cached (see :func:`_cacheable`). Keying on it would invalidate every entry the
  first time somebody raised the limit, for nothing.
"""


def _check_job_fields() -> str:
    """Every field of PageJob is classified, or nothing is cached.

    Returns an empty string when the classification is complete, and an
    explanation when it is not. :class:`PageCache` refuses to run on a non-empty
    return: an unclassified field is one that might change the output and is not
    in the key, which is the exact shape of the bug this cache must not have.
    """
    try:
        from .pipeline import PageJob

        names = {f.name for f in dataclasses.fields(PageJob)}
    except Exception as exc:  # pragma: no cover - only if pipeline is broken
        return f"PageJob could not be inspected ({exc})"
    known = KEYED_JOB_FIELDS | UNKEYED_JOB_FIELDS
    new = names - known
    gone = known - names
    if new:
        return (
            f"PageJob has field(s) this cache has never heard of: {', '.join(sorted(new))}. "
            "Add each one to KEYED_JOB_FIELDS or UNKEYED_JOB_FIELDS in stackroom/cache.py "
            "- if it can change what a page comes out as, it belongs in the key."
        )
    if gone:
        return f"PageJob no longer has field(s) this cache keys on: {', '.join(sorted(gone))}"
    return ""


@functools.lru_cache(maxsize=1)
def _model_schema() -> str:
    """A digest over the shape of every model type the cache serialises.

    The codec below is written out by hand, field by field, because that is the
    only way to be exact about floats. The cost of writing it by hand is that it
    goes stale. This is the tripwire: the field names, their order and their
    declared types go into the key, so a field added to ``Page`` next month makes
    every entry written this month unreachable instead of decoding into a Page
    that is quietly missing it.
    """
    digest = hashlib.sha256()
    for cls in (Box, Word, ImageVariant, Redaction, OcrQuality, Page):
        digest.update(cls.__name__.encode("ascii"))
        for f in dataclasses.fields(cls):
            digest.update(f"|{f.name}:{f.type}".encode())
        digest.update(b"\n")
    for enum in (RedactionKind, PageVerdict, TextSource):
        digest.update(enum.__name__.encode("ascii"))
        for member in enum:
            digest.update(f"|{member.name}={member.value}".encode())
        digest.update(b"\n")
    return digest.hexdigest()[:32]


def key_inputs(
    job: PageJob,
    source_sha256: str,
    env: Environment | None = None,
    salt: str = "",
) -> dict[str, Any]:
    """Exactly what the key is computed over, as plain data.

    Separate from :func:`key_for` so a test can assert on it, so ``stackroom
    cache`` can print it, and so a reader can check the reasoning in
    ``docs/CACHING.md`` against something executable.
    """
    env = env or probe_environment()
    keyed = {name: getattr(job, name) for name in sorted(KEYED_JOB_FIELDS)}
    keyed["widths"] = list(keyed["widths"])
    keyed["formats"] = list(keyed["formats"])
    keyed["ocr_languages"] = list(keyed["ocr_languages"])
    return {
        "format": FORMAT,
        "schema": _model_schema(),
        "source": {"sha256": source_sha256},
        "job": keyed,
        "env": env.as_dict(),
        "tessdata": _tessdata_fingerprint(tuple(job.ocr_languages)),
        "salt": salt or os.environ.get(ENV_SALT, ""),
    }


def key_for(
    job: PageJob,
    source_sha256: str,
    env: Environment | None = None,
    salt: str = "",
) -> str:
    """The cache key for one page: 64 hex characters over :func:`key_inputs`."""
    blob = json.dumps(
        key_inputs(job, source_sha256, env, salt),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------
# the codec: PageOutcome to plain data and back
# --------------------------------------------------------------------------
#
# `model.to_jsonable` exists and is not used here, on purpose. It writes a Box
# as four integers at 1/10,000 of the page (model.SCALE), which is right for the
# JSON the browser reads and wrong for a cache: a warm build would come back
# with boxes rounded to four decimal places and a cold build would not, and the
# negative renders redaction rectangles at three decimal places of a percent.
# The promise here is *byte-identical output, cached or not*, so every float is
# stored as a float. JSON round-trips a finite double exactly - repr() is what
# both ends use - so this is lossless, not nearly lossless.
#
# Shape: the things there are thousands of per page (words, boxes) are flat
# arrays, because a dict of field names per word triples the file. The things
# there is one of (the page, its quality) are objects, because they are read by
# people debugging a cache miss.


class CodecError(ValueError):
    """An entry could not be turned back into a PageOutcome."""


def _expect(cls: type, names: set[str]) -> str:
    actual = {f.name for f in dataclasses.fields(cls)}
    if actual == names:
        return ""
    missing = names - actual
    extra = actual - names
    parts = []
    if extra:
        parts.append(f"new field(s) {', '.join(sorted(extra))}")
    if missing:
        parts.append(f"removed field(s) {', '.join(sorted(missing))}")
    return f"{cls.__name__} has {' and '.join(parts)}"


def _check_codec() -> str:
    """Does this codec still cover every field of every type it writes?

    The schema digest in the key stops *old* entries being read into a changed
    model. This stops *new* entries being written by a codec that has silently
    stopped covering a field - which the digest cannot catch, because both ends
    would agree about the wrong thing.
    """
    problems = [
        _expect(Box, {"x", "y", "w", "h"}),
        _expect(Word, {"text", "box", "conf", "line", "hidden"}),
        _expect(ImageVariant, {"path", "format", "width", "height", "bytes"}),
        _expect(Redaction, {"box", "kind", "codes"}),
        _expect(OcrQuality, {
            "verdict", "word_count", "median_conf", "low_conf_fraction", "stopword_ratio",
            "garbage_ratio", "mean_word_length", "ink_coverage", "reasons",
        }),
        _expect(Page, {
            "number", "width_pt", "height_pt", "source", "words", "lines", "redactions",
            "hidden", "exemptions", "bates", "quality", "redaction_ratio", "images",
            "thumbs", "placeholder", "language",
        }),
    ]
    try:
        from .pipeline import PageOutcome

        problems.append(_expect(PageOutcome, {
            "doc_id", "number", "page", "hidden", "warnings", "error", "seconds",
            "analysis_failed",
        }))
    except Exception as exc:  # pragma: no cover - only if pipeline is broken
        problems.append(f"PageOutcome could not be inspected ({exc})")
    return "; ".join(p for p in problems if p)


def _box(b: Box) -> list[float]:
    return [b.x, b.y, b.w, b.h]


def _unbox(v: Any) -> Box:
    if not isinstance(v, list) or len(v) != 4:
        raise CodecError(f"not a box: {v!r}")
    return Box(float(v[0]), float(v[1]), float(v[2]), float(v[3]))


def _word(w: Word) -> list[Any]:
    return [w.text, w.box.x, w.box.y, w.box.w, w.box.h, w.conf, w.line, w.hidden]


def _unword(v: Any) -> Word:
    if not isinstance(v, list) or len(v) != 8:
        raise CodecError(f"not a word: {v!r}")
    return Word(
        text=str(v[0]),
        box=Box(float(v[1]), float(v[2]), float(v[3]), float(v[4])),
        conf=int(v[5]),
        line=int(v[6]),
        hidden=bool(v[7]),
    )


def _variant(v: ImageVariant) -> list[Any]:
    return [v.path, v.format, v.width, v.height, v.bytes]


def _unvariant(v: Any) -> ImageVariant:
    if not isinstance(v, list) or len(v) != 5:
        raise CodecError(f"not an image variant: {v!r}")
    return ImageVariant(str(v[0]), str(v[1]), int(v[2]), int(v[3]), int(v[4]))


def _redaction(r: Redaction) -> list[Any]:
    return [*_box(r.box), r.kind.value, list(r.codes)]


def _unredaction(v: Any) -> Redaction:
    if not isinstance(v, list) or len(v) != 6:
        raise CodecError(f"not a redaction: {v!r}")
    return Redaction(
        box=Box(float(v[0]), float(v[1]), float(v[2]), float(v[3])),
        kind=RedactionKind(v[4]),
        codes=[str(c) for c in v[5]],
    )


def _quality(q: OcrQuality) -> dict[str, Any]:
    return {
        "verdict": q.verdict.value,
        "word_count": q.word_count,
        "median_conf": q.median_conf,
        "low_conf_fraction": q.low_conf_fraction,
        "stopword_ratio": q.stopword_ratio,
        "garbage_ratio": q.garbage_ratio,
        "mean_word_length": q.mean_word_length,
        "ink_coverage": q.ink_coverage,
        "reasons": list(q.reasons),
    }


def _unquality(v: Any) -> OcrQuality:
    if not isinstance(v, dict):
        raise CodecError("not an OcrQuality")
    return OcrQuality(
        verdict=PageVerdict(v["verdict"]),
        word_count=int(v["word_count"]),
        median_conf=float(v["median_conf"]),
        low_conf_fraction=float(v["low_conf_fraction"]),
        stopword_ratio=float(v["stopword_ratio"]),
        garbage_ratio=float(v["garbage_ratio"]),
        mean_word_length=float(v["mean_word_length"]),
        ink_coverage=float(v["ink_coverage"]),
        reasons=[str(r) for r in v["reasons"]],
    )


def encode_page(page: Page) -> dict[str, Any]:
    """A :class:`~stackroom.model.Page` as plain data.

    ``page.hidden`` is refused rather than dropped. Nothing in the pipeline puts
    recovered text there today - it goes on the outcome - but if that ever
    changes, this raising is the difference between a cache that leaks and a
    cache that stops.
    """
    if page.hidden:
        raise CodecError("this page carries recovered text; it must not be written to disk")
    return {
        "number": page.number,
        "width_pt": page.width_pt,
        "height_pt": page.height_pt,
        "source": page.source.value,
        "words": [_word(w) for w in page.words],
        "lines": list(page.lines),
        "redactions": [_redaction(r) for r in page.redactions],
        "exemptions": list(page.exemptions),
        "bates": page.bates,
        "quality": _quality(page.quality),
        "redaction_ratio": page.redaction_ratio,
        "images": [_variant(v) for v in page.images],
        "thumbs": [_variant(v) for v in page.thumbs],
        "placeholder": page.placeholder,
        "language": page.language,
    }


def decode_page(data: Any) -> Page:
    if not isinstance(data, dict):
        raise CodecError("not a page")
    try:
        return Page(
            number=int(data["number"]),
            width_pt=float(data["width_pt"]),
            height_pt=float(data["height_pt"]),
            source=TextSource(data["source"]),
            words=[_unword(w) for w in data["words"]],
            lines=[str(line) for line in data["lines"]],
            redactions=[_unredaction(r) for r in data["redactions"]],
            hidden=[],
            exemptions=[str(e) for e in data["exemptions"]],
            bates=None if data["bates"] is None else str(data["bates"]),
            quality=_unquality(data["quality"]),
            redaction_ratio=float(data["redaction_ratio"]),
            images=[_unvariant(v) for v in data["images"]],
            thumbs=[_unvariant(v) for v in data["thumbs"]],
            placeholder=str(data["placeholder"]),
            language=str(data["language"]),
        )
    except CodecError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CodecError(f"page will not decode: {exc}") from exc


def encode_outcome(outcome: PageOutcome) -> dict[str, Any]:
    """The outcome as plain data, with the recovered text left out.

    ``hidden`` becomes a count and a list of boxes and nothing else. Not the
    text, and not ``redacted_repr()`` either: the shape preserves word lengths
    and punctuation, which is a usable fingerprint of a name. The boxes are on
    the published page already - a black rectangle is visible in the scan - so
    they cost nothing to keep, and they let ``stackroom cache`` say *this entry
    is for a page that leaked* without saying anything about what leaked.

    An outcome with ``hidden`` set is never *restored* from an entry anyway: see
    :func:`_cacheable`.
    """
    return {
        "doc_id": outcome.doc_id,
        "number": outcome.number,
        "page": encode_page(outcome.page),
        "warnings": list(outcome.warnings),
        "error": outcome.error,
        "seconds": outcome.seconds,
        "analysis_failed": outcome.analysis_failed,
        "leaked": {
            "count": len(outcome.hidden),
            "boxes": [_box(h.box) for h in outcome.hidden],
        },
    }


def decode_outcome(data: Any) -> PageOutcome:
    """Plain data back into a PageOutcome.

    ``hidden`` always comes back empty, which is safe because an entry is only
    ever written for a page that had none.
    """
    from .pipeline import PageOutcome

    if not isinstance(data, dict):
        raise CodecError("not an outcome")
    leaked = data.get("leaked") or {}
    if leaked.get("count"):
        raise CodecError("this entry is for a page that leaked; it is never restored")
    try:
        return PageOutcome(
            doc_id=str(data["doc_id"]),
            number=int(data["number"]),
            page=decode_page(data["page"]),
            hidden=[],
            warnings=[str(w) for w in data["warnings"]],
            error=None if data["error"] is None else str(data["error"]),
            seconds=float(data["seconds"]),
            analysis_failed=bool(data["analysis_failed"]),
        )
    except CodecError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CodecError(f"outcome will not decode: {exc}") from exc


TRANSIENT = (
    "failed",
    "timed out",
    "timeout",
    "could not",
    "not found",
    "unavailable",
    "no page rendering",
)
"""Fragments that mark a page note as possibly describing bad luck.

A page that failed because Tesseract was killed by the OOM reaper, or because
pdftoppm timed out on a busy machine, must not have that failure written down
and served back for ever. The list is deliberately broad: a false positive
costs one page's work on every build, a false negative poisons a page until
somebody clears the cache."""


def _cacheable(outcome: PageOutcome) -> str:
    """May this outcome be stored? Returns the reason it may not, or ``""``.

    Four refusals, in order of how much they matter:

    1. **It leaked.** A page with recovered text under a black box is re-read
       from the original on every build. The cache cannot hold the text, and an
       entry without the text would make the warm build's warning weaker than
       the cold build's - a report that gets quieter the second time you run it
       is worse than no cache.
    2. **The check did not run.** ``analysis_failed`` means we do not know
       whether this page hides anything. Writing down "unknown" and serving it
       back is how an unchecked page becomes a clean bill of health. Anything we
       could not vouch for is looked at again, every time.
    3. **It errored.** Almost always the environment rather than the document.
    4. **A note suggests bad luck rather than a property of the page.**
    """
    if outcome.hidden:
        return "the page has text under a black box; it is re-read every build"
    if outcome.analysis_failed:
        return "the redaction check did not run on this page"
    if outcome.error:
        return f"the page did not process ({outcome.error[:60]})"
    for note in outcome.warnings:
        lowered = note.casefold()
        for fragment in TRANSIENT:
            if fragment in lowered:
                return f"a note on this page may describe bad luck ({fragment!r})"
    return ""


def _looks_annotated(page: Page) -> bool:
    """Has ``annotate_document`` already been over this page?

    Exemption codes and control numbers are decided by looking at the whole
    document, after every page has come back, and they are written *into* the
    pages. Storing a page after that means storing an answer that the job alone
    does not determine. The wiring stores each outcome as it arrives, before
    annotation; this is the belt to that pair of braces.
    """
    return bool(page.exemptions) or page.bates is not None or any(r.codes for r in page.redactions)


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------
#
# Layout, under <cache root>/pages/v1:
#
#   entries/<aa>/<key>.json.gz    the serialised outcome
#   entries/<aa>/<key>.refs       the blob digests that entry needs, one per line
#   blobs/<aa>/<sha256>           one encoded image, named by its own digest
#
# The refs sidecar exists so that eviction can decide which blobs are still
# wanted without decompressing every entry: 5,000 entries is five seconds of
# gzip and a tenth of a second of sidecars.
#
# Writes are atomic - temp file in the same directory, then os.replace - and
# the entry is written last, so it is the commit point. That is the whole of
# the concurrency design; see docs/CACHING.md for why no lock is needed.


@dataclass(slots=True)
class CacheStats:
    root: Path
    entries: int = 0
    blobs: int = 0
    bytes: int = 0
    entry_bytes: int = 0
    blob_bytes: int = 0
    max_bytes: int = DEFAULT_MAX_BYTES
    oldest: float = 0.0
    newest: float = 0.0
    writable: bool = True
    exists: bool = True

    @property
    def full(self) -> float:
        return self.bytes / self.max_bytes if self.max_bytes else 1.0


@dataclass(slots=True)
class PruneReport:
    entries_removed: int = 0
    blobs_removed: int = 0
    bytes_removed: int = 0
    bytes_kept: int = 0
    errors: int = 0


def _shard(name: str) -> str:
    """Two hex characters, so no directory holds 300,000 files.

    ext4 copes; every network filesystem an operator might put a cache on does
    not, and neither does ``ls``."""
    return name[:2]


class PageCache:
    """A content-addressed store of processed pages and their images.

    Nothing here raises at the caller. Every operation either does what it says
    or reports that it did not, because the caller's fallback - process the page
    - is always available and is always correct. That is the property that makes
    a cache safe to add to a program whose job is publishing evidence.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        # The *cache directory*, not the entry directory: `<root>/pages/<layout>`
        # is derived, so that this object always knows where its own README and
        # CACHEDIR.TAG belong and `clear()` always has a bounded thing to delete.
        *,
        max_bytes: int | None = None,
        env: Environment | None = None,
        salt: str = "",
        copy_files: bool | None = None,
        verify: bool | None = None,
        enabled: bool = True,
    ) -> None:
        self.base = base_dir(root)
        self.root = self.base / "pages" / LAYOUT
        self.max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else max(0, max_bytes)
        self.salt = salt or os.environ.get(ENV_SALT, "")
        self.copy_files = (
            _flag(ENV_COPY, default=False) if copy_files is None else bool(copy_files)
        )
        self.verify = _flag(ENV_VERIFY, default=True) if verify is None else bool(verify)

        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.refusals = 0
        self.saved_seconds = 0.0
        self.restore_seconds = 0.0
        self.bytes_restored = 0
        self.added_bytes = 0
        self.warnings: list[str] = []
        self.disabled_reason = ""

        self.env: Environment | None = None
        self._stamped = False
        self._previous_env: dict[str, Any] = {}
        self.digests: dict[str, str] = {}
        self._stamps: dict[str, tuple[int, int]] = {}
        self._hashed: dict[str, tuple[tuple[int, int], str]] = {}
        self._writable = True
        self._linking = not self.copy_files

        self.enabled = enabled and self.max_bytes > 0
        if enabled and self.max_bytes <= 0:
            self.disabled_reason = "the size limit is zero"
        if self.enabled:
            self._start(env)

    # -- setting up ------------------------------------------------------

    def _start(self, env: Environment | None) -> None:
        problem = _check_job_fields() or _check_codec()
        if problem:
            self._disable(problem)
            return
        self.env = env or probe_environment()
        if not self.env.usable:
            self._disable(
                "this looks like a source checkout but its files could not be read, so "
                "there is no way to say which code an entry was written by"
            )
            return
        try:
            (self.root / "entries").mkdir(parents=True, exist_ok=True)
            (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._disable(f"{self.root} could not be created ({exc})")
            return
        # Before anything in this process can overwrite it. See ENV_STAMP.
        self._previous_env = _read_env_stamp(self.root)
        self._write_marker()

    def _disable(self, reason: str) -> None:
        self.enabled = False
        self.disabled_reason = reason
        self.warnings.append(reason)
        _LOG.warning("page cache disabled: %s", reason)

    def _write_marker(self) -> None:
        """CACHEDIR.TAG and a README, best effort, once."""
        for directory, name, body in (
            (self.base, "CACHEDIR.TAG", CACHEDIR_TAG + b"# This directory is a cache.\n"),
            (self.base, "README.txt", README.encode("utf-8")),
        ):
            path = directory / name
            if path.exists():
                continue
            with contextlib.suppress(OSError):
                directory.mkdir(parents=True, exist_ok=True)
                _atomic_write(path, body)

    def _stamp_environment(self) -> None:
        """Record what this build wrote its entries with. Once per process.

        Called from :meth:`put` and not from :meth:`_start`, because merely
        opening a cache must not overwrite the record of what the entries
        already in it were written with - ``stackroom cache show`` opens one.
        Best effort and never fatal: a cache that cannot explain itself is
        still a cache. See :data:`ENV_STAMP`.
        """
        if self._stamped or self.env is None:
            return
        self._stamped = True  # set first: one failed write, not one per page
        payload = json.dumps(
            {"written": int(time.time()), "environment": self.env.as_dict()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        with contextlib.suppress(OSError):
            _atomic_write(self.root / ENV_STAMP, payload)

    def last_env(self) -> dict[str, Any]:
        """What the last build *before this one* wrote its entries with.

        Read from disk when this cache was opened, not now: this build has
        since stamped the directory with its own environment, and a question
        answered by the asker is not answered. Empty when there was no stamp -
        an empty cache, or one written before the stamp existed - or when it
        could not be read. Read only for the operator: nothing that decides a
        hit calls this.
        """
        return dict(self._previous_env)

    def miss_reason(self) -> str:
        """Which parts of the environment moved since this cache was written.

        ``"tesseract moved from 5.3.3 to 5.3.4"`` - the sentence an operator
        staring at *0 of 16 pages came from the cache* actually needs, instead
        of the whole fingerprint and a paragraph of possibilities.

        Empty when there is nothing to say: no stamp, no current environment,
        or nothing changed - in which case the miss was something else, and
        claiming a cause would be worse than saying none.
        """
        previous = self.last_env()
        if not previous or self.env is None:
            return ""
        current = self.env.as_dict()
        moved = [
            _describe_move(name, previous[name], current[name])
            for name in current
            if name in previous and previous[name] != current[name]
        ]
        return "; ".join(part for part in moved if part)

    # -- addressing ------------------------------------------------------

    def note_digests(self, digests: dict[str, str]) -> None:
        """Take the SHA-256s ``discover`` already computed, path to digest.

        Discovery hashes every file it ingests. Hashing them again here would be
        a second pass over every byte of a collection that can be tens of
        gigabytes, to learn something we were already told.

        The size and mtime of each file are noted at the same time, so that
        :meth:`put` can tell whether the file it was told the digest of is still
        the file the worker read. See :meth:`_unchanged_since_discovery`.
        """
        self.digests.update(digests)
        for path in digests:
            try:
                st = os.stat(path)
            except OSError:
                self._stamps.pop(path, None)
            else:
                self._stamps[path] = (st.st_size, st.st_mtime_ns)

    def _unchanged_since_discovery(self, path: str) -> bool:
        """Is this still the file whose digest we were given?

        The pipeline hashes a file in ``discover`` and reads it again, page by
        page, in the workers. A file replaced in between makes that build wrong
        on its own account - the manifest records a digest the renderings did
        not come from - and there is nothing this module can do about that. What
        it can do is refuse to *remember* it, so the mistake dies with the build
        instead of being served to every build afterwards.

        A file we were never told about is not checked: there is nothing to
        check it against, and :meth:`digest_of` will have hashed whatever is
        there now.
        """
        stamp = self._stamps.get(path)
        if stamp is None:
            return True
        try:
            st = os.stat(path)
        except OSError:
            return False
        return (st.st_size, st.st_mtime_ns) == stamp

    def digest_of(self, path: str) -> str:
        """The source digest for a job, from discovery if we have it.

        The fallback hashes the file and remembers the answer against its size
        and mtime, so a 400-page PDF is read once per build rather than 400
        times. It exists for tests and for any caller that has a job but not the
        discovery record.
        """
        known = self.digests.get(path)
        if known:
            return known
        try:
            st = os.stat(path)
        except OSError:
            return ""
        stamp = (st.st_size, st.st_mtime_ns)
        cached = self._hashed.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            return ""
        value = digest.hexdigest()
        self._hashed[path] = (stamp, value)
        return value

    def key(self, job: PageJob, source_sha256: str | None = None) -> str:
        digest = source_sha256 or self.digest_of(job.pdf)
        if not digest:
            return ""
        return key_for(job, digest, self.env, self.salt)

    def _entry_path(self, key: str) -> Path:
        return self.root / "entries" / _shard(key) / f"{key}.json.gz"

    def _refs_path(self, key: str) -> Path:
        return self.root / "entries" / _shard(key) / f"{key}.refs"

    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / _shard(digest) / digest

    # -- reading ---------------------------------------------------------

    def get(self, job: PageJob, source_sha256: str | None = None) -> PageOutcome | None:
        """The stored outcome for this job, with its images put back on disk.

        None means *do the work*, and it means that for every reason: no entry,
        a truncated entry, a blob somebody deleted, an entry written by a model
        that has since changed, a page that leaked. The caller does not need to
        know which.
        """
        if not self.enabled:
            return None
        started = time.perf_counter()
        key = self.key(job, source_sha256)
        if not key:
            self.misses += 1
            return None
        entry = self._read_entry(key)
        if entry is None:
            self.misses += 1
            return None
        try:
            outcome = decode_outcome(entry["outcome"])
        except (CodecError, KeyError, TypeError) as exc:
            self._drop(key, f"entry will not decode ({exc})")
            self.misses += 1
            return None

        restored = self._restore_images(job, outcome, entry)
        if not restored:
            self.misses += 1
            return None

        self.hits += 1
        self.saved_seconds += float(entry["outcome"].get("seconds") or 0.0)
        elapsed = time.perf_counter() - started
        self.restore_seconds += elapsed
        # The honest number: what this build spent getting the page back, not
        # what some earlier build spent producing it. `saved_seconds` carries
        # the other one, and the CLI reports both.
        outcome.seconds = elapsed
        self._touch(key)
        return outcome

    def _read_entry(self, key: str) -> dict[str, Any] | None:
        path = self._entry_path(key)
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            entry = json.loads(gzip.decompress(raw).decode("utf-8"))
        except Exception as exc:  # gzip CRC, truncation, invalid JSON, bad UTF-8
            self._drop(key, f"entry is corrupt ({type(exc).__name__})")
            return None
        if not isinstance(entry, dict):
            self._drop(key, "entry is not an object")
            return None
        if entry.get("key") != key or entry.get("format") != FORMAT:
            # The key is written inside the file as well as being its name, so a
            # truncated-then-refilled file, or a copy somebody moved between
            # shards by hand, is caught rather than served.
            self._drop(key, "entry does not match its own name")
            return None
        return entry

    def _restore_images(
        self, job: PageJob, outcome: PageOutcome, entry: dict[str, Any]
    ) -> bool:
        """Put the encoded images back where this build expects them.

        All or nothing. A partial restore is cleaned up and reported as a miss,
        because half a page's variants on disk and a full page's variants in the
        model would publish a ``<picture>`` pointing at files that are not there.
        """
        wanted = entry.get("blobs") or []
        if not wanted:
            return True
        prefix = job.media_prefix.rstrip("/") + "/"
        media = Path(job.media_dir)
        written: list[Path] = []
        try:
            media.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._note(f"could not write into {media} ({exc})")
            return False
        for record in wanted:
            try:
                name, digest, size = str(record[0]), str(record[1]), int(record[2])
            except (TypeError, ValueError, IndexError):
                self._undo(written)
                self._drop(entry["key"], "entry lists a blob it cannot describe")
                return False
            if "/" in name or "\\" in name or name in ("", ".", ".."):
                # An entry is a file this process wrote, but it is still a file,
                # and a name that escapes media_dir would let a corrupted or
                # crafted cache write anywhere the build can write.
                self._undo(written)
                self._drop(entry["key"], f"entry names an image outside the media folder ({name!r})")
                return False
            target = media / name
            if not self._place(self._blob_path(digest), target, digest, size):
                self._undo(written)
                return False
            written.append(target)
            self.bytes_restored += size
        # Every path in the model must be one of the files we just wrote, under
        # this job's prefix. This is what makes leaving media_dir out of the key
        # safe: if the pipeline ever stops composing paths as prefix + name, the
        # check fails here and the page is simply processed again.
        names = {p.name for p in written}
        for variant in [*outcome.page.images, *outcome.page.thumbs]:
            if not variant.path.startswith(prefix) or variant.path[len(prefix):] not in names:
                self._undo(written)
                self._drop(entry["key"], "entry's image paths do not match this build")
                return False
        return True

    def _place(self, blob: Path, target: Path, digest: str, size: int) -> bool:
        """One blob into the output tree: hard link if we can, copy if we cannot.

        Verification is on by default and costs one read of the file. On a
        5,000-page collection that is about a gigabyte and a half read from a
        warm page cache - seconds - against hours of encoding, and it is what
        makes "byte-identical, cached or not" a checked claim rather than a
        hope. ``STACKROOM_CACHE_VERIFY=0`` turns it off for anyone who wants
        those seconds back.
        """
        try:
            if blob.stat().st_size != size:
                self._forget_blob(blob, "size does not match the entry")
                return False
        except OSError:
            return False
        payload: bytes | None = None
        if self.verify:
            try:
                payload = blob.read_bytes()
            except OSError:
                return False
            if hashlib.sha256(payload).hexdigest() != digest:
                self._forget_blob(blob, "contents do not match its name")
                return False
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        try:
            if self._linking:
                try:
                    os.link(blob, tmp)
                except OSError as exc:
                    # A different filesystem, a filesystem with no hard links, a
                    # Windows share: none of those will start working during
                    # this build, so stop trying and say so once. Anything else
                    # - the blob deleted by a concurrent sweep, say - falls
                    # through to a copy and leaves linking on.
                    if _permanent_link_failure(exc):
                        self._linking = False
                        self._note(
                            "page images are being copied rather than hard-linked: the cache "
                            f"and the output directory cannot share files ({exc.strerror})"
                        )
            if not tmp.exists():
                if payload is not None:
                    _write_bytes(tmp, payload)
                else:
                    shutil.copyfile(blob, tmp)
            os.replace(tmp, target)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            self._note(f"could not put {target.name} in place ({exc})")
            return False
        return True

    def _undo(self, written: Iterable[Path]) -> None:
        for path in written:
            with contextlib.suppress(OSError):
                path.unlink()

    # -- writing ---------------------------------------------------------

    def put(
        self, job: PageJob, outcome: PageOutcome, source_sha256: str | None = None
    ) -> bool:
        """Store this outcome and its images. False means it was not stored.

        Call it with the outcome exactly as ``process_page`` returned it, before
        ``annotate_document`` has been near it.
        """
        if not self.enabled or not self._writable:
            return False
        refusal = _cacheable(outcome)
        if refusal:
            self.refusals += 1
            return False
        if _looks_annotated(outcome.page):
            self.refusals += 1
            self._note(
                "a page was offered to the cache after annotation and was not stored; "
                "store outcomes as they arrive, before annotate_document"
            )
            return False
        key = self.key(job, source_sha256)
        if not key:
            return False
        if not self._unchanged_since_discovery(job.pdf):
            self.refusals += 1
            self._note(
                f"{Path(job.pdf).name} changed while it was being read; nothing from it "
                "was cached. Build it again when it has settled."
            )
            return False

        try:
            body = json.dumps(
                {"format": FORMAT, "key": key, "outcome": encode_outcome(outcome), "blobs": []},
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (CodecError, ValueError, TypeError) as exc:
            # allow_nan=False is doing real work here: a NaN in a ratio would
            # round-trip through JSON as a value json.loads accepts and nothing
            # else does.
            self.refusals += 1
            _LOG.debug("not caching %s p%d: %s", outcome.doc_id, outcome.number, exc)
            return False

        blobs = self._store_images(job, outcome)
        if blobs is None:
            return False
        entry = json.loads(body)
        entry["blobs"] = blobs

        payload = gzip.compress(
            json.dumps(entry, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            compresslevel=6,
            mtime=0,
        )
        refs = "".join(f"{digest}\n" for _name, digest, _size in blobs).encode("ascii")
        if not self._write(self._refs_path(key), refs):
            return False
        if not self._write(self._entry_path(key), payload):
            return False
        self.stores += 1
        self.added_bytes += len(payload) + len(refs)
        self._stamp_environment()
        return True

    def _store_images(
        self, job: PageJob, outcome: PageOutcome
    ) -> list[list[Any]] | None:
        """Copy this page's encoded images into the blob store.

        The files were written by ``raster.encode_page`` moments ago and are
        still in the operating system's page cache, so this is a read and a hard
        link rather than a re-encode. On a filesystem that will not link them it
        is a read and a write, which is the same order of cost as writing them
        the first time and still a fraction of encoding them.
        """
        prefix = job.media_prefix.rstrip("/") + "/"
        media = Path(job.media_dir)
        records: list[list[Any]] = []
        for variant in [*outcome.page.images, *outcome.page.thumbs]:
            if not variant.path.startswith(prefix):
                self._note(f"unexpected image path {variant.path!r}; not caching this page")
                return None
            name = variant.path[len(prefix):]
            if "/" in name:
                self._note(f"unexpected image path {variant.path!r}; not caching this page")
                return None
            source = media / name
            try:
                payload = source.read_bytes()
            except OSError:
                return None
            digest = hashlib.sha256(payload).hexdigest()
            if not self._store_blob(source, digest, payload):
                return None
            records.append([name, digest, len(payload)])
            self.added_bytes += len(payload)
        return records

    def _store_blob(self, source: Path, digest: str, payload: bytes) -> bool:
        target = self._blob_path(digest)
        if target.exists():
            # Content-addressed: an existing blob with this name is this blob.
            # Touched so that eviction counts it as recently wanted.
            self._touch_path(target)
            return True
        tmp = target.with_name(f".{digest}.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            linked = False
            if self._linking:
                try:
                    os.link(source, tmp)
                    linked = True
                except OSError as exc:
                    self._linking = not _permanent_link_failure(exc)
            if not linked:
                _write_bytes(tmp, payload)
            os.replace(tmp, target)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            self._on_write_error(exc)
            return False
        return True

    def _write(self, path: Path, payload: bytes) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, payload)
        except OSError as exc:
            self._on_write_error(exc)
            return False
        return True

    def _on_write_error(self, exc: OSError) -> None:
        """A disk that is full, or a directory that is not ours to write in.

        Both stop the cache writing for the rest of the build and neither stops
        the build. Reads keep working: a full disk does not make the entries
        already there wrong.
        """
        import errno

        self._writable = False
        if exc.errno == errno.ENOSPC:
            self._note(f"the disk holding {self.root} is full; nothing more will be cached")
        elif exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            self._note(f"{self.root} is not writable; this build will not add to the cache")
        else:
            self._note(f"the cache could not be written ({exc}); this build will not add to it")

    # -- housekeeping ----------------------------------------------------

    def _note(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
        _LOG.warning("%s", message)

    def _touch(self, key: str) -> None:
        self._touch_path(self._entry_path(key))

    def _touch_path(self, path: Path) -> None:
        """Last-used time, for eviction. Failing to record it is not a failure.

        mtime rather than atime because atime is off or lazy on most filesystems
        an operator will meet, and a read-only cache directory - a shared
        read-only mount, a cache on a stick - must still serve hits."""
        with contextlib.suppress(OSError):
            os.utime(path, None)

    def _drop(self, key: str, why: str) -> None:
        _LOG.debug("dropping cache entry %s: %s", key[:12], why)
        for path in (self._entry_path(key), self._refs_path(key)):
            with contextlib.suppress(OSError):
                path.unlink()

    def _forget_blob(self, blob: Path, why: str) -> None:
        self._note(f"a cached image was {why}; it has been removed and will be made again")
        with contextlib.suppress(OSError):
            blob.unlink()

    def reset(self) -> None:
        """Start counting again, for the next build.

        A watch session keeps one cache open for hours and runs a build a
        minute. Without this, the second rebuild reports the first one's hits as
        well as its own and the numbers stop meaning anything. Warnings are kept
        - a read-only cache directory is still read-only next time - and so is
        the writability flag, because whatever made writing fail is still true.
        """
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.refusals = 0
        self.saved_seconds = 0.0
        self.restore_seconds = 0.0
        self.bytes_restored = 0

    def summary(self) -> str:
        """One line for the operator, or empty when there is nothing to say."""
        if not self.enabled:
            return f"cache off: {self.disabled_reason}" if self.disabled_reason else ""
        total = self.hits + self.misses
        if not total:
            return ""
        line = f"{self.hits:,} of {total:,} pages came from the cache"
        if self.saved_seconds >= 1:
            line += f", saving about {human_seconds(self.saved_seconds)} of work"
        return line

    # -- size, eviction, removal -----------------------------------------

    def stats(self) -> CacheStats:
        """What is on disk. A walk over the tree; no entry is decompressed."""
        stats = CacheStats(root=self.root, max_bytes=self.max_bytes, writable=self._writable)
        if not self.root.exists():
            stats.exists = False
            return stats
        for path, st in _walk(self.root / "entries"):
            if path.name.endswith(".json.gz"):
                stats.entries += 1
                stats.oldest = min(stats.oldest or st.st_mtime, st.st_mtime)
                stats.newest = max(stats.newest, st.st_mtime)
            stats.entry_bytes += st.st_size
        for _path, st in _walk(self.root / "blobs"):
            stats.blobs += 1
            stats.blob_bytes += st.st_size
        stats.bytes = stats.entry_bytes + stats.blob_bytes
        return stats

    def prune(self, max_bytes: int | None = None) -> PruneReport:
        """Evict whole entries, least recently used first, until we are inside
        the limit; then sweep blobs nothing wants any more.

        Whole entries rather than individual blobs, because half an entry is a
        miss with the storage cost of a hit. Least-recently-*used* rather than
        least-recently-written, because a page nobody has rebuilt in six months
        is exactly the one to lose and a page rebuilt every hour is not - which
        is why :meth:`get` touches the entry it serves.

        Nothing here is fatal. Every unlink is allowed to fail; a file that will
        not go away is counted and left alone.
        """
        budget = self.max_bytes if max_bytes is None else max(0, max_bytes)
        report = PruneReport()
        if not self.root.exists():
            return report

        blob_size: dict[str, int] = {}
        for path, st in _walk(self.root / "blobs"):
            blob_size[path.name] = st.st_size

        entries: list[tuple[float, str, int]] = []
        sidecar: dict[str, int] = {}
        for path, st in _walk(self.root / "entries"):
            if path.name.endswith(".json.gz"):
                entries.append((st.st_mtime, path.name[: -len(".json.gz")], st.st_size))
            elif path.name.endswith(".refs"):
                sidecar[path.name[: -len(".refs")]] = st.st_size
        entries.sort(reverse=True)  # most recently used first

        kept: set[str] = set()
        wanted: set[str] = set()
        used = 0
        for _mtime, key, size in entries:
            refs = self._read_refs(key)
            size += sidecar.get(key, 0)
            cost = size + sum(blob_size.get(d, 0) for d in refs if d not in wanted)
            if used + cost > budget:
                # Including when this is the *first* entry and the limit is
                # smaller than one page: keeping it would break the limit, and
                # dropping it turns the cache into a no-op, which is what a
                # limit that small is asking for.
                report.entries_removed += 1
                report.bytes_removed += size
                report.errors += self._remove(self._entry_path(key))
                report.errors += self._remove(self._refs_path(key))
                continue
            used += cost
            kept.add(key)
            wanted.update(refs)

        for path, st in _walk(self.root / "entries"):
            if not path.name.endswith(".refs"):
                continue
            if path.name[: -len(".refs")] not in kept:
                report.errors += self._remove(path)
                report.bytes_removed += st.st_size

        now = time.time()
        loose = sorted(
            (st.st_mtime, path, st.st_size)
            for path, st in _walk(self.root / "blobs")
            if path.name not in wanted
        )
        for mtime, path, size in loose:
            # A blob nothing references may still be one that another build
            # wrote a second ago and is about to claim - blobs are written
            # before the entry that names them. Keeping it costs a little space
            # for an hour; deleting it would cost that build one page of work.
            # So it is kept while there is room, and not at the expense of the
            # limit, which is a promise and this is not.
            if now - mtime < BLOB_GRACE_SECONDS and used + size <= budget:
                used += size
                continue
            report.blobs_removed += 1
            report.bytes_removed += size
            report.errors += self._remove(path)

        report.bytes_kept = used
        self.added_bytes = 0
        return report

    def _read_refs(self, key: str) -> list[str]:
        try:
            text = self._refs_path(key).read_text(encoding="ascii")
        except OSError:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _remove(self, path: Path) -> int:
        try:
            path.unlink()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            _LOG.debug("could not remove %s: %s", path, exc)
            return 1
        return 0

    def clear(self) -> PruneReport:
        """Delete everything, including layouts written by other versions.

        This is what an operator runs before they publish a source's documents,
        and the promise it makes is total: no page images, no analysis, no entry
        names. It removes ``<cache>/pages`` whole rather than walking it, so a
        file this version does not recognise goes too.
        """
        report = PruneReport()
        pages = self.root.parent
        stats = self.stats()
        report.entries_removed = stats.entries
        report.blobs_removed = stats.blobs
        report.bytes_removed = stats.bytes
        if pages.exists():
            # ignore_errors rather than a callback: `onerror` is deprecated in
            # 3.12 and `onexc` does not exist before it, and what we need to
            # report is only whether anything survived.
            shutil.rmtree(pages, ignore_errors=True)
            if pages.exists():
                report.errors = sum(1 for _path, _st in _walk(pages))
        with contextlib.suppress(OSError):
            (self.root / "entries").mkdir(parents=True, exist_ok=True)
            (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        return report


def _walk(root: Path) -> Iterable[tuple[Path, os.stat_result]]:
    """Every file under *root*, with its stat. Missing directories are empty.

    ``os.scandir`` rather than ``rglob`` because a full cache is 35,000 files
    and scandir gets the stat from the directory entry on Linux and Windows
    instead of a syscall per file."""
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        yield Path(entry.path), entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
        except OSError:
            continue


def _write_bytes(path: Path, payload: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(payload)


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write via a temp file in the same directory, then rename over.

    ``os.replace`` is atomic on POSIX and on Windows, so a reader sees either
    the old file or the new one and never half of either. There is no fsync: a
    cache that loses its last few entries to a power cut has lost nothing that
    cannot be recomputed, and fsync on every page would cost more than the cache
    saves. Corruption from a torn write is caught by gzip's CRC and by the key
    stored inside the entry.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        _write_bytes(tmp, payload)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _permanent_link_failure(exc: OSError) -> bool:
    """Is this a hard link that will never work, or one that failed this time?

    EXDEV is the common one: a cache in ``~/.cache`` and a site on another
    volume. The others are filesystems that do not implement links at all.
    Everything else - a blob a concurrent prune removed a moment ago - is bad
    luck, and bad luck should not cost the whole build its hard links.
    """
    import errno

    return exc.errno in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOSYS, errno.EOPNOTSUPP)


def _flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def open_cache(
    *,
    directory: Path | str | None = None,
    max_bytes: int | str | None = None,
    enabled: bool = True,
    salt: str = "",
    env: Environment | None = None,
) -> PageCache:
    """The cache a build should use, or a disabled one that misses everything.

    Never raises. A caller that cannot open a cache still has a build to run,
    and ``PageCache.warnings`` says what happened so the CLI can pass it on.
    """
    if enabled and not _flag(ENV_ENABLED, default=True):
        enabled = False
    limit: int | None
    try:
        limit = parse_size(max_bytes if max_bytes is not None else os.environ.get(ENV_MAX))
    except ValueError as exc:
        limit = None
        _LOG.warning("%s", exc)
    try:
        cache = PageCache(directory, max_bytes=limit, enabled=enabled, salt=salt, env=env)
    except Exception as exc:  # pragma: no cover - belt and braces
        cache = PageCache(enabled=False)
        cache.disabled_reason = f"the cache could not be opened ({exc})"
        cache.warnings.append(cache.disabled_reason)
    if not enabled and not cache.disabled_reason:
        cache.disabled_reason = "asked not to use it"
    return cache


# --------------------------------------------------------------------------
# watch mode
# --------------------------------------------------------------------------
#
# This lives here rather than in its own module because it only makes sense on
# top of the cache: without one, "rebuild when a file changes" is "spend another
# two hours on the 4,999 pages that did not change", which nobody would leave
# running.
#
# It polls. inotify, FSEvents and ReadDirectoryChangesW are each faster and each
# behave differently - on a network share, on a bind mount, over a queue that
# overflows silently under a large copy - and none of them is in the standard
# library. A stat of every file in a collection is a few milliseconds, once a
# second, and it works the same everywhere. When that stops being true the
# collection is large enough that the build is the slow part by four orders of
# magnitude.

IGNORED_DIRS = frozenset({
    "__macosx", "__pycache__", ".git", ".svn", ".hg", ".idea", ".vscode", "node_modules",
})

IGNORED_SUFFIXES = (".tmp", ".part", ".crdownload", ".swp", ".swx", "~")
"""Half-written files, by the names the tools that make them use. A rebuild
triggered by an editor's swap file is a rebuild of nothing."""


@dataclass(frozen=True, slots=True)
class Change:
    """What moved between two scans."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.modified)

    @property
    def count(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    def describe(self, limit: int = 3) -> str:
        """The change as a person would say it: names, not counts, until there
        are too many names."""
        parts: list[str] = []
        for label, paths in (
            ("new", self.added), ("changed", self.modified), ("gone", self.removed)
        ):
            if not paths:
                continue
            names = [Path(p).name for p in paths]
            shown = ", ".join(names[:limit])
            if len(names) > limit:
                shown += f" and {len(names) - limit} more"
            parts.append(f"{shown} {label}")
        return "; ".join(parts) or "nothing"


Snapshot = dict[str, tuple[int, int]]


class Watcher:
    """A polling watcher over the size and modification time of every file.

    Deliberately not clever. It answers one question - *has anything under here
    changed since I last looked* - and answers it the same way on every
    filesystem.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        ignore: Sequence[Path | str] = (),
        extra: Sequence[Path | str] = (),
        interval: float = 1.0,
        settle: float = 2.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.ignore = tuple(Path(p).resolve() for p in ignore)
        self.extra = tuple(Path(p).resolve() for p in extra if p)
        self.interval = max(0.05, float(interval))
        self.settle = max(0.0, float(settle))
        self.state: Snapshot = self.scan()

    def _skip(self, path: Path) -> bool:
        return any(path == ignored or ignored in path.parents for ignored in self.ignore)

    def scan(self) -> Snapshot:
        """Size and mtime of everything we care about, path to stamp.

        Nanosecond mtime where the filesystem keeps it. Some do not - a few
        network filesystems, and HFS+ - so the size is in the stamp as well;
        between them, an edit that changes neither within one clock tick is the
        only thing that can be missed, and the answer to that is that the
        operator can always run the build again.
        """
        found: Snapshot = {}
        stack = [self.root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        name = entry.name
                        path = Path(entry.path)
                        if name.startswith("."):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if name.casefold() in IGNORED_DIRS or self._skip(path):
                                    continue
                                stack.append(path)
                                continue
                            if name.endswith(IGNORED_SUFFIXES) or self._skip(path):
                                continue
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        found[str(path)] = (st.st_size, st.st_mtime_ns)
            except OSError:
                # A directory that vanished between the scan starting and here,
                # or one we may not read. Neither is a reason to stop watching.
                continue
        for path in self.extra:
            try:
                st = path.stat()
            except OSError:
                continue
            found[str(path)] = (st.st_size, st.st_mtime_ns)
        return found

    def diff(self, before: Snapshot, after: Snapshot) -> Change:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        modified = sorted(p for p in set(before) & set(after) if before[p] != after[p])
        return Change(tuple(added), tuple(removed), tuple(modified))

    def poll(self) -> Change:
        """One look, no waiting. Updates the baseline."""
        current = self.scan()
        change = self.diff(self.state, current)
        self.state = current
        return change

    def wait(
        self,
        stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Change | None:
        """Block until something changes *and then stops changing*.

        The second half is the important one. A 300 MB PDF dropped into the
        folder appears the moment its first byte lands, and building from it
        then reads half a file: poppler reports a page count that is about to be
        wrong, the last page renders as a stripe, and the redaction check runs
        over a page that does not exist yet. So a change is not a change until
        the folder has held still for ``settle`` seconds - which also collapses
        the twelve events of unpacking a zip into one rebuild.

        Returns None only when *stop* asked it to give up.
        """
        pending: Change | None = None
        baseline = self.state
        quiet = 0.0
        while True:
            if stop is not None and stop():
                return None
            sleep(self.interval)
            current = self.scan()
            step = self.diff(self.state, current)
            self.state = current
            if step:
                pending = self.diff(baseline, current) or None
                quiet = 0.0
                continue
            if pending is None:
                continue
            quiet += self.interval
            if quiet >= self.settle:
                return pending


def watch(
    root: Path | str,
    build: Callable[[Change | None], str | None],
    *,
    ignore: Sequence[Path | str] = (),
    extra: Sequence[Path | str] = (),
    interval: float = 1.0,
    settle: float = 2.0,
    emit: Callable[[str], None] = print,
    build_first: bool = True,
    cycles: int | None = None,
    stop: Callable[[], bool] | None = None,
) -> int:
    """Build once, then again whenever the documents change. Returns the number
    of builds run.

    *build* is passed the change that triggered it - None for the first build -
    and may return a line to print. It is called inside a ``try``: a build that
    dies, including on a failed redaction, prints and leaves the watcher
    running, because the operator's next move is to fix the file that broke it
    and the whole point is that they do not have to restart anything.

    *ignore* must include the output directory whenever it sits inside *root*,
    or the site the build writes is a change that triggers a build.
    """
    root = Path(root).resolve()
    watcher = Watcher(root, ignore=ignore, extra=extra, interval=interval, settle=settle)
    runs = 0
    pending: Change | None = None
    if build_first:
        runs += _one(build, None, emit)
    emit(f"Watching {root} for changes. Press Ctrl-C to stop.")
    try:
        while cycles is None or runs < cycles:
            pending = watcher.wait(stop=stop)
            if pending is None:
                break
            emit(f"{_clock()} {pending.describe()} — rebuilding")
            runs += _one(build, pending, emit)
            # Anything the build itself wrote inside the watched tree is not a
            # change worth reacting to. Re-baselining here rather than trusting
            # `ignore` alone is what stops a build that writes a stray file
            # from rebuilding for ever.
            watcher.state = watcher.scan()
    except KeyboardInterrupt:
        emit("")
        emit("Stopped watching.")
    return runs


def _one(
    build: Callable[[Change | None], str | None], change: Change | None, emit: Callable[[str], None]
) -> int:
    started = time.perf_counter()
    try:
        line = build(change)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # a failed build must not end the watch
        emit(f"{_clock()} build failed: {type(exc).__name__}: {exc}")
        return 1
    elapsed = time.perf_counter() - started
    emit(f"{_clock()} {line or 'built'} — {human_seconds(elapsed)}")
    return 1


def _clock() -> str:
    return time.strftime("[%H:%M:%S]")
