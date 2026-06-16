"""``python -m veritas.evals``: run the suite, print the scorecard, exit nonzero on fail."""

from __future__ import annotations

import sys

from veritas.evals.cases import CASES, EvalCase
from veritas.evals.scoring import format_scorecard, run_suite


def main(cases: tuple[EvalCase, ...] = CASES) -> int:
    """Run the eval suite, print its scorecard, and return 0 on pass or 1 on fail.

    Example:
        ``raise SystemExit(main())``
    """
    scorecard = run_suite(cases)
    print(format_scorecard(scorecard))
    return 0 if scorecard.passed else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
