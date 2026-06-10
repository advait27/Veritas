# Veritas

> **Receipts, or it didn't happen.** An open-source MCP server that turns Claude into a
> rigorous, hypothesis-driven data investigator.

> ⚠️ **Pre-release.** Veritas is under active development; the sections below describe the
> design target. The [Status](#status) table shows what is actually built.

Veritas is **not a chat wrapper around a database**. It is an investigation harness in which
Claude does the orchestration and deterministic Python does the rigor:

- **Hypothesis-tree investigation.** "Why did revenue drop?" is answered by building a MECE
  hypothesis tree and falsifying branches with targeted queries — not by a one-shot answer.
- **Receipts-or-it-didn't-happen verification.** Every numeric claim in any output must trace
  to an actually executed artifact (SQL/Python result). Enforcement is deterministic Python,
  not an LLM judge.
- **Discovery with suppression.** The autonomous opportunity/risk discovery pass is built as
  generate → test → suppress → rank, with Benjamini–Hochberg false-discovery-rate control,
  effect-size floors, and a hard cap on surfaced findings. Silence is a feature.
- **A public eval suite.** Synthetic datasets with planted root causes and red herrings, scored
  on root-cause recovery rate and false-discovery rate — including a case where the only
  correct answer is "no significant change".

## Status

| Milestone | Scope | State |
| --- | --- | --- |
| M0 | Project scaffold, CI, license, docs skeleton | ✅ done |
| M1 | Ingest (CSV/Parquet/Excel → DuckDB) + profiling | ⬜ |
| M2 | SQL/Python execution sandbox + artifact store | ⬜ |
| M3 | Findings registry + deterministic claim verification | ⬜ |
| M4 | Discovery probes + FDR suppression | ⬜ |
| M5 | MCP server wiring (stdio), `uvx` entry point | ⬜ |
| M6 | Eval suite with planted-cause cases + scorecard | ⬜ |
| M7 | Skills, examples, finished docs | ⬜ |

## Quickstart

Coming with M5. The target experience:

```jsonc
// Claude Desktop / Claude Code MCP config (target, not yet functional)
{
  "mcpServers": {
    "veritas": {
      "command": "uvx",
      "args": ["veritas-mcp"]
    }
  }
}
```

## Architecture

An architecture diagram lands in M7. In brief: Claude (via MCP tools plus a methodology
skill) orchestrates an `InvestigationSession`; all analysis runs in DuckDB and a sandboxed
Python subprocess; every execution is persisted as an `Artifact`; findings are registered,
deterministically verified against artifacts, and only verified findings can enter a report.

## Non-goals

Veritas deliberately does **not** do:

- **Forecasting or ML models** — no predictive models, no SHAP/explainability.
- **Database / warehouse connectors** — analysis is DuckDB over local CSV/Parquet/Excel files.
  Warehouse support is a possible future milestone, not a v0 feature.
- **A web UI** — Veritas is an MCP server; the client is Claude.
- **Multi-user features** — single-analyst, local-first.

## Development

```sh
uv sync          # install runtime + dev dependencies
make check       # ruff format check + lint + mypy strict + pytest with coverage
```

See [DECISIONS.md](DECISIONS.md) for the decision log and [SECURITY.md](SECURITY.md) for the
threat model.

## License

[Apache-2.0](LICENSE)
