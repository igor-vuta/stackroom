"""The message catalogues, the plural rules, and the runtime that joins them.

Most of this file is a table. That is on purpose: a plural rule is a claim
about a language, and the only honest way to state it is to write down what it
says for the numbers where languages disagree with each other and with the
naive implementation. The Russian table below is the important one - it is
correct for 1, 2 and 5 under every implementation anybody writes, including the
wrong ones, and starts telling them apart at 11, 21 and 111.

The other half is about what a translator can get wrong without anybody
noticing: a missing plural form, a mistyped ``{cout}``, a ``<span>`` in a
message that will be escaped and published as visible angle brackets. Every one
of those reaches a reader in a language the person who built the site probably
cannot read, so each of them is a test.
"""

# ruff: noqa: RUF001, RUF003
# A test file for translations is mostly Cyrillic, and the number-format
# assertions hold real non-breaking spaces because that is the separator being
# asserted. Every ambiguous character below, in code and in comments alike, is
# the thing under test.

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jinja2 import Environment
from markupsafe import Markup

from stackroom import i18n
from stackroom.i18n import (
    DEFAULT_LOCALE,
    Catalog,
    CatalogError,
    MissingMessage,
    NumberFormat,
    Translator,
    available,
    check,
    check_source,
    direction_for,
    load,
    normalize_locale,
    plural_category,
    translator_for,
)

SHIPPED = available()


# --------------------------------------------------------------------------
# plural rules
# --------------------------------------------------------------------------

# The numbers where implementations diverge, plus the ones where they do not so
# that a rule that is wrong *everywhere* is also caught.
RANGE = [0, 1, 2, 3, 4, 5, 11, 12, 14, 15, 20, 21, 22, 25, 100, 101, 102, 111, 112, 1000, 1001, 1002]

# One row per language, in the order of RANGE.
EXPECTED: dict[str, list[str]] = {
    #        0       1      2      3      4      5      11     12     14     15     20     21     22     25     100    101    102    111    112    1000   1001   1002
    "en": ["other", "one", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other"],
    "ru": ["many", "one", "few", "few", "few", "many", "many", "many", "many", "many", "many", "one", "few", "many", "many", "one", "few", "many", "many", "many", "one", "few"],
    "uk": ["many", "one", "few", "few", "few", "many", "many", "many", "many", "many", "many", "one", "few", "many", "many", "one", "few", "many", "many", "many", "one", "few"],
    "pl": ["many", "one", "few", "few", "few", "many", "many", "many", "many", "many", "many", "many", "few", "many", "many", "many", "few", "many", "many", "many", "many", "few"],
    "ar": ["zero", "one", "two", "few", "few", "few", "many", "many", "many", "many", "many", "many", "many", "many", "other", "other", "other", "many", "many", "other", "other", "other"],
    "he": ["other", "one", "two", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other"],
    "fr": ["one", "one", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other"],
    "pt": ["one", "one", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other"],
    "es": ["other", "one", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other"],
    "de": ["other", "one", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other", "other"],
    "zh": ["other"] * 22,
}


@pytest.mark.parametrize("language", sorted(EXPECTED))
def test_the_plural_rule_for_a_language_matches_cldr_across_the_range(language: str) -> None:
    got = [plural_category(language, n) for n in RANGE]
    assert got == EXPECTED[language], dict(zip(RANGE, got, strict=True))


@pytest.mark.parametrize("n", [11, 12, 13, 14, 111, 112, 113, 114, 211, 1011])
def test_the_east_slavic_teens_are_not_singular(n: int) -> None:
    """The specific way a naive Russian rule is wrong.

    "ends in 1, use the singular" gets 1, 21, 31, 101 right and 11, 111, 211
    wrong, which is why it survives being tested with small numbers. Every
    number here ends in 1, 2, 3 or 4 and every one of them is ``many``.
    """
    assert plural_category("ru", n) == "many"
    assert plural_category("uk", n) == "many"


def test_polish_is_not_russian() -> None:
    """The two rules have the same shape and disagree at every multiple of ten plus one.

    Russian says "21 страница" (singular); Polish says "21 stron" (the many
    form). Copying one rule across to the other language is the mistake this
    test exists to catch.
    """
    assert plural_category("ru", 21) == "one"
    assert plural_category("pl", 21) == "many"
    assert plural_category("ru", 101) == "one"
    assert plural_category("pl", 101) == "many"
    # And where they agree, they agree.
    assert plural_category("ru", 22) == plural_category("pl", 22) == "few"


def test_arabic_has_a_dual_and_a_zero() -> None:
    assert plural_category("ar", 0) == "zero"
    assert plural_category("ar", 2) == "two"
    assert plural_category("ar", 3) == "few"


def test_fractions_take_the_form_the_language_reserves_for_them() -> None:
    # English distinguishes by notation, not by value: "1.0 pages".
    assert plural_category("en", 1) == "one"
    assert plural_category("en", 1.0) == "other"
    # Spanish distinguishes by value: "1,0 página".
    assert plural_category("es", 1.0) == "one"
    # Russian sends every fraction to `other`, which is why every Russian
    # catalogue has to carry an `other` form it will never select from a page.
    assert plural_category("ru", 1.5) == "other"
    assert plural_category("ru", 5.0) == "other"


def test_zero_and_one_are_both_singular_in_french() -> None:
    assert plural_category("fr", 0) == "one"
    assert plural_category("fr", 0.5) == "one"


def test_an_unknown_rule_is_an_error_rather_than_a_guess() -> None:
    with pytest.raises(CatalogError, match=r"unknown plural rule"):
        plural_category("klingon", 1)


def test_a_bool_is_not_a_count() -> None:
    with pytest.raises(TypeError):
        plural_category("en", True)


# --------------------------------------------------------------------------
# numbers, percentages, dates, sizes
# --------------------------------------------------------------------------


def test_grouping_and_separators_follow_the_locale() -> None:
    en = NumberFormat()
    assert en.format(999) == "999"
    assert en.format(1000) == "1,000"
    assert en.format(1234567) == "1,234,567"
    assert en.format(-1234) == "-1,234"

    ru = NumberFormat(group=" ", decimal=",")
    assert ru.format(1234567) == "1 234 567"
    assert ru.format(12.5, digits=1) == "12,5"


def test_minimum_grouping_digits_is_honoured() -> None:
    """Spanish groups from five digits, not four: 1000, but 10.000.

    CLDR calls it ``minimumGroupingDigits``. Without it a Spanish site writes
    ``1.000``, which is not wrong enough to look like a bug and is wrong enough
    to look like it was translated by someone who does not read the language.
    """
    es = NumberFormat(group=".", decimal=",", minimum_grouping_digits=2)
    assert es.format(1000) == "1000"
    assert es.format(9999) == "9999"
    assert es.format(10000) == "10.000"


def test_a_number_substituted_into_a_message_is_written_for_the_locale() -> None:
    ru = translator_for("ru")
    assert ru.t("count.pages", count=1234) == "1 234 страницы"
    en = translator_for("en")
    assert en.t("count.pages", count=1234) == "1,234 pages"


def test_percentages_carry_the_locales_own_spacing() -> None:
    assert translator_for("en").pct(6) == "6%"
    assert translator_for("ru").pct(6) == "6 %"
    assert translator_for("en").pct(0.065, of_one=True, digits=1) == "6.5%"
    assert translator_for("ru").pct(0.065, of_one=True, digits=1) == "6,5 %"


def test_dates_are_reordered_and_english_is_left_exactly_as_it_was() -> None:
    assert translator_for("en").date("2026-08-31T12:00:00Z") == "2026-08-31"
    assert translator_for("ru").date("2026-08-31") == "31.08.2026"
    assert translator_for("uk").date("2026-08-31") == "31.08.2026"
    # Anything that is not an ISO date comes back untouched rather than mangled.
    assert translator_for("ru").date("") == ""
    assert translator_for("ru").date("not a date") == "not a date"


def test_file_sizes_match_the_builders_own_formatting_in_english() -> None:
    """`Translator.bytes` is a drop-in for ``build.site.human_bytes``.

    If these ever disagree, wiring the translator in would silently change every
    file size on every English site.
    """
    from stackroom.build.site import human_bytes

    en = translator_for("en")
    for n in (0, 1, 512, 1023, 1024, 1536, 204_800, 1_234_567, 5_000_000_000):
        assert en.bytes(n) == human_bytes(n), n


def test_file_sizes_use_the_locales_alphabet_and_decimal_mark() -> None:
    ru = translator_for("ru")
    assert ru.bytes(512) == "512 Б"
    assert ru.bytes(1536) == "1,5 КБ"
    assert ru.bytes(204_800) == "200 КБ"


# --------------------------------------------------------------------------
# lookup, fallback, strictness
# --------------------------------------------------------------------------


def _catalog(messages: dict, **kwargs) -> Catalog:
    return Catalog(messages=messages, **kwargs)


def test_a_missing_key_falls_back_to_english_and_says_so() -> None:
    english = load(DEFAULT_LOCALE)
    partial = _catalog({"nav.about": "Об архиве"}, locale="ru", plural="ru")
    t = Translator(partial, english)

    assert t.t("nav.about") == "Об архиве"
    assert t.t("nav.search") == "Search"
    assert t.fell_back == {"nav.search"}
    assert "published in English" in " ".join(t.report())


def test_strict_mode_refuses_instead_of_falling_back() -> None:
    english = load(DEFAULT_LOCALE)
    partial = _catalog({"nav.about": "Об архиве"}, locale="ru", plural="ru")
    t = Translator(partial, english, strict=True)
    with pytest.raises(MissingMessage, match=r"nav\.search"):
        t.t("nav.search")


def test_a_key_in_no_catalogue_publishes_itself_rather_than_a_blank() -> None:
    t = Translator(_catalog({}, locale="ru", plural="ru"), load(DEFAULT_LOCALE))
    assert t.t("nav.nonexistent") == "[nav.nonexistent]"
    assert t.unknown == {"nav.nonexistent"}
    assert "no catalogue" in " ".join(t.report())


def test_a_language_with_no_catalogue_gets_english_rather_than_a_failed_build() -> None:
    t = translator_for("cy")  # Welsh: a real language, no catalogue here
    assert t.locale == "en"
    assert t.t("nav.about") == "About"
    assert t.missing_catalog == "cy"  # and it is recorded
    # Not as a missing key: nothing fell back, because the catalogue in hand is
    # English and has every key in it.
    assert not t.fell_back


def test_a_language_with_no_catalogue_is_reported_as_that_and_not_as_missing_keys() -> None:
    """The operator wrote `language = "de"`, and the fact is that there is no
    German catalogue - not that some number of German strings are missing from
    a German catalogue that does not exist. The message has to name the real
    problem, say the site still built, and say what to do about it."""
    warning = " ".join(translator_for("de").report())
    assert "no catalogue for 'de'" in warning
    assert "English" in warning
    assert "stackroom.i18n new de" in warning
    # The other message, which is about a catalogue that exists and is partial,
    # must not be the one that fires.
    assert "missing from the" not in warning


def test_a_partial_catalogue_is_still_reported_as_missing_keys() -> None:
    t = Translator(_catalog({"nav.about": "Об архиве"}, locale="ru", plural="ru"), load(DEFAULT_LOCALE))
    t.t("nav.search")
    warning = " ".join(t.report())
    assert "missing from the 'ru' catalogue" in warning
    assert "no catalogue for" not in warning


def test_a_plural_message_asked_for_without_a_count_is_a_bug_not_a_guess() -> None:
    with pytest.raises(CatalogError, match="count"):
        translator_for("en").t("count.pages")


def test_the_plural_form_is_chosen_by_the_catalogues_own_rule() -> None:
    ru = translator_for("ru")
    assert ru.t("count.pages", count=1) == "1 страница"
    assert ru.t("count.pages", count=2) == "2 страницы"  # genitive singular

    assert ru.t("count.pages", count=5) == "5 страниц"
    assert ru.t("count.pages", count=21) == "21 страница"
    assert ru.t("count.pages", count=111) == "111 страниц"

    uk = translator_for("uk")
    assert uk.t("count.documents", count=1) == "1 документ"
    assert uk.t("count.documents", count=2) == "2 документи"
    assert uk.t("count.documents", count=5) == "5 документів"


def test_ukrainian_and_russian_differ_where_the_grammar_differs() -> None:
    """Same plural rule, different words in the ``few`` slot.

    Russian's 2-4 form is a genitive singular, Ukrainian's is a nominative
    plural, so "2 документа" and "2 документи" are both right and neither is a
    typo for the other. A catalogue produced by transliterating the Russian one
    would fail here.
    """
    assert translator_for("ru").t("count.documents", count=2) == "2 документа"
    assert translator_for("uk").t("count.documents", count=2) == "2 документи"


# --------------------------------------------------------------------------
# markup, escaping, placeholders
# --------------------------------------------------------------------------


def test_an_html_message_returns_markup_and_escapes_its_parameters() -> None:
    t = translator_for("en")
    out = t.t("doc.numbered_html", prefix='OCA-<b>"2018"-')
    assert isinstance(out, Markup)
    assert '<span class="mono">' in out
    assert "&lt;b&gt;" in out and "&#34;" in out


def test_a_plain_message_is_a_plain_string_for_jinja_to_escape() -> None:
    out = translator_for("en").t("nav.about")
    assert not isinstance(out, Markup)
    assert out == "About"


def test_a_parameter_that_is_already_markup_is_left_alone() -> None:
    t = translator_for("en")
    link = Markup('<a href="x">the document list</a>')
    out = t.t("search.noscript_html", count=2, link=link)
    assert '<a href="x">' in out


def test_doubled_braces_are_a_literal_brace() -> None:
    t = Translator(_catalog({"x": "{{literal}} {real}"}), load(DEFAULT_LOCALE))
    assert t.t("x", real="value") == "{literal} value"


def test_a_placeholder_with_no_argument_stays_visible_and_is_loud_in_strict_mode() -> None:
    t = Translator(_catalog({"x": "hello {name}"}), load(DEFAULT_LOCALE))
    assert t.t("x") == "hello {name}"
    strict = Translator(_catalog({"x": "hello {name}"}), load(DEFAULT_LOCALE), strict=True)
    with pytest.raises(CatalogError, match="name"):
        strict.t("x")


# --------------------------------------------------------------------------
# the shipped catalogues
# --------------------------------------------------------------------------


def test_english_ships_and_is_the_source() -> None:
    assert DEFAULT_LOCALE in SHIPPED
    assert SHIPPED[0] == DEFAULT_LOCALE


def test_the_languages_this_change_promised_are_shipped() -> None:
    assert {"en", "ru", "uk"} <= set(SHIPPED)


def test_the_english_catalogue_is_internally_consistent() -> None:
    assert check_source() == []


@pytest.mark.parametrize("code", [c for c in SHIPPED if c != DEFAULT_LOCALE])
def test_a_shipped_catalogue_is_complete(code: str) -> None:
    report = check(code)
    assert report.ok, "\n".join(report.lines())
    assert report.coverage == 1.0


@pytest.mark.parametrize("code", SHIPPED)
def test_every_plural_message_carries_every_form_the_language_needs(code: str) -> None:
    catalog = load(code)
    for key, message in catalog.messages.items():
        if isinstance(message, dict):
            missing = [f for f in catalog.forms if f not in message]
            assert not missing, f"{code}: {key} is missing {missing}"


@pytest.mark.parametrize("code", SHIPPED)
def test_no_message_is_left_with_an_unsubstituted_placeholder(code: str) -> None:
    """Render every message in every catalogue and look for a stray ``{…}``.

    The parameters come from the *English* source, because that is the contract
    the templates call under: a translation that invents ``{pages}`` where the
    source has ``{count}`` publishes the literal text ``{pages}`` to a reader.
    """
    english = load(DEFAULT_LOCALE)
    catalog = load(code)
    t = Translator(catalog, english)
    leftover = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

    for key, source in english.messages.items():
        slots = i18n.message_placeholders(source)
        params = {name: f"<{name}>" for name in slots}
        counts = [1, 2, 5, 11, 21, 100, 111] if isinstance(catalog.messages.get(key, source), dict) else [1]
        for n in counts:
            if "count" in slots or isinstance(catalog.messages.get(key, source), dict):
                params["count"] = n
            rendered = str(t.t(key, **params))
            assert not leftover.search(rendered), f"{code}: {key} -> {rendered}"
    assert not t.fell_back and not t.unknown


@pytest.mark.parametrize("code", SHIPPED)
def test_markup_only_appears_in_keys_that_declare_it(code: str) -> None:
    """A ``<`` in a message that is not an ``_html`` key is published as ``&lt;``.

    Silent when it happens: the page renders, and the tag the translator meant
    appears as visible angle brackets in the middle of a sentence.
    """
    for key, message in load(code).messages.items():
        forms = [message] if isinstance(message, str) else list(message.values())
        if any("<" in form for form in forms):
            assert key.endswith("_html"), f"{code}: {key} carries markup"


@pytest.mark.parametrize("code", SHIPPED)
def test_an_html_message_keeps_the_tags_its_english_source_has(code: str) -> None:
    """Not a full parse: a count of opening tags, which is what goes missing."""
    english = load(DEFAULT_LOCALE)
    catalog = load(code)
    tag = re.compile(r"<([a-z]+)")
    for key, source in english.messages.items():
        if not key.endswith("_html") or key not in catalog.messages:
            continue
        want = sorted(tag.findall(source if isinstance(source, str) else source["other"]))
        got_message = catalog.messages[key]
        got = sorted(tag.findall(got_message if isinstance(got_message, str) else got_message["other"]))
        assert want == got, f"{code}: {key} has tags {got}, English has {want}"


@pytest.mark.parametrize("code", SHIPPED)
def test_a_catalogue_declares_a_direction_and_a_name_in_its_own_language(code: str) -> None:
    catalog = load(code)
    assert catalog.direction in ("ltr", "rtl")
    assert catalog.name and not catalog.name.startswith("TODO")
    assert catalog.english_name


# A message may come out of translation unchanged for exactly two reasons, and
# both of them are checkable. Anything else identical to the English is a line
# somebody skipped, and a budget of "a handful" stopped meaning anything once
# the catalogue passed three hundred messages.
_INVARIANT = frozenset({
    # File names, algorithm names and format names, which are the same string
    # in every language that uses them at all.
    "manifest.json", "SHA-256", "Markdown",
    # Unit symbols. Some languages transliterate them - Russian writes КБ - and
    # some do not; both are the translator's call and neither is an oversight.
    "B", "KB", "MB", "GB", "TB",
    # Two letters of the alphabet as a sign for "type". Latin-script languages
    # keep it; Cyrillic ones write Аа.
    "Aa",
})

_PLACEHOLDER_OR_TAG = re.compile(r"\{[^{}]*\}|<[^<>]*>")


def _may_survive(message: object) -> bool:
    """True when a message identical to the English is legitimately so."""
    forms = [message] if isinstance(message, str) else list(message.values())  # type: ignore[union-attr]
    for form in forms:
        bare = _PLACEHOLDER_OR_TAG.sub("", form).strip()
        # Nothing but punctuation and placeholders: a frame, not a sentence.
        if not any(ch.isalpha() for ch in bare):
            continue
        words = [w.strip("`;:,.()[]\u2014\u2013 ") for w in bare.split()]
        if all(not w or w in _INVARIANT for w in words):
            continue
        return False
    return True


@pytest.mark.parametrize("code", [c for c in SHIPPED if c != DEFAULT_LOCALE])
def test_a_translation_is_actually_a_translation(code: str) -> None:
    """Every message left in English is one that could not have been anything else.

    Proper nouns, file names and unit symbols legitimately survive translation,
    and so does a message that is nothing but punctuation around its
    placeholders - "{page} · {site}" is the same string in every language. A
    catalogue that is 90% English is a scaffold somebody forgot to finish, and
    this is the difference stated as a rule rather than as a budget.
    """
    english = load(DEFAULT_LOCALE)
    unexplained = [
        key for key in check(code).untranslated
        if not _may_survive(english.messages[key])
    ]
    assert unexplained == []


# --------------------------------------------------------------------------
# catalogue loading refuses the things that are silent when wrong
# --------------------------------------------------------------------------


def _write(tmp_path: Path, code: str, body: dict) -> Path:
    (tmp_path / f"{code}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_a_plural_message_with_no_other_form_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "xx", {"plural": "en", "messages": {"k": {"one": "a"}}})
    with pytest.raises(CatalogError, match="no 'other' form"):
        load("xx", locales_dir=tmp_path)


def test_a_form_that_is_not_a_cldr_category_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "xx", {"plural": "en", "messages": {"k": {"singular": "a", "other": "b"}}})
    with pytest.raises(CatalogError, match=r"CLDR categories"):
        load("xx", locales_dir=tmp_path)


def test_an_unknown_plural_rule_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "xx", {"plural": "nope", "messages": {}})
    with pytest.raises(CatalogError, match="names no rule"):
        load("xx", locales_dir=tmp_path)


def test_a_percent_pattern_without_the_number_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "xx", {"plural": "en", "number": {"percent": "%"}, "messages": {}})
    with pytest.raises(CatalogError, match="percent"):
        load("xx", locales_dir=tmp_path)


def test_a_missing_catalogue_names_the_ones_that_exist(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="TRANSLATING"):
        load("xx", locales_dir=tmp_path)


def test_the_checker_finds_a_mistyped_placeholder(tmp_path: Path) -> None:
    english = load(DEFAULT_LOCALE)
    body = {
        "locale": "xx",
        "plural": "en",
        "messages": {k: (v if isinstance(v, str) else dict(v)) for k, v in english.messages.items()},
    }
    body["messages"]["page.n"] = "Seite {nummer}"
    _write(tmp_path, "xx", body)
    report = check("xx", locales_dir=tmp_path, source=english)
    assert not report.ok
    assert any(key == "page.n" for key, _, _ in report.wrong_placeholders)


def test_the_checker_finds_a_missing_plural_form(tmp_path: Path) -> None:
    english = load(DEFAULT_LOCALE)
    body = {
        "locale": "xx",
        "plural": "ru",
        "messages": {k: (v if isinstance(v, str) else dict(v)) for k, v in english.messages.items()},
    }
    _write(tmp_path, "xx", body)
    report = check("xx", locales_dir=tmp_path, source=english)
    assert not report.ok
    missing = dict((key, forms) for key, forms in report.incomplete_plurals)
    assert "count.pages" in missing
    assert set(missing["count.pages"]) == {"few", "many"}


# --------------------------------------------------------------------------
# locales, scripts and direction
# --------------------------------------------------------------------------


def test_a_locale_code_is_normalised_from_whatever_the_operator_wrote() -> None:
    assert normalize_locale("ru") == "ru"
    assert normalize_locale("rus") == "ru"      # Tesseract / ISO 639-2
    assert normalize_locale("ru_RU") == "ru"
    assert normalize_locale("uk-UA") == "uk"
    assert normalize_locale("") == "en"
    assert normalize_locale(None) == "en"


def test_a_language_with_no_stopword_list_still_normalises() -> None:
    """The reason this function is not ``lang.normalize_language_codes``.

    That one drops a code it has no word list for, which is right for OCR
    quality and would silently turn an Arabic interface into an English one.
    """
    from stackroom.lang import normalize_language_codes

    assert normalize_language_codes(["ar"]) == []
    assert normalize_locale("ar") == "ar"
    assert normalize_locale("he_IL") == "he"


def test_direction_is_known_for_the_scripts_that_need_it() -> None:
    assert direction_for("en") == "ltr"
    assert direction_for("ru") == "ltr"
    assert direction_for("ar") == "rtl"
    assert direction_for("he") == "rtl"
    assert direction_for("fa") == "rtl"
    assert direction_for("ur") == "rtl"
    assert direction_for(None) == "ltr"


# --------------------------------------------------------------------------
# the Jinja seam
# --------------------------------------------------------------------------


def test_a_template_can_use_t_as_a_global_and_as_a_filter() -> None:
    env = Environment(autoescape=True)
    i18n.install(env, translator_for("ru"))
    assert env.from_string("{{ t('nav.about') }}").render() == "Об архиве"
    assert env.from_string("{{ 'nav.about'|t }}").render() == "Об архиве"
    assert env.from_string("{{ t('count.pages', count=5) }}").render() == "5 страниц"
    assert env.from_string("{{ n(1234) }}").render() == "1 234"
    assert env.from_string("{{ ui_lang }}/{{ ui_dir }}").render() == "ru/ltr"


def test_a_plain_message_is_escaped_by_jinja_and_an_html_one_is_not() -> None:
    env = Environment(autoescape=True)
    i18n.install(env, translator_for("en"))
    out = env.from_string("{{ t('doc.numbered_html', prefix='A&B') }}").render()
    assert '<span class="mono">' in out and "A&amp;B" in out
    plain = env.from_string("{{ t('page.where', title='A&B', number=1, total=2) }}").render()
    assert "A&amp;B" in plain and "<" not in plain


# --------------------------------------------------------------------------
# the English output of the site must not change
# --------------------------------------------------------------------------


def test_the_catalogue_reproduces_the_builders_english_ribbon_label_exactly() -> None:
    """The reading of the strip of ticks, assembled from the catalogue.

    ``build.site._ribbon_label`` builds this sentence in Python today. Wiring it
    to the catalogue must not change one byte of the English, so this composes
    it the way the wiring will and compares the two.
    """
    from stackroom.build.site import _ribbon_label, page_state
    from stackroom.model import Box, OcrQuality, Page, PageVerdict, Redaction, RedactionKind

    def redacted(number: int, ratio: float) -> Page:
        return Page(
            number=number,
            redaction_ratio=ratio,
            redactions=[Redaction(box=Box(0.1, 0.1, 0.5, 0.1), kind=RedactionKind.RASTER)],
        )

    pages = [Page(number=1), Page(number=2)]
    pages.append(redacted(3, 0.3))
    pages.append(redacted(4, 0.95))
    pages.append(Page(number=5, quality=OcrQuality(verdict=PageVerdict.UNREADABLE)))
    pages.append(Page(number=6, quality=OcrQuality(verdict=PageVerdict.BLANK)))

    t = translator_for("en")
    counts: dict[str, int] = {}
    for page in pages:
        state = page_state(page)
        counts[state] = counts.get(state, 0) + 1
    parts = [t.t("count.pages", count=len(pages))]
    for state, key in (("part", "ribbon.part"), ("full", "ribbon.full"),
                       ("dark", "ribbon.dark"), ("void", "ribbon.void")):
        if counts.get(state):
            parts.append(t.t(key, count=counts[state]))
    composed = t.t("ribbon.end", list=t.t("ribbon.join").join(parts))

    assert composed == _ribbon_label(pages)


def test_the_russian_ribbon_label_reads_as_russian() -> None:
    t = translator_for("ru")
    parts = [t.t("count.pages", count=16), t.t("ribbon.part", count=4), t.t("ribbon.full", count=1)]
    assert t.t("ribbon.end", list=t.t("ribbon.join").join(parts)) == (
        "16 страниц, 4 изъяты частично, 1 изъята полностью."
    )


# --------------------------------------------------------------------------
# the check that only matters for a language whose `one` is not just 1
# --------------------------------------------------------------------------


def test_which_plural_forms_have_to_print_their_number() -> None:
    """English may hardcode "1 page"; Russian may not.

    ``one`` in English is exactly the number 1. ``one`` in Russian is 1, 21,
    31, 101, 1001 - so a Russian ``one`` form that writes a literal 1 publishes
    "1 страница" on a document that has twenty-one of them, and no other check
    in this file would notice.
    """
    assert i18n.ambiguous_forms("en") == {"other"}
    assert i18n.ambiguous_forms("ru") == {"one", "few", "many"}
    assert i18n.ambiguous_forms("uk") == {"one", "few", "many"}
    assert i18n.ambiguous_forms("pl") == {"few", "many"}
    # French `one` covers 0 as well as 1, so it too has to print the number.
    assert "one" in i18n.ambiguous_forms("fr")
    # Arabic's zero, one and two each match exactly one value.
    assert i18n.ambiguous_forms("ar") == {"few", "many", "other"}


def test_the_checker_catches_a_hardcoded_one_in_an_east_slavic_catalogue(tmp_path: Path) -> None:
    english = load(DEFAULT_LOCALE)
    body = {
        "locale": "xx",
        "plural": "ru",
        "messages": {
            k: (v if isinstance(v, str) else {f: v.get("other", "") for f in ("one", "few", "many", "other")})
            for k, v in english.messages.items()
        },
    }
    # The mistake: the English `one` form, copied across unchanged.
    body["messages"]["count.pages"]["one"] = "1 страница"
    (tmp_path / "xx.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    report = check("xx", locales_dir=tmp_path, source=english)
    assert not report.ok
    assert ("count.pages", ["one"]) in report.hardcoded_counts


@pytest.mark.parametrize("code", SHIPPED)
def test_no_shipped_catalogue_hardcodes_a_count(code: str) -> None:
    if code == DEFAULT_LOCALE:
        assert check_source() == []
        return
    assert check(code).hardcoded_counts == []


def test_the_russian_one_form_survives_twenty_one() -> None:
    """The end-to-end version of the rule above, on the shipped catalogue."""
    ru = translator_for("ru")
    assert ru.t("page.redactions", count=1) == "1 изъятие"
    assert ru.t("page.redactions", count=21) == "21 изъятие"
    assert ru.t("page.redactions", count=11) == "11 изъятий"
    assert ru.t("count.documents", count=101) == "101 документ"


# --------------------------------------------------------------------------
# the negative's own prose
# --------------------------------------------------------------------------
#
# `build/negative.py` composes about thirty sentences in Python that no
# template ever sees: the three arrangement names, their captions, the SVG
# labels, and the seven "what this picture cannot show" entries. It used to
# carry its own English-only `_plural()`; these are the tests that it does not
# any more.


def _negative_collection():
    """A small release with something withheld on it, built by hand.

    Deliberately not a fixture and deliberately tiny: what is under test is
    which words come out, not the geometry, which `test_negative.py` owns.
    """
    from stackroom.model import (
        Box,
        Collection,
        CollectionStats,
        Document,
        Page,
        Redaction,
        RedactionKind,
    )

    def box(x, y, w, h, *codes):
        return Redaction(box=Box(x, y, w, h), kind=RedactionKind.VECTOR, codes=list(codes))

    pages = [
        Page(number=1, redactions=[box(0.1, 0.1, 0.6, 0.3, "b(5)"), box(0.1, 0.5, 0.2, 0.02)],
             redaction_ratio=0.4, exemptions=["b(5)"]),
        Page(number=2, redactions=[box(0.05, 0.05, 0.9, 0.9, "b(6)")], redaction_ratio=0.95,
             exemptions=["b(6)"]),
    ]
    doc = Document(id="memo", title="Memorandum", filename="memo.pdf", sha256="a" * 64,
                   size_bytes=1024, pages=pages)
    stats = CollectionStats(documents=1, pages=2, pages_with_redactions=2, redaction_boxes=3)
    return Collection(documents=[doc], stats=stats, title="Papers of the Commission")


def test_the_negative_defaults_to_english_so_existing_callers_keep_working() -> None:
    """`t` is keyword-only and optional; without it nothing changed."""
    from stackroom.build import negative as negative_mod

    context = negative_mod.page_context(_negative_collection())
    assert context["page_title"] == "The negative"
    names = [f.name for f in context["fields"]]
    assert names[:1] == ["In page order"]
    assert "By exemption" in names and "By size" in names
    headings = [spot["heading"] for spot in context["blind_spots"]]
    assert "Redactions that leave no mark" in headings


def test_the_negative_says_everything_in_the_language_it_was_given() -> None:
    """Every sentence this module composes, in Russian, with no English left.

    The check is on the *page*, not on the keys: a string that still reads as
    Latin prose is a string somebody forgot to move into the catalogue, and
    that is exactly the failure this test exists to catch.
    """
    from stackroom.build import negative as negative_mod

    context = negative_mod.page_context(_negative_collection(), t=translator_for("ru"))
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    latin_words = re.compile(r"\b(?:page|redaction|exemption|withheld|the|and)\b", re.I)

    # The collection's title is a fact about the archive, not interface text:
    # a Russian-language archive of English-titled documents keeps the titles.
    title = _negative_collection().title
    said = [context["page_title"], context["page_description"].replace(title, "…")]
    for field in context["fields"]:
        said += [field.name, field.caption, field.label]
    for spot in context["blind_spots"]:
        said += [spot["heading"], spot["body"]]
    for row in context["code_rows"]:
        said += [row["median_text"], row["total_text"]]
        # A coded row's label is the statutory gloss from ingest/exemptions.py,
        # which stays in English deliberately - see docs/TRANSLATING.md. The
        # uncoded row's label is this module's own sentence and does not.
        if not row["code"]:
            said.append(row["label"])

    for sentence in said:
        if not any(ch.isalpha() for ch in sentence):
            continue   # a bare percentage; the catalogue decides its separators
        assert cyrillic.search(sentence), f"still English: {sentence!r}"
        assert not latin_words.search(sentence), f"English word left in: {sentence!r}"


def test_the_negatives_russian_counts_survive_twenty_one() -> None:
    """The `_plural()` this module used to carry could not do this at all.

    A Russian ``one`` form covers 21, so every count the negative prints has to
    print its number - the trap `ambiguous_forms` exists for, reached through
    the module that used to be the worst offender.
    """
    ru = translator_for("ru")
    assert "21" in str(ru.t("negative.label_page_dropped", count=21))
    assert "21" in str(ru.t("negative.blind_gaps_pages", count=21))
    assert str(ru.t("negative.label_code_group", code="b(5)", count=21)).startswith("b(5), 21 ")
    # 21 takes the singular form and 11 does not, in the same message.
    one = str(ru.t("negative.blind_odd_body", count=21))
    many = str(ru.t("negative.blind_odd_body", count=11))
    assert one != many


def test_a_share_of_a_page_is_said_the_same_way_by_python_and_by_the_script() -> None:
    """`negative.share_*` is read by both, which is why it is in JS_SHARED.

    The tooltip in `assets/js/negative.js` recomputes a rectangle's share from
    the drawing rather than being shipped every box's measurements a second
    time, so the only thing keeping the two from disagreeing is that they name
    the same four keys.
    """
    bundle = i18n.browser_catalog(translator_for("ru"))
    for key in ("negative.share_none", "negative.share_tiny",
                "negative.share_small", "negative.share_large"):
        assert key in bundle["messages"], key


# --------------------------------------------------------------------------
# the plural rule, as data a browser can run
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rule", sorted(i18n.PLURAL_RULES))
def test_the_plural_table_reproduces_its_rule_for_every_whole_number(rule: str) -> None:
    """The encoding is checked here as well as inside `plural_data`.

    `plural_data` verifies itself and raises, which means a broken rule fails
    the build rather than the test suite - so this is the same claim asserted
    from outside, where a reader of the tests can see what is being promised:
    a hundred-entry table indexed by ``i % 100``, plus a handful of exact small
    values, is the whole of every cardinal rule this project implements.
    """
    data = i18n.plural_data(rule)

    def decode(n: int) -> str:
        index = data["x"].get(str(n))
        if index is None:
            index = int(data["t"][n % 100])
        return data["c"][index]

    for n in [*range(0, 300), 999, 1000, 1001, 1011, 1111, 10_000, 100_001, 1_000_002]:
        assert decode(n) == plural_category(rule, n), f"{rule} at {n}"


def test_the_plural_table_refuses_a_rule_it_cannot_carry() -> None:
    """A rule that looked at three digits would need a real evaluator.

    None does today. If one ever did, the honest failure is a build that stops
    rather than a browser that quietly disagrees with the page around it.
    """
    i18n.PLURAL_RULES["_thousands"] = lambda i, v, n: "one" if i % 1000 == 7 else "other"
    try:
        with pytest.raises(CatalogError, match=r"not a small value|does not fit"):
            i18n.plural_data("_thousands")
    finally:
        del i18n.PLURAL_RULES["_thousands"]


def test_the_plural_table_is_small_enough_to_ship_on_every_page() -> None:
    for rule in i18n.PLURAL_RULES:
        encoded = json.dumps(i18n.plural_data(rule), separators=(",", ":"))
        assert len(encoded) < 400, f"{rule} encodes to {len(encoded)} bytes"


# --------------------------------------------------------------------------
# the browser bundle
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", SHIPPED)
def test_the_browser_bundle_has_the_shape_the_scripts_read(code: str) -> None:
    bundle = i18n.browser_catalog(translator_for(code))
    assert set(bundle) == {"locale", "dir", "plural", "number", "messages", "fell_back"}
    assert bundle["locale"] == code
    assert bundle["dir"] in ("ltr", "rtl")
    assert set(bundle["plural"]) == {"c", "t", "x"}
    assert len(bundle["plural"]["t"]) == 100
    assert set(bundle["number"]) == {"group", "decimal", "min", "percent"}
    assert "{n}" in bundle["number"]["percent"]
    assert bundle["messages"], "a bundle with no messages is a bundle nobody needs"


def test_the_bundle_carries_every_js_key_and_the_shared_ones_and_nothing_else() -> None:
    """What travels is decided by the prefix, plus one short, named exception.

    Adding a string to a script is adding one key to `en.json` - there is no
    second list of what the browser needs, which is the whole point of the
    prefix. `JS_SHARED` is the exception, and it is here so that it cannot grow
    without somebody looking at it.
    """
    english = load(DEFAULT_LOCALE)
    bundle = i18n.browser_catalog(translator_for(DEFAULT_LOCALE))
    prefixed = {k for k in english.messages if k.startswith(i18n.JS_PREFIX)}
    assert set(bundle["messages"]) == prefixed | i18n.JS_SHARED
    assert set(english.messages) >= i18n.JS_SHARED, "JS_SHARED names a key that does not exist"
    assert not (i18n.JS_SHARED & prefixed), "a js. key does not need to be in JS_SHARED too"


def test_every_string_a_script_writes_is_a_key_in_the_catalogue() -> None:
    """Every `sr.t('…')` in the shipped JavaScript names a message that exists.

    A key the browser asks for and the catalogue has not got is published to
    the reader as its own name in brackets. Nothing else in this repository
    would notice, because the call is in a language the Python tests do not
    read - so they read it here instead.
    """
    english = load(DEFAULT_LOCALE)
    bundle = set(i18n.browser_catalog(translator_for(DEFAULT_LOCALE))["messages"])
    assets = Path(i18n.__file__).resolve().parent / "assets"
    files = [*sorted((assets / "js").glob("*.js")), assets / "search.js", assets / "viewer.js"]
    asked = re.compile(r"""\bt\(\s*['"]([a-z][a-z0-9_.]*\.[a-z][a-z0-9_]*)['"]""")

    missing: list[str] = []
    for path in files:
        for key in asked.findall(path.read_text(encoding="utf-8")):
            if key not in english.messages:
                missing.append(f"{path.name}: {key} is in no catalogue")
            elif key not in bundle:
                missing.append(f"{path.name}: {key} is not in the browser bundle")
    assert missing == []


def test_a_key_the_translator_has_not_reached_is_published_in_english_and_named(
    tmp_path: Path,
) -> None:
    """A reader gets a sentence; a contributor gets a list.

    The substitution happens here, at build time, rather than in the browser -
    so `fell_back` is a list for one console warning and not a second lookup
    path a script has to implement.
    """
    english = load(DEFAULT_LOCALE)
    partial = {k: v for k, v in english.messages.items() if k != "js.search.searching"}
    (tmp_path / "xx.json").write_text(
        json.dumps({"locale": "xx", "plural": "en", "messages": partial}, ensure_ascii=False),
        encoding="utf-8",
    )
    translator = Translator(load("xx", locales_dir=tmp_path), english)
    bundle = i18n.browser_catalog(translator)
    assert bundle["fell_back"] == ["js.search.searching"]
    assert bundle["messages"]["js.search.searching"] == english.messages["js.search.searching"]


def test_the_browser_script_defines_one_global_and_nothing_else() -> None:
    source = i18n.browser_script(translator_for("ru"))
    assert source.count("window.stackroomMessages") == 1
    body = source.split("window.stackroomMessages = ", 1)[1].rstrip().rstrip(";")
    assert json.loads(body)["locale"] == "ru"


def test_the_browser_script_cannot_end_its_own_block_early() -> None:
    """It is one copy-and-paste away from being inlined by somebody."""
    english = load(DEFAULT_LOCALE)
    catalog = Catalog(
        locale="xx",
        messages={"js.search.searching": "</script><script>alert(1)</script>"},
    )
    source = i18n.browser_script(Translator(catalog, english))
    assert "</script" not in source
    assert "\\u003c/script" in source
    body = source.split("window.stackroomMessages = ", 1)[1].rstrip().rstrip(";")
    assert json.loads(body)["messages"]["js.search.searching"].startswith("</script>")


def test_the_bundle_costs_less_as_a_file_than_it_would_inline() -> None:
    """The measurement behind the decision, held to so it stays true.

    At the supported ceiling of 20,000 pages, inlining the Russian bundle in
    every page would be several hundred megabytes of duplicated HTML in a
    folder somebody is expected to mirror, zip and carry on a stick. As one
    file it is fetched once and precached with the rest of the shell.
    """
    raw = len(i18n.browser_script(translator_for("ru")).encode("utf-8"))
    assert raw < 40_000, "the bundle has grown past what one head request should cost"
    assert raw * 20_000 > 100_000_000, (
        "if inlining this were cheap the file could go away; check the arithmetic"
    )


# --------------------------------------------------------------------------
# the same rule, run in a real browser
# --------------------------------------------------------------------------
#
# The plural rule now exists in two places: `PLURAL_RULES` here, and six lines
# of JavaScript in `assets/js/prefs.js` reading the table `plural_data`
# generates from it. Asserting by inspection that the two agree is exactly the
# reasoning that gets 21 wrong in Russian, so these tests run the shipped
# JavaScript in Chromium and compare its answers to Python's.

ASSETS = Path(i18n.__file__).resolve().parent / "assets"

# 0 and 1 because every implementation gets them right; 2, 4 and 5 because the
# East Slavic forms change there; 11 and 25 because they are `many` despite
# looking like `one` or `few`; 21, 22 and 101 because that is where Russian and
# Polish part company and where a naive `n === 1` is wrong; 111 because it is
# the number a rule that checks the units and not the tens gets wrong.
AWKWARD = [0, 1, 2, 4, 5, 11, 21, 22, 25, 100, 101, 111]


@pytest.fixture(scope="module")
def reader(browser):
    """A page with the shipped prefs.js on it, and nothing else.

    The real file, not a copy of the six lines under test: a test that reads a
    transcription of the code cannot catch the code being wrong.
    """
    page = browser.new_page()
    page.set_content("<!doctype html><title>t</title><body>")
    yield page
    page.close()


def _load_catalogue(page, code: str) -> None:
    """Put one shipped catalogue on the page the way the build would."""
    page.evaluate("() => { delete window.stackroomReader; }")
    page.add_script_tag(content=i18n.browser_script(translator_for(code)))
    page.add_script_tag(path=str(ASSETS / "js" / "prefs.js"))


@pytest.mark.parametrize("code", SHIPPED)
def test_the_javascript_picks_the_same_plural_form_as_python(reader, code: str) -> None:
    """The one assertion this whole design exists to be able to make.

    A naive `n === 1` passes at 0, 1, 2 and 5 in every language here and is
    wrong in Russian at 21 - which is why the table is generated from the
    Python rule rather than written twice, and why this compares answers rather
    than reading both implementations and nodding.
    """
    _load_catalogue(reader, code)
    catalog = load(code)
    got = reader.evaluate(
        "(ns) => ns.map(n => window.stackroomReader.plural(n))", AWKWARD
    )
    want = [plural_category(catalog.plural, n) for n in AWKWARD]
    assert got == want, dict(zip(AWKWARD, zip(got, want, strict=True), strict=True))


@pytest.mark.parametrize("code", SHIPPED)
def test_the_javascript_writes_the_same_sentence_as_python(reader, code: str) -> None:
    """Not just the same category - the same finished sentence.

    The search status line is the one a reader meets most often, and it is a
    plural message with a count and a quoted query in it.
    """
    _load_catalogue(reader, code)
    translator = translator_for(code)
    got = reader.evaluate(
        "(ns) => ns.map(n => window.stackroomReader.t('js.search.hits',"
        " { count: n, query: 'смета' }))",
        AWKWARD,
    )
    want = [str(translator.t("js.search.hits", count=n, query="смета")) for n in AWKWARD]
    assert got == want


@pytest.mark.parametrize("code", SHIPPED)
def test_the_javascript_writes_a_number_the_way_python_does(reader, code: str) -> None:
    """`Intl` decides where the groups fall; the catalogue decides the rest.

    Measured in Chromium: `Intl.NumberFormat('fr')` separates with U+202F where
    the catalogue would say U+00A0, `Intl.NumberFormat('pl')` applies CLDR's
    own minimumGroupingDigits rather than the catalogue's, and an unknown code
    silently resolves to en-US. Any of those would put two spellings of the
    same number on one page, so the separators and the grouping threshold come
    from the catalogue and this is the test that they do.
    """
    _load_catalogue(reader, code)
    translator = translator_for(code)
    numbers = [0, 1, 999, 1000, 1234, 9999, 10_000, 1_234_567, 20_000]
    got = reader.evaluate("(ns) => ns.map(n => window.stackroomReader.n(n))", numbers)
    assert got == [translator.n(n) for n in numbers]

    percents = [0, 15, 63, 100]
    got = reader.evaluate("(ns) => ns.map(n => window.stackroomReader.pct(n))", percents)
    assert got == [translator.pct(n) for n in percents]


def test_the_browser_falls_back_to_english_visibly_but_not_loudly(reader) -> None:
    """A contributor gets one console warning; a reader gets a sentence."""
    english = load(DEFAULT_LOCALE)
    partial = {k: v for k, v in english.messages.items() if k != "js.search.searching"}
    catalog = Catalog(locale="xx", plural="en", messages=partial)
    warnings: list[str] = []
    reader.on("console", lambda m: warnings.append(m.text) if m.type == "warning" else None)
    reader.evaluate("() => { delete window.stackroomReader; }")
    reader.add_script_tag(content=i18n.browser_script(Translator(catalog, english)))
    reader.add_script_tag(path=str(ASSETS / "js" / "prefs.js"))

    # The reader sees a sentence, in English, that reads.
    served = reader.evaluate("() => window.stackroomReader.t('js.search.searching')")
    assert served == english.messages["js.search.searching"]
    # The contributor sees which key it was, and how to find the rest.
    assert any("js.search.searching" in w and "stackroom.i18n check" in w for w in warnings)


def test_a_key_in_no_catalogue_at_all_names_itself_in_the_browser(reader) -> None:
    """The same policy `t()` follows in Python: never a blank."""
    _load_catalogue(reader, DEFAULT_LOCALE)
    assert reader.evaluate(
        "() => window.stackroomReader.t('js.nothing.here')"
    ) == "[js.nothing.here]"


def test_the_browser_escapes_parameters_only_in_an_html_message(reader) -> None:
    """The `_html` naming rule, enforced on the browser side of it too."""
    _load_catalogue(reader, DEFAULT_LOCALE)
    marked = reader.evaluate(
        "() => window.stackroomReader.t('js.negative.tip_where_html',"
        " { title: 'A & B <b>', page: 'page 1' })"
    )
    assert "<strong>" in marked and "&amp;" in marked and "<b>" not in marked
    plain = reader.evaluate(
        "() => window.stackroomReader.t('js.search.result_where',"
        " { title: 'A & B', number: 2 })"
    )
    assert plain == "A & B — page 2"


# --------------------------------------------------------------------------
# the one piece of interface text inside the search body
# --------------------------------------------------------------------------
#
# `page.html.jinja` writes the bar that stands in for a redacted passage as
#
#     <span class="withheld" role="img" aria-label="withheld, …"></span>
#
# and that element is inside the block carrying `data-pagefind-body`, which
# ARCHITECTURE.md's search contract says holds this page's tokens and nothing
# else. `Page.words` order and token order in that block have to stay
# identical, because the search index returns matches as positions into it and
# the viewer turns those positions into boxes drawn on the scan. One
# divergence and every highlight in the archive lands on the wrong word.
#
# An `aria-label` is an attribute, so the reasoning says it contributes no text
# node and the contract is intact. The reasoning is not the test. What follows
# indexes the same page four ways - the English label, a Russian one, a much
# longer Russian one, and no label at all - and asserts that Pagefind's own
# answers are identical, with a control that puts the same three words in as a
# text node and must diverge. Without the control this would pass on an index
# that had stopped being built.


_TOKENS = ["The", "Director", "approved", "the", "transfer", "of", "funds", "on", "March", "the", "fourth", "and", "notified", "the", "committee", "immediately", "afterwards", "without", "any", "further", "delay"]

_BAR = {
    "english": '<span class="withheld" role="img" aria-label="withheld, personal privacy"></span>',
    "russian": '<span class="withheld" role="img" aria-label="изъято, неприкосновенность частной жизни"></span>',
    "long": '<span class="withheld" role="img" aria-label="изъято одно место в тексте по '
            'основанию неприкосновенности частной жизни, смотрите пояснение ниже"></span>',
    "bare": '<span class="withheld"></span>',
    # The control: the same words as a text node rather than as an attribute.
    "as-text": '<span class="withheld">withheld, personal privacy</span>',
}


def _indexable_page(bar: str) -> str:
    parts = []
    for index, word in enumerate(_TOKENS):
        if index == 8:
            parts.append(bar)
        parts.append(f'<span class="w" data-i="{index}">{word}</span>')
    body = " ".join(parts)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        "<title>Page 1</title></head><body><main>"
        f'<div class="text-layer" data-pagefind-body>{body}</div>'
        "</main></body></html>\n"
    )


@pytest.fixture(scope="module")
def indexed_variants(tmp_path_factory):
    """One tiny site per variant, each with its own Pagefind index."""
    search_mod = pytest.importorskip("stackroom.build.search")
    usable, why = search_mod.pagefind_available()
    if not usable:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("pagefind-aria")
    out = {}
    for name, bar in _BAR.items():
        site = root / name
        page = site / "d" / "doc" / "p" / "1"
        page.mkdir(parents=True)
        (page / "index.html").write_text(_indexable_page(bar), encoding="utf-8")
        search_mod.build_index(site, language="en")
        out[name] = site
    return out


def _index_bytes(site: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(site)): p.read_bytes()
        for p in sorted((site / "_pagefind").rglob("*"))
        if p.is_file() and p.suffix in {".pf_index", ".pf_fragment", ".pf_meta"}
    }


def test_translating_the_withheld_bars_label_does_not_move_the_search_index(
    indexed_variants,
) -> None:
    """The empirical form of "an attribute contributes no token".

    Four labels, one index. If an `aria-label` reached the tokeniser at all,
    the Russian one - which is longer, and in another script - would not
    produce the same bytes as the English one.
    """
    english = _index_bytes(indexed_variants["english"])
    assert english, "pagefind wrote no index; this test would prove nothing"
    for name in ("russian", "long", "bare"):
        assert _index_bytes(indexed_variants[name]) == english, (
            f"the {name} label changed the index"
        )


def test_the_same_words_as_a_text_node_do_move_it(indexed_variants) -> None:
    """The control, without which the test above is not evidence of anything."""
    assert _index_bytes(indexed_variants["as-text"]) != _index_bytes(
        indexed_variants["english"]
    )


def test_token_positions_are_unchanged_by_the_label(browser, indexed_variants) -> None:
    """The claim that actually matters: `result.words[i]` still indexes `Page.words`.

    Byte-identical index files are strong evidence, but what the viewer uses is
    the position Pagefind hands back at query time, so that is what is asked
    for - through Pagefind's own runtime, in a real browser.
    """
    import functools
    import http.server
    import socketserver
    import threading

    wanted = {"committee": [14], "director": [1], "delay": [20]}
    answers: dict[str, dict[str, list[int]]] = {}

    for name in ("english", "russian", "long", "bare", "as-text"):
        site = indexed_variants[name]
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(site)
        )
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        page = browser.new_page()
        try:
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/d/doc/p/1/index.html")
            answers[name] = page.evaluate(
                """async (queries) => {
                    const mod = await import('/_pagefind/pagefind.js');
                    const out = {};
                    for (const q of queries) {
                        const r = await mod.search(q);
                        out[q] = r.results.length ? r.results[0].words : [];
                    }
                    return out;
                }""",
                list(wanted),
            )
        finally:
            page.close()
            server.shutdown()
            server.server_close()

    for name in ("english", "russian", "long", "bare"):
        assert answers[name] == wanted, f"{name} moved a token position"
    # And the control moves every position after the bar by the three words it
    # inserted, which is exactly the failure the contract is about.
    assert answers["as-text"]["committee"] == [17]
    assert answers["as-text"]["director"] == [1]
