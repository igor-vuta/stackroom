"""The CI-sized run of the fuzz harness.

This file is the smoke test: every structural hazard whose cost is not the point
of it, plus a handful of byte mutations, each in its own process under a
wall-clock budget. It is meant to finish inside a minute or two on a laptop.

The soak lives outside pytest, because a soak that finishes is not a soak::

    python tests/fuzz/harness.py --seconds 1800
    python tests/fuzz/harness.py --cases 500 --seed 20240601

Both print the seed, and any failure prints the case name, the seed and the path
to the corpus it left behind, which is everything needed to reproduce it.

What is asserted here is only what ``harness.py`` asserts: no hang, no unhandled
traceback, nothing written outside the output directory, and no text recovered
from under a failed redaction appearing in anything Stackroom generated. Nothing
about whether the answers were right.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import corpus  # noqa: E402
import harness  # noqa: E402

SMOKE_SEED = 20240601
"""Fixed, so a red CI run is reproducible from the branch alone. The soak uses a
random seed; this does not."""

KNOWN_FAILURES: dict[str, str] = {}
"""Hazards this harness reproduces that are not fixed yet.

Marked ``xfail(strict=True)`` rather than deleted, so the harness stays green
until somebody fixes the defect and then goes red to say so. A smoke test that
is permanently failing gets muted, and a muted harness catches nothing.

Empty since F17 was fixed: ``discover.printable()`` now normalises undecodable
filenames where names enter the model, so ``undecodable-filename`` runs clean.
"""


def _cases() -> list:
    out = []
    for name in corpus.HAZARDS:
        if name in corpus.SLOW:
            continue
        reason = KNOWN_FAILURES.get(name)
        marks = [pytest.mark.xfail(reason=reason, strict=True)] if reason else []
        out.append(pytest.param(name, marks=marks, id=name))
    return out


@pytest.mark.parametrize("name", _cases())
def test_a_hostile_document_neither_hangs_nor_escapes(name, tmp_path):
    """One structural hazard, one child process, one jail."""
    _, filename, data = next(
        case for case in corpus.hazard_cases(SMOKE_SEED) if case[0] == name
    )
    try:
        (tmp_path / filename).touch()
        (tmp_path / filename).unlink()
    except OSError:  # pragma: no cover - depends on the host
        # APFS refuses a name that is not valid UTF-8, so the hazard cannot
        # exist on this filesystem, let alone reach the build.
        pytest.skip(f"this filesystem refuses the case's filename: {name}")
    failure, elapsed, verdict = harness.run_case(
        name, filename, data, SMOKE_SEED,
        budget=harness.DEFAULT_BUDGET, keep=True, root=tmp_path,
    )
    assert failure is None, str(failure)
    assert verdict is not None
    assert elapsed < harness.DEFAULT_BUDGET


def test_byte_mutations_of_a_valid_document(tmp_path):
    """A short mutation run: the parser's edges rather than its structure."""
    failures = []
    for index, (name, filename, data) in enumerate(
        corpus.mutation_cases(12, SMOKE_SEED)
    ):
        failure, _, _ = harness.run_case(
            name, filename, data, SMOKE_SEED + index,
            budget=harness.DEFAULT_BUDGET, keep=True, root=tmp_path / f"m{index}",
        )
        if failure:
            failures.append(str(failure))
    assert failures == [], "\n\n".join(failures)


@pytest.mark.parametrize("name", sorted(corpus.SLOW))
def test_a_hazard_whose_cost_is_the_point(name, tmp_path):
    """The expensive hazards, skipped unless asked for.

    ``pytest tests/fuzz --runslow`` is not wired up on purpose: these are run by
    ``harness.py`` in a soak, where a four-minute case is expected rather than
    surprising, and putting them in the default run would make the suite the
    kind of thing people skip.
    """
    pytest.skip(f"cost hazard; run `python tests/fuzz/harness.py --case {name}`")
