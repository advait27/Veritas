"""Tests for run_sql: read-only execution, Parquet artifacts, and bounded previews."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from veritas.execute import run_sql
from veritas.ingest import ingest_file
from veritas.security import PREVIEW_BYTE_CAP, UnsafeSqlError

if TYPE_CHECKING:
    from pathlib import Path

    from veritas.session import InvestigationSession


@pytest.fixture
def loaded(session: InvestigationSession, trio_paths: dict[str, Path]) -> InvestigationSession:
    """A session with the cross-format trio CSV ingested (table = its dataset_id)."""
    ingest_file(session, trio_paths["csv"], name="trio")
    return session


def _table(session: InvestigationSession) -> str:
    return session.list_datasets()[0].table_name


def test_run_sql_persists_artifact_and_parquet(loaded: InvestigationSession) -> None:
    table = _table(loaded)
    record = run_sql(loaded, f"SELECT category, amount FROM {table} ORDER BY amount")
    assert record.status == "ok"
    assert record.kind == "sql"
    assert record.columns == ["category", "amount"]
    assert record.row_count == 6
    assert record.data_path is not None
    assert (loaded.session_dir / record.data_path).exists()
    # the artifact is registered and retrievable
    assert loaded.get_artifact(record.artifact_id) == record


def test_run_sql_preview_is_markdown_table(loaded: InvestigationSession) -> None:
    table = _table(loaded)
    record = run_sql(loaded, f"SELECT count(*) AS n FROM {table}")
    assert "| n |" in record.preview
    assert "| 6 |" in record.preview


def test_run_sql_aggregation_result(loaded: InvestigationSession) -> None:
    table = _table(loaded)
    record = run_sql(
        loaded, f"SELECT category, count(*) AS n FROM {table} GROUP BY category ORDER BY 1"
    )
    assert record.row_count == 3  # categories a, b, c
    assert record.column_types[0].upper() in {"VARCHAR", "STRING"}


def test_run_sql_rejects_non_select_before_execution(loaded: InvestigationSession) -> None:
    table = _table(loaded)
    with pytest.raises(UnsafeSqlError):
        run_sql(loaded, f"DROP TABLE {table}")


def test_run_sql_records_duckdb_error_as_artifact(loaded: InvestigationSession) -> None:
    table = _table(loaded)
    record = run_sql(loaded, f"SELECT no_such_column FROM {table}")
    assert record.status == "error"
    assert record.error is not None and "no_such_column" in record.error
    assert record.data_path is None
    # the half-written parquet must not be left behind
    leftovers = list(loaded.artifacts_dir.glob(f"{record.artifact_id}.parquet"))
    assert leftovers == []
    # errors are receipts too: still registered
    assert loaded.get_artifact(record.artifact_id).status == "error"


def test_run_sql_row_cap_in_preview(loaded: InvestigationSession) -> None:
    record = run_sql(loaded, "SELECT n FROM range(100) AS t(n)")
    assert record.row_count == 100
    assert "showing 50 of 100 rows" in record.preview
    assert record.preview.count("\n| ") <= 51  # header sep + 50 data rows, roughly


def test_run_sql_empty_result(loaded: InvestigationSession) -> None:
    record = run_sql(loaded, "SELECT n FROM range(0) AS t(n)")
    assert record.row_count == 0
    assert "_0 rows_" in record.preview


def test_run_sql_null_and_pipe_rendering(loaded: InvestigationSession) -> None:
    record = run_sql(loaded, "SELECT NULL AS a, 'x|y' AS b, 'NULL' AS c")
    assert "␀" in record.preview  # a real NULL renders as the null token
    assert "x\\|y" in record.preview  # the pipe is escaped, not a column break
    # the literal string 'NULL' must NOT be conflated with a real NULL
    assert record.preview.count("␀") == 1


def test_run_sql_byte_cap_truncates_preview(loaded: InvestigationSession) -> None:
    # 50 rows of ~200-char cells far exceed the 4 KB cap (each cell is capped at 200,
    # so the table — not a single cell — is what overflows).
    record = run_sql(loaded, "SELECT repeat('x', 200) AS c FROM range(50)")
    assert len(record.preview.encode("utf-8")) <= PREVIEW_BYTE_CAP + 64
    assert "preview truncated" in record.preview


def test_run_sql_timestamptz_is_host_independent(loaded: InvestigationSession) -> None:
    # Even with a non-UTC session timezone, the preview renders the instant in UTC
    # (DECISIONS.md, D-014/D-019) — and never needs pytz to materialize.
    loaded.conn.execute("SET TimeZone='America/New_York'")
    record = run_sql(loaded, "SELECT TIMESTAMPTZ '2024-01-01 00:00:00+00' AS t")
    assert record.status == "ok"
    assert "2024-01-01 00:00:00" in record.preview
