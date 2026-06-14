"""Automated dataset profiling: per-column stats, date candidates, time coverage.

All statistics are computed engine-side in DuckDB. Text derived from cell values is
hard-capped at :data:`CELL_TEXT_CAP` characters in the value fields (top-k, min/max)
and throughout the markdown rendering — the first boundary where untrusted dataset
text could leak into model context (full sanitization lands with ``security.py`` in
M2; see DECISIONS.md, D-012). ``candidate_date_columns`` and ``TimeCoverage.column``
hold normalized identifiers, never original names.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from veritas.session import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Callable

    import duckdb

    from veritas.session import ColumnSchema, InvestigationSession

DATE_PARSE_SUCCESS_THRESHOLD = 0.95
"""A text column is a candidate date column when ≥ this share of its non-null values
parse as timestamps (DECISIONS.md, D-010)."""

TOP_K = 5
CELL_TEXT_CAP = 200
EXAMPLE_GAPS_LIMIT = 5
_GAP_SCAN_LIMIT = 1_000_000

ColumnKind = Literal["numeric", "temporal", "boolean", "text", "other"]

_NUMERIC_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
        "FLOAT",
        "REAL",
        "DOUBLE",
    }
)

_CALENDAR_FLOOR_SECONDS = 28 * 86400.0
"""Modal deltas at or above ~one month switch gap arithmetic to calendar (month) space."""

_STEP_NAMES: tuple[tuple[str, int], ...] = (
    ("week", 604_800),
    ("day", 86_400),
    ("hour", 3_600),
    ("minute", 60),
    ("second", 1),
)


def cap_text(value: object, limit: int = CELL_TEXT_CAP) -> str:
    """Render an untrusted cell value as text, hard-capped at ``limit`` characters.

    Example:
        >>> cap_text("x" * 500) == "x" * 200
        True
    """
    text = str(value)
    return text if len(text) <= limit else text[:limit]


class TopValue(BaseModel):
    """One frequent value (rendered as capped text) and its occurrence count."""

    value: str
    count: int


class NumericStats(BaseModel):
    """Distribution statistics for a numeric column; fields are None when undefined.

    Computed over finite values only — NaN/±inf are excluded (DECISIONS.md, D-016).
    """

    mean: float | None
    median: float | None
    std: float | None
    p5: float | None
    p95: float | None


class TimeCoverage(BaseModel):
    """Time coverage of one candidate date column, at its inferred native grain.

    ``column`` holds the *normalized* identifier (unambiguous even with duplicate
    original headers); ``original_name`` carries the untrusted source name.
    ``native_grain`` is a label like ``day``, ``quarter``, or ``30-minute`` (D-017).
    """

    column: str
    original_name: str
    min_value: datetime
    max_value: datetime
    native_grain: str | None
    gap_count: int | None
    example_gaps: list[str]


class ColumnProfile(BaseModel):
    """Profile of a single column; cell-derived text fields are length-capped."""

    position: int
    original_name: str
    normalized_name: str
    duckdb_type: str
    kind: ColumnKind
    null_count: int
    null_rate: float
    distinct_count: int
    min_value: str | None
    max_value: str | None
    top_values: list[TopValue]
    numeric: NumericStats | None
    date_parse_success_rate: float | None


class ProfileReport(BaseModel):
    """Full profiling report for one dataset.

    The compact JSON (:meth:`to_json`) and the human-readable markdown
    (:meth:`to_markdown`) are both rendered from this single model.
    """

    dataset_id: str
    name: str
    row_count: int
    column_count: int
    generated_at: datetime
    date_parse_success_threshold: float
    columns: list[ColumnProfile]
    candidate_date_columns: list[str]
    time_coverage: list[TimeCoverage]

    def to_json(self) -> str:
        """Return the report as compact JSON.

        Example:
            ``ProfileReport.model_validate_json(report.to_json()) == report``
        """
        return self.model_dump_json()

    def to_markdown(self) -> str:
        """Render the report as human-readable markdown (same data as :meth:`to_json`).

        Example:
            ``print(report.to_markdown())``
        """
        header = [
            f"# Profile: {_md_escape(cap_text(self.name))}",
            "",
            f"- dataset_id: `{self.dataset_id}`",
            f"- rows: {self.row_count} | columns: {self.column_count}",
            f"- generated_at: {self.generated_at.isoformat()}",
            "",
        ]
        sections = [
            _render_columns_section(self),
            _render_numeric_section(self),
            _render_dates_section(self),
        ]
        return "\n".join(header + [s for s in sections if s])


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _fmt_float(value: float | None) -> str:
    return "—" if value is None else f"{value:.6g}"


def _column_label(profile: ColumnProfile) -> str:
    label = _md_escape(cap_text(profile.original_name))
    if profile.normalized_name != profile.original_name:
        label += f" (as `{profile.normalized_name}`)"
    return label


def _render_columns_section(report: ProfileReport) -> str:
    lines = [
        "## Columns",
        "",
        "| # | column | type | nulls | distinct | min | max | top values |",
        "| - | - | - | - | - | - | - | - |",
    ]
    for col in report.columns:
        top = "; ".join(f"{_md_escape(tv.value)} (\u00d7{tv.count})" for tv in col.top_values)
        lines.append(
            f"| {col.position} | {_column_label(col)} | {col.duckdb_type} "
            f"| {col.null_count} ({col.null_rate:.1%}) | {col.distinct_count} "
            f"| {_md_escape(col.min_value) if col.min_value is not None else '—'} "
            f"| {_md_escape(col.max_value) if col.max_value is not None else '—'} "
            f"| {top or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_numeric_section(report: ProfileReport) -> str:
    numeric_cols = [c for c in report.columns if c.numeric is not None]
    if not numeric_cols:
        return ""
    lines = [
        "## Numeric columns",
        "",
        "| column | mean | median | std | p5 | p95 |",
        "| - | - | - | - | - | - |",
    ]
    for col in numeric_cols:
        stats = col.numeric
        assert stats is not None  # filtered above; keeps mypy precise
        lines.append(
            f"| {_column_label(col)} | {_fmt_float(stats.mean)} | {_fmt_float(stats.median)} "
            f"| {_fmt_float(stats.std)} | {_fmt_float(stats.p5)} | {_fmt_float(stats.p95)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_dates_section(report: ProfileReport) -> str:
    probed = [c for c in report.columns if c.date_parse_success_rate is not None]
    if not report.candidate_date_columns and not probed:
        return ""
    lines = ["## Date columns", ""]
    lines.append(
        f"Candidates (dtype, or parse rate ≥ {report.date_parse_success_threshold:.0%}): "
        + (", ".join(f"`{c}`" for c in report.candidate_date_columns) or "none")
    )
    for cov in report.time_coverage:
        gaps = (
            "unknown grain"
            if cov.native_grain is None
            else (
                f"grain={cov.native_grain}, gaps={cov.gap_count}"
                + (f" (e.g. {', '.join(cov.example_gaps)})" if cov.example_gaps else "")
            )
        )
        lines.append(
            f"- `{cov.column}`: {cov.min_value.isoformat(sep=' ')} → "
            f"{cov.max_value.isoformat(sep=' ')}; {gaps}"
        )
    for col in probed:
        rate = col.date_parse_success_rate
        assert rate is not None  # filtered above; keeps mypy precise
        lines.append(f"- `{col.normalized_name}` parses as timestamp: {rate:.1%}")
    lines.append("")
    return "\n".join(lines)


def _kind_of(duckdb_type: str) -> ColumnKind:
    upper = duckdb_type.upper()
    if upper in _NUMERIC_TYPES or upper.startswith("DECIMAL"):
        return "numeric"
    if upper == "DATE" or upper.startswith("TIMESTAMP"):
        return "temporal"
    if upper == "BOOLEAN":
        return "boolean"
    if upper == "VARCHAR":
        return "text"
    return "other"


def _numeric_stats(
    conn: duckdb.DuckDBPyConnection, table_sql: str, column_sql: str
) -> NumericStats:
    # stats are computed over finite values only: NaN/±inf would crash stddev_samp
    # and poison the aggregates (DECISIONS.md, D-016); min/max stay raw on purpose
    finite = f"(CASE WHEN isfinite(CAST({column_sql} AS DOUBLE)) THEN {column_sql} END)"
    row = conn.execute(
        f"SELECT avg({finite})::DOUBLE, median({finite})::DOUBLE, "
        f"stddev_samp({finite})::DOUBLE, quantile_cont({finite}, 0.05)::DOUBLE, "
        f"quantile_cont({finite}, 0.95)::DOUBLE FROM {table_sql}"
    ).fetchone()
    assert row is not None  # aggregate queries always return one row
    return NumericStats(mean=row[0], median=row[1], std=row[2], p5=row[3], p95=row[4])


def _top_values(conn: duckdb.DuckDBPyConnection, table_sql: str, column_sql: str) -> list[TopValue]:
    # positional GROUP BY/ORDER BY: an alias could collide with a real column name
    rows = conn.execute(
        f"SELECT {column_sql}, count(*) FROM {table_sql} "
        f"WHERE {column_sql} IS NOT NULL "
        f"GROUP BY 1 ORDER BY 2 DESC, 1 ASC LIMIT {TOP_K}"
    ).fetchall()
    return [TopValue(value=cap_text(row[0]), count=int(row[1])) for row in rows]


def _date_parse_rate(
    conn: duckdb.DuckDBPyConnection, table_sql: str, column_sql: str, non_null: int
) -> float | None:
    if non_null == 0:
        return None
    row = conn.execute(
        f"SELECT count(TRY_CAST({column_sql} AS TIMESTAMP)) FROM {table_sql}"
    ).fetchone()
    assert row is not None  # aggregate queries always return one row
    return int(row[0]) / non_null


def _profile_column(
    conn: duckdb.DuckDBPyConnection,
    table_sql: str,
    column: ColumnSchema,
    row_count: int,
) -> ColumnProfile:
    column_sql = quote_identifier(column.normalized_name)
    kind = _kind_of(column.duckdb_type)
    row = conn.execute(
        f"SELECT count({column_sql}), count(DISTINCT {column_sql}), "
        f"min({column_sql}), max({column_sql}) FROM {table_sql}"
    ).fetchone()
    assert row is not None  # aggregate queries always return one row
    non_null, distinct = int(row[0]), int(row[1])
    return ColumnProfile(
        position=column.position,
        original_name=column.original_name,
        normalized_name=column.normalized_name,
        duckdb_type=column.duckdb_type,
        kind=kind,
        null_count=row_count - non_null,
        null_rate=(row_count - non_null) / row_count if row_count else 0.0,
        distinct_count=distinct,
        min_value=cap_text(row[2]) if row[2] is not None else None,
        max_value=cap_text(row[3]) if row[3] is not None else None,
        top_values=_top_values(conn, table_sql, column_sql),
        numeric=_numeric_stats(conn, table_sql, column_sql) if kind == "numeric" else None,
        date_parse_success_rate=(
            _date_parse_rate(conn, table_sql, column_sql, non_null) if kind == "text" else None
        ),
    )


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return None


def _distinct_timestamps(
    conn: duckdb.DuckDBPyConnection, table_sql: str, column_sql: str, kind: ColumnKind
) -> list[datetime]:
    expr = column_sql if kind == "temporal" else f"TRY_CAST({column_sql} AS TIMESTAMP)"
    # positional ORDER BY: an alias could collide with a real column name
    rows = conn.execute(
        f"SELECT DISTINCT {expr} FROM {table_sql} WHERE {expr} IS NOT NULL ORDER BY 1"
    ).fetchall()
    timestamps = (_as_datetime(row[0]) for row in rows)
    return [ts for ts in timestamps if ts is not None]


def _modal_delta(deltas: list[float]) -> float | None:
    """Return the most common delta (ties broken toward the smaller delta)."""
    if not deltas:
        return None
    counts = Counter(deltas)
    return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]


def _humanize_step(step_seconds: int) -> str:
    """Label a step size, e.g. ``86400 -> 'day'``, ``1800 -> '30-minute'``."""
    for name, size in _STEP_NAMES:
        if step_seconds >= size and step_seconds % size == 0:
            count = step_seconds // size
            return name if count == 1 else f"{count}-{name}"
    return f"{step_seconds}-second"


def _grid_gaps(
    indices: list[int], step: int, render: Callable[[int], str]
) -> tuple[int, list[str]]:
    """Count empty slots on the grid (first + k*step); off-grid points count as present-less."""
    first, last = indices[0], indices[-1]
    on_grid = {index for index in indices if (index - first) % step == 0}
    slots = (last - first) // step + 1
    gap_count = slots - len(on_grid)
    examples: list[str] = []
    if slots <= _GAP_SCAN_LIMIT:
        for slot in range(first, last + 1, step):
            if slot not in on_grid:
                examples.append(render(slot))
                if len(examples) == EXAMPLE_GAPS_LIMIT:
                    break
    return gap_count, examples


def _calendar_gaps(timestamps: list[datetime]) -> tuple[str, int, list[str]]:
    """Gap detection in month-index space for monthly-or-coarser series (D-017)."""
    indices = sorted({ts.year * 12 + ts.month - 1 for ts in timestamps})
    deltas = [float(later - earlier) for earlier, later in pairwise(indices)]
    step = max(1, int(_modal_delta(deltas) or 1))
    grain = {1: "month", 3: "quarter", 12: "year"}.get(step, f"{step}-month")

    def render(index: int) -> str:
        if step % 12 == 0:
            return str(index // 12)
        return f"{index // 12:04d}-{index % 12 + 1:02d}"

    gap_count, examples = _grid_gaps(indices, step, render)
    return grain, gap_count, examples


def _fixed_step_gaps(
    timestamps: list[datetime], modal_seconds: float
) -> tuple[str, int, list[str]]:
    """Gap detection on a fixed grid whose step is the modal delta itself (D-017)."""
    step = max(1, round(modal_seconds))
    base = timestamps[0]
    indices = sorted({round((ts - base).total_seconds()) for ts in timestamps})

    def render(offset: int) -> str:
        tick = base + timedelta(seconds=offset)
        return tick.date().isoformat() if step % 86_400 == 0 else tick.isoformat(sep=" ")

    gap_count, examples = _grid_gaps(indices, step, render)
    return _humanize_step(step), gap_count, examples


def _time_coverage(
    conn: duckdb.DuckDBPyConnection,
    table_sql: str,
    column: ColumnSchema,
    kind: ColumnKind,
) -> TimeCoverage | None:
    timestamps = _distinct_timestamps(
        conn, table_sql, quote_identifier(column.normalized_name), kind
    )
    if not timestamps:
        return None
    deltas = [(later - earlier).total_seconds() for earlier, later in pairwise(timestamps)]
    modal = _modal_delta(deltas)
    grain: str | None
    gap_count: int | None
    examples: list[str]
    if modal is None:
        grain, gap_count, examples = None, None, []
    elif modal >= _CALENDAR_FLOOR_SECONDS:
        grain, gap_count, examples = _calendar_gaps(timestamps)
    else:
        grain, gap_count, examples = _fixed_step_gaps(timestamps, modal)
    return TimeCoverage(
        column=column.normalized_name,
        original_name=column.original_name,
        min_value=timestamps[0],
        max_value=timestamps[-1],
        native_grain=grain,
        gap_count=gap_count,
        example_gaps=examples,
    )


def profile_dataset(session: InvestigationSession, dataset_id: str) -> ProfileReport:
    """Profile every column of an ingested dataset and detect candidate date columns.

    Example:
        ``report = profile_dataset(session, record.dataset_id); print(report.to_markdown())``

    Raises:
        UnknownDatasetError: if ``dataset_id`` is not registered in the session.
    """
    record = session.get_dataset(dataset_id)
    conn = session.conn
    table_sql = quote_identifier(record.table_name)
    count_row = conn.execute(f"SELECT count(*) FROM {table_sql}").fetchone()
    row_count = int(count_row[0]) if count_row is not None else 0

    profiles = [
        _profile_column(conn, table_sql, column, row_count)
        for column in record.schema_record.columns
    ]
    candidates = [
        profile
        for profile in profiles
        if profile.kind == "temporal"
        or (
            profile.date_parse_success_rate is not None
            and profile.date_parse_success_rate >= DATE_PARSE_SUCCESS_THRESHOLD
        )
    ]
    coverage = [
        _time_coverage(conn, table_sql, column, _kind_of(column.duckdb_type))
        for column in record.schema_record.columns
        if column.normalized_name in {profile.normalized_name for profile in candidates}
    ]
    return ProfileReport(
        dataset_id=record.dataset_id,
        name=record.name,
        row_count=row_count,
        column_count=len(profiles),
        generated_at=datetime.now(UTC),
        date_parse_success_threshold=DATE_PARSE_SUCCESS_THRESHOLD,
        columns=profiles,
        candidate_date_columns=[profile.normalized_name for profile in candidates],
        time_coverage=[cov for cov in coverage if cov is not None],
    )
