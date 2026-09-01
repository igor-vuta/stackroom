"""Translating the generated site.

An archive of Russian documents that publishes an English interface is telling
its readers the archive is not for them. This module is what stops that: every
piece of interface text in the built site comes from a catalogue, chosen by
``language`` in ``stackroom.toml``, and substituted at **build time**. The
output is still a static site - there is no runtime translation layer, no
language switcher, no second request. One build, one language, one folder of
plain files.

Why not gettext, why not babel
------------------------------
Both are good and neither is here, because a new runtime dependency is a new
thing every operator has to install, every distribution has to package and
every reviewer has to audit, and what they buy us is one plural table and one
number formatter. Those are eighty lines. The catalogues are JSON so a
translator can open one in any editor, diff it in a pull request, and never
learn a tool.

What a catalogue is
-------------------
``locales/<code>.json``, shipped inside the package::

    {
      "locale": "ru",
      "name": "Русский",
      "english_name": "Russian",
      "direction": "ltr",
      "plural": "ru",
      "number": {"group": "\\u00a0", "decimal": ",", "percent": "{n} %"},
      "date": {"short": "{d}.{m}.{y}"},
      "messages": {
        "nav.about": "Об архиве",
        "page.redactions": {"one": "…", "few": "…", "many": "…", "other": "…"}
      }
    }

A message is either a string or an object keyed by CLDR plural category. Both
kinds take ``{named}`` placeholders and never positional ones: a translator
must be free to move ``{count}`` to the end of the sentence, which is where
several of the languages this project cares about want it.

Two rules the checker enforces, because both are silent when broken:

- **A message whose key ends in ``_html`` may contain markup, and no other
  message may.** ``t()`` escapes the parameters of an ``_html`` message and
  trusts the message itself; for every other key it returns a plain string that
  Jinja escapes on the way out. Without the naming rule there is no way to tell
  by looking whether a ``<`` in a catalogue is a tag or a less-than sign.
- **A translation carries exactly the placeholders its English source
  carries.** ``{cout}`` renders as the literal text ``{cout}`` on a published
  page, in a language the person who built the site probably cannot read.

Translator notes live in one place, ``en.json``'s top-level ``notes``, keyed by
message key. They are not duplicated into the other catalogues because a note
duplicated is a note that drifts; ``python -m stackroom.i18n show <key>`` and
``python -m stackroom.i18n check <code> --missing`` both print the note beside
the English, which is where a translator actually needs it.

Falling back
------------
A key missing from a catalogue falls back to English rather than to a crash or
to an empty page, and every fallback is recorded on the translator so a build
can say how much of the interface was not translated. A contributor working on
a catalogue runs with ``strict=True`` (or ``STACKROOM_I18N_STRICT=1``) and gets
an exception at the first missing key instead.
"""

# ruff: noqa: RUF002
# The plural rules are documented by quoting the languages they are rules for -
# "1 страница / 2 страницы / 5 страниц" is the whole argument for the Russian
# one - and the number formats hold real non-breaking spaces, because a
# non-breaking space is the separator Russian uses and an ordinary one would be
# the bug. Same reasoning as `lang.py`: every ambiguous character in this file
# is deliberate and load-bearing.

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from markupsafe import Markup, escape

from .lang import normalize_language_codes

__all__ = [
    "CATEGORIES",
    "DEFAULT_LOCALE",
    "JS_PREFIX",
    "JS_SHARED",
    "LOCALES",
    "Catalog",
    "CatalogError",
    "MissingMessage",
    "Report",
    "Translator",
    "ambiguous_forms",
    "available",
    "browser_catalog",
    "browser_json",
    "browser_script",
    "check",
    "direction_for",
    "format_number",
    "install",
    "load",
    "normalize_locale",
    "plural_category",
    "plural_data",
    "translator_for",
]

LOCALES = Path(__file__).resolve().parent / "locales"
DEFAULT_LOCALE = "en"

CATEGORIES: tuple[str, ...] = ("zero", "one", "two", "few", "many", "other")
"""The CLDR cardinal plural categories, in CLDR's own order.

Not every language uses every one of them, and no language uses all six for
cardinals except Arabic and Welsh. ``other`` is required everywhere: CLDR
guarantees it exists for every language, and it is where a value that matches
no rule lands.
"""


class CatalogError(ValueError):
    """A catalogue a person needs to fix."""


class MissingMessage(KeyError):
    """A key that is not in the catalogue, raised only in strict mode."""


# --------------------------------------------------------------------------
# plural rules
# --------------------------------------------------------------------------
#
# Each rule takes the three CLDR operands this project can actually produce and
# returns a category name.
#
#   n  the absolute value of the number
#   i  its integer part
#   v  the number of visible fraction digits
#
# CLDR also defines w, f, t, c and e. Nothing in this site pluralises a compact
# number ("1.2M") or a number with trailing fraction zeros, so those operands
# are always zero here and the rules below drop them. Where dropping one
# changes an answer for some language, the rule says so.
#
# The rules are transcriptions of CLDR's `plurals.xml`, not paraphrases of it.
# Read them beside https://cldr.unicode.org/index/cldr-spec/plural-rules .


def _rule_other(i: int, v: int, n: float) -> str:
    """Chinese, Japanese, Korean, Vietnamese, Thai: no cardinal agreement."""
    return "other"


def _rule_one_i(i: int, v: int, n: float) -> str:
    """English, German, Dutch, Italian: ``one`` is exactly the integer 1.

    ``v == 0`` matters and is not decoration: English says "1.0 pages", not
    "1.0 page", and a rule written as ``n == 1`` gets that wrong.
    """
    return "one" if i == 1 and v == 0 else "other"


def _rule_one_n(i: int, v: int, n: float) -> str:
    """Spanish: ``one`` is the value 1, fractional or not.

    Spanish says "1,0 página" where English says "1.0 pages" - the agreement
    follows the value, not the notation.

    Approximated: CLDR 42 added a ``many`` category to Spanish for round
    millions ("1 millón de páginas"). It is reachable only from the compact and
    long number formats, which this project never emits - every number here is
    written out in full - so ``many`` is not implemented. The visible cost if
    it were ever wanted is one word for exactly the values 1000000, 2000000 and
    so on; the plain form Spanish uses at those values is the ``other`` form
    anyway, so nothing on a page is wrong today.
    """
    return "one" if n == 1 else "other"


def _rule_one_zero(i: int, v: int, n: float) -> str:
    """French, Portuguese: zero and one both take the singular.

    "0 page", "1 page", "2 pages". CLDR writes French as ``i = 0,1`` and
    Portuguese as ``i = 0..1``; both mean the integer part, so 0.5 is ``one``.
    """
    return "one" if i in (0, 1) else "other"


def _rule_east_slavic(i: int, v: int, n: float) -> str:
    """Russian and Ukrainian, which share a rule for cardinals exactly.

    The one people get wrong::

        1 страница     21 страница    101 страница    one
        2 страницы     22 страницы    103 страницы    few
        5 страниц      11 страниц     111 страниц     many
        0 страниц      25 страниц     100 страниц     many

    111 is the trap. The naive rule - "ends in 1, use the singular" - produces
    *111 страница*, because it never checks the tens. The teens 11-14 take the
    ``many`` form whatever their last digit is, and 111, 112, 113 and 114 are
    teens in their last two digits. Any implementation that looks at ``i % 10``
    without also looking at ``i % 100`` is wrong for 11, 12, 13, 14, 111, 112,
    113, 114, 211… and right everywhere else, which is why it survives casual
    testing.

    ``other`` is unreachable from this site: it is the fraction form ("1,5
    страницы"), and every count here is a whole number of pages, documents or
    boxes. Catalogues still have to carry it - CLDR requires ``other`` for
    every language - and ``en.json``'s note says what to put there.
    """
    if v != 0:
        return "other"
    tens = i % 100
    units = i % 10
    if units == 1 and tens != 11:
        return "one"
    if 2 <= units <= 4 and not (12 <= tens <= 14):
        return "few"
    return "many"


def _rule_polish(i: int, v: int, n: float) -> str:
    """Polish: the same shape as East Slavic, and not the same rule.

    ``one`` is only the bare 1 - Polish says "21 stron", not "21 strona", where
    Russian says "21 страница". Copying the Russian rule across is the mistake
    this function exists to not make.
    """
    if v != 0:
        return "other"
    if i == 1:
        return "one"
    tens = i % 100
    units = i % 10
    if 2 <= units <= 4 and not (12 <= tens <= 14):
        return "few"
    return "many"


def _rule_arabic(i: int, v: int, n: float) -> str:
    """Arabic: all six categories, and the only rule here that uses ``zero``.

    0 صفحة, 1 صفحة, 2 صفحتان, 3-10 صفحات, 11-99 صفحة, 100+ صفحة - the dual is
    a grammatical number, not a special case, and a catalogue that leaves
    ``two`` out of an Arabic message is wrong at exactly the value 2.
    """
    if n == 0:
        return "zero"
    if n == 1:
        return "one"
    if n == 2:
        return "two"
    if v != 0:
        return "other"
    tens = i % 100
    if 3 <= tens <= 10:
        return "few"
    if 11 <= tens <= 99:
        return "many"
    return "other"


def _rule_hebrew(i: int, v: int, n: float) -> str:
    """Hebrew: singular, dual, plural.

    Approximated: CLDR before v42 also gave Hebrew a ``many`` category for
    multiples of ten above zero ("20 עמודים" behaving differently from "22").
    It was removed as not reflecting modern usage, and this follows the current
    table. A catalogue may still supply ``many``; it will never be selected.
    """
    if v != 0:
        return "other"
    if i == 1:
        return "one"
    if i == 2:
        return "two"
    return "other"


PLURAL_RULES: dict[str, Callable[[int, int, float], str]] = {
    "other": _rule_other,
    "en": _rule_one_i,
    "de": _rule_one_i,
    "nl": _rule_one_i,
    "it": _rule_one_i,
    "es": _rule_one_n,
    "fr": _rule_one_zero,
    "pt": _rule_one_zero,
    "ru": _rule_east_slavic,
    "uk": _rule_east_slavic,
    "pl": _rule_polish,
    "ar": _rule_arabic,
    "he": _rule_hebrew,
    "zh": _rule_other,
    "ja": _rule_other,
    "ko": _rule_other,
    "vi": _rule_other,
    "th": _rule_other,
}
"""Rule set per language code. The key is also what a catalogue names in
``plural``, so a language can borrow another's rule by naming it - Belarusian
would say ``"plural": "ru"`` and be right."""

PLURAL_FORMS: dict[str, tuple[str, ...]] = {
    "other": ("other",),
    "en": ("one", "other"),
    "de": ("one", "other"),
    "nl": ("one", "other"),
    "it": ("one", "other"),
    "es": ("one", "other"),
    "fr": ("one", "other"),
    "pt": ("one", "other"),
    "ru": ("one", "few", "many", "other"),
    "uk": ("one", "few", "many", "other"),
    "pl": ("one", "few", "many", "other"),
    "ar": ("zero", "one", "two", "few", "many", "other"),
    "he": ("one", "two", "other"),
    "zh": ("other",),
    "ja": ("other",),
    "ko": ("other",),
    "vi": ("other",),
    "th": ("other",),
}
"""Which categories a catalogue using each rule must supply.

This is what "complete" means for a plural message, and it is why the checker
can tell a Russian catalogue that it is missing ``few`` rather than waiting for
a reader to meet the number 22.
"""


def ambiguous_forms(rule: str, *, limit: int = 200) -> set[str]:
    """Categories that match more than one integer, so the count must be printed.

    This is the rule behind the single most common mistranslation of a plural
    message, and it is invisible to every other check. English's ``one``
    matches exactly the number 1, so "1 page" is a correct English ``one``
    form. Russian's ``one`` matches 1, 21, 31, 101 and 1001, so a Russian
    ``one`` form written the same way - "1 страница" - publishes the sentence
    "1 страница" on a page that has twenty-one of them.

    A translator copying the shape of the English catalogue makes this mistake
    every time, and the site looks fine until somebody counts.
    """
    counts: dict[str, int] = {}
    for n in range(limit + 1):
        category = plural_category(rule, n)
        counts[category] = counts.get(category, 0) + 1
    return {category for category, seen in counts.items() if seen > 1}


def _operands(value: float | int | Decimal) -> tuple[int, int, float]:
    """CLDR's ``i``, ``v`` and ``n`` for a number we are about to print.

    ``v`` is the count of fraction digits **as written**, so it is taken from
    the decimal text rather than from the float, and trailing zeros count:
    "1.0" has v=1 and is plural in English.
    """
    if isinstance(value, bool):  # bool is an int, and never a count
        raise TypeError("plural selection needs a number, not a bool")
    if isinstance(value, int):
        return (abs(value), 0, float(abs(value)))
    dec = Decimal(str(value)).copy_abs()
    exponent = dec.as_tuple().exponent
    v = -int(exponent) if isinstance(exponent, int) and exponent < 0 else 0
    return (int(dec // 1), v, float(dec))


def plural_category(rule: str, value: float | int) -> str:
    """The CLDR category *value* falls into under the named *rule*."""
    fn = PLURAL_RULES.get(rule)
    if fn is None:
        raise CatalogError(
            f"unknown plural rule {rule!r}; known rules: {', '.join(sorted(PLURAL_RULES))}"
        )
    i, v, n = _operands(value)
    return fn(i, v, n)


# --------------------------------------------------------------------------
# numbers and dates
# --------------------------------------------------------------------------


@dataclass(slots=True)
class NumberFormat:
    """How this locale writes a number.

    The templates used to write ``'{:,}'.format(n)``, which is US English and
    nothing else: a Russian reader expects ``1 234`` with a non-breaking space,
    a Spanish reader expects ``1234`` at four digits and ``10.000`` at five.
    """

    group: str = ","
    """Thousands separator. Non-breaking space (U+00A0) in Russian, Ukrainian,
    French and Polish - an ordinary space would let ``16 000`` break across a
    line as ``16`` and ``000``."""

    decimal: str = "."
    minimum_grouping_digits: int = 1
    """How many digits must precede the first separator before one is written.

    CLDR's ``minimumGroupingDigits``. 1 groups from four digits (``1,000``); 2
    groups only from five (Spanish: ``1000`` but ``10.000``). Getting this
    wrong looks like a typo to a native reader and like nothing at all to
    everybody else.
    """

    percent: str = "{n}%"
    """Where the sign goes and whether a space precedes it. Russian, Ukrainian,
    French and Spanish all put a non-breaking space before ``%``; English does
    not."""

    def format(self, value: float | int, *, digits: int | None = None) -> str:
        if isinstance(value, bool):
            raise TypeError("format() needs a number, not a bool")
        if digits is None:
            text = f"{value:d}" if isinstance(value, int) else f"{value:f}".rstrip("0").rstrip(".")
        else:
            text = f"{value:.{digits}f}"
        sign = ""
        if text.startswith("-"):
            sign, text = "-", text[1:]
        whole, _, frac = text.partition(".")
        if len(whole) >= 3 + self.minimum_grouping_digits:
            chunks = []
            while len(whole) > 3:
                chunks.append(whole[-3:])
                whole = whole[:-3]
            chunks.append(whole)
            whole = self.group.join(reversed(chunks))
        return sign + whole + (self.decimal + frac if frac else "")


def format_number(value: float | int, fmt: NumberFormat | None = None, *, digits: int | None = None) -> str:
    """Convenience for callers that have no catalogue in hand."""
    return (fmt or NumberFormat()).format(value, digits=digits)


_MONTH_KEYS = (
    "month.1", "month.2", "month.3", "month.4", "month.5", "month.6",
    "month.7", "month.8", "month.9", "month.10", "month.11", "month.12",
)

_RTL = frozenset({"ar", "he", "fa", "ur", "yi", "dv", "ps", "ckb", "sd", "ug"})
"""Languages written right to left.

Kept here rather than in :mod:`stackroom.lang`, which answers a different
question - ``script_of`` groups Hebrew with Arabic because the only decision it
takes from the answer is whether the vowel rule applies. Direction is a
property of the *language*, not of the script it happens to be in.
"""


def direction_for(code: str | None) -> str:
    """``"rtl"`` or ``"ltr"`` for a language code."""
    return "rtl" if normalize_locale(code) in _RTL else "ltr"


def normalize_locale(code: str | None) -> str:
    """Whatever the operator wrote, as a code a catalogue could be named for.

    Accepts what :func:`stackroom.lang.normalize_language_codes` accepts -
    ``rus``, ``ru_RU``, ``en-GB`` - and then, unlike it, keeps a base subtag it
    has never heard of. That difference is the whole reason this function
    exists: ``normalize_language_codes`` drops a code with no stopword list,
    which is correct for OCR quality and catastrophic here, because it would
    silently turn an Arabic or Hebrew interface into an English one.
    """
    if not code:
        return DEFAULT_LOCALE
    known = normalize_language_codes([code])
    if known:
        return known[0]
    base = code.strip().lower().replace("-", "_").split("_")[0]
    return base or DEFAULT_LOCALE


# --------------------------------------------------------------------------
# catalogues
# --------------------------------------------------------------------------

Message = str | dict[str, str]


@dataclass(slots=True)
class Catalog:
    """One language's messages, loaded from ``locales/<code>.json``."""

    locale: str = DEFAULT_LOCALE
    name: str = "English"
    """The language's name in itself: Русский, Українська, English."""
    english_name: str = "English"
    direction: str = "ltr"
    plural: str = "en"
    number: NumberFormat = field(default_factory=NumberFormat)
    date: dict[str, str] = field(default_factory=lambda: {"short": "{y}-{m}-{d}"})
    messages: dict[str, Message] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @property
    def forms(self) -> tuple[str, ...]:
        """The plural categories this catalogue has to supply."""
        return PLURAL_FORMS.get(self.plural, ("other",))

    def keys(self) -> set[str]:
        return set(self.messages)


def _number_format(raw: Any, where: str) -> NumberFormat:
    if raw is None:
        return NumberFormat()
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: 'number' must be an object.")
    fmt = NumberFormat()
    for key, attr in (
        ("group", "group"),
        ("decimal", "decimal"),
        ("percent", "percent"),
    ):
        if key in raw:
            if not isinstance(raw[key], str):
                raise CatalogError(f"{where}: number.{key} must be text.")
            setattr(fmt, attr, raw[key])
    if "minimum_grouping_digits" in raw:
        value = raw["minimum_grouping_digits"]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4:
            raise CatalogError(f"{where}: number.minimum_grouping_digits must be 1-4.")
        fmt.minimum_grouping_digits = value
    if "{n}" not in fmt.percent:
        raise CatalogError(f"{where}: number.percent must contain {{n}}, e.g. \"{{n}} %\".")
    return fmt


def load(locale: str, *, locales_dir: Path | None = None) -> Catalog:
    """Read one catalogue. Raises :class:`CatalogError` on anything malformed."""
    directory = Path(locales_dir or LOCALES)
    code = normalize_locale(locale)
    path = directory / f"{code}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        have = ", ".join(available(locales_dir=directory)) or "none"
        raise CatalogError(
            f"no catalogue for {code!r}. Shipped: {have}.\n"
            f"  To add one: copy {directory / 'en.json'} to {path.name} and translate it;\n"
            f"  docs/TRANSLATING.md is the checklist."
        ) from exc
    except (OSError, ValueError) as exc:
        raise CatalogError(f"{path}: could not be read as JSON.\n  {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"{path}: the top level must be an object.")

    messages = raw.get("messages")
    if not isinstance(messages, dict):
        raise CatalogError(f"{path}: 'messages' is missing or is not an object.")
    for key, value in messages.items():
        if isinstance(value, str):
            continue
        if isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
            unknown = set(value) - set(CATEGORIES)
            if unknown:
                raise CatalogError(
                    f"{path}: {key!r} has plural form(s) {sorted(unknown)} that are not "
                    f"CLDR categories. Use: {', '.join(CATEGORIES)}."
                )
            if "other" not in value:
                raise CatalogError(
                    f"{path}: {key!r} is a plural message with no 'other' form. CLDR "
                    "requires one for every language; it is the form a value that "
                    "matches no rule falls back to."
                )
            continue
        raise CatalogError(
            f"{path}: {key!r} must be text, or an object of plural forms whose values "
            "are all text."
        )

    notes = raw.get("notes") or {}
    if not isinstance(notes, dict):
        raise CatalogError(f"{path}: 'notes' must be an object keyed by message key.")

    plural = raw.get("plural") or code
    if plural not in PLURAL_RULES:
        raise CatalogError(
            f"{path}: plural = {plural!r} names no rule. Known: "
            f"{', '.join(sorted(PLURAL_RULES))}.\n"
            "  A language may borrow another's rule by naming it - Belarusian is 'ru'."
        )
    direction = raw.get("direction") or direction_for(code)
    if direction not in ("ltr", "rtl"):
        raise CatalogError(f"{path}: direction must be 'ltr' or 'rtl', not {direction!r}.")

    date = raw.get("date") or {}
    if not isinstance(date, dict) or not all(isinstance(v, str) for v in date.values()):
        raise CatalogError(f"{path}: 'date' must be an object of patterns.")
    date.setdefault("short", "{y}-{m}-{d}")

    return Catalog(
        locale=str(raw.get("locale") or code),
        name=str(raw.get("name") or code),
        english_name=str(raw.get("english_name") or code),
        direction=direction,
        plural=plural,
        number=_number_format(raw.get("number"), str(path)),
        date=date,
        messages=messages,
        notes=notes,
        path=path,
    )


def available(*, locales_dir: Path | None = None) -> list[str]:
    """Codes with a catalogue in the package, sorted, English first."""
    directory = Path(locales_dir or LOCALES)
    if not directory.is_dir():
        return []
    codes = sorted(p.stem for p in directory.glob("*.json"))
    if DEFAULT_LOCALE in codes:
        codes.remove(DEFAULT_LOCALE)
        codes.insert(0, DEFAULT_LOCALE)
    return codes


# --------------------------------------------------------------------------
# substitution
# --------------------------------------------------------------------------

_FIELD = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def placeholders(text: str) -> set[str]:
    """The ``{named}`` slots in a message, ignoring ``{{`` and ``}}``."""
    return {m.group(1) for m in _FIELD.finditer(text) if m.group(1)}


def message_placeholders(message: Message) -> set[str]:
    """The union of the slots across every form of a message.

    Union rather than intersection on purpose: an English ``one`` form is
    allowed to say "1 page" with no ``{count}`` in it while ``other`` says
    "{count} pages", and both are correct.
    """
    if isinstance(message, str):
        return placeholders(message)
    out: set[str] = set()
    for form in message.values():
        out |= placeholders(form)
    return out


# --------------------------------------------------------------------------
# the translator
# --------------------------------------------------------------------------


class Translator:
    """A catalogue, plus everything a template needs to use it.

    Instances are cheap and immutable apart from the fallback record, which is
    what lets a build report how much of its own interface it published in the
    wrong language.
    """

    def __init__(
        self,
        catalog: Catalog,
        fallback: Catalog | None = None,
        *,
        strict: bool | None = None,
        plural_arg: str = "count",
    ) -> None:
        self.catalog = catalog
        self.fallback = fallback if fallback is not None and fallback.locale != catalog.locale else None
        if strict is None:
            strict = os.environ.get("STACKROOM_I18N_STRICT", "").strip().lower() in {
                "1", "true", "yes", "on",
            }
        self.strict = strict
        self.plural_arg = plural_arg
        self.fell_back: set[str] = set()
        """Keys served from English because this catalogue had none."""
        self.unknown: set[str] = set()
        """Keys in no catalogue at all. In a shipped build this must be empty;
        a non-empty set means a template asked for a message that was never
        written, and the key itself was published to a reader."""
        self.missing_catalog: str | None = None
        """The language that was asked for, when there is no catalogue for it.

        Set by :func:`translator_for`, and deliberately not a fake entry in
        :attr:`fell_back`: "there is no catalogue for 'de'" and "nine keys are
        missing from the German one" are different facts with different fixes,
        and a build that reports the first as the second sends an operator to
        edit a file that does not exist. Nothing here falls back per key,
        because the catalogue in hand *is* English.
        """

    # -- properties templates read ---------------------------------------

    @property
    def locale(self) -> str:
        return self.catalog.locale

    @property
    def direction(self) -> str:
        return self.catalog.direction

    @property
    def language_name(self) -> str:
        return self.catalog.name

    # -- lookup ----------------------------------------------------------

    def has(self, key: str) -> bool:
        return key in self.catalog.messages

    def _message(self, key: str) -> tuple[Message | None, Catalog]:
        if key in self.catalog.messages:
            return self.catalog.messages[key], self.catalog
        if self.strict:
            raise MissingMessage(
                f"{key!r} is not in the {self.catalog.locale!r} catalogue "
                f"({self.catalog.path}). Add it, or run without strict mode to fall "
                "back to English."
            )
        if self.fallback is not None and key in self.fallback.messages:
            self.fell_back.add(key)
            return self.fallback.messages[key], self.fallback
        self.unknown.add(key)
        return None, self.catalog

    def t(self, key: str, /, **params: Any) -> str | Markup:
        """The translated message for *key*.

        Plural selection uses the ``count`` parameter when the message has
        forms; the value is also available to the message as ``{count}``, and
        is written with this locale's separators like every other number.

        A key ending in ``_html`` comes back as :class:`~markupsafe.Markup`
        with its parameters escaped - that is how a sentence keeps its ``<a>``
        or its ``<span class="mono">`` while a document title inside it stays
        safe. Every other key comes back as a plain string for Jinja to escape.
        """
        message, source = self._message(key)
        if message is None:
            # Never publish a blank: an untranslated key on the page is ugly
            # and obvious, which is exactly what should happen, and it names
            # itself so the person who sees it can fix it.
            return f"[{key}]"

        if isinstance(message, dict):
            count = params.get(self.plural_arg)
            if count is None:
                raise CatalogError(
                    f"{key!r} is a plural message and was asked for without a "
                    f"{self.plural_arg!r} argument."
                )
            category = plural_category(source.plural, count)
            text = message.get(category) or message.get("other") or ""
        else:
            text = message

        html = key.endswith("_html")
        return self._substitute(text, params, key=key, html=html)

    __call__ = t
    """A translator is callable, so ``t("nav.about")`` in a build module reads
    exactly like ``{{ t('nav.about') }}`` in a template.

    The templates get :meth:`t` bound as a Jinja global by :func:`install`, and
    Python code that holds the translator itself would otherwise have to write
    ``t.t(...)``. One name for one thing, in both places."""

    def _substitute(self, text: str, params: Mapping[str, Any], *, key: str, html: bool) -> str | Markup:
        fmt = self.catalog.number

        def render(value: Any) -> str:
            if isinstance(value, bool):
                return str(value)
            if isinstance(value, (int, float)):
                return fmt.format(value)
            return str(value)

        missing: list[str] = []

        def replace(match: re.Match[str]) -> str:
            whole = match.group(0)
            if whole == "{{":
                return "{"
            if whole == "}}":
                return "}"
            name = match.group(1)
            if name not in params:
                missing.append(name)
                return whole
            value = params[name]
            if html:
                return str(value if isinstance(value, Markup) else escape(render(value)))
            return render(value)

        # The message itself is trusted and its parameters are not. Escaping the
        # message would eat the markup an `_html` message exists to carry;
        # escaping the parameters is what keeps a document title with an
        # ampersand in it from breaking the page it is named on. The catalogues
        # ship inside the package, next to the templates, and are trusted on
        # exactly the same footing.
        out = _FIELD.sub(replace, text)
        if missing and self.strict:
            raise CatalogError(
                f"{key!r} wants parameter(s) {sorted(set(missing))} that were not "
                "passed. The placeholder was left visible on the page."
            )
        return Markup(out) if html else out

    # -- numbers, percentages, dates -------------------------------------

    def n(self, value: float | int, *, digits: int | None = None) -> str:
        """A number, written the way this locale writes numbers."""
        return self.catalog.number.format(value, digits=digits)

    def pct(self, value: float | int, *, digits: int = 0, of_one: bool = False) -> str:
        """A percentage. *value* is already a percentage unless *of_one*."""
        share = value * 100 if of_one else value
        return self.catalog.number.percent.replace("{n}", self.n(share, digits=digits))

    def date(self, value: str, *, style: str = "short") -> str:
        """An ISO ``YYYY-MM-DD`` (or a longer ISO timestamp) in local order.

        Only reordering and separators, no month names unless the catalogue
        supplies them: a wrong month name is worse than a right number, and
        every locale that matters here writes dates in digits.
        """
        text = (value or "")[:10]
        parts = text.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            return text
        y, m, d = parts
        pattern = self.catalog.date.get(style) or self.catalog.date.get("short") or "{y}-{m}-{d}"
        month_name = ""
        index = int(m)
        if 1 <= index <= 12:
            candidate = _MONTH_KEYS[index - 1]
            if self.has(candidate):
                month_name = str(self.t(candidate))
        return (
            pattern.replace("{y}", y)
            .replace("{m}", m)
            .replace("{d}", d)
            .replace("{d0}", str(int(d)))
            .replace("{m0}", str(int(m)))
            .replace("{month}", month_name or m)
        )

    def bytes(self, count: int) -> str:
        """A file size, with the unit from the catalogue.

        Mirrors ``build.site.human_bytes`` exactly - same thresholds, same one
        decimal place, same stripping of a trailing ``.0`` - so wiring it in is
        a substitution rather than a change of behaviour.
        """
        value = float(count)
        if value < 1024:
            # Not `self.n()`: `human_bytes` writes a raw count here, and the only
            # values that would differ are 1000-1023, so grouping them would
            # change the English output of every existing site to buy nothing.
            return self.t("bytes.b", count=int(value), size=str(int(value)))
        for unit in ("kb", "mb", "gb"):
            value /= 1024.0
            if value < 1024 or unit == "gb":
                size = self.n(round(value, 1), digits=1)
                if size.endswith(self.catalog.number.decimal + "0"):
                    size = size[: -(len(self.catalog.number.decimal) + 1)]
                return self.t(f"bytes.{unit}", count=value, size=size)
        return self.t("bytes.gb", count=value, size=self.n(round(value, 1), digits=1))

    # -- reporting -------------------------------------------------------

    def report(self) -> list[str]:
        """Warnings a build can print. Empty when the catalogue is complete."""
        out: list[str] = []
        if self.unknown:
            out.append(
                f"{len(self.unknown)} interface string(s) are in no catalogue and were "
                f"published as their own key: {', '.join(sorted(self.unknown)[:5])}"
                + ("…" if len(self.unknown) > 5 else "")
            )
        if self.missing_catalog:
            out.append(
                f"there is no catalogue for {self.missing_catalog!r}, so the whole "
                "interface was published in English. The site is built and complete; "
                "only its interface language is not the one that was asked for. "
                "`python -m stackroom.i18n list` shows what ships, and "
                f"`python -m stackroom.i18n new {self.missing_catalog}` starts a new "
                "one - docs/TRANSLATING.md is the checklist."
            )
        if self.fell_back:
            out.append(
                f"{len(self.fell_back)} interface string(s) are missing from the "
                f"{self.catalog.locale!r} catalogue and were published in English "
                f"(run `python -m stackroom.i18n check {self.catalog.locale}`)"
            )
        return out


def translator_for(
    language: str | None,
    *,
    locales_dir: Path | None = None,
    strict: bool | None = None,
) -> Translator:
    """The translator for a configured ``language``, falling back to English.

    A language with no catalogue is not an error - most of the languages
    ``stackroom.toml`` accepts for OCR have none - it is an English interface
    and a warning, which is what an operator who wrote ``language = "de"``
    should get instead of a failed build.
    """
    english = load(DEFAULT_LOCALE, locales_dir=locales_dir)
    code = normalize_locale(language)
    if code == DEFAULT_LOCALE:
        return Translator(english, strict=strict)
    try:
        catalog = load(code, locales_dir=locales_dir)
    except CatalogError:
        translator = Translator(english, strict=strict)
        translator.missing_catalog = code
        return translator
    return Translator(catalog, english, strict=strict)


# --------------------------------------------------------------------------
# the browser bundle
# --------------------------------------------------------------------------
#
# About 150 of the messages in the catalogue are written by JavaScript rather
# than by a template: the search status line, the citation panel, the command
# palette, the offline controls, the full-size viewer, the passage bar and the
# keyboard sheet. They are translated at build time like everything else - the
# site stays static, nothing is fetched at load, and there is no runtime
# translation layer - by emitting the messages those files need into
# `assets/i18n.js`, which the head of every page loads immediately before
# `prefs.js`, and which `prefs.js` reads once and republishes on
# `window.stackroomReader`.
#
# One file, one place, read once. It is a *file* and not an inline block
# because inlining costs it again on every page, and a *script* and not a
# fetched .json because an archive opened from a folder is on `file://`, where
# `fetch()` is refused outright and `<script src>` loads - the full argument is
# in `_SCRIPT_HEADER`, which ships in the generated file itself. `prefs.js` is
# the only script loaded synchronously in the head of every template, which is
# what makes it the right reader: every deferred file already runs after it and
# already talks to it.
#
# Three things the bundle has to carry besides the strings:
#
# 1. **The plural rule**, because the search status alone says "1 page
#    contains" and "5 pages contain" and Russian needs three forms of it with
#    the same rule the Python side implements. It travels as *data* - see
#    `plural_data` - generated from `PLURAL_RULES` at build time, so there is
#    no second implementation of a plural rule to drift out of step with the
#    first. A language whose rule Python knows works in the browser the moment
#    its catalogue exists.
#
# 2. **The number format**, because `Intl.NumberFormat` disagrees with this
#    project's catalogues about two things and does it silently. Measured in
#    Chromium 141: `Intl.NumberFormat('fr').format(1234)` separates with U+202F
#    where `locales/fr.json` would say U+00A0, and `Intl.NumberFormat('pl')`
#    applies CLDR's own `minimumGroupingDigits` rather than the catalogue's, so
#    it writes `1234` where a catalogue asking for grouping from four digits
#    wants `1 234`. An unknown code does not raise either: `Intl` resolved
#    `"xx"` to `en-US` and grouped with commas. So the browser uses `Intl` for
#    *where the groups fall* and the catalogue for *which characters go there
#    and when grouping starts* - which is the half of the job `Intl` is
#    genuinely better at, and the half the catalogue is the authority on.
#
# 3. **Which keys fell back to English**, so a contributor gets one
#    `console.warn` naming them and a reader gets a sentence that reads.

JS_PREFIX = "js."
"""Keys destined for the browser. The prefix is what selects them, so adding a
string to a script is adding one key to ``en.json`` and nothing else - no
second list to keep in step, and ``check`` covers the browser's messages on
exactly the same footing as the templates'."""

JS_SHARED: frozenset[str] = frozenset(
    {
        # File sizes. offline.js reports what the browser is holding and what
        # it is offering, in the same units and the same words the rest of the
        # archive uses.
        "bytes.b", "bytes.kb", "bytes.mb", "bytes.gb", "bytes.tb",
        # Counts of things. The palette's document rows say "12 pages" in the
        # same words the browse list does.
        "count.documents", "count.pages",
        # The strip of page ticks. Two of the four state words are the
        # legend's own; the other two are shorter and have their own `js.` keys.
        "key.full", "key.void",
        # Page names and the pager's arrows, which the full-size view reuses so
        # that its steps and the page's own pager cannot disagree - including
        # about which way the arrows point in a right-to-left catalogue.
        "page.n", "page.next", "page.prev", "page.where",
        "ribbon.join",
        # The negative's tooltip recomputes a rectangle's share of its page
        # from the drawing rather than being shipped every box's measurements
        # again, so it has to say the answer in the words the page uses.
        "negative.share_none", "negative.share_tiny",
        "negative.share_small", "negative.share_large",
    }
)
"""Keys the browser needs that are not its own.

Every one of these is a string a *template* also publishes, and the reason it
travels is that a script says the same thing about the same fact somewhere else
on the page. Duplicating them under a ``js.`` key would give a translator two
places to write "Page {number}" and one of them to get wrong.

It is a list, and a list is a thing that can fall out of step - so it is short,
it lives beside the emitter, and every key in it is checked by
:func:`check_source` like any other.
"""


def plural_data(rule: str, *, limit: int = 2_000) -> dict[str, Any]:
    """The plural rule for *rule*, encoded so six lines of JavaScript can run it.

    A plural rule cannot be shipped as a table of answers - the counts are
    whatever the archive turns out to hold - and shipping it as a second
    implementation in JavaScript is how the two implementations come to
    disagree at 21. So it is shipped as data derived from the Python rule
    itself, and the derivation checks itself:

    Every cardinal rule in :data:`PLURAL_RULES` is, for whole numbers, a
    function of ``i % 100`` plus a handful of exact small values. Russian is
    purely the residue (1, 21, 101 and 1001 are all ``one`` because 1 is);
    English is not, because ``one`` is the number 1 and not 101, so 1 is an
    exception on top of a table that says ``other`` everywhere. Arabic needs
    three exceptions (0, 1, 2) over a residue table. That is the whole
    encoding::

        {"c": ["one", "few", "many", "other"],   # the categories used
         "t": "2012223333…",                     # c-index per i % 100
         "x": {"1": 0}}                          # exact values that override

    and the reader is::

        var k = X[i]; if (k === undefined) k = +T.charAt(i % 100);
        return C[k];

    Roughly 150 bytes, and periodic, so it costs almost nothing compressed.

    The encoding is *verified* here rather than assumed: every integer up to
    *limit*, and a scatter of large ones, is decoded and compared against
    :func:`plural_category`. A future rule that does not fit - one that looked
    at ``i % 1000``, say - raises rather than shipping a browser that quietly
    disagrees with the pages around it.
    """
    answers = [plural_category(rule, n) for n in range(limit + 1)]

    # The table is read off the *large* values of each residue class, so that
    # anything a small value does differently lands in the exception map rather
    # than poisoning every number that shares its last two digits.
    table: list[str] = []
    for residue in range(100):
        tail = [answers[n] for n in range(100 + residue, limit + 1, 100)]
        table.append(tail[0] if tail else answers[residue])

    exact = {n: answers[n] for n in range(limit + 1) if answers[n] != table[n % 100]}
    if exact and max(exact) >= 100:
        raise CatalogError(
            f"plural rule {rule!r} needs an exception for {max(exact)}, which is not a "
            "small value; the residue encoding in plural_data() cannot carry it and the "
            "browser would need a real rule evaluator."
        )

    def decode(n: int) -> str:
        return exact.get(n, table[n % 100])

    probes = [*range(limit + 1), 5_000, 9_999, 10_001, 12_345, 100_002, 999_999, 1_000_011]
    for n in probes:
        if decode(n) != plural_category(rule, n):
            raise CatalogError(
                f"plural rule {rule!r} does not fit the residue encoding: at {n} the "
                f"table says {decode(n)!r} and the rule says {plural_category(rule, n)!r}."
            )

    used = sorted(set(table) | set(exact.values()), key=CATEGORIES.index)
    return {
        "c": used,
        "t": "".join(str(used.index(c)) for c in table),
        "x": {str(n): used.index(c) for n, c in sorted(exact.items())},
    }


def browser_catalog(translator: Translator, *, prefix: str = JS_PREFIX) -> dict[str, Any]:
    """Everything the scripts need to write this archive's language, as a dict.

    The messages keep their full keys, ``js.`` and all, so a contributor who
    greps ``en.json`` for the sentence they saw on screen finds the same string
    in the script that writes it. The three bytes a key costs are the most
    compressible thing in the file.

    Missing keys are filled in from English *here*, at build time, rather than
    left for the browser to notice: a reader gets a sentence that reads, and
    the keys that fell back are listed in ``fell_back`` for one console warning
    a contributor will see and a reader never will.
    """
    catalog = translator.catalog
    english = translator.fallback
    messages: dict[str, Message] = {}
    fell_back: list[str] = []
    source = english.messages if english is not None else catalog.messages

    for key in sorted(source):
        if not (key.startswith(prefix) or key in JS_SHARED):
            continue
        if key in catalog.messages:
            messages[key] = catalog.messages[key]
        else:
            messages[key] = source[key]
            fell_back.append(key)

    number = catalog.number
    return {
        "locale": catalog.locale,
        "dir": catalog.direction,
        "plural": plural_data(catalog.plural),
        "number": {
            "group": number.group,
            "decimal": number.decimal,
            "min": number.minimum_grouping_digits,
            "percent": number.percent,
        },
        "messages": messages,
        "fell_back": fell_back,
    }


def browser_json(translator: Translator, *, prefix: str = JS_PREFIX) -> str:
    """:func:`browser_catalog` as the compact JSON that goes in the page."""
    return json.dumps(
        browser_catalog(translator, prefix=prefix),
        separators=(",", ":"),
        ensure_ascii=False,
    )


_SCRIPT_HEADER = """\
/* Stackroom interface messages, written by the build. Do not edit.
 *
 * Generated from locales/{locale}.json by stackroom.i18n.browser_script(). It
 * is a script rather than JSON in a <script type="application/json"> block for
 * two measured reasons.
 *
 * It is a *file* because inlining it costs it again on every page: {raw} bytes
 * of it, which on an archive at this project's supported ceiling of 20,000
 * pages is {inline} MB of duplicated HTML on a disk somebody is expected to
 * mirror, zip and carry. As one file it is fetched once, cached, and precached
 * by the service worker with everything else.
 *
 * It is a *script* and not a fetched .json because an archive read from a
 * folder on a disk is served from file://, where fetch() is refused outright -
 * "URL scheme file is not supported" - while <script src> loads. A catalogue
 * that disappeared when the archive was copied to a stick would take the whole
 * interface's language with it.
 *
 * prefs.js reads this once and republishes t(), n() and pct() on
 * window.stackroomReader. Nothing else touches it.
 */
"""


def browser_script(translator: Translator, *, prefix: str = JS_PREFIX) -> str:
    """The whole of ``assets/i18n.js``: one global, and a comment saying why.

    Written by :meth:`build.site.SiteBuilder.copy_assets` and loaded from the
    head of every page, before ``prefs.js``, which is the only script that
    reads it.
    """
    text = browser_json(translator, prefix=prefix)
    header = _SCRIPT_HEADER.format(
        locale=translator.catalog.locale,
        raw=len(text.encode("utf-8")),
        inline=round(len(text.encode("utf-8")) * 20_000 / 1_000_000),
    )
    # `</script` cannot end a src-loaded file the way it can end an inline
    # block, but the escape costs nothing and this string is one copy-and-paste
    # away from being inlined by somebody.
    safe = text.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"{header}window.stackroomMessages = {safe};\n"


def install(env: Any, translator: Translator) -> Translator:
    """Give a Jinja environment the globals and filters the templates use.

    ``t`` is both, deliberately: ``{{ t('nav.about') }}`` reads better where a
    message takes parameters and ``{{ 'nav.about'|t }}`` reads better where it
    does not, and a translator sees the same key either way.
    """
    env.globals.update(
        t=translator.t,
        n=translator.n,
        pct=translator.pct,
        dt=translator.date,
        ui_lang=translator.locale,
        ui_dir=translator.direction,
        text_direction=direction_for,
    )
    env.filters.update(t=translator.t, n=translator.n)
    return translator


# --------------------------------------------------------------------------
# checking a catalogue
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Report:
    """What is wrong with one catalogue, in the order a translator should fix it."""

    locale: str
    total: int = 0
    translated: int = 0
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    incomplete_plurals: list[tuple[str, list[str]]] = field(default_factory=list)
    wrong_placeholders: list[tuple[str, list[str], list[str]]] = field(default_factory=list)
    stray_markup: list[str] = field(default_factory=list)
    hardcoded_counts: list[tuple[str, list[str]]] = field(default_factory=list)
    """Plural forms that write a literal number where this language's category
    matches more than one. See :func:`ambiguous_forms`."""
    untranslated: list[str] = field(default_factory=list)
    """Keys whose text is byte-identical to the English. Not an error - "OK" is
    "OK" in a dozen languages - but a list worth reading once."""

    @property
    def ok(self) -> bool:
        return not (
            self.missing
            or self.extra
            or self.incomplete_plurals
            or self.wrong_placeholders
            or self.stray_markup
            or self.hardcoded_counts
        )

    @property
    def coverage(self) -> float:
        return (self.translated / self.total) if self.total else 1.0

    def lines(self) -> list[str]:
        out = [
            f"{self.locale}: {self.translated}/{self.total} messages "
            f"({self.coverage * 100:.0f}%)"
        ]
        # A scaffolded catalogue is structurally perfect and entirely English,
        # so "100%" on its own would be a lie the moment somebody reads the
        # site. Proper nouns legitimately survive translation; a hundred of
        # them do not.
        if self.untranslated and len(self.untranslated) > 8:
            out.append(
                f"  {len(self.untranslated)} message(s) are still the English "
                "text (--untranslated lists them)"
            )
        for key in self.missing:
            out.append(f"  missing            {key}")
        for key in self.extra:
            out.append(f"  not in en.json     {key}")
        for key, forms in self.incomplete_plurals:
            out.append(f"  needs plural form  {key}: {', '.join(forms)}")
        for key, want, got in self.wrong_placeholders:
            out.append(
                f"  placeholders       {key}: English has {sorted(want)}, "
                f"this has {sorted(got)}"
            )
        for key in self.stray_markup:
            out.append(
                f"  markup             {key}: contains '<' but the key does not end "
                "in _html"
            )
        for key, forms in self.hardcoded_counts:
            out.append(
                f"  hardcoded number   {key}: form(s) {', '.join(forms)} print no "
                "{count}, but this language uses that form for more than one number"
            )
        return out


def check(
    locale: str,
    *,
    locales_dir: Path | None = None,
    source: Catalog | None = None,
) -> Report:
    """Compare one catalogue against the English source.

    Everything here is a question a translator can answer without running the
    site: is anything missing, does every plural message carry every form this
    language needs, does every translation carry the same placeholders as its
    source, and is there markup where markup is not allowed.
    """
    english = source or load(DEFAULT_LOCALE, locales_dir=locales_dir)
    catalog = load(locale, locales_dir=locales_dir)
    report = Report(locale=catalog.locale, total=len(english.messages))

    for key in sorted(english.messages):
        source_message = english.messages[key]
        if key not in catalog.messages:
            report.missing.append(key)
            continue
        report.translated += 1
        message = catalog.messages[key]

        if isinstance(source_message, dict) and not isinstance(message, dict):
            report.incomplete_plurals.append((key, list(catalog.forms)))
        elif isinstance(message, dict):
            wanted = [f for f in catalog.forms if f not in message]
            if wanted:
                report.incomplete_plurals.append((key, wanted))

        want = message_placeholders(source_message)
        got = message_placeholders(message)
        if want != got:
            report.wrong_placeholders.append((key, sorted(want), sorted(got)))

        # A form this language reuses across several numbers has to print the
        # number. English "1 page" is right because English only says `one` at
        # 1; Russian "1 страница" is wrong because Russian says `one` at 21 too.
        if isinstance(message, dict) and "count" in message_placeholders(source_message):
            reused = ambiguous_forms(catalog.plural)
            blind = [
                form
                for form in catalog.forms
                if form in reused
                and form in message
                and "count" not in placeholders(message[form])
            ]
            if blind:
                report.hardcoded_counts.append((key, blind))

        forms = [message] if isinstance(message, str) else list(message.values())
        if not key.endswith("_html") and any("<" in form for form in forms):
            report.stray_markup.append(key)
        if catalog.locale != DEFAULT_LOCALE and message == source_message:
            report.untranslated.append(key)

    for key in sorted(catalog.messages):
        if key not in english.messages:
            report.extra.append(key)

    return report


def check_source(*, locales_dir: Path | None = None) -> list[str]:
    """Problems in ``en.json`` itself, which nothing else can catch.

    English is the source, so the comparison in :func:`check` has nothing to
    compare it to. These are the two invariants that still apply.
    """
    english = load(DEFAULT_LOCALE, locales_dir=locales_dir)
    problems: list[str] = []
    for key, message in sorted(english.messages.items()):
        forms = [message] if isinstance(message, str) else list(message.values())
        if not key.endswith("_html") and any("<" in form for form in forms):
            problems.append(f"{key}: contains '<' but the key does not end in _html")
        # `_html` also covers a message that carries no tags of its own but
        # frames a parameter that is markup - "…are in {link}." A key with
        # neither tags nor placeholders can never produce markup, so it is
        # simply misnamed, and the name is what tells `t()` whether to escape.
        if (
            key.endswith("_html")
            and not any("<" in form for form in forms)
            and not message_placeholders(message)
        ):
            problems.append(f"{key}: ends in _html but can never carry markup; rename it")
        if isinstance(message, dict):
            wanted = [f for f in english.forms if f not in message]
            if wanted:
                problems.append(f"{key}: missing plural form(s) {', '.join(wanted)}")
            reused = ambiguous_forms(english.plural)
            if "count" in message_placeholders(message):
                blind = [
                    form
                    for form in english.forms
                    if form in reused and form in message
                    and "count" not in placeholders(message[form])
                ]
                if blind:
                    problems.append(
                        f"{key}: form(s) {', '.join(blind)} print no {{count}} but "
                        "cover more than one number"
                    )
    for key in sorted(english.notes):
        if key not in english.messages:
            problems.append(f"notes[{key!r}]: a note for a message that does not exist")
    return problems


# --------------------------------------------------------------------------
# a command a contributor can run
# --------------------------------------------------------------------------


def _scaffold(code: str, locales_dir: Path) -> str:
    """A new catalogue, English throughout, with the plural forms stubbed out."""
    english = load(DEFAULT_LOCALE, locales_dir=locales_dir)
    rule = code if code in PLURAL_RULES else "other"
    forms = PLURAL_FORMS.get(rule, ("other",))
    messages: dict[str, Any] = {}
    for key, message in english.messages.items():
        if isinstance(message, dict):
            template = message.get("other", "")
            messages[key] = {form: template for form in forms}
        else:
            messages[key] = message
    body = {
        "locale": code,
        "name": "TODO: the name of this language, written in that language",
        "english_name": "TODO: the name of this language, in English",
        "direction": direction_for(code),
        "plural": rule,
        "number": {"group": ",", "decimal": ".", "minimum_grouping_digits": 1, "percent": "{n}%"},
        "date": {"short": "{y}-{m}-{d}"},
        "messages": messages,
    }
    return json.dumps(body, ensure_ascii=False, indent=2) + "\n"


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m stackroom.i18n",
        description="Check, inspect and scaffold Stackroom's message catalogues.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="is a catalogue complete and consistent?")
    p_check.add_argument("locale", nargs="*", help="codes to check; default is all of them")
    p_check.add_argument("--missing", action="store_true", help="print the English and the note for every missing key")
    p_check.add_argument("--untranslated", action="store_true", help="also list keys left in English")

    p_show = sub.add_parser("show", help="the English, the note and the placeholders for one key")
    p_show.add_argument("key")

    p_new = sub.add_parser("new", help="write a starting catalogue for a new language")
    p_new.add_argument("locale")

    sub.add_parser("list", help="the catalogues that ship in this package")

    args = parser.parse_args(argv)
    directory = LOCALES

    if args.command == "list":
        for code in available(locales_dir=directory):
            cat = load(code, locales_dir=directory)
            print(f"{code:5} {cat.name:16} {cat.english_name:12} {cat.direction}  "
                  f"plural={cat.plural}  {len(cat.messages)} messages")
        return 0

    if args.command == "show":
        english = load(DEFAULT_LOCALE, locales_dir=directory)
        message = english.messages.get(args.key)
        if message is None:
            print(f"{args.key}: not a message in en.json")
            return 1
        print(f"{args.key}")
        if isinstance(message, dict):
            for form in CATEGORIES:
                if form in message:
                    print(f"  [{form}] {message[form]}")
        else:
            print(f"  {message}")
        slots = sorted(message_placeholders(message))
        print(f"  placeholders: {', '.join(slots) or 'none'}")
        note = english.notes.get(args.key)
        if note:
            print(f"  note: {note}")
        for code in available(locales_dir=directory):
            if code == DEFAULT_LOCALE:
                continue
            other = load(code, locales_dir=directory).messages.get(args.key)
            print(f"  {code}: {other if other is not None else '(missing)'}")
        return 0

    if args.command == "new":
        path = directory / f"{normalize_locale(args.locale)}.json"
        if path.exists():
            print(f"{path} already exists.")
            return 1
        path.write_text(_scaffold(normalize_locale(args.locale), directory), encoding="utf-8")
        print(f"wrote {path}\nEvery message is still English. docs/TRANSLATING.md is the checklist.")
        return 0

    problems = check_source(locales_dir=directory)
    if problems:
        print("en.json (the source):")
        for line in problems:
            print(f"  {line}")
    failed = bool(problems)
    codes = args.locale or [c for c in available(locales_dir=directory) if c != DEFAULT_LOCALE]
    english = load(DEFAULT_LOCALE, locales_dir=directory)
    for code in codes:
        report = check(code, locales_dir=directory, source=english)
        for line in report.lines():
            print(line)
        if args.missing:
            for key in report.missing:
                print(f"    {key}")
                print(f"      en: {english.messages[key]}")
                note = english.notes.get(key)
                if note:
                    print(f"      note: {note}")
        if args.untranslated and report.untranslated:
            print(f"  {len(report.untranslated)} key(s) identical to the English:")
            for key in report.untranslated:
                print(f"    {key}")
        failed = failed or not report.ok
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - a command, not a library path
    raise SystemExit(_main())
