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
   model-written code behind layered controls (DECISIONS.md, D-022/D-024): a static AST
   gate in the parent (import whitelist + a denylist of escape-shaped names — `getattr`,
   `globals`, `__builtins__`, … — and dunder attributes) that rejects code before any
   process starts; a separate subprocess started in its own session/process group
   (killed as a group on timeout); `setrlimit` CPU/memory caps with the wall-clock
   timeout as the enforced backstop; network egress neutralized on both the `socket`
   module and the low-level `_socket` extension (verified by a test that attempts a
   connection and asserts failure); a restricted `__builtins__` with a whitelist-guarded
   `__import__` so the import policy holds even at runtime; and an ephemeral working dir
   exposing only the requested datasets. **Residual risk:** without OS-level sandboxing
   (containers/seccomp — out of scope for a pip-installable tool), a whitelisted library
   can still *read* local files, and a sufficiently clever gadget chain may still reach
   restricted behaviour; this is mitigated, not eliminated, by no network egress and
   bounded output.
3. **Bulk data exfiltration into model context.** Tool results return bounded previews
   (≤ 50 rows / ≤ 4 KB), never full tables; full results persist only to local Parquet
   artifacts under the session directory. Results stream to Parquet via DuckDB's writer
   and are never materialized into process memory (DECISIONS.md, D-020). The SQL gate
   (`validate_select`) is a *hardened denylist* — single read-only `SELECT`, no
   settings/PRAGMA, no filesystem/network functions, and no replacement-scan file
   strings — not an engine-enforced jail; the durable engine-level boundary is deferred
   (DECISIONS.md, D-024), so a future DuckDB file-reading function outside the denylist
   is the residual risk, bounded by the absence of network egress.

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability" on the repository's Security tab). Do not open public issues
for security reports.
