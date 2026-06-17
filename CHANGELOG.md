# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-06-17

Initial release — a feature-complete v0 of the Veritas MCP server.

### Added

- **MCP server over stdio** with a `veritas` console entry point (`uvx veritas-mcp`),
  exposing nine investigation tools: `ingest_dataset`, `profile_dataset`, `run_sql`,
  `run_python`, `discover`, `record_finding`, `verify_finding`, `get_artifact`, and
  `investigation_state`.
- **Ingestion + profiling** of CSV/Parquet/Excel into DuckDB with name-preserving,
  SQL-safe schema capture; per-column statistics, date-column detection, and time-coverage
  analysis.
- **Execution layer**: read-only SQL behind a hardened gate, and model-written Python in an
  isolated subprocess (no network, resource limits, restricted builtins). Every execution —
  success or failure — is persisted as an artifact with a bounded, sanitized preview.
- **Deterministic receipts-verification**: findings carry numeric claims pinned to artifact
  cells by key; `verify_finding` re-reads each claim and refuses any prose number no claim
  backs. Enforcement is plain Python, never an LLM judge.
- **Autonomous discovery** pass: three nonparametric probes (Spearman, Kruskal-Wallis,
  chi-square) with Benjamini-Hochberg false-discovery-rate control, per-metric effect-size
  floors, a finding cap, and a conserved suppression ledger. Silence is a feature.
- **Public eval suite** (`python -m veritas.evals`): five seeded planted-cause datasets
  scored on root-cause recovery and false-discovery rate, including a no-signal case.
- **Investigator methodology** shipped as the MCP prompt `investigation_methodology` and as
  an installable Agent skill (`skills/veritas-investigator/SKILL.md`).
- Documentation: README with architecture diagram, a worked-investigation walkthrough,
  `SECURITY.md` threat model, and a full decision log (`DECISIONS.md`).

[0.1.0]: https://github.com/advait27/Veritas
