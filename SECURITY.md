# Security

> **Status: M2 controls implemented.** Sanitization and the SQL/Python execution gates
> shipped in M2 (`src/veritas/security.py`, `execute.py`, `sandbox.py`,
> `_sandbox_child.py`). This document states the threat model up front so the
> implementation is built against it, and it is expanded as each control ships.

## Threat model

Veritas runs untrusted *data* next to a powerful *orchestrator* (Claude). The three
threats we design against:

1. **Prompt injection via dataset content.** Column names and cell values (e.g. a CSV
   column literally named `ignore previous instructions and ...`) flow back to the
   model through tool outputs. All dataset-derived text is treated as untrusted input.
   `security.sanitize_text` neutralizes it *structurally* (DECISIONS.md, D-021): every
   Unicode control/format character is dropped, newlines/tabs are escaped to visible
   sequences, the value is length-capped, and table cells additionally escape pipes —
   so content can never break its framing. We deliberately do **not** phrase-match for
   "injection"; that is unreliable theater.
2. **Arbitrary code execution escaping the sandbox.** `run_python` executes
   model-written code behind five layers (DECISIONS.md, D-022): a static AST gate in
   the parent (import whitelist + escape-builtin/dunder denylist) that rejects code
   before any process starts; a separate subprocess; `setrlimit` CPU/memory caps with a
   wall-clock timeout backstop; network egress neutralized at the `socket` layer
   (verified by a test that attempts a connection and asserts failure); and an
   ephemeral working dir exposing only the requested datasets. **Residual risk:**
   without OS-level sandboxing (containers/seccomp — out of scope for a pip-installable
   tool), a whitelisted library can still *read* local files; this is mitigated, not
   eliminated, by no network egress and bounded output.
3. **Bulk data exfiltration into model context.** Tool results return bounded previews
   (≤ 50 rows / ≤ 4 KB), never full tables; full results persist only to local Parquet
   artifacts under the session directory. Results stream to Parquet via DuckDB's writer
   and are never materialized into process memory (DECISIONS.md, D-020).

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability" on the repository's Security tab). Do not open public issues
for security reports.
