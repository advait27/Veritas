# Security

> **Status: skeleton.** The controls described here are implemented milestone by
> milestone (sandbox and sanitization land in M2). This document states the threat
> model up front so the implementation is built against it, and it will be expanded
> as each control ships.

## Threat model

Veritas runs untrusted *data* next to a powerful *orchestrator* (Claude). The three
threats we design against:

1. **Prompt injection via dataset content.** Column names and cell values (e.g. a CSV
   column literally named `ignore previous instructions and ...`) flow back to the
   model through tool outputs. All dataset-derived text is treated as untrusted input:
   sanitized, length-capped, and never interpolated into anything instruction-shaped
   (`security.py`, M2).
2. **Arbitrary code execution escaping the sandbox.** `run_python` executes
   model-written code. It runs in a subprocess with CPU/time/memory limits, no network
   egress, an ephemeral temp dir, and an AST-enforced import whitelist. Network
   blocking is verified by a test that attempts a socket connection and asserts
   failure (M2).
3. **Bulk data exfiltration into model context.** Tool results return bounded previews
   (≤ 50 rows / ≤ 4 KB), never full tables; full results persist only to local Parquet
   artifacts.

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability" on the repository's Security tab). Do not open public issues
for security reports.
