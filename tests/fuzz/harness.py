#!/usr/bin/env python3
"""Run the ingest pipeline over hostile documents and check three things only.

1. **It never hangs.** Each case runs in a child process under a wall-clock
   budget; a case that has to be killed is a failure, not a slow test.
2. **It never crashes with an unhandled traceback.** The pipeline's own
   contract is that one bad page comes back with ``error`` set rather than
   raising, and that the CLI turns the remaining failures into a message. An
   exception type this harness does not recognise means neither happened.
3. **It never writes outside the output directory.** The input folder, the
   working directory and a tree of canary files are hashed before and after.

One safety property is checked as well, because it is a safety property rather
than an accuracy one: text recovered from under a failed redaction must not
appear in anything Stackroom *generated*. ``files/`` is excluded, because the
original is published byte for byte by design.

Nothing about *correctness* is asserted. Whether a redaction was found, whether
the text was read, whether the ratio is right - those are
``tests/test_pipeline.py``'s and ``tests/test_redaction.py``'s business, and a
fuzz harness that also asserted them would be red for reasons nobody could
triage.

Usage
-----
::

    python tests/fuzz/harness.py --list
    python tests/fuzz/harness.py                     # every hazard, once
    python tests/fuzz/harness.py --skip-slow         # ... minus the costly ones
    python tests/fuzz/harness.py --cases 200 --seed 7
    python tests/fuzz/harness.py --seconds 1800      # soak until the clock runs out
    python tests/fuzz/harness.py --case cropbox-mismatch --keep

Every failure prints the case name and the seed, which is all that is needed to
reproduce it, and ``--keep`` leaves the corpus and the output on disk. The exit
status is 1 if anything failed, so it can be dropped into CI as it stands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import corpus  # noqa: E402

DEFAULT_BUDGET = 90.0
"""Seconds one ordinary case may take before it counts as a hang.

Generous on purpose. A budget tight enough to catch a slow page is also tight
enough to fire on a loaded CI machine, and a harness that cries wolf is a
harness people stop rerunning."""

SLOW_BUDGET = 600.0
"""And for the cases whose cost *is* the hazard - a 400-page document, a page of
twenty thousand rectangles. Those exist to measure throughput under a hostile
input, not to be finished quickly."""

# Exceptions the pipeline and CLI are documented to raise and to handle. Anything
# else reaching the top is the finding.
HANDLED = {
    "FileNotFoundError",   # "no readable documents under ..." - raised, caught by the CLI
    "NotADirectoryError",  # discover() on a non-directory
    "SafetyStop",          # the whole point of the tool
    "ConfigError",         # a stackroom.toml a person has to fix
    "MissingLanguageError",
    "SearchError",
}


# --------------------------------------------------------------------------
# what one run is allowed to touch
# --------------------------------------------------------------------------


def snapshot(root: Path) -> dict[str, str]:
    """Path -> digest for every file under *root*, following no symlinks."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            out[str(path.relative_to(root))] = "symlink:" + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            out[str(path.relative_to(root))] = digest
    return out


def plant_canaries(root: Path) -> None:
    """Files a traversal bug would plausibly land on, in the shapes it would use."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("canary.txt", ".stackroom", "manifest.json", "index.html"):
        (root / name).write_text(f"canary {name}\n", encoding="utf-8")
    nested = root / "etc" / "cron.d"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "job").write_text("canary\n", encoding="utf-8")


# --------------------------------------------------------------------------
# the child: one case, in its own process
# --------------------------------------------------------------------------


def run_one_in_process(case: str, folder: Path, out: Path) -> dict[str, object]:
    """Ingest *folder* into *out*. Returns a JSON-able verdict, never raises."""
    sys.path.insert(0, str(HERE.parent))  # tests/, for nothing yet but consistency
    from stackroom import pipeline
    from stackroom.build import site as site_mod
    from stackroom.config import Config, ConfigError
    from stackroom.config import find as config_find
    from stackroom.config import load as config_load

    result: dict[str, object] = {"case": case, "status": "ok"}
    try:
        found = config_find(folder)
        cfg = config_load(found) if found else Config()
        # Force the cheap path regardless of what the hostile config asked for:
        # the harness is testing the pipeline, not the encoders.
        cfg.render.dpi = 72
        cfg.render.widths = [400]
        cfg.render.thumb_width = 200
        cfg.render.formats = ["webp"]
        cfg.ocr.mode = "never"
        cfg.search.enabled = False
        cfg.safety.hidden_text = "warn"  # so a leak does not end the run early

        collection, outcomes = pipeline.build_collection(folder, cfg, out, workers=1)
        pipeline.check_safety(outcomes, cfg)
        site_mod.attach_about(collection, cfg)
        report = site_mod.build_site(collection, cfg, out)

        result["pages"] = sum(len(d.pages) for d in collection.documents)
        result["files"] = report.files_written
        result["page_errors"] = sum(1 for o in outcomes if o.error)
        result["hidden"] = sum(len(o.hidden) for o in outcomes)
        # The one correctness property worth asserting here, because it is a
        # safety property rather than an accuracy one: text recovered from under
        # a failed redaction must not appear in anything Stackroom *generated*.
        #
        # `files/` is excluded on purpose. The original is published byte for
        # byte by design - that is guarantee 2 - so of course the leak is in it.
        # What must not happen is the leak appearing in a page, a JSON payload or
        # the search index, which is the promise the CLI makes even in warn mode.
        leaked = []
        for outcome in outcomes:
            for hidden_text in outcome.hidden:
                needle = hidden_text.text.strip().encode("utf-8", "replace")
                if len(needle) < 6:
                    continue
                for path in out.rglob("*"):
                    if not path.is_file():
                        continue
                    if path.relative_to(out).parts[:1] == ("files",):
                        continue
                    if needle in path.read_bytes():
                        leaked.append(str(path.relative_to(out)))
        result["leaked_into"] = sorted(set(leaked))
    except ConfigError as exc:
        result["status"] = "handled"
        result["error"] = f"ConfigError: {exc}"
    except Exception as exc:
        name = type(exc).__name__
        result["status"] = "handled" if name in HANDLED else "unhandled"
        result["error"] = f"{name}: {exc}"
        result["traceback"] = traceback.format_exc()[-2000:]
    return result


# --------------------------------------------------------------------------
# the parent
# --------------------------------------------------------------------------


@dataclass
class Failure:
    case: str
    seed: int
    reason: str
    detail: str = ""
    where: str = ""

    def __str__(self) -> str:
        line = f"{self.case} (seed {self.seed}): {self.reason}"
        if self.where:
            line += f"\n    corpus: {self.where}"
        if self.detail:
            line += "\n    " + self.detail.strip().replace("\n", "\n    ")
        return line


@dataclass
class Report:
    ran: int = 0
    handled: int = 0
    skipped: int = 0
    failures: list[Failure] = field(default_factory=list)
    slowest: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failures


def run_case(
    name: str,
    filename: str,
    data: bytes,
    seed: int,
    *,
    budget: float = DEFAULT_BUDGET,
    keep: bool = False,
    root: Path | None = None,
) -> tuple[Failure | None, float, dict[str, object] | None]:
    """Run one case in a child process inside a jail, and judge it."""
    jail = Path(root or tempfile.mkdtemp(prefix="stackroom-fuzz-"))
    folder = jail / "release"
    out = jail / "site"
    canaries = jail / "canaries"
    work = jail / "cwd"
    work.mkdir(parents=True, exist_ok=True)
    plant_canaries(canaries)
    corpus.write_case(folder, filename, data)

    before_input = snapshot(folder)
    before_canaries = snapshot(canaries)
    before_work = snapshot(work)

    env = dict(os.environ)
    env["TMPDIR"] = str(jail / "tmp")
    (jail / "tmp").mkdir(exist_ok=True)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(HERE), str(HERE.parent), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", name,
             "--folder", str(folder), "--out", str(out)],
            capture_output=True,
            text=True,
            timeout=budget,
            cwd=str(work),
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        failure = Failure(name, seed, f"did not finish within {budget:g}s", where=str(folder))
        return failure, elapsed, None
    elapsed = time.monotonic() - started

    verdict: dict[str, object] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("VERDICT "):
            verdict = json.loads(line[len("VERDICT "):])

    failure: Failure | None = None
    if proc.returncode != 0 or verdict is None:
        failure = Failure(
            name, seed,
            f"the child exited {proc.returncode} without a verdict",
            detail=(proc.stderr or proc.stdout)[-1500:],
            where=str(folder),
        )
    elif verdict.get("status") == "unhandled":
        failure = Failure(
            name, seed,
            f"unhandled exception: {verdict.get('error')}",
            detail=str(verdict.get("traceback", "")),
            where=str(folder),
        )
    elif verdict.get("leaked_into"):
        failure = Failure(
            name, seed,
            f"recovered text was written to {verdict['leaked_into']}",
            where=str(folder),
        )
    else:
        after_input = snapshot(folder)
        after_canaries = snapshot(canaries)
        after_work = snapshot(work)
        for label, before, after in (
            ("the input folder", before_input, after_input),
            ("the canary tree", before_canaries, after_canaries),
            ("the working directory", before_work, after_work),
        ):
            if before != after:
                changed = sorted(set(before) ^ set(after)) or [
                    k for k in before if before[k] != after.get(k)
                ]
                failure = Failure(
                    name, seed, f"wrote outside the output directory: {label} changed",
                    detail=", ".join(changed[:10]), where=str(folder),
                )
                break

    if not keep and failure is None:
        shutil.rmtree(jail, ignore_errors=True)
    return failure, elapsed, verdict


def run(
    *,
    cases: int = 0,
    seed: int = 0,
    seconds: float = 0.0,
    only: str | None = None,
    budget: float = DEFAULT_BUDGET,
    slow_budget: float = SLOW_BUDGET,
    skip_slow: bool = False,
    keep: bool = False,
    quiet: bool = False,
) -> Report:
    """Run the hazard corpus, then mutations until *cases* or *seconds* is up."""
    report = Report()
    deadline = time.monotonic() + seconds if seconds else None

    def note(line: str) -> None:
        if not quiet:
            print(line, flush=True)

    for name, filename, data in corpus.hazard_cases(seed):
        if only and only != name:
            continue
        if skip_slow and name in corpus.SLOW and not only:
            report.skipped += 1
            continue
        failure, elapsed, verdict = run_case(
            name, filename, data, seed,
            budget=slow_budget if name in corpus.SLOW else budget,
            keep=keep,
        )
        report.ran += 1
        report.slowest = max(report.slowest, elapsed)
        if verdict and verdict.get("status") == "handled":
            report.handled += 1
        if failure:
            report.failures.append(failure)
            note(f"  FAIL {failure}")
        else:
            note(f"  ok   {name} ({elapsed:.1f}s, {len(data):,} bytes)")
        if deadline and time.monotonic() > deadline:
            return report

    if only:
        return report

    index = 0
    while (cases and index < cases) or (deadline and time.monotonic() < deadline):
        name, filename, data = next(corpus.mutation_cases(1, seed + index))
        failure, elapsed, verdict = run_case(
            name, filename, data, seed + index, budget=budget, keep=keep
        )
        report.ran += 1
        report.slowest = max(report.slowest, elapsed)
        if verdict and verdict.get("status") == "handled":
            report.handled += 1
        if failure:
            report.failures.append(failure)
            note(f"  FAIL {failure}")
        index += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--folder", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    parser.add_argument("--list", action="store_true", help="print the hazard names")
    parser.add_argument("--case", help="run one hazard by name")
    parser.add_argument("--cases", type=int, default=0, help="how many mutations to run")
    parser.add_argument("--seed", type=int, default=random.randrange(1 << 30))
    parser.add_argument("--seconds", type=float, default=0.0, help="soak for this long")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                        help="seconds one case may take before it counts as a hang")
    parser.add_argument("--skip-slow", action="store_true",
                        help="leave out the hazards whose cost is the point")
    parser.add_argument("--keep", action="store_true", help="leave the corpus on disk")
    args = parser.parse_args(argv)

    if args.child:
        verdict = run_one_in_process(args.child, Path(args.folder), Path(args.out))
        print("VERDICT " + json.dumps(verdict, default=str), flush=True)
        return 0

    if args.list:
        for name in corpus.HAZARDS:
            print(name)
        return 0

    print(f"seed {args.seed}")
    report = run(
        cases=args.cases,
        seed=args.seed,
        seconds=args.seconds,
        only=args.case,
        budget=args.budget,
        skip_slow=args.skip_slow,
        keep=args.keep,
    )
    print(
        f"\n{report.ran} case(s), {report.handled} refused cleanly, "
        f"{report.skipped} skipped, {len(report.failures)} failure(s), "
        f"slowest {report.slowest:.1f}s"
    )
    for failure in report.failures:
        print("\n" + str(failure))
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
