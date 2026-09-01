"""A very small Markdown subset, rendered safely.

Stackroom needs to turn one file - the operator's ``about.md`` - into a few
paragraphs of HTML. Pulling in a Markdown implementation for that would add a
dependency, a parser, and an HTML sanitiser to a tool whose entire promise is
that the output is inspectable.

So: escape everything first, then put back a fixed list of constructs. Nothing
here can emit a tag that was not written by this module, which means a stray
``<script>`` in ``about.md`` renders as the text ``<script>`` and not as a
script - a property worth having even though the file is written by the
operator, because the operator is often pasting from an agency's cover letter.

Supported: paragraphs, ``#``-``###`` headings, ``-``/``*``/``1.`` lists,
``>`` quotes, ``---`` rules, ``**bold**``, ``*italic*``, ``` `code` ```, and
``[text](url)`` links to http, https, mailto and relative targets.
"""

from __future__ import annotations

import html
import re

__all__ = ["plain_text", "render_markdown"]

_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\w)")
_CODE = re.compile(r"`([^`\n]+)`")
_SAFE_URL = re.compile(r"^(?:https?://|mailto:|[./#]|[A-Za-z0-9_-]+[/.])")


def _inline(text: str) -> str:
    """Escape, then re-introduce only the marks we chose to support."""
    out = html.escape(text, quote=True)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)

    def link(match: re.Match[str]) -> str:
        label, href = match.group(1), html.unescape(match.group(2))
        if not _SAFE_URL.match(href):
            # A javascript: or data: URL, or something we do not recognise.
            # Show the reader what was written rather than following it.
            return f"{label} ({html.escape(href)})"
        safe = html.escape(href, quote=True)
        external = href.startswith(("http://", "https://"))
        rel = ' rel="noopener noreferrer"' if external else ""
        return f'<a href="{safe}"{rel}>{label}</a>'

    return _LINK.sub(link, out)


def render_markdown(text: str) -> str:
    """Render the subset. Returns HTML that is safe to insert unescaped."""
    if not text or not text.strip():
        return ""

    # Comments are stripped before anything else. They are how the scaffolded
    # about.md talks to the operator, they routinely span several lines, and
    # line-by-line handling published everything after the first one.
    text = re.sub(r"<!--.*?(?:-->|\Z)", "", text, flags=re.S)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    list_kind: str | None = None
    quoting = False

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para).strip())}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    def close_quote() -> None:
        nonlocal quoting
        if quoting:
            out.append("</blockquote>")
            quoting = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_para()
            close_list()
            close_quote()
            continue

        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            flush_para()
            close_list()
            close_quote()
            out.append("<hr>")
            continue

        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush_para()
            close_list()
            close_quote()
            # The page already has an <h1>; a heading in the operator's prose is
            # a section within it, so everything shifts down one level.
            level = min(4, len(heading.group(1)) + 1)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        if stripped.startswith(">"):
            flush_para()
            close_list()
            if not quoting:
                out.append("<blockquote>")
                quoting = True
            para.append(stripped.lstrip("> ").strip())
            continue

        item = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", stripped)
        if item:
            flush_para()
            close_quote()
            kind = "ul" if item.group(1) in "-*+" else "ol"
            if list_kind != kind:
                close_list()
                out.append(f"<{kind}>")
                list_kind = kind
            out.append(f"<li>{_inline(item.group(2))}</li>")
            continue

        close_list()
        para.append(stripped)

    flush_para()
    close_list()
    close_quote()
    return "\n".join(out)


def plain_text(markdown: str, limit: int = 240) -> str:
    """A one-line summary of a Markdown block, for a meta description."""
    text = re.sub(r"<!--.*?-->", " ", markdown, flags=re.S)
    text = re.sub(r"[#>*_`]+", " ", text)
    text = _LINK.sub(lambda m: m.group(1), text)
    text = " ".join(text.split())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"
