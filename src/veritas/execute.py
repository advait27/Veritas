"""Run read-only SQL against the session DuckDB and persist every run as an artifact.

``run_sql`` is the SQL half of M2's execution layer. The query passes the read-only
gate (:func:`veritas.security.validate_select`), its *full* result streams to a local
Parquet artifact via DuckDB's own writer (never materialized through the Python
client, so no timezone/pytz hazard — DECISIONS.md, D-014/D-019), and only a bounded,
sanitized preview re-enters model context (SECURITY.md, threat 3). Failures are
recorded as artifacts too: an execution that errored is still a receipt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import duckdb

from veritas.security import (
    PREVIEW_BYTE_CAP,
    PREVIEW_ROW_CAP,
    sanitize_text,
    validate_select,
)
from veritas.session import ArtifactKind, ArtifactRecord, new_id, quote_identifier

if TYPE_CHECKING:
    from pathlib import Path

    from veritas.session import InvestigationSession


def run_sql(session: InvestigationSession, sql: str) -> ArtifactRecord:
    """Execute a read-only ``SELECT`` and persist its result as an artifact.

    The query is validated (single read-only ``SELECT``, no filesystem functions), its
    full result is written to ``artifacts/<id>.parquet``, and the returned record holds
    the schema, row count, a bounded markdown preview, and the relative artifact path.

    Args:
        session: the investigation session whose DuckDB and artifact store are used.
        sql: the query to run.

    Returns:
        An :class:`~veritas.session.ArtifactRecord` with ``status='ok'`` on success or
        ``status='error'`` (and a sanitized ``error``) if DuckDB rejected the query.

    Raises:
        UnsafeSqlError: if ``sql`` is not a safe read-only query (raised before any
            execution, by the security gate).

    Example:
        ``record = run_sql(session, "SELECT category, count(*) AS n FROM ds GROUP BY 1")``
    """
    validated = validate_select(session.conn, sql)
    artifact_id = new_id("art")
    data_path = session.artifacts_dir / f"{artifact_id}.parquet"
    try:
        session.conn.sql(validated).write_parquet(str(data_path))
    except duckdb.Error as err:
        data_path.unlink(missing_ok=True)  # drop any half-written file
        return _register_error(session, artifact_id, "sql", validated, err)

    columns, types, row_count, preview = tabular_preview(session.conn, data_path)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        kind="sql",
        created_at=datetime.now(UTC),
        source=validated,
        status="ok",
        row_count=row_count,
        columns=columns,
        column_types=types,
        data_path=str(data_path.relative_to(session.session_dir)),
        preview=preview,
    )
    session.register_artifact(record)
    return record


def tabular_preview(
    conn: duckdb.DuckDBPyConnection, data_path: Path
) -> tuple[list[str], list[str], int, str]:
    """Read schema, row count, and a bounded preview from a result Parquet file.

    Shared by ``run_sql`` and ``run_python`` (whose result frame is also a Parquet
    artifact). The preview casts every column to text *in SQL* — timezone-aware columns
    via ``AT TIME ZONE 'UTC'`` for host independence — so no value is materialized into
    Python, then sanitizes each cell and caps the rendered table at the byte limit.

    Args:
        conn: a DuckDB connection used to read back the Parquet file.
        data_path: path to the result Parquet written by the caller.

    Returns:
        ``(columns, column_types, row_count, preview_markdown)``.

    Example:
        ``cols, types, n, md = tabular_preview(session.conn, path)``
    """
    described = conn.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(data_path)]).fetchall()
    columns = [str(row[0]) for row in described]
    types = [str(row[1]) for row in described]
    count_row = conn.execute("SELECT count(*) FROM read_parquet(?)", [str(data_path)]).fetchone()
    row_count = int(count_row[0]) if count_row is not None else 0
    rows = _fetch_preview_rows(conn, data_path, columns, types)
    return columns, types, row_count, _render_table(columns, rows, row_count)


def _fetch_preview_rows(
    conn: duckdb.DuckDBPyConnection,
    data_path: Path,
    columns: list[str],
    types: list[str],
) -> list[list[str]]:
    """Fetch up to ``PREVIEW_ROW_CAP`` rows with every column cast to safe text."""
    exprs: list[str] = []
    for name, dtype in zip(columns, types, strict=True):
        col = quote_identifier(name)
        if dtype.upper() == "TIMESTAMP WITH TIME ZONE":
            exprs.append(f"CAST(({col} AT TIME ZONE 'UTC') AS VARCHAR)")
        else:
            exprs.append(f"CAST({col} AS VARCHAR)")
    select = ", ".join(exprs) if exprs else "*"
    raw = conn.execute(
        f"SELECT {select} FROM read_parquet(?) LIMIT ?",
        [str(data_path), PREVIEW_ROW_CAP],
    ).fetchall()
    return [[_render_cell(value) for value in row] for row in raw]


_NULL_TOKEN = "␀"  # U+2400; distinguishes a real SQL NULL from the string value "NULL"


def _render_cell(value: object) -> str:
    """Render one already-text-cast cell: NULLs explicit, pipes escaped, text sanitized.

    A real SQL ``NULL`` renders as ``␀`` (not the literal text ``NULL``), so a column
    whose value is genuinely the string ``"NULL"`` is never conflated with missing data —
    the kind of silently-wrong reading Veritas exists to prevent.
    """
    if value is None:
        return _NULL_TOKEN
    return sanitize_text(value).replace("|", "\\|")


def _render_table(columns: list[str], rows: list[list[str]], row_count: int) -> str:
    """Render a GitHub-flavored markdown table, byte-capped for model context."""
    header = [sanitize_text(name).replace("|", "\\|") for name in columns]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    if row_count == 0:
        lines.append("_0 rows_")
    elif len(rows) < row_count:
        lines.append(f"_showing {len(rows)} of {row_count} rows_")
    return _cap_bytes("\n".join(lines), PREVIEW_BYTE_CAP)


_TRUNCATION_MARKER = "\n_…(preview truncated)_"


def _cap_bytes(text: str, cap: int) -> str:
    """Truncate ``text`` to at most ``cap`` total UTF-8 bytes, marker included.

    Room for the truncation marker is reserved *inside* ``cap`` so the returned string
    never exceeds the documented bound.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    budget = max(cap - len(_TRUNCATION_MARKER.encode("utf-8")), 0)
    truncated = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return truncated + _TRUNCATION_MARKER


def _register_error(
    session: InvestigationSession,
    artifact_id: str,
    kind: ArtifactKind,
    source: str,
    err: Exception,
) -> ArtifactRecord:
    """Persist a failed execution as an ``error`` artifact and return it."""
    record = ArtifactRecord(
        artifact_id=artifact_id,
        kind=kind,
        created_at=datetime.now(UTC),
        source=source,
        status="error",
        error=sanitize_text(str(err), cap=PREVIEW_BYTE_CAP),
    )
    session.register_artifact(record)
    return record
