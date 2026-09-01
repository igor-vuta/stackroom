"""Reading ``stackroom.toml``, and above all failing to read it.

A configuration error is the first thing Stackroom ever says to an operator,
and it is usually said to someone who has just been handed 2,000 pages and is
in a hurry. So the error messages are a feature with a contract, and this file
is where the contract is checked: every message names **the file**, **the key**
and **what to write instead**. A message that only says "invalid value" is a
bug even though the exception type is right.

The rest is the boring half - defaults, round-tripping, and ``find()`` walking
up from a subdirectory - which matters because a collection needs no
configuration file at all and every default has to be defensible on its own.

Nothing here touches the network or a real collection; every file under test is
written into ``tmp_path`` by the test that reads it.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from stackroom.config import (
    ABOUT_TEMPLATE,
    CONFIG_NAME,
    TEMPLATE,
    Config,
    ConfigError,
    OcrConfig,
    find,
    load,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def write(tmp_path: Path, text: str, name: str = CONFIG_NAME) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def refusal(tmp_path: Path, text: str) -> str:
    """Load a bad file and return the message the operator will read."""
    path = write(tmp_path, text)
    with pytest.raises(ConfigError) as caught:
        load(path)
    message = str(caught.value)
    assert str(path) in message, f"the message does not name the file:\n{message}"
    return message


EVERY_SETTING = """\
title = "Papers of the Commission"
description = "Two hundred pages, badly redacted."
language = "fr"
jurisdiction = "uk"
base_url = "https://example.org/archive/"
source_url = "https://example.org/request/12"
license = "CC0-1.0"
contact = "archive@example.org"
exclude = ["drafts/*"]
include = ["*.pdf"]

[ocr]
languages = ["fra", "eng"]
mode = "always"
psm = 6
auto_rotate = false
timeout = 30.0

[render]
dpi = 300
widths = [2000, 1000]
thumb_width = 320
formats = ["webp"]
max_megapixels = 20.0

[safety]
hidden_text = "warn"
publish_originals = false
strip_metadata = true

[search]
enabled = false
min_query = 3
"""


# --------------------------------------------------------------------------
# 1. defaults and round-tripping
# --------------------------------------------------------------------------


def test_a_collection_with_no_configuration_file_still_has_every_setting():
    """``stackroom build ./papers`` must work on a folder nobody prepared.

    If any default here becomes ``None`` or a required argument, the tool stops
    working on the case it is meant to be easiest at.
    """
    cfg = load(None)
    assert cfg.path is None
    assert cfg.title == "Untitled collection"
    assert cfg.language == "en"
    assert cfg.jurisdiction == "us"
    assert cfg.ocr.languages == ["eng"]
    assert cfg.ocr.mode == "auto"
    assert cfg.render.dpi == 150
    assert cfg.render.widths == [1600, 900]
    assert cfg.safety.hidden_text == "stop", "the strict setting is the default on purpose"
    assert cfg.safety.publish_originals is True
    assert cfg.search.enabled is True


def test_two_defaults_configs_do_not_share_their_mutable_lists():
    """A shared default list would let one collection edit another's settings."""
    a, b = load(None), load(None)
    a.ocr.languages.append("rus")
    a.render.widths.append(400)
    a.exclude.append("*.tmp")
    assert b.ocr.languages == ["eng"]
    assert b.render.widths == [1600, 900]
    assert b.exclude == []


def test_the_scaffolded_template_is_a_valid_configuration_file(tmp_path):
    """``stackroom init`` writes TEMPLATE; loading it must not raise.

    A template that its own reader rejects is the most embarrassing bug in a
    configuration module, and the easiest one to ship.
    """
    cfg = load(write(tmp_path, TEMPLATE.format(title="Papers of the Commission")))
    assert cfg.title == "Papers of the Commission"
    assert cfg.jurisdiction == "us"
    assert cfg.ocr.languages == ["eng"]
    assert cfg.ocr.mode == "auto"
    assert cfg.render.dpi == 150
    assert cfg.safety.hidden_text == "stop"


def test_every_setting_written_in_a_file_arrives_in_the_dataclass(tmp_path):
    """The round trip, key by key, including the four sections."""
    cfg = load(write(tmp_path, EVERY_SETTING))

    assert cfg.title == "Papers of the Commission"
    assert cfg.description == "Two hundred pages, badly redacted."
    assert cfg.language == "fr"
    assert cfg.jurisdiction == "uk"
    assert cfg.base_url == "https://example.org/archive/"
    assert cfg.source_url == "https://example.org/request/12"
    assert cfg.license == "CC0-1.0"
    assert cfg.contact == "archive@example.org"
    assert cfg.exclude == ["drafts/*"]
    assert cfg.include == ["*.pdf"]

    assert cfg.ocr.languages == ["fra", "eng"]
    assert cfg.ocr.mode == "always"
    assert cfg.ocr.psm == 6
    assert cfg.ocr.auto_rotate is False
    assert cfg.ocr.timeout == 30.0

    assert cfg.render.dpi == 300
    assert cfg.render.widths == [2000, 1000]
    assert cfg.render.thumb_width == 320
    assert cfg.render.formats == ["webp"]
    assert cfg.render.max_megapixels == 20.0

    assert cfg.safety.hidden_text == "warn"
    assert cfg.safety.publish_originals is False
    assert cfg.safety.strip_metadata is True

    assert cfg.search.enabled is False
    assert cfg.search.min_query == 3

    assert cfg.path == tmp_path / CONFIG_NAME


def test_no_setting_exists_that_a_file_cannot_set(tmp_path):
    """A field nobody can write is either dead or an undocumented default.

    ``EVERY_SETTING`` above is the exhaustive list; this test fails when a
    setting is added to the dataclass and not to the file format's tests, which
    is the moment to decide whether it is really configurable.
    """
    written = {line.split("=")[0].strip() for line in EVERY_SETTING.splitlines() if "=" in line}
    written |= {"ocr", "render", "safety", "search"}
    declared = {f.name for f in fields(Config)} - {"path", "about_path"}
    assert declared <= written, f"not covered by the round-trip test: {sorted(declared - written)}"


def test_the_ocr_section_carries_exactly_one_language_list() -> None:
    """One language setting for the recogniser, and deliberately no second one.

    ``ocr.languages`` is what Tesseract should expect on a scan: a filter, and
    an expensive one, because every extra alphabet costs accuracy on the
    others. It is *not* a claim about what the documents are written in, and
    the quality check treats it as a prior that can only help a page - see
    ``lang.stopword_ratio``.

    The temptation, when those two facts are separated, is to add a second key
    for the second one. This project already has three language settings people
    confuse - this, ``search.language`` and the interface ``language`` - and a
    fourth would buy almost nothing, because the judge takes the maximum over
    every word list it has whatever it is told. This test is here so that
    adding one is a decision somebody makes on purpose, with an argument, and
    not a reflex.
    """
    assert {f.name for f in fields(OcrConfig)} == {
        "languages", "mode", "psm", "auto_rotate", "timeout"
    }
    assert Config().ocr.languages == ["eng"]


def test_an_about_file_beside_the_configuration_is_found(tmp_path):
    """``about.md`` is the provenance narrative; the config is how it is found."""
    (tmp_path / "about.md").write_text(ABOUT_TEMPLATE, encoding="utf-8")
    cfg = load(write(tmp_path, 'title = "x"'))
    assert cfg.about_path == tmp_path / "about.md"


def test_a_collection_without_an_about_file_says_so_rather_than_guessing(tmp_path):
    cfg = load(write(tmp_path, 'title = "x"'))
    assert cfg.about_path is None


# --------------------------------------------------------------------------
# 2. unknown keys
# --------------------------------------------------------------------------


def test_a_misspelt_top_level_key_is_named_along_with_the_alternatives(tmp_path):
    """"titel" is a typo, and the fix is in the message or it is nowhere."""
    message = refusal(tmp_path, 'titel = "Papers"')
    assert "titel" in message
    assert "unknown" in message.lower()
    for suggestion in ("title", "description", "jurisdiction", "ocr", "render", "safety", "search"):
        assert suggestion in message, f"the message does not offer {suggestion!r}:\n{message}"


def test_a_misspelt_key_inside_a_section_names_the_section_and_its_keys(tmp_path):
    """The alternatives offered must be the ones valid *here*.

    Listing every setting in the file would bury the four that would have
    worked in this table.
    """
    message = refusal(tmp_path, '[ocr]\nlangauges = ["eng"]\n')
    assert "langauges" in message
    assert "ocr" in message
    assert "languages" in message and "mode" in message and "psm" in message
    assert "jurisdiction" not in message, "a key from another section is not a suggestion"


def test_a_section_written_as_a_scalar_is_shown_how_a_table_is_written(tmp_path):
    """``ocr = 3`` is a shape error, and the fix is a two-line example."""
    message = refusal(tmp_path, "ocr = 3")
    assert "[ocr]" in message
    assert "table" in message


# --------------------------------------------------------------------------
# 3. wrong types
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "label", "wanted"),
    [
        ('[safety]\npublish_originals = "yes"', "safety.publish_originals", "true or false"),
        ("[ocr]\nauto_rotate = 1", "ocr.auto_rotate", "true or false"),
        ('[ocr]\nlanguages = "eng"', "ocr.languages", "should be a list"),
        ('exclude = "drafts/*"', "exclude", "should be a list"),
        ("title = 3", "title", "text in quotes"),
        ('[render]\ndpi = "150"', "render.dpi", "should be a number"),
        ("[render]\ndpi = true", "render.dpi", "should be a number"),
        ("[ocr]\ntimeout = false", "ocr.timeout", "should be a number"),
    ],
)
def test_a_value_of_the_wrong_type_says_which_type_in_words(tmp_path, body, label, wanted):
    """Not "expected bool, got str": the reader is holding a text editor.

    Each message has to name the dotted key so it can be found in the file, and
    describe the type in the language the file is written in.
    """
    message = refusal(tmp_path, body)
    assert label in message
    assert wanted in message


def test_a_boolean_is_not_quietly_accepted_where_a_number_belongs(tmp_path):
    """``True == 1`` in Python, and ``dpi = true`` would render at 1 dpi."""
    message = refusal(tmp_path, "[render]\ndpi = true")
    assert "render.dpi" in message


def test_a_wrong_type_message_quotes_the_value_that_was_written(tmp_path):
    """Half of finding a bad line is recognising it when you scroll past it."""
    assert "'yes'" in refusal(tmp_path, '[safety]\nstrip_metadata = "yes"')


# --------------------------------------------------------------------------
# 4. values outside the vocabulary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "label", "bad", "allowed"),
    [
        ('[ocr]\nmode = "sometimes"', "ocr.mode", "sometimes", ("auto", "always", "never")),
        (
            '[safety]\nhidden_text = "ignore"',
            "safety.hidden_text",
            "ignore",
            ("stop", "warn"),
        ),
        ('jurisdiction = "au"', "jurisdiction", "au", ("us", "uk", "ca", "eu")),
    ],
)
def test_a_value_outside_the_vocabulary_lists_the_whole_vocabulary(
    tmp_path, body, label, bad, allowed
):
    """Three settings are enumerations, and a closed list can always be printed.

    ``safety.hidden_text = "ignore"`` is the one that matters: it is what an
    operator reaches for when the build stops, and the list of two options is
    how they learn that "publish it anyway" is not on offer.
    """
    message = refusal(tmp_path, body)
    assert label in message
    assert bad in message
    for option in allowed:
        assert option in message, f"{option!r} missing from:\n{message}"


def test_the_case_of_an_enumerated_value_is_not_quietly_corrected(tmp_path):
    """``mode = "AUTO"`` is a typo we can name, not a spelling we can guess."""
    assert "AUTO" in refusal(tmp_path, '[ocr]\nmode = "AUTO"')


# --------------------------------------------------------------------------
# 5. values outside a range
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dpi", [0, 71, 601, 4800])
def test_a_dpi_outside_the_supported_range_says_what_to_use_instead(tmp_path, dpi):
    """72-600 is the honest range, and 150 is the answer most people want.

    A build at 4,800 dpi does not fail, it takes a day and fills a disk, so the
    refusal has to arrive before the work starts.
    """
    message = refusal(tmp_path, f"[render]\ndpi = {dpi}")
    assert "render.dpi" in message
    assert str(dpi) in message
    assert "72" in message and "600" in message
    assert "150" in message, "the message should say what a good value looks like"


@pytest.mark.parametrize("dpi", [72, 150, 300, 600])
def test_the_ends_of_the_dpi_range_are_inside_it(tmp_path, dpi):
    assert load(write(tmp_path, f"[render]\ndpi = {dpi}")).render.dpi == dpi


def test_an_empty_width_list_is_refused_with_an_example(tmp_path):
    """No widths means no images, and the page template has nothing to show."""
    message = refusal(tmp_path, "[render]\nwidths = []")
    assert "render.widths" in message
    assert "empty" in message
    assert "1600" in message


@pytest.mark.parametrize("width", [0, 199, 6001])
def test_a_width_outside_the_range_names_the_offending_number(tmp_path, width):
    message = refusal(tmp_path, f"[render]\nwidths = [1600, {width}]")
    assert "render.widths" in message
    assert str(width) in message
    assert "200" in message and "6000" in message


def test_an_unknown_image_format_lists_the_four_that_work(tmp_path):
    """The encoder would fail much later, in a worker, on page 1,900."""
    message = refusal(tmp_path, '[render]\nformats = ["tiff"]')
    assert "tiff" in message
    for known in ("avif", "webp", "jpeg", "png"):
        assert known in message


def test_an_empty_language_list_is_refused_with_tesseract_spellings(tmp_path):
    """OCR with no language recognises nothing, and the codes are not obvious.

    "en" is the code a person guesses; "eng" is the code Tesseract wants, so
    the example in the message is doing real work.
    """
    message = refusal(tmp_path, "[ocr]\nlanguages = []")
    assert "ocr.languages" in message
    assert "empty" in message
    assert "eng" in message


def test_a_base_url_without_a_scheme_says_which_schemes_are_meant(tmp_path):
    """``example.org`` in a canonical link resolves against the page, not the site."""
    message = refusal(tmp_path, 'base_url = "example.org"')
    assert "base_url" in message
    assert "example.org" in message
    assert "https://" in message


@pytest.mark.parametrize("url", ["https://example.org/a/", "http://example.org", "/archive/"])
def test_a_base_url_that_can_be_resolved_is_accepted(tmp_path, url):
    assert load(write(tmp_path, f'base_url = "{url}"')).base_url == url


# --------------------------------------------------------------------------
# 6. files that are not configuration at all
# --------------------------------------------------------------------------


def test_a_file_that_is_not_toml_says_so_and_quotes_the_parser(tmp_path):
    """A stray smart quote from a word processor is the usual cause.

    The parser's own complaint carries the line number, so it is passed
    through rather than swallowed.
    """
    message = refusal(tmp_path, 'title = "Papers of the Commission\njurisdiction = "us"\n')
    assert "not valid TOML" in message
    assert "line 1" in message, "the parser's explanation, and with it the line number, was dropped"


def test_a_configuration_file_that_is_not_there_is_a_config_error_not_an_oserror(tmp_path):
    """``--config typo.toml`` must produce the same kind of message as a bad key.

    A raw ``FileNotFoundError`` escaping here reaches the operator as a
    traceback, which teaches them nothing about which path was tried.
    """
    missing = tmp_path / "elsewhere" / CONFIG_NAME
    with pytest.raises(ConfigError) as caught:
        load(missing)
    message = str(caught.value)
    assert str(missing) in message
    assert "could not be read" in message


def test_a_configuration_error_is_a_value_error(tmp_path):
    """Callers that catch ValueError - including the CLI - must catch this."""
    with pytest.raises(ValueError):
        load(write(tmp_path, "nonsense = 1"))


BAD_FILES = [
    'titel = "x"',
    "[ocr]\nlangauges = []",
    "ocr = 3",
    '[ocr]\nlanguages = "eng"',
    "title = 3",
    '[render]\ndpi = "150"',
    '[ocr]\nmode = "sometimes"',
    '[safety]\nhidden_text = "ignore"',
    'jurisdiction = "au"',
    "[render]\ndpi = 4800",
    "[render]\nwidths = []",
    "[render]\nwidths = [10]",
    '[render]\nformats = ["tiff"]',
    "[ocr]\nlanguages = []",
    'base_url = "example.org"',
    'title = "unterminated\n',
]


@pytest.mark.parametrize("body", BAD_FILES)
def test_no_refusal_leaks_the_vocabulary_of_the_implementation(tmp_path, body):
    """Every message is read by a journalist, not by whoever wrote this module.

    Python type names, exception class names and tracebacks are all signs that
    a message was written for the wrong reader.
    """
    message = refusal(tmp_path, body)
    for jargon in ("Traceback", "TypeError", "ValueError", "<class", "dataclass", "NoneType"):
        assert jargon not in message, f"{jargon!r} in:\n{message}"
    assert message.strip() == message.rstrip(), "a message should not end in whitespace"
    assert "\n" not in message.splitlines()[0], "the first line should stand alone"


@pytest.mark.parametrize("body", BAD_FILES)
def test_every_refusal_begins_with_the_path_to_the_file(tmp_path, body):
    """The operator may be building four collections; the file has to be named.

    ``refusal`` asserts this for every message in this file, so this test is
    the same assertion made where it can be read.
    """
    assert refusal(tmp_path, body).startswith(str(tmp_path / CONFIG_NAME))


# --------------------------------------------------------------------------
# 7. find()
# --------------------------------------------------------------------------


def test_the_configuration_is_found_beside_the_documents(tmp_path):
    written = write(tmp_path, 'title = "x"')
    assert find(tmp_path) == written


def test_the_configuration_is_found_from_deep_inside_the_collection(tmp_path):
    """Documents live in subfolders; the file for them sits at the top.

    ``stackroom build papers/2019/march`` has to find ``papers/stackroom.toml``
    or the operator's title, languages and safety policy silently do nothing.
    """
    written = write(tmp_path, 'title = "x"')
    deep = tmp_path / "2019" / "march" / "annexes"
    deep.mkdir(parents=True)
    assert find(deep) == written


def test_the_nearest_configuration_wins(tmp_path):
    """A collection inside a collection uses its own settings."""
    write(tmp_path, 'title = "outer"')
    inner = tmp_path / "inner"
    nearest = write(inner, 'title = "inner"')
    assert find(inner) == nearest
    assert load(find(inner)).title == "inner"


def test_a_file_is_looked_up_from_the_folder_it_is_in(tmp_path):
    """``find()`` is given whatever the operator typed, which may be one PDF."""
    written = write(tmp_path, 'title = "x"')
    pdf = tmp_path / "memo.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    assert find(pdf) == written


def test_no_configuration_anywhere_above_is_not_an_error(tmp_path):
    """The unconfigured collection is the supported case, not the sad path."""
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert find(deep) is None
    assert load(None).title == "Untitled collection"


def test_a_directory_named_like_the_config_file_is_not_a_config_file(tmp_path):
    """``stackroom.toml/`` is a folder somebody made by accident, not settings."""
    (tmp_path / CONFIG_NAME).mkdir()
    assert find(tmp_path) is None
