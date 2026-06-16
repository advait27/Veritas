"""Run the eval cases through the real pipeline and score recovery vs. false discovery.

Each case is scored end-to-end on the deterministic spine: build the dataset, ingest it
(M1), run the discovery pass (M4) with its full suppression, and compare the surfaced
discoveries to the planted causes. Two numbers matter, mirroring the README's promise:
the *root-cause recovery rate* (planted causes that surfaced) and the *false-discovery
rate* (surfaced discoveries that were not planted). A case passes only when it recovers
every planted cause and surfaces nothing spurious — for the no-signal case that means
staying completely silent.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, Field

from veritas.discovery import discover
from veritas.evals.cases import CASES
from veritas.ingest import ingest_file
from veritas.session import InvestigationSession

if TYPE_CHECKING:
    from veritas.discovery import DiscoveryReport
    from veritas.evals.cases import EvalCase


class CaseResult(BaseModel):
    """The score for one eval case: what was planted, what surfaced, and whether it passed."""

    name: str
    description: str
    planted_count: int
    surfaced_count: int
    recovered_count: int
    false_discovery_count: int
    recovery_rate: float
    false_discovery_rate: float
    surfaced_pairs: list[list[str]] = Field(default_factory=list)
    missed_pairs: list[list[str]] = Field(default_factory=list)
    passed: bool


class Scorecard(BaseModel):
    """The whole suite: per-case results and the two aggregate rates, with a verdict."""

    cases: list[CaseResult]
    root_cause_recovery_rate: float
    false_discovery_rate: float
    passed: bool


def score_case(case: EvalCase, report: DiscoveryReport) -> CaseResult:
    """Score one case's discovery report against its planted causes.

    Args:
        case: the eval case (carrying the planted column pairs).
        report: the discovery report produced for the case's dataset.

    Returns:
        A :class:`CaseResult`; ``passed`` is true only when every planted cause was
        recovered and no unplanted discovery surfaced.

    Example:
        ``score_case(case, report).passed``
    """
    surfaced = [frozenset(discovery.columns) for discovery in report.discoveries]
    recovered = case.planted & set(surfaced)
    false_count = sum(1 for pair in surfaced if pair not in case.planted)
    recovery_rate = 1.0 if not case.planted else len(recovered) / len(case.planted)
    fdr = false_count / len(surfaced) if surfaced else 0.0
    return CaseResult(
        name=case.name,
        description=case.description,
        planted_count=len(case.planted),
        surfaced_count=len(surfaced),
        recovered_count=len(recovered),
        false_discovery_count=false_count,
        recovery_rate=recovery_rate,
        false_discovery_rate=fdr,
        surfaced_pairs=[sorted(pair) for pair in surfaced],
        missed_pairs=[sorted(pair) for pair in (case.planted - set(surfaced))],
        passed=len(recovered) == len(case.planted) and false_count == 0,
    )


def run_case(session: InvestigationSession, case: EvalCase, workdir: Path) -> CaseResult:
    """Build, ingest, and discover one case, returning its score.

    Args:
        session: the session to ingest into and discover within.
        case: the eval case to run.
        workdir: a directory to write the case's CSV into.

    Returns:
        The :class:`CaseResult` for this case.

    Example:
        ``run_case(session, CASES[0], tmp_path).passed``
    """
    frame = case.build(np.random.default_rng(case.seed))
    path = workdir / f"{case.name}.csv"
    frame.to_csv(path, index=False)
    dataset_id = ingest_file(session, path, name=case.name).dataset_id
    report = discover(session, dataset_id)
    return score_case(case, report)


def run_suite(cases: tuple[EvalCase, ...] = CASES) -> Scorecard:
    """Run every eval case in a throwaway session and return the aggregate scorecard.

    Args:
        cases: the cases to run (defaults to the full built-in suite).

    Returns:
        A :class:`Scorecard` with per-case results and the two aggregate rates.

    Example:
        ``run_suite().passed``
    """
    with tempfile.TemporaryDirectory(prefix="veritas_evals_") as tmp_name:
        tmp = Path(tmp_name)
        with InvestigationSession(base_dir=tmp / "sessions") as session:
            results = [run_case(session, case, tmp) for case in cases]
    return _aggregate(results)


def _aggregate(results: list[CaseResult]) -> Scorecard:
    """Roll per-case results into the suite-level scorecard."""
    total_planted = sum(result.planted_count for result in results)
    total_recovered = sum(result.recovered_count for result in results)
    total_surfaced = sum(result.surfaced_count for result in results)
    total_false = sum(result.false_discovery_count for result in results)
    return Scorecard(
        cases=results,
        root_cause_recovery_rate=1.0 if not total_planted else total_recovered / total_planted,
        false_discovery_rate=total_false / total_surfaced if total_surfaced else 0.0,
        passed=all(result.passed for result in results),
    )


def format_scorecard(scorecard: Scorecard) -> str:
    """Render a scorecard as a human-readable markdown table.

    Example:
        ``print(format_scorecard(run_suite()))``
    """
    lines = [
        "# Veritas eval scorecard",
        "",
        f"- root-cause recovery rate: {scorecard.root_cause_recovery_rate:.0%}",
        f"- false-discovery rate: {scorecard.false_discovery_rate:.0%}",
        f"- verdict: {'PASS' if scorecard.passed else 'FAIL'}",
        "",
        "| case | planted | surfaced | recovered | false | result |",
        "| - | - | - | - | - | - |",
    ]
    lines.extend(
        f"| {result.name} | {result.planted_count} | {result.surfaced_count} "
        f"| {result.recovered_count} | {result.false_discovery_count} "
        f"| {'PASS' if result.passed else 'FAIL'} |"
        for result in scorecard.cases
    )
    return "\n".join(lines)
