"""The Markdown subset, attacked.

``textblock`` exists so that Stackroom can render one file - the operator's
``about.md`` - without adding a Markdown implementation, an HTML sanitiser and
their combined attack surface to a tool whose promise is that its output is
inspectable. It buys that by escaping everything first and then putting back a
fixed list of constructs, which yields one very strong property:

    **No ``<`` in the output came from the input.** Every tag in the rendered
    HTML was written by this module.

Most of this file is that property, pushed at from as many directions as the
subset allows: script tags, ``javascript:`` and ``data:`` links, event handler
attributes, unbalanced marks, markup inside a link label. The operator is
usually pasting from an agency's cover letter, which is to say from a Word
document that has been through three systems, so "the operator wrote this file
themselves" is not the safety argument it sounds like.

The rest checks that the supported constructs actually work, because a safe
renderer that renders nothing is easy and useless.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from stackroom.config import ABOUT_TEMPLATE
from stackroom.textblock import plain_text, render_markdown

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

OWN_TAGS = (
    "p", "h2", "h3", "h4", "ul", "ol", "li", "blockquote", "hr", "strong", "em", "code", "a",
)
"""Every tag this module is allowed to emit. Anything else in the output came
from the input, which is the bug this file exists to catch."""

VOID_TAGS = {"hr"}

_OWN_TAG = re.compile(r"</?(?:" + "|".join(OWN_TAGS) + r")(?: [^<>]*)?>")


def residue(html: str) -> str:
    """The rendered HTML with this module's own tags removed.

    Whatever is left is text that came from the input, so it must not contain
    an angle bracket: if it does, something got through unescaped.
    """
    return _OWN_TAG.sub("", html)


class Tags(HTMLParser):
    """Collects the tag structure, so a test can check it is balanced."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.unbalanced: list[str] = []
        self.names: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.names.append(tag)
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.unbalanced.append(tag)

    def handle_data(self, data):
        self.text.append(data)


def parse(html: str) -> Tags:
    tags = Tags()
    tags.feed(html)
    tags.close()
    return tags


ATTACKS = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
    '<img src=x onerror="alert(1)">',
    "<iframe src=//evil.example></iframe>",
    "<style>body{display:none}</style>",
    "[x](javascript:alert(1))",
    "[x](JAVASCRIPT:alert(1))",
    "[x](data:text/html,<script>alert(1)</script>)",
    "[x](vbscript:msgbox(1))",
    '[x](https://ok.example/" onmouseover="alert(1))',
    "[<script>alert(1)</script>](https://ok.example/)",
    "[x](https://ok.example/?a=1&b=2)",
    "**<b>bold</b>**",
    "`<code>&</code>`",
    "> <script>alert(1)</script>",
    "- <script>alert(1)</script>",
    "# <script>alert(1)</script>",
    "]]>&<![CDATA[",
    "a < b and c > d",
    "<!--[if IE]><script>alert(1)</script><![endif]-->",
    "<a href=\"javascript:alert(1)\">click</a>",
]


# --------------------------------------------------------------------------
# 1. the property: no tag in the output was written by the input
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", ATTACKS)
def test_no_angle_bracket_in_the_output_came_from_the_input(source):
    """The whole design in one assertion.

    Strip the tags this module is allowed to emit; anything left holding a
    ``<`` or ``>`` is markup that survived the escape, which is a hole in the
    only defence there is.
    """
    left = residue(render_markdown(source))
    assert "<" not in left, f"unescaped markup survived: {left!r}"
    assert ">" not in left, f"unescaped markup survived: {left!r}"


@pytest.mark.parametrize("source", ATTACKS)
def test_the_output_of_an_attack_is_still_well_formed(source):
    """A safe renderer that emits a dangling ``<li>`` breaks the page around it.

    ``about.md`` is rendered into the middle of the about page, so unbalanced
    tags do not stay inside it.
    """
    tags = parse(render_markdown(source))
    assert tags.stack == [], f"never closed: {tags.stack}"
    assert tags.unbalanced == [], f"closed but never opened: {tags.unbalanced}"
    assert set(tags.names) <= set(OWN_TAGS), f"foreign tags: {set(tags.names) - set(OWN_TAGS)}"


def test_a_script_tag_in_the_source_is_shown_to_the_reader_as_text():
    """The exact case from the module docstring, spelled out.

    The reader should see the characters the operator pasted, which is also how
    they find out their cover letter had a script tag in it.
    """
    html = render_markdown("<script>alert(1)</script>")
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert parse(html).text == ["<script>alert(1)</script>"]


def test_an_event_handler_attribute_cannot_reach_the_output():
    """``onerror`` is only dangerous on a tag, and there is no tag here."""
    html = render_markdown('<img src=x onerror="alert(1)">')
    assert "<img" not in html
    assert "onerror" in html, "the reader should still see what was written"
    assert "&lt;img" in html


# --------------------------------------------------------------------------
# 2. links
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "javascript&#58;alert(1)",
    ],
)
def test_a_scheme_we_do_not_recognise_never_becomes_a_link(url):
    """An unknown scheme is shown, not followed.

    Refusing to link is better than dropping the text: the reader can still see
    that the document claimed to point somewhere, and can judge it.
    """
    html = render_markdown(f"[click here]({url})")
    assert "<a " not in html, html
    assert "href" not in html, html
    assert "click here" in html
    assert re.split(r"[^a-zA-Z]", url)[0] in html, "the URL was silently swallowed"


@pytest.mark.parametrize(
    ("url", "external"),
    [
        ("https://example.org/a", True),
        ("http://example.org/a", True),
        ("mailto:archive@example.org", False),
        ("./files/memo.pdf", False),
        ("../index.html", False),
        ("#section", False),
        ("files/memo.pdf", False),
        ("example.org/x", False),
    ],
)
def test_a_target_we_do_recognise_becomes_a_link(url, external):
    """The four kinds of link an about page actually needs."""
    html = render_markdown(f"[label]({url})")
    assert f'href="{url}"' in html, html
    assert ">label</a>" in html
    assert ('rel="noopener noreferrer"' in html) is external


def test_a_link_label_may_contain_the_marks_the_subset_supports():
    """``[**the request**](...)`` is how an about page cites a request."""
    html = render_markdown("[**the request**](https://example.org/foi/12)")
    assert '<a href="https://example.org/foi/12" rel="noopener noreferrer">' in html
    assert "<strong>the request</strong></a>" in html


def test_markup_in_a_link_label_is_escaped_like_any_other_text():
    """A label is not a hole in the escape.

    This is the shape of the bug in most hand-rolled renderers: the label is
    re-inserted into an anchor after the escaping pass has run.
    """
    html = render_markdown("[<script>alert(1)</script>](https://example.org/)")
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert set(parse(html).names) <= {"p", "a"}


def test_a_quote_inside_a_url_cannot_close_the_href_attribute():
    """The one way a *permitted* scheme could still inject an attribute."""
    html = render_markdown('[x](https://ok.example/"onmouseover="alert(1))')
    assert 'onmouseover="alert' not in html
    assert "&quot;" in html


def test_an_ampersand_in_a_url_is_escaped_without_breaking_the_link():
    html = render_markdown("[q](https://example.org/s?a=1&b=2)")
    assert 'href="https://example.org/s?a=1&amp;b=2"' in html


# --------------------------------------------------------------------------
# 3. the supported constructs
# --------------------------------------------------------------------------


def test_paragraphs_are_separated_by_blank_lines_and_joined_within_one():
    """Soft line breaks inside a paragraph are joined with a space.

    A cover letter wrapped at 72 columns must not render as one word per line.
    """
    html = render_markdown("one line\nand its continuation\n\na second paragraph")
    assert html == "<p>one line and its continuation</p>\n<p>a second paragraph</p>"


@pytest.mark.parametrize(
    ("source", "tag"),
    [("# Heading", "h2"), ("## Heading", "h3"), ("### Heading", "h4")],
)
def test_headings_shift_down_one_level_because_the_page_owns_the_h1(source, tag):
    """The about page's own title is the ``<h1>``; the prose starts below it."""
    assert render_markdown(source) == f"<{tag}>Heading</{tag}>"


def test_a_fourth_level_heading_is_not_a_heading():
    """The subset stops at three, and stopping is not the same as crashing."""
    html = render_markdown("#### Heading")
    assert html.startswith("<p>")
    assert "####" in html


@pytest.mark.parametrize("bullet", ["-", "*", "+"])
def test_every_bullet_marker_makes_the_same_list(bullet):
    html = render_markdown(f"{bullet} first\n{bullet} second")
    assert html == "<ul>\n<li>first</li>\n<li>second</li>\n</ul>"


@pytest.mark.parametrize("source", ["1. first\n2. second", "1) first\n2) second"])
def test_a_numbered_list_is_an_ordered_list(source):
    assert render_markdown(source) == "<ol>\n<li>first</li>\n<li>second</li>\n</ol>"


def test_a_bulleted_list_and_a_numbered_list_do_not_run_into_each_other():
    """Changing marker closes the list and opens the other kind."""
    html = render_markdown("- a\n1. b")
    assert html == "<ul>\n<li>a</li>\n</ul>\n<ol>\n<li>b</li>\n</ol>"
    assert parse(html).stack == []


def test_a_nested_list_is_flattened_rather_than_lost():
    """Indentation is not in the subset, and losing the items would be worse.

    An operator writing an indented list gets a flat one - every item present,
    every tag balanced - rather than a swallowed paragraph or a stray marker.
    """
    html = render_markdown("- outer\n  - inner\n  - also inner\n- second outer")
    tags = parse(html)
    assert [t.strip() for t in tags.text if t.strip()] == [
        "outer",
        "inner",
        "also inner",
        "second outer",
    ]
    assert tags.stack == [] and tags.unbalanced == []
    assert "-" not in "".join(tags.text), "a list marker leaked into the text"


def test_a_quote_is_a_blockquote_and_the_marker_does_not_survive():
    html = render_markdown("> The request was refused in part.")
    assert html == "<blockquote>\n<p>The request was refused in part.</p>\n</blockquote>"


def test_a_quote_followed_by_a_list_closes_the_quote_first():
    """The two block constructs share the same paragraph buffer.

    If the quote is not closed the list ends up inside it, which reads as the
    agency having said something it did not say.
    """
    html = render_markdown("> what they said\n- what we asked")
    assert html.index("</blockquote>") < html.index("<ul>")
    assert parse(html).stack == []


def test_a_blank_line_closes_a_quote_and_a_list():
    html = render_markdown("> quoted\n\n- item\n\nplain")
    tags = parse(html)
    assert tags.stack == [] and tags.unbalanced == []
    assert tags.names.count("blockquote") == 1
    assert tags.names.count("ul") == 1


@pytest.mark.parametrize("rule", ["---", "***", "___", "----------"])
def test_a_horizontal_rule_is_a_rule_and_not_an_empty_heading(rule):
    assert render_markdown(rule) == "<hr>"


def test_bold_italic_and_code_are_the_only_inline_marks():
    html = render_markdown("**bold** and *italic* and `code()`")
    assert html == "<p><strong>bold</strong> and <em>italic</em> and <code>code()</code></p>"


def test_code_spans_show_their_contents_rather_than_running_them():
    """A code span is where an operator pastes the thing that broke."""
    html = render_markdown("`<b>&amp;</b>`")
    assert html == "<p><code>&lt;b&gt;&amp;amp;&lt;/b&gt;</code></p>"


@pytest.mark.parametrize(
    "source",
    [
        "*unbalanced emphasis",
        "**unbalanced strong",
        "a * b * c",
        "5 * 3 * 2 = 30",
        "`unclosed code",
        "**",
        "***",
        "snake_case_word and another_one",
    ],
)
def test_an_unbalanced_mark_is_left_as_the_text_it_is(source):
    """Prose is full of asterisks and underscores that are not emphasis.

    Guessing wrong here turns a footnote marker into italics that never end.
    """
    tags = parse(render_markdown(source))
    assert tags.stack == [] and tags.unbalanced == []
    assert set(tags.names) <= {"p", "hr", "em", "strong"}


def test_an_asterisk_inside_a_word_is_not_emphasis():
    assert render_markdown("2*3*4") == "<p>2*3*4</p>"


# --------------------------------------------------------------------------
# 4. comments, line endings and emptiness
# --------------------------------------------------------------------------


def test_a_comment_on_one_line_never_reaches_the_reader():
    """Comments are how the scaffolded about.md talks to the operator."""
    html = render_markdown("<!-- fill this in -->\n\nWhat we know.")
    assert "fill this in" not in html
    assert html == "<p>What we know.</p>"


def test_a_comment_spanning_several_lines_never_reaches_the_reader():
    """The scaffolded ``about.md`` ships a multi-line comment.

    An operator who writes their provenance note above the comment and does not
    delete it publishes our instructions to them - ending in a stray ``-->`` -
    as the first thing a reader sees on the about page.

    The regression this guards: comments used to be dropped line by line, so
    only the line that *opened* one went, and a ``<!-- ... -->`` spanning
    several lines leaked its body and its closing marker into the page.
    ``render_markdown`` now strips comments from the whole document, before it
    splits anything into lines.
    """
    html = render_markdown("<!--\ninstructions to the operator\n-->\n\nWhat we know.")
    assert "instructions to the operator" not in html
    assert "--&gt;" not in html and "-->" not in html
    assert html == "<p>What we know.</p>"


def test_the_scaffolded_about_file_renders_to_nothing_but_its_heading():
    """``stackroom init`` writes ABOUT_TEMPLATE, so this is the default page.

    Everything below the heading is a comment addressed to the operator.

    The regression this guards is the same multi-line comment bug, on the one
    file Stackroom writes itself: every archive built from a scaffolded
    ``about.md`` that nobody edited used to publish our guidance to its
    operator - "say who released these documents, and to whom" - to its
    readers, as the whole of its about page.
    """
    html = render_markdown(ABOUT_TEMPLATE)
    assert html == "<h2>About this collection</h2>"


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_a_file_written_on_windows_renders_the_same_as_one_written_anywhere(newline):
    """``about.md`` arrives from whatever wrote it, often Notepad.

    A stray carriage return at the end of every line would end up inside the
    text of every paragraph, where it is invisible until someone greps for a
    phrase and cannot find it.
    """
    source = f"# Heading{newline}{newline}One paragraph,{newline}wrapped.{newline}"
    assert render_markdown(source) == render_markdown(
        source.replace(newline, "\n")
    )
    assert "\r" not in render_markdown(source)


@pytest.mark.parametrize("source", ["", "   ", "\n\n\n", " \t \n \t ", "\r\n\r\n"])
def test_an_empty_file_renders_to_an_empty_string(source):
    """The about page checks this value for truth to decide whether to show it.

    An empty ``<p></p>`` would put a heading over nothing.
    """
    assert render_markdown(source) == ""


def test_a_document_of_only_a_comment_renders_to_an_empty_string():
    assert render_markdown("<!-- nothing to say yet -->") == ""


# --------------------------------------------------------------------------
# 5. plain_text, for the meta description
# --------------------------------------------------------------------------


def test_the_summary_drops_the_marks_and_keeps_the_words():
    text = plain_text("# Heading\n\nA **release** of *records* from 2019.")
    assert text == "Heading A release of records from 2019."
    assert plain_text("Run `stackroom build` on the folder") == "Run stackroom build on the folder"


def test_the_summary_keeps_the_label_of_a_link_and_not_its_target():
    """A meta description full of URLs is a description of nothing."""
    text = plain_text("See [the original request](https://example.org/foi/12) for details.")
    assert text == "See the original request for details."


def test_the_summary_drops_comments_including_multi_line_ones():
    """``plain_text`` gets this right, which is what the renderer should do."""
    assert plain_text("<!--\nnote to self\n-->\nPublished today.") == "Published today."


def test_the_summary_is_cut_at_a_word_boundary_and_says_it_was_cut():
    """A meta description is truncated by the search engine if we do not do it."""
    text = plain_text("word " * 200, limit=60)
    assert len(text) <= 60
    assert text.endswith("…")
    assert not text.rstrip("…").endswith(" ")


def test_a_short_summary_is_returned_whole():
    assert plain_text("Two hundred pages.") == "Two hundred pages."
