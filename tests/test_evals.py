"""Tests for the eval suite: every planted cause recovers, noise stays silent."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from veritas.discovery import Discovery, DiscoveryReport, DiscoverySummary, discover
from veritas.evals.__main__ import main
from veritas.evals.cases import CASES, EvalCase
from veritas.evals.scoring import (
    Scorecard,
    format_scorecard,
    run_case,
    run_suite,
    score_case,
)
from veritas.ingest import ingest_file

if TYPE_CHECKING:
    from pathlib import Path

    from veritas.session import InvestigationSession


def _case(name: str) -> EvalCase:
    return next(case for case in CASES if case.name == name)


def _discovery(columns: list[str]) -> Discovery:
    return Discovery(
        headline="x",
        kind="spearman",
        columns=columns,
        test="t",
        statistic=1.0,
        p_value=0.01,
        effect_metric="abs Spearman rho",
        effect_size=0.9,
        effect_label="strong",
        n=100,
        artifact_id="art_x",
    )


def _report(pairs: list[list[str]]) -> DiscoveryReport:
    summary = DiscoverySummary(
        probes_generated=len(pairs),
        probes_run=len(pairs),
        dropped_insufficient_data=0,
        dropped_probe_cap=0,
        dropped_not_significant=0,
        dropped_below_effect_floor=0,
        dropped_finding_cap=0,
        surfaced=len(pairs),
        fdr_alpha=0.1,
    )
    return DiscoveryReport(discoveries=[_discovery(pair) for pair in pairs], summary=summary)


# --- integration: each case through the real pipeline -----------------------------------


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_case_passes(session: InvestigationSession, tmp_path: Path, case: EvalCase) -> None:
    result = run_case(session, case, tmp_path)
    assert result.passed
    assert result.recovered_count == result.planted_count
    assert result.false_discovery_count == 0


def test_pure_noise_is_silent(session: InvestigationSession, tmp_path: Path) -> None:
    result = run_case(session, _case("pure_noise"), tmp_path)
    assert result.surfaced_count == 0
    assert result.recovery_rate == 1.0  # vacuous: nothing to recover


def test_tiny_effect_trap_is_floored_not_surfaced(
    session: InvestigationSession, tmp_path: Path
) -> None:
    case = _case("tiny_effect_trap")
    frame = case.build(np.random.default_rng(case.seed))
    path = tmp_path / "trap.csv"
    frame.to_csv(path, index=False)
    dataset_id = ingest_file(session, path, name=case.name).dataset_id
    report = discover(session, dataset_id)
    surfaced = {frozenset(d.columns) for d in report.discoveries}
    assert frozenset({"trap_x", "trap_y"}) not in surfaced  # the trap never surfaces
    assert report.summary.dropped_below_effect_floor >= 1  # it was caught by the floor


# --- aggregate scorecard ----------------------------------------------------------------


def test_run_suite_is_perfect() -> None:
    scorecard = run_suite()
    assert isinstance(scorecard, Scorecard)
    assert scorecard.passed
    assert scorecard.root_cause_recovery_rate == 1.0
    assert scorecard.false_discovery_rate == 0.0
    assert len(scorecard.cases) == len(CASES)


def test_run_suite_with_only_noise_is_vacuously_perfect() -> None:
    scorecard = run_suite((_case("pure_noise"),))
    assert scorecard.passed
    assert scorecard.root_cause_recovery_rate == 1.0  # no planted causes anywhere
    assert scorecard.false_discovery_rate == 0.0


# --- scoring logic: false discovery and missed-cause branches ---------------------------


def test_score_case_counts_a_false_discovery() -> None:
    case = _case("numeric_driver")  # planted: {driver, outcome}
    result = score_case(case, _report([["driver", "outcome"], ["driver", "noise_0"]]))
    assert result.recovered_count == 1
    assert result.false_discovery_count == 1
    assert result.false_discovery_rate == 0.5
    assert not result.passed


def test_score_case_records_a_missed_cause() -> None:
    case = _case("numeric_driver")
    result = score_case(case, _report([["driver", "noise_0"]]))
    assert result.recovered_count == 0
    assert result.recovery_rate == 0.0
    assert result.missed_pairs == [["driver", "outcome"]]
    assert not result.passed


# --- formatting -------------------------------------------------------------------------


def test_format_scorecard_pass() -> None:
    text = format_scorecard(run_suite())
    assert "# Veritas eval scorecard" in text
    assert "recovery rate: 100%" in text
    assert "verdict: PASS" in text
    assert "numeric_driver" in text


def test_format_scorecard_marks_failures() -> None:
    case = _case("numeric_driver")
    failing = score_case(case, _report([["driver", "noise_0"]]))
    scorecard = Scorecard(
        cases=[failing],
        root_cause_recovery_rate=0.0,
        false_discovery_rate=0.0,
        passed=False,
    )
    text = format_scorecard(scorecard)
    assert "verdict: FAIL" in text
    assert text.count("FAIL") >= 2  # the verdict line and the case row


# --- CLI entry point --------------------------------------------------------------------


def test_main_returns_zero_and_prints(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main((_case("pure_noise"),))  # noise-only keeps the CLI test fast
    assert exit_code == 0
    assert "# Veritas eval scorecard" in capsys.readouterr().out
