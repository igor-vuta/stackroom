"""Reading ``stackroom.toml``.

A collection needs no configuration file at all - ``stackroom build ./papers``
works - so everything here has a defensible default. The file exists for the
things only the operator knows: what the collection is called, where it came
from, which languages the scans are in, and how careful to be.

Errors here are read by someone who has just been handed 2,000 pages and is in
a hurry. They name the file, the key, what was wrong with it, and what to write
instead.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):  # pragma: no cover - trivial branch
    import tomllib
else:  # pragma: no cover - trivial branch
    import tomli as tomllib

CONFIG_NAME = "stackroom.toml"
ABOUT_NAME = "about.md"


class ConfigError(ValueError):
    """A configuration file that a person needs to fix."""


@dataclass(slots=True)
class OcrConfig:
    languages: list[str] = field(default_factory=lambda: ["eng"])
    """Tesseract language codes, in order of expected frequency.

    **This is the recogniser's list, and it is a filter.** Tesseract is told to
    expect exactly these, and every extra alphabet costs accuracy on the
    others, because the recogniser then has more shapes to choose between for
    the same ink. Measured on this project's synthetic English scans, ``eng``
    against ``eng+rus``: no difference at all on undamaged pages, and on a page
    scanned three degrees out of upright the word error rate doubled, 0.0076 to
    0.0152, and two words came back spelled in Cyrillic letters drawn the same
    way as the Latin ones - ``no`` and ``cape``, which is the failure an
    operator reported. That is why the default is one language, and why adding
    a second should be a decision about a collection rather than a precaution.

    **It is not a claim about what the documents are written in**, and nothing
    downstream may treat it as one. ``ingest/quality.py`` is handed this list
    as a *prior*: it can raise a page's stopword ratio, never lower it, and a
    page in a language nobody declared is judged as the language it is, by
    ``lang.detect_language``'s word lists rather than by this. It used to be a
    filter there too, and the result was that a born-digital Russian page in a
    collection declared ``["eng"]`` scored a stopword ratio of zero, was told
    its own text layer "does not read as language", and was re-OCR'd badly or
    published as unreadable.

    There is deliberately **no second key** for "what the text is expected to
    be". It would be a fourth language setting in a tool that already has three
    that people confuse - this one, ``search.language`` and the top-level
    ``language`` - and it would buy almost nothing: the judge takes the maximum
    over every word list it has, so a declared list changes the answer only for
    a page that is genuinely two languages at once. What the archive learns
    about a page's language, it learns by reading the page; when that answer is
    not one of these codes, the build says so, once, with a count."""
    mode: str = "auto"
    """``auto`` reads the embedded text layer and only recognises pages that
    lack one or whose layer is broken. ``always`` recognises every page even
    when text is present, which is slower and occasionally better on a PDF
    whose text layer was itself produced by worse OCR. ``never`` skips
    recognition, which leaves scans unsearchable and says so on the page."""
    psm: int = 3
    auto_rotate: bool = True
    timeout: float = 120.0


@dataclass(slots=True)
class RenderConfig:
    dpi: int = 150
    widths: list[int] = field(default_factory=lambda: [1600, 900])
    thumb_width: int = 240
    formats: list[str] = field(default_factory=lambda: ["avif", "webp"])
    max_megapixels: float = 40.0


@dataclass(slots=True)
class SafetyConfig:
    hidden_text: str = "stop"
    """What to do when text is found underneath a redaction box.

    ``stop`` refuses to build. ``warn`` builds and prints the finding. ``ignore``
    is not offered: if you want to publish a document with a failed redaction,
    fix the document.

    The default is the strict one on purpose. Publishing a failed redaction can
    expose a source, and the operator is usually the last person who could have
    caught it."""

    publish_originals: bool = True
    """Copy each source file into the site so readers can check the renderings
    against the bytes. Turning this off makes the archive unverifiable; the
    build says so."""

    strip_metadata: bool = False
    """Remove author, producer and revision metadata from published originals.
    Off by default because the metadata is itself evidence about the
    production - but a leaked draft's tracked-changes history is a real
    hazard, so the option is here."""


@dataclass(slots=True)
class SearchConfig:
    enabled: bool = True

    language: str = ""
    """The language the search index stems in, when it is not the documents'.

    Empty - the default - means *ask the documents*: the language most of the
    pages were actually read as. Set this only to overrule that.

    It is deliberately not the top-level ``language``, which is the language of
    the **interface** and decides the masthead, the notices and ``<html lang>``.
    Those are two different facts about an archive and they disagree often: a
    Russian-language archive of English documents wants a Russian interface and
    an English stemmer, and using one setting for both gives it a Russian
    stemmer over English prose, where "filed" and "filing" stop being the same
    word. See ``SiteBuilder.index_language`` and docs/TRANSLATING.md."""

    min_query: int = 2
    """Below this length a query matches most of the corpus, and latency tracks
    the number of hits rather than the corpus size: 3 ms at 59 hits, 3.2 s at
    20,000. Refusing one-character queries is not a limitation, it is the
    difference between instant and frozen."""


@dataclass(slots=True)
class Config:
    title: str = "Untitled collection"
    description: str = ""
    language: str = "en"
    jurisdiction: str = "us"
    base_url: str = ""
    source_url: str = ""
    license: str = ""
    contact: str = ""

    ocr: OcrConfig = field(default_factory=OcrConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    search: SearchConfig = field(default_factory=SearchConfig)

    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)

    path: Path | None = None
    """Where this came from, or None if it is all defaults."""

    about_path: Path | None = None


_SECTIONS = {"ocr": OcrConfig, "render": RenderConfig, "safety": SafetyConfig, "search": SearchConfig}

def _jurisdictions() -> set[str]:
    """The vocabularies that actually exist, asked at the source.

    This used to be a hardcoded set here as well, which made "adding a
    jurisdiction is one entry in a dict" false: the entry went in, and the
    config file then refused the name.
    """
    try:
        from .ingest.exemptions import VOCABULARIES

        return set(VOCABULARIES)
    except Exception:  # pragma: no cover - a broken import is reported elsewhere
        return {"us", "uk", "ca", "eu"}


_VALID = {
    "ocr.mode": {"auto", "always", "never"},
    "safety.hidden_text": {"stop", "warn"},
    "jurisdiction": _jurisdictions(),
}


MAX_CONFIG_DEPTH = 3
"""How many directories above the documents ``find`` will look.

Enough for the layout the walk exists to serve - ``stackroom build
papers/2019/march`` finding ``papers/stackroom.toml`` - and not enough to reach
a home directory or ``/tmp`` from anywhere real. See :func:`find`.
"""


def find(start: Path) -> Path | None:
    """Look for ``stackroom.toml`` beside the documents, then a little above.

    Not all the way to the filesystem root, which is what it used to do. This
    file decides the title, the jurisdiction, whether originals are published
    and how long a subprocess may run, and a file the operator has never opened
    - in ``/tmp``, in a shared parent, in a home directory - should not be able
    to decide those. During the review that produced ``docs/THREAT-MODEL.md`` a
    ``stackroom.toml`` written by an unrelated process governed a build and
    aborted it.

    Three levels rather than one, because ``stackroom build papers/2019/march``
    genuinely has to find ``papers/stackroom.toml``
    (``test_the_configuration_is_found_from_deep_inside_the_collection``), and
    that case is indistinguishable on disk from the hostile one: both are a
    configuration file three directories above an empty folder. A depth bound
    cannot tell them apart, so it is a floor rather than the fix - the fix is
    that the CLI now prints which file it used, and says so louder when the
    file is not inside the folder the operator named.
    """
    start = Path(start).resolve()
    here = start if start.is_dir() else start.parent
    for candidate in (here, *list(here.parents)[:MAX_CONFIG_DEPTH]):
        found = candidate / CONFIG_NAME
        if found.is_file():
            return found
    return None


def load(path: Path | None) -> Config:
    """Read a configuration file, or return the defaults when there is none."""
    if path is None:
        return Config()
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: this is not valid TOML.\n  {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: could not be read.\n  {exc}") from exc

    cfg = Config(path=path)
    known_top = {f.name for f in fields(Config)} - {"path", "about_path"}

    for key, value in raw.items():
        if key in _SECTIONS:
            _fill(getattr(cfg, key), value, path, key)
        elif key in known_top:
            _set(cfg, key, value, path, key)
        else:
            raise ConfigError(
                f"{path}: unknown setting {key!r}.\n"
                f"  Known settings: {', '.join(sorted(known_top | set(_SECTIONS)))}"
            )

    about = path.parent / ABOUT_NAME
    cfg.about_path = about if about.is_file() else None
    _validate(cfg, path)
    return cfg


def _fill(section: Any, value: Any, path: Path, prefix: str) -> None:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [{prefix}] should be a table, e.g.\n  [{prefix}]\n  ...")
    known = {f.name for f in fields(section)}
    for key, val in value.items():
        if key not in known:
            raise ConfigError(
                f"{path}: unknown setting {prefix}.{key!r}.\n"
                f"  Known settings in [{prefix}]: {', '.join(sorted(known))}"
            )
        _set(section, key, val, path, f"{prefix}.{key}")


def _set(target: Any, key: str, value: Any, path: Path, label: str) -> None:
    current = getattr(target, key)
    if isinstance(current, bool) and not isinstance(value, bool):
        raise ConfigError(f"{path}: {label} should be true or false, not {value!r}.")
    if isinstance(current, list) and not isinstance(value, list):
        raise ConfigError(f"{path}: {label} should be a list, e.g. {label} = [{value!r}]")
    if isinstance(current, str) and not isinstance(value, str):
        raise ConfigError(f"{path}: {label} should be text in quotes, not {value!r}.")
    if (
        isinstance(current, (int, float))
        and not isinstance(current, bool)
        and (not isinstance(value, (int, float)) or isinstance(value, bool))
    ):
        raise ConfigError(f"{path}: {label} should be a number, not {value!r}.")
    setattr(target, key, value)


def _validate(cfg: Config, path: Path) -> None:
    for label, allowed in _VALID.items():
        obj: Any = cfg
        *parents, leaf = label.split(".")
        for parent in parents:
            obj = getattr(obj, parent)
        value = getattr(obj, leaf)
        if value not in allowed:
            raise ConfigError(
                f"{path}: {label} = {value!r} is not one of "
                f"{', '.join(repr(a) for a in sorted(allowed))}."
            )

    if cfg.render.dpi < 72 or cfg.render.dpi > 600:
        raise ConfigError(
            f"{path}: render.dpi = {cfg.render.dpi} is outside 72-600.\n"
            "  150 is right for typed pages; go to 300 only for small print or handwriting."
        )
    if not cfg.render.widths:
        raise ConfigError(f"{path}: render.widths is empty; give at least one, e.g. [1600, 900]")
    for w in cfg.render.widths:
        if not isinstance(w, int) or not 200 <= w <= 6000:
            raise ConfigError(f"{path}: render.widths contains {w!r}; each must be 200-6000.")
    unknown_formats = [f for f in cfg.render.formats if f not in ("avif", "webp", "jpeg", "png")]
    if unknown_formats:
        raise ConfigError(
            f"{path}: render.formats contains {unknown_formats!r}; "
            "choose from 'avif', 'webp', 'jpeg', 'png'."
        )
    if not cfg.ocr.languages:
        raise ConfigError(
            f"{path}: ocr.languages is empty. Use Tesseract codes, e.g. ['eng', 'rus'].\n"
            "  This is what the recogniser should expect on a scan, not what the "
            "documents are written in - a page in another language is still read and "
            "judged as that language when it has its own text layer."
        )
    if cfg.base_url and not cfg.base_url.startswith(("http://", "https://", "/")):
        raise ConfigError(
            f"{path}: base_url = {cfg.base_url!r} should start with https:// or /."
        )
    # Bounded because a stackroom.toml often arrives *with* the documents, and
    # a number here is an argument to a subprocess run on files nobody trusts.
    if not 1.0 <= cfg.ocr.timeout <= 3600.0:
        raise ConfigError(
            f"{path}: ocr.timeout = {cfg.ocr.timeout} is outside 1-3600 seconds.\n"
            "  0 does not mean 'no limit' here - pytesseract reads a falsy timeout as "
            "no timeout at all, so tesseract is never interrupted and one page can stop "
            "the build for ever."
        )
    if not 0 <= cfg.ocr.psm <= 13:
        raise ConfigError(
            f"{path}: ocr.psm = {cfg.ocr.psm} is outside 0-13.\n"
            "  3 is the default and is right for a page of text; see `tesseract --help-psm`."
        )
    if not 1 <= cfg.search.min_query <= 20:
        raise ConfigError(
            f"{path}: search.min_query = {cfg.search.min_query} is outside 1-20.\n"
            "  Below 2 a query matches most of the corpus and search takes seconds."
        )


TEMPLATE = """\
# Every setting here is optional. Delete what you don't need.

title = "{title}"
description = ""

# Where the site will live. Only needed for absolute links in the manifest
# and for citation URLs; the site itself uses relative paths and works from
# any directory, including a USB stick.
# base_url = "https://example.org/archive/"

# Which statute's withholding codes to look for: us, uk, ca, eu.
jurisdiction = "us"

# The language of the *interface* - the masthead, the notices, the ledger.
# See docs/TRANSLATING.md for what ships and how to add one.
# language = "en"

[ocr]
# Tesseract language codes: what the recogniser should expect on a *scan*. Run
# `stackroom doctor` to see what is installed. Every extra alphabet costs
# accuracy on the others, so list what is really there and nothing else - a
# page in a language you did not list is still read and judged correctly if it
# has its own text layer, and the build tells you it found one.
languages = ["eng"]
# auto = recognise only pages without usable text. always = recognise everything.
mode = "auto"

[render]
dpi = 150
widths = [1600, 900]

[safety]
# What to do if text is found underneath a black box. Leave this alone unless
# you have a reason: it is the setting that stops you publishing a leak.
hidden_text = "stop"

[search]
# The language the search index stems in. Left unset, it is the language the
# documents were read as - which is not always the interface language above.
# language = "en"
"""

ABOUT_TEMPLATE = """\
# About this collection

<!--
Readers trust an archive that explains itself. Say, in a few sentences:

  - who released these documents, and to whom
  - under what request, and when
  - what is missing, and why
  - what you have and have not verified

Stackroom will not invent this for you, and a collection without it looks
like it fell off the back of a lorry.
-->
"""
