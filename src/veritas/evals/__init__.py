"""Veritas public eval suite: planted-cause datasets scored on recovery vs. false discovery.

Run it with ``python -m veritas.evals``. The suite ingests each synthetic dataset and runs
the real discovery pass, then scores how many planted root causes were recovered and how
many surfaced discoveries were spurious — including a case whose only correct answer is
silence.
"""

from veritas.evals.cases import CASES, EvalCase
from veritas.evals.scoring import (
    CaseResult,
    Scorecard,
    format_scorecard,
    run_case,
    run_suite,
    score_case,
)

__all__ = [
    "CASES",
    "CaseResult",
    "EvalCase",
    "Scorecard",
    "format_scorecard",
    "run_case",
    "run_suite",
    "score_case",
]
