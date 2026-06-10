# Decisions Log

Architecture and process decisions that are not obvious from the code itself.
Format: `D-NNN (date) — title`, then context and rationale. Chronological order.

## D-001 (2026-06-10) — Distribution name `veritas-mcp`, import package `veritas`

The bare name `veritas` is not reliably available on PyPI and is ambiguous. The
distribution is published as `veritas-mcp`; the import package stays `veritas`.
The `veritas` console script (for `uvx`) will be added in M5 together with
`server.py` — adding the entry point earlier would ship a console command that
crashes on import.

## D-002 (2026-06-10) — Working name kept configurable

The project spec treats "Veritas" as a working name. The MCP server display name
defaults to `veritas.DEFAULT_SERVER_NAME` and will be overridable via the
`VERITAS_SERVER_NAME` environment variable when the server is wired up in M5.
Code and docs avoid hard-coding the name where practical.

## D-003 (2026-06-10) — Full runtime dependency set declared at M0

The tech stack is fixed by the spec (DuckDB, pydantic v2, scipy/statsmodels,
matplotlib, mcp SDK, pandas/numpy/openpyxl). Declaring everything in M0 keeps
`uv.lock` and the CI cache stable across milestones instead of churning the
resolution every milestone. Cost: a slightly heavier install before the code
that uses each dependency exists. Accepted.

## D-004 (2026-06-10) — Build/layout: hatchling, src layout, `py.typed` from day one

`src/` layout prevents accidental imports of the working tree, hatchling is the
lightest maintained backend with PEP 639 license metadata support, and shipping
`py.typed` from M0 means downstream type checking works from the first release.

## D-005 (2026-06-10) — Lint/type/coverage policy encoded in tooling, not prose

The quality bar from the spec is enforced mechanically where a tool exists:

- ruff `D` rules (google convention) → "every public function has a docstring";
- ruff `PL` complexity rules → approximate the "no function > 60 lines" rule
  (`PLR0915` caps statement count; true line counting has no ruff rule);
- mypy `strict = true` over both `src` and `tests`;
- pytest `--cov-fail-under=85` from M0 → the ≥ 85 % overall coverage gate.

`PLR2004` (magic-value comparison) is ignored globally: thresholds and constants
are routine in statistics code. Tests are exempt from docstring rules only.

## D-006 (2026-06-10) — `uv.lock` committed; CI installs with `uv sync --locked`

Reproducible CI and contributor installs beat resolution freshness for an
application-shaped project. `--locked` makes CI fail loudly if the lockfile
drifts from `pyproject.toml`.
