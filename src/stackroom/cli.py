"""The command line.

Most people will meet Stackroom twice: once when they run `build` on a folder
they were sent this morning, and once, weeks later, when they run it again after
the agency sends the rest. Both times they are in a hurry and this is not the
interesting part of their day. So: few commands, no flags they have to learn,
and output that says what happened rather than what the program did.

The one place this refuses to be terse is when it finds text underneath a black
box. That message is allowed to take up the whole screen.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import time
import traceback
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from . import __version__, model, pipeline
from . import cache as cache_mod
from . import compare as compare_mod
from . import config as config_mod
from .build import search as search_mod
from .build import site as site_mod
from .ingest import discover as discover_mod

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Turn a folder of documents into a public, searchable archive.",
    rich_markup_mode="rich",
)

console = Console()
err = Console(stderr=True)

_debug = False
"""Is ``--debug`` on for this invocation?

Module state rather than a parameter threaded through eleven functions,
because it is a property of the run and every one of those functions would
have to carry it only to hand it to :func:`_die`. Set once, at the top of the
command; read only here.
"""

DEBUG_HINT = "Run it again with --debug for the traceback and what is installed here."


def _set_debug(on: bool) -> None:
    global _debug
    _debug = bool(on)


def _debug_report(to: Console) -> None:
    """The traceback, and everything an issue would otherwise have to ask for.

    CONTRIBUTING asks for documents that break the tool and the issue template
    asks for terminal output; between them they ask an operator for information
    the program never printed. This prints it: the traceback of whatever is
    being handled right now, the versions of every tool that touches a page,
    and the interpreter and platform. It is written to whichever stream the
    caller is already using so that ``2>report.txt`` catches all of it.
    """
    exc = sys.exc_info()[1]
    to.print()
    to.print("[bold]--debug[/]")
    if exc is not None:
        to.print("[dim]" + escape("".join(traceback.format_exception(exc)).rstrip()) + "[/]")
    else:
        to.print("[dim]no exception was being handled at this point[/]")

    rows: list[tuple[str, str]] = [
        ("stackroom", __version__),
        ("python", sys.version.split()[0]),
        ("os", f"{platform.platform()} ({platform.machine()})"),
        ("command", " ".join(sys.argv)),
        ("cwd", str(Path.cwd())),
    ]
    try:
        env = cache_mod.probe_environment()
    except Exception as probe_failure:  # pragma: no cover - belt and braces
        rows.append(("environment", f"could not be probed ({probe_failure})"))
    else:
        # Everything that can change what a page comes out as. Same list the
        # page cache keys on, which is not a coincidence: if two machines
        # disagree about a document, this is where the difference is.
        for name, value in env.as_dict().items():
            if name in ("stackroom", "platform"):
                continue  # already above, in a form a person can read
            rows.append((name, str(value)))
    usable, detail = search_mod.pagefind_available()
    rows.append(("pagefind", detail if usable else f"not usable ({detail})"))
    for name in ("SOURCE_DATE_EPOCH", "OMP_THREAD_LIMIT", "TESSDATA_PREFIX", "LANG"):
        if os.environ.get(name):
            rows.append((name, os.environ[name]))

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("", style="dim")
    table.add_column("", overflow="fold")
    for name, value in rows:
        table.add_row(name, escape(value))
    to.print(table)
    to.print()
    to.print(
        "[dim]Paste all of the above into an issue. If a particular document "
        "triggers it, say what kind rather than attaching it - see CONTRIBUTING.md.[/]"
    )
    to.print()


def _version(value: bool) -> None:
    if value:
        console.print(f"stackroom {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Print the version and exit."
    ),
) -> None:
    pass


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


@app.command()
def build(
    source: Path = typer.Argument(Path("."), help="Folder of documents to publish."),
    out: Path = typer.Option(Path("site"), "--out", "-o", help="Where to write the site."),
    config: Path | None = typer.Option(None, "--config", "-c", help="A stackroom.toml to use."),
    title: str | None = typer.Option(None, "--title", help="Override the collection title."),
    workers: int | None = typer.Option(None, "--workers", "-j", help="Parallel workers."),
    no_search: bool = typer.Option(False, "--no-search", help="Skip building the search index."),
    force: bool = typer.Option(False, "--force", help="Overwrite a non-empty output folder."),
    unsafe_publish_leaks: bool = typer.Option(
        False,
        "--unsafe-publish-leaks",
        help="Publish even if a redaction failed to remove its text. Read the warning first.",
    ),
    i_know: bool = typer.Option(
        False,
        "--i-know",
        help=(
            f"Build a collection larger than {search_mod.DEGRADED_PAGES:,} pages, "
            "knowing search will be slow."
        ),
    ),
    watch: bool = typer.Option(
        False, "--watch", "-w", help="Keep running, and rebuild whenever the documents change."
    ),
    watch_interval: float = typer.Option(
        1.0, "--watch-interval", help="Seconds between checks in --watch.", show_default=True
    ),
    use_cache: bool = typer.Option(
        True, "--cache/--no-cache", help="Reuse pages already read by an earlier build."
    ),
    cache_dir: Path | None = typer.Option(
        None, "--cache-dir", help="Where the page cache lives. Default: stackroom cache path"
    ),
    cache_max: str | None = typer.Option(
        None, "--cache-max", help="Size limit for the page cache, e.g. 5GB."
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="On failure, print the traceback and what is installed here - for an issue.",
    ),
) -> None:
    """Read a folder of documents and write a static archive."""
    _set_debug(debug)
    source = source.expanduser().resolve()
    out = out.expanduser().resolve()

    if not source.exists():
        _die(
            f"There is nothing at {source}.",
            "Point stackroom at the folder holding the documents, e.g. stackroom build ./release",
        )
    _check_source_date_epoch()

    cache = _open_cache(cache_dir, cache_max, use_cache)
    built = False

    def once(_change: object = None) -> str:
        nonlocal built
        cfg = _load_config(source, config, announce=not built)
        built = True
        if title:
            cfg.title = title
        if no_search:
            cfg.search.enabled = False
        if unsafe_publish_leaks:
            cfg.safety.hidden_text = "warn"
        return _build_once(source, out, cfg, workers, force, i_know, cache, brief=watch)

    if not watch:
        once()
        return

    # Everything the build writes has to be invisible to the watcher, or the
    # site it produces is a change and the rebuild never stops. The cache is
    # excluded for the same reason, in case somebody has put it inside the
    # collection folder.
    def watched(change: object = None) -> str:
        """`once`, but a build that stops does not end the session.

        A failed redaction, or a collection past the page ceiling, raises
        `typer.Exit` after printing a report that says exactly what is wrong
        and which file. The operator's next move is to fix that file, which is
        the whole reason they are running --watch, so the watcher keeps
        running - and ends the rebuild with a line naming what happened rather
        than with `build failed: Exit: 2`, which names neither.
        """
        try:
            return once(change)
        except typer.Exit as stop:
            return f"stopped without publishing (exit code {stop.exit_code})"

    cfg = _load_config(source, config, announce=False)
    cache_mod.watch(
        source,
        watched,
        ignore=[out, cache.root, cache.base],
        extra=[p for p in (cfg.path, cfg.about_path) if p],
        interval=watch_interval,
        emit=lambda line: console.print(escape(line) if line else ""),
    )


def _open_cache(
    cache_dir: Path | None, cache_max: str | None, use_cache: bool
) -> cache_mod.PageCache:
    try:
        cache = cache_mod.open_cache(directory=cache_dir, max_bytes=cache_max, enabled=use_cache)
    except ValueError as exc:  # an unreadable --cache-max
        _die(str(exc))
    for warning in cache.warnings:
        console.print(f"[yellow]{escape(warning)}[/]")
    return cache


def _build_once(
    source: Path,
    out: Path,
    cfg: config_mod.Config,
    workers: int | None,
    force: bool,
    i_know: bool,
    cache: cache_mod.PageCache,
    *,
    brief: bool = False,
) -> str:
    """One build, start to finish. Called once, or once per change in --watch."""
    started = time.perf_counter()
    _prepare_out(out, force)

    # The ceiling is enforced from inside the ingest, the moment discovery has
    # counted the pages and before the first page is rasterised. It used to run
    # here, after the whole collection had been read: a 60,000-page production
    # was read in full - hours of pdftoppm and Tesseract - and then refused.
    collection, outcomes = _ingest(
        source, cfg, out, workers, cache, on_counted=lambda pages: _page_ceiling(pages, i_know)
    )
    findings, unchecked = _safety(outcomes, cfg)
    if not brief:
        _page_notes(outcomes)

    site_mod.attach_about(collection, cfg)
    if not collection.about_html and not brief:
        console.print()
        console.print("[yellow]No about.md[/] — the archive will not be able to say where it came from.")
        console.print("  Write a few sentences in [bold]about.md[/] beside the documents and build again.")

    try:
        with console.status("Writing the site…", spinner="dots"):
            report = site_mod.build_site(collection, cfg, out)
    except typer.Exit:
        raise
    except Exception as exc:  # the other half of the build, diagnosable too
        _die(f"The build failed while writing the site.\n  {exc}")

    elapsed = time.perf_counter() - started
    if brief:
        return _brief(collection, cache, findings, unchecked)
    _report(collection, report, findings, unchecked, out, elapsed, cache)
    return f"{collection.stats.pages:,} pages"


def _brief(collection, cache: cache_mod.PageCache, findings, unchecked) -> str:
    """One line per rebuild in --watch: what was read, and what was reused.

    Plain text, deliberately. What this returns is not printed here: the
    watcher puts a clock in front of it and an elapsed time after it, and hands
    the whole line to an `emit` that escapes it - which it has to, because a
    timestamp is square brackets and a file name is allowed to be. Rich markup
    in here reaches the terminal as the literal characters `[red]`.
    """
    parts = [f"{collection.stats.pages:,} pages"]
    if cache.enabled and (cache.hits or cache.misses):
        parts.append(f"{cache.hits:,} cached, {cache.misses:,} read")
    if findings:
        parts.append(f"{len(findings)} page(s) leaking")
    if unchecked:
        parts.append(f"{len(unchecked)} unchecked")
    return ", ".join(parts)


def _page_ceiling(pages: int, i_know: bool) -> None:
    """Refuse a collection past the size the search index stands behind.

    ARCHITECTURE.md and ``build/search.py`` have both promised this flag since
    the beginning, and neither the flag nor the check existed. A limit that is
    documented and unenforced is worse than one that is neither, because the
    reader who acted on the documentation is the one who gets the slow archive.

    Handed to :func:`stackroom.pipeline.build_collection` as ``on_counted``, so
    it runs while discovery still has nothing but a page count and stops the
    build before anything is rendered. Refusing a collection is cheap; refusing
    it after reading it is not.
    """
    if pages <= search_mod.DEGRADED_PAGES or i_know:
        return
    _die(
        f"{pages:,} pages is past what stackroom stands behind "
        f"({search_mod.DEGRADED_PAGES:,}).",
        "Split the collection, or pass --i-know: search still works, but readers "
        f"download about {search_mod.estimate_cold_start(pages) // 1024:,} KB "
        "before they can type and common queries take seconds.",
    )


def _check_source_date_epoch() -> None:
    """Refuse a ``SOURCE_DATE_EPOCH`` that is not a timestamp.

    Somebody who exported this asked for a build whose date does not move, and
    the whole point of asking is that two builds come out the same. Falling back
    to the clock because the value has a stray newline in it would give them the
    opposite of what they asked for, quietly, in a file they will not read.
    """
    raw = os.environ.get(model.SOURCE_DATE_EPOCH, "").strip()
    if raw and model.source_date_epoch() is None:
        _die(
            f"SOURCE_DATE_EPOCH is set to {raw!r}, which is not a Unix timestamp.",
            "It should be whole seconds since 1970, e.g. "
            "SOURCE_DATE_EPOCH=$(git log -1 --format=%ct). Unset it to use the clock.",
        )


def _load_config(
    source: Path, explicit: Path | None, announce: bool = True
) -> config_mod.Config:
    path = explicit or config_mod.find(source)
    try:
        cfg = config_mod.load(path)
    except config_mod.ConfigError as exc:
        _die(str(exc))
    if path is not None and not explicit and announce:
        # Said out loud because it is not always the file the operator meant:
        # this one decides the jurisdiction, whether originals are published,
        # and how long a subprocess may run. `find` looks above the folder it
        # was given, and it cannot tell "the collection root" from "somebody
        # else's directory" - so when the file comes from outside the folder
        # the operator named, that is a warning rather than a note.
        inside = path.parent == source or source in path.parent.parents
        if inside:
            console.print(f"[dim]Using {escape(str(path))}[/]")
        else:
            console.print(
                f"[yellow]Using {escape(str(path))}[/], which is not inside "
                f"{escape(str(source))}."
            )
            console.print("  [dim]Pass --config to choose the file yourself.[/]")
    if cfg.title == "Untitled collection":
        # The folder's own name, and a folder name can hold bytes that are not
        # valid UTF-8 just as easily as a file name can.
        stem = discover_mod.printable(source.name)
        cfg.title = stem.replace("-", " ").replace("_", " ").strip().title() or cfg.title
    if cfg.about_path is None:
        candidate = source / config_mod.ABOUT_NAME
        cfg.about_path = candidate if candidate.is_file() else None
    return cfg


def _prepare_out(out: Path, force: bool) -> None:
    marker = out / ".stackroom"
    if out.exists() and any(out.iterdir()):
        # The marker is written *before* anything else, so a build that dies
        # halfway still leaves a folder the next run recognises as its own.
        # Without it, one crash makes the output directory permanently
        # untouchable and the operator has to guess that rm -rf is safe.
        # The marker is the only file that means "stackroom wrote this".
        # manifest.json is also what a Web App Manifest is called and .nojekyll
        # is in every GitHub Pages site that has ever needed one; either alone
        # is somebody else's website, and this function empties what it claims.
        looks_ours = marker.is_file() or (
            (out / "manifest.json").is_file()
            and (out / "assets" / "stackroom.css").is_file()
        )
        if not looks_ours and not force:
            _die(
                f"{out} is not empty and was not built by stackroom.",
                "Choose an empty folder with --out, or pass --force if you are sure.",
            )
        for entry in out.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "This folder was written by stackroom and is rewritten on every build.\n"
        "Anything you add here will be deleted.\n",
        encoding="utf-8",
    )


def _ingest(
    source: Path,
    cfg: config_mod.Config,
    out: Path,
    workers: int | None,
    cache: cache_mod.PageCache | None = None,
    on_counted=None,
):
    with Progress(
        SpinnerColumn(style="dim"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=28, complete_style="cyan", finished_style="cyan"),
        TextColumn("{task.completed}/{task.total} pages"),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Reading", total=None)

        def on_event(event: pipeline.ProgressEvent) -> None:
            if event.kind == "page":
                progress.update(task, total=event.total or None, completed=event.done)
            elif event.kind == "note":
                progress.console.print(f"  [dim]{escape(event.label)}: {escape(event.detail)}[/]")

        try:
            return pipeline.build_collection(
                source,
                cfg,
                out,
                progress=on_event,
                workers=workers,
                cache=cache,
                on_counted=on_counted,
            )
        except typer.Exit:
            # `on_counted` refuses an over-large collection by raising this.
            # It is a decision, not a failure, and click's Exit is a
            # RuntimeError - so without this line the `except Exception` below
            # would swallow it and report it as a damaged document.
            raise
        except FileNotFoundError as exc:
            _die(str(exc))
        except Exception as exc:  # a build that dies must still be diagnosable
            _die(f"The build failed while reading the documents.\n  {exc}")


def _page_notes(outcomes, *, to: Console | None = None, limit: int = 12) -> None:
    """Everything ingest wanted a person to know, grouped so it is readable.

    SECURITY.md promises that ambiguous evidence is reported for a human rather
    than resolved silently, and these are that evidence: *this box has text
    under it but the render is not flat, check it by hand*, *this page could
    not be rendered, so it was never checked*, *the redaction check failed
    here*. Until now every one of them was computed and thrown away.

    Grouped by message rather than listed per page, because 400 copies of one
    sentence is not a report - and safety notes first, because the operator
    reading this is deciding whether to publish, and "we could not check page
    12" has to beat "page 12 was scanned 90 degrees out of upright".

    ``limit`` bounds the *legibility* notes only. A safety note is the same
    evidence the leak report is made of, and "... and 3 other note(s)" is not a
    way to tell somebody that one of the three was "this page was never checked
    for text under a black box". They are all printed, however many there are.
    """
    out = to or console
    # A dict rather than a list per message: a page that reports the same
    # sentence twice - an error and a warning that say the same thing, a
    # warning raised once per box - is one page, and counting it twice would
    # make "and N more" name pages that are not there.
    grouped: dict[str, dict[str, None]] = {}
    for outcome in outcomes:
        where = f"{outcome.doc_id} p{outcome.number}"
        for message in ([outcome.error] if outcome.error else []) + list(outcome.warnings):
            grouped.setdefault(message, {})[where] = None
    if not grouped:
        return

    ordered = sorted(
        ((message, list(where)) for message, where in grouped.items()),
        key=lambda kv: (not pipeline.note_is_about_safety(kv[0]), -len(kv[1]), kv[0]),
    )
    safety = [note for note in ordered if pipeline.note_is_about_safety(note[0])]
    rest = [note for note in ordered if not pipeline.note_is_about_safety(note[0])]
    out.print()
    out.print("[bold]Notes from reading the pages[/]")
    for message, pages in safety + rest[:limit]:
        shown = ", ".join(pages[:4])
        if len(pages) > 4:
            shown += f" and {len(pages) - 4} more page(s)"
        colour = "red" if pipeline.note_is_about_safety(message) else "yellow"
        out.print(f"  [{colour}]{escape(message)}[/]")
        out.print(f"    [dim]{escape(shown)}[/]")
    if len(rest) > limit:
        out.print(f"  [dim]... and {len(rest) - limit} other note(s) about these pages.[/]")


def _safety(outcomes, cfg: config_mod.Config):
    try:
        return pipeline.check_safety(outcomes, cfg)
    except pipeline.SafetyStop as stop:
        _leak_report(stop)
        # On the same stream as the report, because half of what stops a build
        # now is "we could not check this page" and the operator's next question
        # is always "which pages, and why".
        _page_notes(outcomes, to=err)
        raise typer.Exit(code=2) from None


LEAK_ROWS = 40
"""How many passages the leak report prints before it starts counting instead.

A page can hide two hundred passages and a collection can have two hundred such
pages; printing a row for each is not a report either. Forty rows is about two
screens - enough to see the shape of the problem, few enough to read - and
everything past it is accounted for in a sentence rather than dropped. ``--debug``
prints every row.
"""

LEAK_ROWS_PER_PAGE = 5
"""How many of one page's passages are shown before the rest are counted.

Without this, one page with four hundred leaks would fill the whole budget and
the other nineteen pages would never be named - and *which pages* is the first
thing the operator needs.
"""

LEAK_SHAPE = 52
"""How wide the shape column is allowed to be, in characters."""


def _leak_shape(item) -> str:
    """One passage as a shape, truncated visibly rather than quietly.

    The row beside this prints the passage's true length, so a shape cut to the
    column width with nothing to show for it puts two numbers on one line that
    disagree. The ellipsis is the difference between "this is what leaked" and
    "this is the first 52 characters of what leaked".
    """
    shape = item.redacted_repr()
    return shape if len(shape) <= LEAK_SHAPE else shape[: LEAK_SHAPE - 1] + "…"


def _leak_report(stop: pipeline.SafetyStop, *, full: bool | None = None) -> None:
    """The one message in this program allowed to take up the whole screen.

    It is also the one report in this program that must not omit evidence
    silently. An operator reads it to decide whether the files they are about
    to hand over are safe, and a table that prints four rows under the sentence
    "5 passage(s) ... are covered by a black box but still readable" has told
    them the collection is one passage better than it is. So: the table is
    still bounded - a page with two hundred leaks does not get two hundred rows
    - but every passage past the bound is counted, in place, and the report
    says how to see the rest.

    ``full`` overrides ``--debug`` for the caller's own reasons; left alone, it
    is ``--debug``, which is where the unabridged list belongs. It is already
    the flag for "print everything you know about this run", the report goes to
    stderr so ``2> leaks.txt`` catches all of it, and putting the full list
    behind a flag beats a new one nobody would know to reach for.
    """
    full = _debug if full is None else full
    findings = stop.findings
    pages = len(findings)
    passages = sum(len(hidden) for _, _, hidden in findings)

    err.print()
    err.print("[bold red]Stopped: this collection would publish a failed redaction.[/]")
    err.print()
    err.print(escape(str(stop)))
    err.print()
    err.print("On these pages a black box was drawn over text that is still in the file.")
    err.print("Anyone who downloads the original can select it, copy it, or read it with")
    err.print("one command. Publishing as-is would expose exactly what someone tried to remove.")
    err.print()

    # A stop with nothing to list is a stop for pages that could not be checked
    # at all, and `str(stop)` above has already said so. An empty table under
    # "every one of the 0 passage(s)" would be a report of nothing.
    if not findings:
        _leak_advice()
        return

    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("Document", style="bold")
    table.add_column("Page", justify="right")
    table.add_column("Length", justify="right")
    table.add_column("Shape", style="dim")

    rows = 0
    shown_pages = 0
    shown_passages = 0
    for doc_id, number, hidden in findings:
        if not full and rows >= LEAK_ROWS:
            break
        take = len(hidden)
        if not full:
            take = min(take, LEAK_ROWS_PER_PAGE, LEAK_ROWS - rows)
        for item in hidden[:take]:
            table.add_row(escape(doc_id), str(number), f"{len(item.text):,}", escape(_leak_shape(item)))
            rows += 1
        shown_pages += 1
        shown_passages += take
        # The rest of *this* page, counted on the page it belongs to. A total
        # at the bottom would not say which page the operator has not seen.
        if take < len(hidden):
            table.add_row(
                escape(doc_id),
                str(number),
                "",
                f"… and {len(hidden) - take:,} more passage(s) on this page",
            )
            rows += 1
    err.print(table)

    if shown_passages < passages or shown_pages < pages:
        where = f"{shown_passages:,} of {passages:,} passage(s)"
        if shown_pages < pages:
            where += f", on {shown_pages:,} of {pages:,} page(s)"
        err.print(f"  [bold]Listed above: {where}.[/]")
        err.print("  [dim]--debug lists every one; this goes to stderr, so `2> leaks.txt` keeps it.[/]")
    else:
        err.print(
            f"  [dim]That is every one of the {passages:,} passage(s), "
            f"on all {pages:,} page(s).[/]"
        )
    _leak_advice()


def _leak_advice() -> None:
    """What to do about it - the half of the report that is the same either way."""
    err.print()
    err.print("[bold]The recovered text is not shown, and was never written to disk.[/]")
    err.print()
    err.print("What to do:")
    err.print("  1. Go back to whoever produced these files and ask for a corrected release.")
    err.print("  2. Or redact them properly yourself — the text must be [bold]removed[/], not covered.")
    err.print("     A black rectangle drawn over words in a PDF hides nothing.")
    err.print()
    err.print(
        "[dim]If you have decided to publish anyway - for instance because the text\n"
        "underneath is already public - pass --unsafe-publish-leaks. Stackroom will\n"
        "still keep the recovered text out of the site, but the original file you\n"
        "publish will still contain it.[/]"
    )
    err.print()


def _explain_a_cold_cache(cache) -> None:
    """Why nothing came back from a cache that is not empty.

    "0 of 16 pages came from the cache" reads like a broken cache and is almost
    always a moved key: a page's key covers the file's bytes, the job, and every
    version that can change what a page comes out as, so a new Tesseract, a new
    Poppler, or - in a working tree - one edited line of Stackroom's own source
    misses every entry at once. That is the cache being careful, and it should
    say so rather than leaving an operator to guess.

    ``cache.py`` stamps each layout directory with the environment its entries
    were last written with - :data:`cache.ENV_STAMP`, written once per build and
    never consulted by a key - so this can usually name the field that moved:
    *tesseract moved from 5.3.3 to 5.3.4*, and the operator stops guessing. Two
    cases it cannot: a cache written before that file existed, and one where
    nothing in the environment changed at all, which means the miss was the
    documents or the job rather than the setup. Both fall back to printing this
    build's fingerprint, which is what this said before and is still better than
    silence.
    """
    if cache is None or not cache.enabled or cache.hits or not cache.misses:
        return
    stats = cache.stats()
    if not stats.entries:
        console.print("  [dim]The cache was empty, so every page was read from the file.[/]")
        return
    console.print(
        f"  [dim]Nothing matched: the cache holds {stats.entries:,} page(s) from earlier "
        "builds, but none of them was written by this exact setup, so every key missed.[/]"
    )
    moved = cache.miss_reason()
    if moved:
        console.print(f"  [dim]Since this cache was last written, {escape(moved)}.[/]")
        return
    env = cache.env.as_dict() if cache.env is not None else {}
    named = ", ".join(
        f"{name} {env[name]}"
        for name in ("stackroom", "source", "poppler", "tesseract", "pdfminer", "numpy")
        if env.get(name)
    )
    if named:
        console.print(f"  [dim]This build's fingerprint: {escape(named)}.[/]")
    if str(env.get("source", "")).startswith("src:"):
        console.print(
            "  [dim]That 'source' digest covers every .py file in the package, because this "
            "is a working tree: editing one line of Stackroom moves it, and that is deliberate.[/]"
        )


def _report(
    collection, report, findings, unchecked, out: Path, seconds: float, cache=None
) -> None:
    stats = collection.stats
    console.print()
    console.print(f"[bold]{escape(collection.title)}[/]")
    console.print(
        f"  {stats.documents} document{'' if stats.documents == 1 else 's'}, "
        f"{stats.pages:,} pages, read in {seconds:.0f}s"
    )
    if stats.pages_with_redactions:
        # Both figures, because neither carries the meaning on its own: one page
        # withheld in full out of a thousand is 100% of that page and 0.1% of
        # the release, and an operator about to be quoted needs to know which
        # number they are looking at. The first is the one on the front page.
        console.print(
            f"  {stats.pages_with_redactions:,} of {stats.pages:,} pages carry redactions; "
            f"{stats.redaction_ratio * 100:.1f}% of the content on those pages is withheld"
        )
        console.print(
            f"  [dim]that is {stats.redaction_ratio_collection * 100:.1f}% of the content in "
            "the whole release, measured as redacted area over inked area[/]"
        )
        if stats.unmeasured_pages:
            console.print(
                f"  [dim]{stats.unmeasured_pages:,} page(s) have no measurable content - no "
                "text and no boxes - so they are in neither figure[/]"
            )
        if stats.exemption_counts:
            # Three codes, and a count of the ones that did not fit. A release
            # citing nine exemptions and showing three of them, with nothing
            # saying so, reads as a release that cites three.
            top = list(stats.exemption_counts.items())[:3]
            rest = len(stats.exemption_counts) - len(top)
            console.print(
                "  Exemptions cited: "
                + ", ".join(f"{code} ({count})" for code, count in top)
                + (f", and {rest} other code(s)" if rest else "")
            )
    if stats.unreadable_pages:
        console.print(
            f"  [yellow]{stats.unreadable_pages:,} pages could not be read[/] — "
            "search will not find anything on them"
        )
    gaps = sum(len(d.bates_gaps) for d in collection.documents)
    if gaps:
        console.print(f"  [yellow]{gaps} gap{'' if gaps == 1 else 's'} in the page numbering[/] — pages withheld in full")
    if findings:
        console.print(
            f"  [red]{len(findings)} page(s) contain text under a black box[/] — "
            "published anyway, because you asked"
        )
    if unchecked:
        console.print(f"  [yellow]{len(unchecked)} page(s) could not be checked for hidden text[/]")

    if cache is not None:
        line = cache.summary()
        if line:
            console.print(f"  [dim]{escape(line)}[/]")
        _explain_a_cold_cache(cache)
        for warning in cache.warnings:
            console.print(f"  [yellow]{escape(warning)}[/]")

    console.print()
    total = report.bytes_written + report.media_bytes + report.originals_bytes
    console.print(f"  Wrote {report.files_written:,} files, {site_mod.human_bytes(total)}, to [bold]{escape(str(out))}[/]")
    if report.search:
        info = report.search
        if info.pages_indexed:
            console.print(
                f"  Search covers {info.pages_indexed:,} pages; "
                f"readers download {site_mod.human_bytes(info.cold_start_bytes)} before they can type"
            )
        for warning in info.warnings:
            console.print(f"  [yellow]{escape(warning)}[/]")
    for warning in report.warnings or []:
        console.print(f"  [yellow]{escape(warning)}[/]")

    console.print()
    console.print(f"  Preview it with [bold]stackroom serve {escape(str(out))}[/]")
    console.print()


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


@app.command()
def check(
    source: Path = typer.Argument(Path("."), help="Folder of documents to inspect."),
    config: Path | None = typer.Option(None, "--config", "-c"),
    workers: int | None = typer.Option(None, "--workers", "-j"),
    scratch: Path | None = typer.Option(
        None,
        "--scratch",
        help="Where to render the page images. Default: the system temporary folder.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="On failure, print the traceback and what is installed here - for an issue.",
    ),
) -> None:
    """Look for failed redactions without building anything.

    Run this before you send documents anywhere, not only before you publish
    them. It reads the files, renders each page, and reports any text that a
    black box covers but does not remove.

    It writes no site - but it does have to rasterise every page to look at the
    pixels, and those images go to a temporary folder that is deleted when the
    command finishes. The path is printed below; pass --scratch to put it on a
    ramdisk if these documents must not touch a disk at all.
    """
    import tempfile

    _set_debug(debug)
    source = source.expanduser().resolve()
    cfg = _load_config(source, config)
    cfg.search.enabled = False

    parent = scratch.expanduser().resolve() if scratch else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="stackroom-check-", dir=str(parent) if parent else None
    ) as tmp:
        # Said out loud, every time. "check builds nothing and writes nothing"
        # was never true - it renders every page - and somebody deciding whether
        # to run this on a document that must not touch disk needs the path, not
        # a reassurance.
        console.print(f"[dim]Rendering pages into {escape(tmp)} (deleted when this finishes).[/]")
        # Deliberately no cache. This command promises that the pages it renders
        # live in the folder named above and are deleted when it finishes, and a
        # cache would keep copies of them somewhere else - which is the opposite
        # of what somebody passing --scratch /ramdisk is asking for. It is also
        # the command whose whole job is to look at the file, so it looks.
        collection, outcomes = _ingest(source, cfg, Path(tmp), workers)

    leaking = [o for o in outcomes if o.hidden]
    unchecked = [o for o in outcomes if o.analysis_failed]

    console.print()
    if not leaking and not unchecked:
        console.print(
            f"[green]Clear.[/] {collection.stats.pages:,} pages checked; no text found under any "
            "black box."
        )
        _page_notes(outcomes)
        console.print()
        return

    if leaking:
        _leak_report(
            pipeline.SafetyStop(
                f"{sum(len(o.hidden) for o in leaking)} passage(s) on {len(leaking)} page(s).",
                [(o.doc_id, o.number, o.hidden) for o in leaking],
            )
        )
    if unchecked:
        console.print(
            f"[yellow]{len(unchecked)} page(s) could not be checked at all.[/] "
            "That is not a clean bill of health."
        )
    # Last, and always: this is the command whose whole job is to tell an
    # operator what it could not vouch for.
    _page_notes(outcomes)
    raise typer.Exit(code=2)


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------


@app.command()
def compare(
    old: Path = typer.Argument(..., help="The earlier release."),
    new: Path = typer.Argument(..., help="The release you are publishing."),
    out: Path = typer.Option(Path("site"), "--out", "-o", help="Where to write the site."),
    config: Path | None = typer.Option(None, "--config", "-c", help="A stackroom.toml to use."),
    title: str | None = typer.Option(None, "--title", help="Override the collection title."),
    old_label: str | None = typer.Option(
        None, "--old-label", help="What to call the earlier release on the page, e.g. 'the 2019 release'."
    ),
    new_label: str | None = typer.Option(None, "--new-label", help="What to call this one."),
    workers: int | None = typer.Option(None, "--workers", "-j", help="Parallel workers."),
    no_search: bool = typer.Option(False, "--no-search", help="Skip building the search index."),
    no_old_scans: bool = typer.Option(
        False,
        "--no-old-scans",
        help="Do not publish thumbnails of the earlier release's changed pages.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite a non-empty output folder."),
    unsafe_publish_leaks: bool = typer.Option(
        False,
        "--unsafe-publish-leaks",
        help="Publish even if a redaction failed to remove its text. Read the warning first.",
    ),
    i_know: bool = typer.Option(False, "--i-know", help="Build past the page ceiling."),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="On failure, print the traceback and what is installed here - for an issue.",
    ),
) -> None:
    """Publish a release, and say what changed since an earlier one.

    Reads both folders, works out which page of the new release is which page
    of the old one, and writes the new release as an archive with a `compare/`
    section reporting what was disclosed, what was withheld, and what moved.

    It publishes text from *both* folders: a passage the earlier release printed
    and this one covers is quoted from the earlier one. Only point this at
    documents you are willing to publish.

    docs/COMPARING.md is the long version, including how it can be wrong.
    """
    _set_debug(debug)
    started = time.perf_counter()
    old = old.expanduser().resolve()
    new = new.expanduser().resolve()
    out = out.expanduser().resolve()
    _check_source_date_epoch()

    for folder, which in ((old, "earlier"), (new, "new")):
        if not folder.exists():
            _die(
                f"There is nothing at {folder}.",
                f"The {which} release should be a folder of documents, "
                "e.g. stackroom compare ./release-2019 ./release-2024",
            )
    if old == new:
        _die(
            "Those are the same folder.",
            "Point compare at two productions of the same documents.",
        )

    cfg = _load_config(new, config)
    old_cfg = _load_config(old, config) if config is None else cfg
    if title:
        cfg.title = title
    if no_search:
        cfg.search.enabled = False
    if unsafe_publish_leaks:
        cfg.safety.hidden_text = "warn"
        old_cfg.safety.hidden_text = "warn"

    _prepare_out(out, force)

    seen: list = []

    def remember(label: str, outcomes) -> None:
        console.print(f"[dim]Read {escape(label)}: {len(outcomes)} pages[/]")
        seen.extend(outcomes)

    try:
        with Progress(
            SpinnerColumn(style="dim"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=28, complete_style="cyan", finished_style="cyan"),
            TextColumn("{task.completed}/{task.total} pages"),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Reading both releases", total=None)

            def on_event(event: pipeline.ProgressEvent) -> None:
                if event.kind == "page":
                    progress.update(task, total=event.total or None, completed=event.done)
                elif event.kind == "note":
                    progress.console.print(
                        f"  [dim]{escape(event.label)}: {escape(event.detail)}[/]"
                    )

            comparison, report = compare_mod.run_comparison(
                old,
                new,
                out,
                cfg,
                old_cfg=old_cfg,
                old_label=old_label or old.name,
                new_label=new_label or new.name,
                workers=workers,
                progress=on_event,
                on_ingest=remember,
                # The same ceiling `build` enforces, on both releases, before
                # either is rendered. `--i-know` has been on this command since
                # it was written and did nothing: a comparison reads two
                # collections and publishes one of them as an ordinary archive,
                # with the same search index a `build` of it would have.
                on_counted=lambda pages: _page_ceiling(pages, i_know),
                publish_old_scans=not no_old_scans,
            )
    except pipeline.SafetyStop as stop:
        _leak_report(stop)
        _page_notes(seen, to=err)
        raise typer.Exit(code=2) from None
    except FileNotFoundError as exc:
        _die(str(exc))

    _page_notes(seen)
    _compare_report(comparison, report, out, time.perf_counter() - started)


def _compare_report(comparison, report, out: Path, seconds: float) -> None:
    """What changed, in the order a person cares about it.

    Deliberately not `_report`: that one summarises an archive, and the thing
    the operator ran this command to find out is not how many pages there are.
    """
    console.print()
    console.print(
        f"[bold]{escape(comparison.old.label)}[/] → [bold]{escape(comparison.new.label)}[/]"
    )
    if not comparison.anything:
        console.print("  Nothing changed: same documents, same pages, no black box moved.")
        return
    if comparison.disclosed:
        console.print(
            f"  [bold cyan]{comparison.disclosed} passage(s) newly disclosed[/] — "
            "under a black box before, readable now"
        )
    if comparison.withheld:
        console.print(
            f"  [bold]{comparison.withheld} passage(s) newly withheld[/] — "
            "published before, covered now"
        )
    console.print(
        f"  {comparison.lifted} redaction(s) lifted, {comparison.imposed} imposed; "
        f"{comparison.pages_added} page(s) added, {comparison.pages_removed} gone"
    )
    unaligned = [d for d in comparison.documents if not d.alignment.aligned]
    if unaligned:
        console.print(
            f"  [yellow]{len(unaligned)} document(s) could not be aligned[/] — "
            "nothing is claimed about them"
        )
    if comparison.unpaired_old or comparison.unpaired_new:
        console.print(
            f"  [yellow]{len(comparison.unpaired_old) + len(comparison.unpaired_new)} "
            "document(s) had no counterpart in the other release[/]"
        )
    weak = sum(d.unusable_pairs for d in comparison.documents)
    if weak:
        console.print(f"  [dim]{weak} page pair(s) matched too weakly to compare[/]")
    console.print(
        f"  [dim]{comparison.noise_tokens:,} word(s) read differently by the two "
        "recognitions and claimed as nothing[/]"
    )
    console.print("  [dim]How this can be wrong: docs/COMPARING.md, and compare/index.html[/]")

    console.print()
    console.print(
        f"  {comparison.new.documents} document(s), {comparison.new.pages:,} pages read "
        f"in {seconds:.0f}s"
    )
    total = report.bytes_written + report.media_bytes + report.originals_bytes
    console.print(
        f"  Wrote {report.files_written:,} files, {site_mod.human_bytes(total)}, "
        f"to [bold]{escape(str(out))}[/]"
    )
    for warning in report.warnings or []:
        console.print(f"  [yellow]{escape(warning)}[/]")
    console.print()
    console.print(f"  Preview it with [bold]stackroom serve {escape(str(out))}[/]")
    console.print()


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

cache_app = typer.Typer(
    help="Show, prune or empty the page cache.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(cache_app, name="cache")

_CACHE_DIR = typer.Option(None, "--cache-dir", help="A cache other than the default one.")


@cache_app.callback(invoke_without_command=True)
def cache_main(ctx: typer.Context, cache_dir: Path | None = _CACHE_DIR) -> None:
    """Show what the page cache is holding."""
    if ctx.invoked_subcommand is None:
        _cache_show(cache_dir)


@cache_app.command("show")
def cache_show(cache_dir: Path | None = _CACHE_DIR) -> None:
    """What is in the cache, and where."""
    _cache_show(cache_dir)


@cache_app.command("path")
def cache_path(
    cache_dir: Path | None = _CACHE_DIR,
    entries: bool = typer.Option(
        False, "--entries", help="Print the entry directory inside it instead."
    ),
) -> None:
    """Print the cache directory and nothing else, for scripts.

    This is the directory --cache-dir takes, and the one `stackroom cache`
    shows as "Where". Feeding it back in reopens the cache you had:

        stackroom build ./release --cache-dir "$(stackroom cache path)"

    It used to print the entry directory inside it, <base>/pages/<layout>,
    which is the one path here that cannot be fed back in - --cache-dir pointed
    at it opens a second cache nested inside the first, silently, and every
    page misses. Pass --entries to ask for that path by name.
    """
    print(cache_mod.cache_root(cache_dir) if entries else cache_mod.base_dir(cache_dir))


@cache_app.command("prune")
def cache_prune(
    cache_dir: Path | None = _CACHE_DIR,
    max_size: str | None = typer.Option(None, "--max", help="Trim to this size, e.g. 2GB."),
) -> None:
    """Evict least-recently-used pages until the cache is inside its limit."""
    cache = _open_cache(cache_dir, max_size, True)
    report = cache.prune(cache_mod.parse_size(max_size) if max_size else None)
    console.print()
    console.print(
        f"  Removed {report.entries_removed:,} page(s) and {report.blobs_removed:,} image(s), "
        f"freeing {cache_mod.human_bytes(report.bytes_removed)}."
    )
    if report.errors:
        console.print(f"  [yellow]{report.errors} file(s) could not be removed.[/]")
    console.print()


@cache_app.command("clear")
def cache_clear(
    cache_dir: Path | None = _CACHE_DIR,
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask."),
) -> None:
    """Delete everything in the cache.

    Run this before you hand a machine on, and after you finish with documents
    that are not yours to keep. The cache holds rendered images of every page
    you have built or previewed.
    """
    cache = _open_cache(cache_dir, None, True)
    stats = cache.stats()
    console.print()
    if not stats.exists or not (stats.entries or stats.blobs):
        console.print(f"  Nothing to clear in {escape(str(cache.base))}.")
        console.print()
        return
    console.print(
        f"  This will delete {stats.entries:,} cached page(s) and {stats.blobs:,} page "
        f"image(s), {cache_mod.human_bytes(stats.bytes)}, from"
    )
    console.print(f"  [bold]{escape(str(cache.base))}[/]")
    if not yes and not typer.confirm("  Delete them?", default=False):
        console.print("  Left alone.")
        console.print()
        return
    report = cache.clear()
    console.print(f"  Deleted {cache_mod.human_bytes(report.bytes_removed)}.")
    if report.errors:
        console.print(f"  [yellow]{report.errors} file(s) could not be removed.[/]")
    console.print()


def _cache_show(cache_dir: Path | None) -> None:
    cache = _open_cache(cache_dir, None, True)
    stats = cache.stats()
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("", style="dim")
    table.add_column("", overflow="fold")
    # Both paths, labelled, because they are not the same one and only the
    # first can be handed back to --cache-dir. `stackroom cache path` prints
    # that first one; `--entries` prints the second.
    table.add_row("Where", str(cache.base))
    table.add_row("Entries in", str(cache.root))
    table.add_row("Pages", f"{stats.entries:,}")
    table.add_row("Page images", f"{stats.blobs:,}")
    table.add_row(
        "Size",
        f"{cache_mod.human_bytes(stats.bytes)} of "
        f"{cache_mod.human_bytes(stats.max_bytes)} ({stats.full * 100:.0f}%)",
    )
    if stats.newest:
        table.add_row("Last used", time.strftime("%Y-%m-%d %H:%M", time.localtime(stats.newest)))
    if not stats.writable:
        table.add_row("Writable", "[yellow]no[/]")
    console.print()
    console.print(table)
    console.print()
    console.print(
        "  [dim]This holds rendered images of pages from documents built on this machine.[/]"
    )
    console.print(
        "  [dim]Treat it as carefully as you treat them; delete it with[/] "
        "[bold]stackroom cache clear[/][dim].[/]"
    )
    console.print(
        "  [dim]Text found underneath a black box is never written here, or anywhere.[/]"
    )
    console.print()


# --------------------------------------------------------------------------
# serve, init, doctor
# --------------------------------------------------------------------------


@app.command()
def serve(
    directory: Path = typer.Argument(Path("site"), help="A built site."),
    port: int = typer.Option(8000, "--port", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
    open_browser: bool = typer.Option(False, "--open"),
) -> None:
    """Preview a built archive in a browser."""
    from .serve import serve as run_server

    try:
        run_server(directory.expanduser().resolve(), host=host, port=port, open_browser=open_browser)
    except Exception as exc:
        _die(str(exc))


@app.command()
def init(
    directory: Path = typer.Argument(Path("."), help="Where the documents are."),
) -> None:
    """Write a stackroom.toml and an about.md to fill in."""
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    cfg_path = directory / config_mod.CONFIG_NAME
    if cfg_path.exists():
        console.print(f"[dim]{config_mod.CONFIG_NAME} is already there; leaving it alone.[/]")
    else:
        title = directory.name.replace("-", " ").replace("_", " ").strip().title() or "Untitled collection"
        cfg_path.write_text(config_mod.TEMPLATE.format(title=title), encoding="utf-8")
        made.append(config_mod.CONFIG_NAME)

    about_path = directory / config_mod.ABOUT_NAME
    if about_path.exists():
        console.print(f"[dim]{config_mod.ABOUT_NAME} is already there; leaving it alone.[/]")
    else:
        about_path.write_text(config_mod.ABOUT_TEMPLATE, encoding="utf-8")
        made.append(config_mod.ABOUT_NAME)

    console.print()
    if made:
        console.print("Wrote " + " and ".join(f"[bold]{m}[/]" for m in made) + f" in {directory}.")
    console.print("Fill in [bold]about.md[/] — it is the part readers use to decide whether to trust this.")
    console.print(f"Then run [bold]stackroom build {directory}[/].")
    console.print()


@app.command()
def doctor() -> None:
    """Check that everything this needs is installed."""
    table = Table(box=None, pad_edge=False)
    table.add_column("")
    table.add_column("")
    table.add_column("", style="dim")

    ok = True

    def row(name: str, good: bool, detail: str, fix: str = "") -> None:
        nonlocal ok
        ok = ok and good
        table.add_row("[green]✓[/]" if good else "[red]✗[/]", name, detail if good else (fix or detail))

    poppler = shutil.which("pdftoppm")
    row(
        "poppler (pdftoppm)",
        bool(poppler),
        poppler or "",
        "not found — install poppler-utils (apt) or poppler (brew)",
    )

    try:
        from .ingest import ocr as ocr_mod

        langs = ocr_mod.available_languages()
        row("tesseract", True, f"{ocr_mod.tesseract_version()}, {len(langs)} language(s)")
        row("  languages", True, ", ".join(sorted(langs)[:12]))
    except Exception as exc:
        row("tesseract", False, "", f"not usable — {exc}")

    usable, detail = search_mod.pagefind_available()
    row(
        "pagefind",
        usable,
        detail,
        "not found — run: pip install pagefind   (without it, the archive builds but has no search)",
    )

    try:
        from PIL import features

        row("AVIF encoding", bool(features.check("avif")), "images will be ~31% smaller",
            "not available — sites will use WebP only, which is fine but larger")
    except Exception:
        row("AVIF encoding", False, "", "could not be determined")

    console.print()
    console.print(table)
    console.print()
    if ok:
        console.print("[green]Everything Stackroom needs is here.[/]")
    else:
        console.print("Stackroom will still run, but the items marked ✗ limit what it can do.")
    console.print()


# --------------------------------------------------------------------------


def _die(message: str, hint: str = "") -> None:
    """Stop with one line a person can act on - and, on --debug, everything else.

    The one line is the whole message when things are working as intended: a
    folder that is not there, a collection past the ceiling. It is *not* enough
    when the cause is a document nobody has seen before, which is the report
    ``CONTRIBUTING.md`` most wants and the one an operator cannot produce
    without a traceback. So when an exception is being handled, the message
    ends by naming ``--debug``, and ``--debug`` prints the rest.
    """
    handling = sys.exc_info()[1] is not None
    err.print()
    err.print(f"[red]{escape(message)}[/]")
    if hint:
        err.print(f"  {escape(hint)}")
    if handling and not _debug:
        err.print(f"  [dim]{escape(DEBUG_HINT)}[/]")
    err.print()
    if _debug:
        _debug_report(err)
    raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
