"""Load CSV/Parquet/Excel files into DuckDB with name-preserving schema capture.

Original column names are untrusted input and are preserved byte-for-byte in the
:class:`~veritas.session.SchemaRecord`; generated SQL only ever uses the normalized
identifiers produced by :func:`normalize_column_names`. Excel is read via
openpyxl/pandas only — no DuckDB extensions are installed or loaded at runtime
(DECISIONS.md, D-007).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pandas as pd

from veritas.session import (
    ColumnSchema,
    DatasetRecord,
    SchemaRecord,
    SourceFormat,
    new_id,
    quote_identifier,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from veritas.session import InvestigationSession


class IngestError(ValueError):
    """Raised when a dataset file cannot be ingested."""


_SUFFIX_TO_FORMAT: dict[str, SourceFormat] = {
    ".csv": "csv",
    ".parquet": "parquet",
    ".xlsx": "xlsx",
}

_CSV_READER = "read_csv_auto(?, header=true)"
_PARQUET_READER = "read_parquet(?)"


def _reserved_keywords(conn: duckdb.DuckDBPyConnection) -> frozenset[str]:
    rows = conn.execute(
        "SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category = 'reserved'"
    ).fetchall()
    return frozenset(str(row[0]).lower() for row in rows)


def normalize_column_names(original_names: Sequence[str], reserved: frozenset[str]) -> list[str]:
    """Map original (untrusted) column names to unique, SQL-safe identifiers.

    Deterministic, position-ordered: lowercase, runs of characters outside
    ``[a-z0-9_]`` become a single ``_``, leading/trailing ``_`` are stripped, an empty
    result becomes ``col_<position>`` (0-based), a digit-leading result gains a ``c_``
    prefix, DuckDB reserved keywords gain a trailing ``_``, and collisions (duplicate
    originals or normalization clashes) gain ``_2``, ``_3``, ... suffixes.

    Example:
        >>> normalize_column_names(["Revenue (USD)", "revenue usd", "select"],
        ...                        frozenset({"select"}))
        ['revenue_usd', 'revenue_usd_2', 'select_']
    """
    used: set[str] = set()
    normalized: list[str] = []
    for position, original in enumerate(original_names):
        base = re.sub(r"[^a-z0-9_]+", "_", original.lower()).strip("_")
        base = re.sub(r"_+", "_", base)
        if not base:
            base = f"col_{position}"
        if base[0].isdigit():
            base = f"c_{base}"
        if base in reserved:
            base = f"{base}_"
        candidate = base
        suffix = 1
        while candidate in used:
            suffix += 1
            candidate = f"{base}_{suffix}"
        used.add(candidate)
        normalized.append(candidate)
    return normalized


def _csv_original_header(conn: duckdb.DuckDBPyConnection, source: Path) -> list[str]:
    """Read the raw first row of a CSV as the original header, dialect-sniffed."""
    try:
        row = conn.execute(
            "SELECT * FROM read_csv_auto(?, header=false, all_varchar=true) LIMIT 1",
            [str(source)],
        ).fetchone()
    except duckdb.Error as err:
        msg = f"could not read CSV header from {source}: {err}"
        raise IngestError(msg) from err
    if row is None:
        msg = f"{source} is empty — a header row is required (DECISIONS.md, D-008)"
        raise IngestError(msg)
    return ["" if value is None else str(value) for value in row]


def _guard_csv_dialect(conn: duckdb.DuckDBPyConnection, source: Path) -> None:
    """Reject CSVs whose sniffed dialect skips leading rows (DECISIONS.md, D-015).

    When the sniffer decides to skip rows (e.g. a ragged header narrower than the data),
    both the header-extraction read and the main read would silently treat the first
    *data* row as the header, fabricating original names from cell values.
    """
    try:
        row = conn.execute("SELECT SkipRows FROM sniff_csv(?)", [str(source)]).fetchone()
    except duckdb.Error as err:
        msg = f"could not sniff CSV dialect for {source}: {err}"
        raise IngestError(msg) from err
    skip_rows = int(row[0]) if row is not None and row[0] is not None else 0
    if skip_rows > 0:
        msg = (
            f"{source}: the CSV sniffer skips the first {skip_rows} row(s) — likely a "
            "ragged or malformed header; the first physical row must be the header "
            "(DECISIONS.md, D-015)"
        )
        raise IngestError(msg)


def _create_table_from_reader(
    conn: duckdb.DuckDBPyConnection, table: str, reader_sql: str, source: Path
) -> None:
    """Create the dataset table from a DuckDB table function such as ``read_parquet``."""
    try:
        conn.execute(
            f"CREATE TABLE {quote_identifier(table)} AS SELECT * FROM {reader_sql}",
            [str(source)],
        )
    except duckdb.Error as err:
        msg = f"could not ingest {source}: {err}"
        raise IngestError(msg) from err


def _table_column_names(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = conn.execute(f"DESCRIBE {quote_identifier(table)}").fetchall()
    return [str(row[0]) for row in rows]


def _rename_columns(conn: duckdb.DuckDBPyConnection, table: str, normalized: Sequence[str]) -> None:
    """Rename all columns to the normalized identifiers, by position, in two phases.

    The two-phase rename (everything to a placeholder first) makes the operation safe
    even if a current name collides with another column's target name. The placeholder
    prefix is derived from the freshly generated dataset id (= ``table``), so an
    untrusted source file cannot contain a column that collides with it.
    """
    current = _table_column_names(conn, table)
    if len(current) != len(normalized):
        msg = (
            f"header width ({len(normalized)}) does not match table width "
            f"({len(current)}) for {table}"
        )
        raise IngestError(msg)
    quoted_table = quote_identifier(table)
    placeholder = f"_{table}_tmp_"
    for position, name in enumerate(current):
        conn.execute(
            f"ALTER TABLE {quoted_table} RENAME COLUMN {quote_identifier(name)} "
            f"TO {quote_identifier(f'{placeholder}{position}')}"
        )
    for position, target in enumerate(normalized):
        conn.execute(
            f"ALTER TABLE {quoted_table} RENAME COLUMN "
            f"{quote_identifier(f'{placeholder}{position}')} TO {quote_identifier(target)}"
        )


def _normalize_timestamptz(conn: duckdb.DuckDBPyConnection, table: str) -> None:
    """Convert TIMESTAMPTZ columns to naive UTC TIMESTAMP (DECISIONS.md, D-014).

    Keeps stored values host-independent and avoids materializing tz-aware values
    through the DuckDB Python client (which requires pytz, not a dependency).
    """
    quoted_table = quote_identifier(table)
    for row in conn.execute(f"DESCRIBE {quoted_table}").fetchall():
        if str(row[1]).upper() == "TIMESTAMP WITH TIME ZONE":
            column = quote_identifier(str(row[0]))
            conn.execute(
                f"ALTER TABLE {quoted_table} ALTER {column} TYPE TIMESTAMP "
                f"USING ({column} AT TIME ZONE 'UTC')"
            )


def _normalize_excel_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Undo pandas reader artifacts so xlsx profiles match the equivalent CSV (D-013).

    ``convert_dtypes()`` restores nullable integers (pandas reads an int column with
    blanks as float64), and datetime columns whose values are all midnight become
    plain dates — Excel has no date-vs-timestamp distinction, so midnight-only is the
    faithful reading.
    """
    frame = frame.convert_dtypes()
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            non_null = series.dropna()
            if not non_null.empty and bool((non_null.dt.normalize() == non_null).all()):
                frame[column] = series.dt.date.where(series.notna(), other=None)
    return frame


def _ingest_excel(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    source: Path,
    reserved: frozenset[str],
) -> tuple[list[str], list[str]]:
    """Load an .xlsx file via openpyxl/pandas and return (originals, normalized) names."""
    try:
        head = pd.read_excel(source, engine="openpyxl", header=None, nrows=1)
    except Exception as err:
        msg = f"could not read Excel file {source}: {err}"
        raise IngestError(msg) from err
    if head.empty:
        msg = f"{source} has no header row (DECISIONS.md, D-008)"
        raise IngestError(msg)
    originals = ["" if pd.isna(value) else str(value) for value in head.iloc[0].tolist()]
    normalized = normalize_column_names(originals, reserved)
    frame = _normalize_excel_frame(
        pd.read_excel(source, engine="openpyxl", header=0, names=normalized)
    )
    view_name = f"_veritas_frame_{table}"
    conn.register(view_name, frame)
    try:
        conn.execute(
            f"CREATE TABLE {quote_identifier(table)} AS SELECT * FROM {quote_identifier(view_name)}"
        )
    finally:
        conn.unregister(view_name)
    return originals, normalized


def ingest_file(
    session: InvestigationSession, path: str | Path, name: str | None = None
) -> DatasetRecord:
    """Load a CSV/Parquet/Excel file into the session's DuckDB and register it.

    The dataset table is named by its ``dataset_id`` and its columns use normalized
    identifiers; original names live in the returned record's ``schema_record``.

    Example:
        ``record = ingest_file(session, "orders.csv", name="orders")``

    Raises:
        IngestError: for missing files, unsupported extensions, or unreadable content.
    """
    source = Path(path)
    if not source.exists():
        msg = f"file not found: {source}"
        raise IngestError(msg)
    source_format = _SUFFIX_TO_FORMAT.get(source.suffix.lower())
    if source_format is None:
        supported = ", ".join(sorted(_SUFFIX_TO_FORMAT))
        msg = f"unsupported file type {source.suffix!r} (supported: {supported})"
        raise IngestError(msg)

    dataset_id = new_id("ds")
    conn = session.conn
    reserved = _reserved_keywords(conn)
    if source_format == "csv":
        originals = _csv_original_header(conn, source)
        _guard_csv_dialect(conn, source)
        normalized = normalize_column_names(originals, reserved)
        _create_table_from_reader(conn, dataset_id, _CSV_READER, source)
        _rename_columns(conn, dataset_id, normalized)
    elif source_format == "parquet":
        _create_table_from_reader(conn, dataset_id, _PARQUET_READER, source)
        originals = _table_column_names(conn, dataset_id)
        normalized = normalize_column_names(originals, reserved)
        _rename_columns(conn, dataset_id, normalized)
    else:
        originals, normalized = _ingest_excel(conn, dataset_id, source, reserved)
    _normalize_timestamptz(conn, dataset_id)

    described = conn.execute(f"DESCRIBE {quote_identifier(dataset_id)}").fetchall()
    columns = [
        ColumnSchema(
            position=position,
            original_name=originals[position],
            normalized_name=normalized[position],
            duckdb_type=str(described[position][1]),
        )
        for position in range(len(described))
    ]
    count_row = conn.execute(f"SELECT count(*) FROM {quote_identifier(dataset_id)}").fetchone()
    row_count = int(count_row[0]) if count_row is not None else 0

    record = DatasetRecord(
        dataset_id=dataset_id,
        name=name if name is not None else source.stem,
        source_path=str(source),
        source_format=source_format,
        ingested_at=datetime.now(UTC),
        row_count=row_count,
        column_count=len(columns),
        table_name=dataset_id,
        schema_record=SchemaRecord(columns=columns),
    )
    session.register_dataset(record)
    return record
