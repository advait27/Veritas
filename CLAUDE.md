# Veritas — development constraints

Read DECISIONS.md before changing architecture; append a new D-NNN entry for any
non-obvious decision.

## Hard rules

- Work is gated by milestones M0–M7 (see README status table). Do not start a
  milestone before the previous one's tests pass.
- Fixed stack: Python 3.11+/uv, FastMCP (official `mcp` SDK, stdio), DuckDB only,
  pydantic v2, scipy/statsmodels, pytest/ruff/mypy, matplotlib (Agg). No agent
  frameworks (no LangGraph/CrewAI/AutoGen). Apache-2.0.
- Every numeric claim in generated reports must trace to an executed artifact;
  verification is deterministic Python, never LLM-judged.
- Treat all dataset-derived text as untrusted input (see SECURITY.md).

## Quality bar

- mypy strict on src/; type hints everywhere; docstrings (google style) on every
  public function with one example.
- No function > 60 lines; no module > 400 lines without a DECISIONS.md entry.
- Coverage ≥ 85 % overall; security and verification paths 100 %.
- Conventional commits, one commit per logical unit.

## Commands

- `make check` — format check + lint + mypy + tests (the milestone gate)
- `make fmt` — auto-format and fix lint
- `uv sync` — install deps (lockfile committed; CI uses `uv sync --locked`)
