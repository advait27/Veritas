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

## D-007 (2026-06-10) — Excel ingestion via openpyxl/pandas only (M1 amendment)

No DuckDB extensions are installed or loaded at runtime — extensions download on
first use, adding network dependence and CI flakiness. `.xlsx` files are read with
pandas/openpyxl, registered via `duckdb.register()`, and materialized with
`CREATE TABLE AS`. A test asserts the `excel`/`spatial` extensions are not loaded
after an Excel ingest. `.xls` (legacy BIFF) is not supported.

## D-008 (2026-06-10) — Column-name preservation and normalization scheme

Original column names are untrusted input and are preserved byte-for-byte in
`SchemaRecord` (sanitization is M2's job; profiling only length-caps them on
output). Generated SQL uses normalized identifiers derived deterministically:
lowercase → runs of `[^a-z0-9_]` become one `_` → strip edge `_` → empty becomes
`col_<position>` (0-based) → digit-leading gains `c_` prefix → DuckDB reserved
keywords (queried from `duckdb_keywords()`) gain trailing `_` → collisions
(duplicate originals or normalization clashes) gain `_2`, `_3`, … by position.
The mapping is bidirectional: normalized→original is unique; original→normalized
is one-to-many for duplicate headers. Ingested files must have a header row;
zero-byte/headerless files raise `IngestError`. CSV originals are read via a
separate `header=false, all_varchar=true` pass because DuckDB silently dedupes
duplicate header names (`revenue` → `revenue_1`).

## D-009 (2026-06-10) — Dataset identity and session layout (M1 amendment)

`dataset_id` = `ds_` + 12 hex chars of a uuid4 — no content hashing. The DuckDB
table is named by the `dataset_id` itself (stable, collision-free, always a safe
identifier); the user-facing dataset name lives only in metadata. Session layout:
`<base>/sess_<id>/session.duckdb` plus `datasets/<dataset_id>.json` per dataset,
so `InvestigationSession.open()` can reload a session's registry and tables.

## D-010 (2026-06-10) — Date-candidate detection: TRY_CAST over all non-null values

A text column is a candidate date column when ≥ 95 % of its non-null values
survive `TRY_CAST(col AS TIMESTAMP)` (threshold recorded in the report itself;
boundary inclusive). The probe runs engine-side over **all** non-null values — no
sampling at M1 scale, so the reported parse success rate is exact. Consequence of
TRY_CAST semantics (pinned in tests): ISO-style strings parse; `03/15/2024`,
`20240101`, and bare numbers do not. Temporal dtypes (DATE/TIMESTAMP) are
candidates by dtype; TIME-only columns are not treated as temporal.

## D-011 (2026-06-10) — Native grain = modal consecutive delta; calendar-aware gaps

Time coverage infers a candidate column's native grain as the *mode* of deltas
between consecutive distinct timestamps (more robust than the minimum, which one
pair of close timestamps would corrupt), mapped to the largest standard grain ≤
the mode (year floor 354 d, month floor 28 d, then week/day/hour/minute/second).
Gap counts use calendar arithmetic for month/year grains and fixed-step
arithmetic otherwise; off-grain timestamps are bucketed by rounding, and at most
5 example gaps are reported (scan capped at 1 M slots). A single distinct
timestamp yields `native_grain=None` and no gap count.

## D-012 (2026-06-10) — 200-char cap on cell-derived text in profile outputs

Profiling is the first place untrusted cell text reaches tool output. All
cell-derived strings (top-k values, min/max, original column names in markdown)
are hard-capped at 200 characters; markdown rendering additionally escapes pipes
and newlines so tables cannot be broken by cell content. This is a bound, not a
sanitizer — instruction-pattern neutralization is `security.py`'s job in M2.

## D-013 (2026-06-11) — Excel reader artifacts normalized to match CSV semantics

The cross-format amendment requires the same logical table to produce identical
profiles as .csv, .parquet, and .xlsx. pandas/openpyxl introduce two artifacts the
data does not contain: an integer column with blanks reads as float64 (`1` → `1.0`)
and every date reads as a datetime. `_ingest_excel` therefore applies
`convert_dtypes()` (restoring nullable integers) and converts datetime columns whose
non-null values are all midnight to plain dates. Excel has no date-vs-timestamp or
int-vs-float distinction at the storage level, so this is the faithful reading, not
coercion magic. Found by adversarial review: the original trio fixture contained no
date/nullable-int columns, so the divergence was invisible to CI.

## D-014 (2026-06-11) — TIMESTAMPTZ normalized to naive UTC TIMESTAMP at ingest

Two reasons: (a) determinism — a TIMESTAMPTZ cast to TIMESTAMP renders in the
host's session timezone, so the same file would profile differently per machine;
(b) the DuckDB Python client requires `pytz` (no longer pulled in by pandas ≥ 3.0)
to materialize tz-aware values, so profiling any TIMESTAMPTZ column crashed.
Ingest rewrites such columns via `ALTER ... TYPE TIMESTAMP USING (col AT TIME ZONE
'UTC')`. The conversion uses the statically-linked, already-loaded `icu` extension —
nothing is downloaded or newly loaded, consistent with D-007. If M2's `run_sql`
surfaces raw TIMESTAMPTZ results, `pytz` becomes a dependency decision for M2.

## D-015 (2026-06-11) — CSV dialect guard; parquet duplicate-name limitation

When DuckDB's CSV sniffer decides to skip leading rows (e.g. a header narrower than
the data rows), both our header-extraction read and the main read silently treat the
first *data* row as the header — fabricating "original" names from cell values.
Ingest now queries `sniff_csv()` and rejects any file with `SkipRows > 0`: the first
physical row must be the header (consistent with D-008). Known limitation, parquet:
duplicate column names in parquet metadata are deduplicated by DuckDB's reader
itself (`revenue` → `revenue_1`, visible even via `parquet_schema()`), so recorded
originals for such files are the reader's deduplicated names. DuckDB cannot even
write such a file un-mangled; accepted as a format-level edge case.

## D-016 (2026-06-11) — Numeric stats computed over finite values only

NaN/±inf in a DOUBLE column (routine in pandas/parquet exports; CSV `nan`/`inf`
literals sniff as DOUBLE) crashed `stddev_samp` with an OutOfRangeException and
would otherwise poison mean/quantiles. `_numeric_stats` filters through
`isfinite()`; min/max remain raw (so `max_value` may honestly read `"nan"`), and
`null_count` is unaffected. Recorded in the `NumericStats` docstring as contract.

## D-017 (2026-06-11) — Native grain = modal step itself (supersedes D-011's fixed floors)

D-011 mapped the modal delta onto six fixed grains, which produced factually false
gap counts for regular series between grains: a 30-minute series reported
`grain=minute` with ~1300 phantom gaps; quarterly data reported `grain=month` with
phantom gaps. Veritas exists to prevent confidently-wrong numbers, so gap arithmetic
now uses the modal step directly: modal deltas ≥ 28 days move to month-index space
(modal month-step → labels `month`, `quarter`, `year`, or `N-month`), finer series
use the modal second-step as the grid (labels `day`, `30-minute`, `N-hour`, …).
Off-grid timestamps count as neither slots nor gaps. Mode ties break toward the
smaller delta; single distinct timestamp → `native_grain=None`.

## D-018 (2026-06-14) — `profile.py` kept as one module over 400 lines

The quality bar caps modules at 400 lines "without a DECISIONS.md entry"; this is
that entry. `profile.py` is ~515 lines because dataset profiling is one cohesive
pipeline whose stages share private constants and helpers: dtype classification,
numeric stats, top-k, date-candidate detection (D-010), modal-step time coverage
(D-017), and the bounded markdown rendering (D-012). Each function stays under the
60-line limit and the public surface is a single `profile_dataset` entry point plus
the report models. Splitting the markdown rendering into its own module would still
leave profiling itself over 400 lines while severing the shared `cap_text`/escape
helpers and the report models from their only caller, so the seam buys churn, not
clarity. Revisit if a second report format or a non-markdown renderer lands.

## D-019 (2026-06-15) — `run_sql` read-only gate: parser + keyword + function denylist

DuckDB's parser is the first layer (`extract_statements`): exactly one statement,
type `SELECT`. But that alone is not "read-only" — DuckDB classifies `PRAGMA` and
`CALL` as `SELECT`, and a plain `SELECT` can still read the filesystem via table
functions (`read_csv('/etc/passwd')`, `glob('/**')`) or reach the network via httpfs.
So two more deterministic layers sit on top (`security.validate_select`): a
first-keyword denylist (catches `PRAGMA`/`CALL`/settings/DDL/DML/transaction verbs
after comment-stripping) and a denylist of filesystem/network functions matched as
`name(` tokens. False positives (a *column* named `glob`) are avoided because the
match requires a call paren; a determined reader of exotic functions is the residual
gap, accepted because the broader system blocks network egress and bounds output.
This runs on the session connection (no separate read-only connection) because
`enable_external_access` is a global setting the ingest path needs left on.

## D-020 (2026-06-15) — `run_sql` results stream to Parquet; previews are text-cast, host-independent

The full result is written with DuckDB's own Parquet writer (`relation.write_parquet`),
never materialized through the Python client — so a `TIMESTAMPTZ` in the result never
needs `pytz` (the D-014 hazard) and large results never enter process memory. Schema,
row count, and preview are then read back from that Parquet. The preview casts every
column to `VARCHAR` *in SQL*, special-casing `TIMESTAMP WITH TIME ZONE` via
`AT TIME ZONE 'UTC'`, so previews are identical regardless of the host session
timezone and still require no `pytz`. The preview is bounded twice (SECURITY.md,
threat 3): ≤ 50 rows and ≤ 4 KB, with every cell run through `sanitize_text` and
pipes escaped so dataset content cannot break the markdown table. Failed executions
are recorded as `error` artifacts — an execution that errored is still a receipt.

## D-021 (2026-06-15) — Untrusted-text neutralization is structural, not phrase-matching

`sanitize_text` does not try to *detect* prompt injection ("ignore previous
instructions"); a denylist of phrases is unreliable theater, which Veritas exists to
avoid. Instead it removes the ability of dataset/execution-derived text to *break its
framing*: every Unicode control/format character is dropped (NUL, C1, zero-width,
bidi overrides), newlines/tabs become visible escapes, and the value is hard-capped.
The text can still *say* anything; it can no longer forge a new line, hide characters,
or exceed its cell. This is the M2 realization of D-008/D-012's deferred "sanitization
is M2's job".

## D-022 (2026-06-15) — `run_python` containment model and its honest limits

Model-written Python runs behind five layers: (1) a static AST gate in the *parent*
(`security.check_python_source`) — import whitelist, no relative imports, and a
denylist of escape-shaped builtins/dunder attributes — so policy violations never
spawn a process; (2) a separate subprocess; (3) resource limits via `setrlimit`
(`RLIMIT_CPU` is reliable; `RLIMIT_AS` is best-effort — macOS rejects it, so the
parent's wall-clock `subprocess` timeout is the real backstop); (4) network egress
neutralized by replacing the `socket` entry points with raisers (defense in depth:
whitelisted libraries like pandas can still open sockets); (5) an ephemeral working
dir with only the requested datasets present, exported as Parquet and handed in as
DataFrames (`df` when exactly one, plus a `datasets` map). The full result frame and
figures persist as artifacts; only a bounded preview and capped stdout return.

Honest limit: this is containment, not a jail. Without OS-level sandboxing
(containers/seccomp, out of scope for a pip-installable tool), a whitelisted library
can still *read* local files (`pd.read_csv('/etc/passwd')`); that is mitigated — not
eliminated — by no network egress and bounded output, and is documented in
SECURITY.md as residual risk. The child is split into importable functions
(`block_network`, `apply_limits`, `load_datasets`, `run_in_namespace`) so the
security-relevant logic is unit-tested in-process; only `main` runs solely in the
subprocess (and is excluded from coverage), with an end-to-end test exercising the
real subprocess wiring.

## D-023 (2026-06-15) — Artifact store lives in the session, mirroring datasets

`ArtifactRecord` and its registry sit in `session.py` next to `DatasetRecord` (the
module's docstring anticipated "artifacts (M2)"), persisted as
`artifacts/<artifact_id>.json` with the full result at `artifacts/<id>.parquet` and
figures at `artifacts/<id>_fig<n>.png`. Execution modules build the record and call
`session.register_artifact`, exactly as ingest builds a `DatasetRecord` and calls
`register_dataset` — one owner of session state, reloaded by `InvestigationSession.open`.
