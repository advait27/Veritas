"""Tests for profiling: stats correctness, edge fixtures, date coverage, rendering."""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from veritas.ingest import ingest_file
from veritas.profile import CELL_TEXT_CAP, ColumnProfile, ProfileReport, profile_dataset
from veritas.session import InvestigationSession

FIXTURES = Path(__file__).parent / "fixtures"


def _profile(session: InvestigationSession, path: Path) -> ProfileReport:
    record = ingest_file(session, path)
    return profile_dataset(session, record.dataset_id)


def _column(report: ProfileReport, normalized_name: str) -> ColumnProfile:
    return next(c for c in report.columns if c.normalized_name == normalized_name)


def test_trio_csv_stats(session: InvestigationSession, trio_paths: dict[str, Path]) -> None:
    report = _profile(session, trio_paths["csv"])
    assert (report.row_count, report.column_count) == (6, 5)

    day = _column(report, "day")
    assert (day.kind, day.duckdb_type) == ("temporal", "DATE")
    assert report.candidate_date_columns == ["day"]

    qty = _column(report, "qty")
    assert (qty.kind, qty.duckdb_type) == ("numeric", "BIGINT")
    assert qty.null_count == 2
    assert qty.null_rate == pytest.approx(2 / 6)
    assert qty.numeric is not None
    assert qty.numeric.mean == pytest.approx(4.25)

    id_col = _column(report, "id")
    assert (id_col.kind, id_col.duckdb_type) == ("numeric", "BIGINT")
    assert (id_col.null_count, id_col.null_rate, id_col.distinct_count) == (0, 0.0, 6)
    assert (id_col.min_value, id_col.max_value) == ("1", "6")
    assert id_col.numeric is not None
    assert id_col.numeric.mean == pytest.approx(3.5)
    assert id_col.numeric.median == pytest.approx(3.5)
    assert id_col.numeric.std == pytest.approx(math.sqrt(3.5))
    assert id_col.numeric.p5 == pytest.approx(1.25)
    assert id_col.numeric.p95 == pytest.approx(5.75)

    category = _column(report, "category")
    assert (category.kind, category.distinct_count) == ("text", 3)
    assert [(tv.value, tv.count) for tv in category.top_values] == [
        ("a", 3),
        ("b", 2),
        ("c", 1),
    ]
    assert category.numeric is None

    amount = _column(report, "amount")
    assert amount.kind == "numeric"
    assert amount.numeric is not None
    assert amount.numeric.mean == pytest.approx(24.75)
    assert amount.numeric.median == pytest.approx(10.5)
    # ties broken by count desc then value asc — deterministic
    assert [(tv.value, tv.count) for tv in amount.top_values] == [
        ("10.5", 2),
        ("0.5", 1),
        ("7.75", 1),
        ("20.25", 1),
        ("99.0", 1),
    ]


def test_cross_format_profiles_identical(
    session: InvestigationSession, trio_paths: dict[str, Path]
) -> None:
    reports = {fmt: _profile(session, path) for fmt, path in trio_paths.items()}
    base = reports["csv"]
    assert base.candidate_date_columns == ["day"]  # the fixture's temporal column
    for fmt in ("parquet", "xlsx"):
        other = reports[fmt]
        assert other.row_count == base.row_count
        assert other.candidate_date_columns == base.candidate_date_columns
        assert [c.model_dump() for c in other.columns] == [c.model_dump() for c in base.columns]
        assert [tc.model_dump() for tc in other.time_coverage] == [
            tc.model_dump() for tc in base.time_coverage
        ]


def test_empty_dataset(session: InvestigationSession) -> None:
    report = _profile(session, FIXTURES / "empty.csv")
    assert report.row_count == 0
    for col in report.columns:
        assert (col.null_count, col.null_rate, col.distinct_count) == (0, 0.0, 0)
        assert col.min_value is None and col.max_value is None
        assert col.top_values == []
        assert col.date_parse_success_rate is None
    assert report.candidate_date_columns == []
    assert "rows: 0" in report.to_markdown()


def test_single_row_dataset(session: InvestigationSession) -> None:
    report = _profile(session, FIXTURES / "single_row.csv")
    score = _column(report, "score")
    assert score.numeric is not None
    assert score.numeric.mean == pytest.approx(3.5)
    assert score.numeric.std is None  # sample std undefined for n=1
    assert score.numeric.p5 == pytest.approx(3.5)
    assert score.numeric.p95 == pytest.approx(3.5)


def test_all_null_column(session: InvestigationSession) -> None:
    report = _profile(session, FIXTURES / "all_null.csv")
    ghost = _column(report, "ghost")
    assert ghost.duckdb_type == "VARCHAR"  # pinned: DuckDB sniffs all-null as VARCHAR
    assert (ghost.null_count, ghost.null_rate, ghost.distinct_count) == (3, 1.0, 0)
    assert ghost.min_value is None and ghost.max_value is None
    assert ghost.top_values == []
    assert ghost.date_parse_success_rate is None  # no non-null values to probe


def test_mixed_type_column_pinned_to_varchar(session: InvestigationSession) -> None:
    report = _profile(session, FIXTURES / "mixed_type.csv")
    amount = _column(report, "amount")
    # pinned: DuckDB's sniffer falls back to VARCHAR for mixed numeric/text content
    assert amount.duckdb_type == "VARCHAR"
    assert amount.kind == "text"
    assert amount.null_count == 0
    values = {tv.value for tv in amount.top_values}
    assert values == {"10", "2.5", "three", "40"}


def _write_messy_dates(path: Path) -> None:
    lines = ["id,day,noise,almost"]
    base = date(2024, 1, 1)
    for i in range(1, 41):
        day = "not-a-date" if i in (7, 23) else (base + timedelta(days=i)).isoformat()
        noise = "junk" if i % 5 in (0, 1) else (base + timedelta(days=i)).isoformat()
        almost = "junk" if i in (7, 23, 31) else (base + timedelta(days=i)).isoformat()
        lines.append(f"{i},{day},{noise},{almost}")
    path.write_text("\n".join(lines) + "\n")


def test_date_parse_rate_and_candidate_threshold(
    session: InvestigationSession, tmp_path: Path
) -> None:
    path = tmp_path / "messy.csv"
    _write_messy_dates(path)
    report = _profile(session, path)

    assert report.date_parse_success_threshold == pytest.approx(0.95)  # spec: >= 95%

    day = _column(report, "day")
    assert day.duckdb_type == "VARCHAR"
    assert day.date_parse_success_rate == pytest.approx(38 / 40)  # exactly at 0.95

    noise = _column(report, "noise")
    assert noise.date_parse_success_rate == pytest.approx(24 / 40)

    almost = _column(report, "almost")
    assert almost.date_parse_success_rate == pytest.approx(37 / 40)  # 0.925, just below

    # boundary is inclusive and pinned from both sides: 0.95 qualifies, 0.925 does not
    assert report.candidate_date_columns == ["day"]
    coverage = report.time_coverage[0]
    assert coverage.column == "day"
    assert coverage.native_grain == "day"
    assert coverage.gap_count == 2
    assert coverage.example_gaps == ["2024-01-08", "2024-01-24"]


def test_temporal_dtype_candidate_and_gap_detection(
    session: InvestigationSession, tmp_path: Path
) -> None:
    lines = ["d,v"]
    for i in range(30):
        day = date(2024, 1, 1) + timedelta(days=i)
        if day.day in (10, 20):
            continue
        lines.append(f"{day.isoformat()},{i}")
    path = tmp_path / "daily.csv"
    path.write_text("\n".join(lines) + "\n")

    report = _profile(session, path)
    d_col = _column(report, "d")
    assert (d_col.duckdb_type, d_col.kind) == ("DATE", "temporal")
    assert d_col.date_parse_success_rate is None  # probe only applies to text columns
    assert report.candidate_date_columns == ["d"]

    coverage = report.time_coverage[0]
    assert coverage.min_value.date() == date(2024, 1, 1)
    assert coverage.max_value.date() == date(2024, 1, 30)
    assert coverage.native_grain == "day"
    assert coverage.gap_count == 2
    assert coverage.example_gaps == ["2024-01-10", "2024-01-20"]


def test_monthly_grain(session: InvestigationSession, tmp_path: Path) -> None:
    months = [1, 2, 3, 5, 6]  # April missing
    lines = ["m,v"] + [f"2024-{m:02d}-01,{m}" for m in months]
    path = tmp_path / "monthly.csv"
    path.write_text("\n".join(lines) + "\n")

    report = _profile(session, path)
    coverage = report.time_coverage[0]
    assert coverage.native_grain == "month"
    assert coverage.gap_count == 1
    assert coverage.example_gaps == ["2024-04"]


def test_quarterly_grain(session: InvestigationSession, tmp_path: Path) -> None:
    quarters = ["2023-01-01", "2023-04-01", "2023-10-01", "2024-01-01"]  # 2023-07 missing
    lines = ["q,v"] + [f"{q},1" for q in quarters]
    path = tmp_path / "quarterly.csv"
    path.write_text("\n".join(lines) + "\n")

    report = _profile(session, path)
    coverage = report.time_coverage[0]
    assert coverage.native_grain == "quarter"
    assert coverage.gap_count == 1
    assert coverage.example_gaps == ["2023-07"]


def test_thirty_minute_grain(session: InvestigationSession, tmp_path: Path) -> None:
    base = date(2024, 3, 1)
    lines = ["ts,v"]
    for i in range(48):
        if i in (10, 31):
            continue
        minutes = i * 30
        lines.append(f"{base.isoformat()} {minutes // 60:02d}:{minutes % 60:02d}:00,{i}")
    path = tmp_path / "halfhour.csv"
    path.write_text("\n".join(lines) + "\n")

    report = _profile(session, path)
    coverage = report.time_coverage[0]
    assert coverage.native_grain == "30-minute"  # not "minute" with 1000+ false gaps
    assert coverage.gap_count == 2
    assert coverage.example_gaps == ["2024-03-01 05:00:00", "2024-03-01 15:30:00"]


def test_nan_and_inf_do_not_crash_numeric_stats(
    session: InvestigationSession, tmp_path: Path
) -> None:
    path = tmp_path / "nan.csv"
    path.write_text("v\n1.5\nnan\ninf\n2.5\n")

    report = _profile(session, path)
    col = _column(report, "v")
    assert col.duckdb_type == "DOUBLE"  # pinned: DuckDB sniffs nan/inf literals as DOUBLE
    assert col.numeric is not None
    # stats over finite values only (D-016)
    assert col.numeric.mean == pytest.approx(2.0)
    assert col.numeric.std == pytest.approx(math.sqrt(0.5))
    assert col.numeric.p5 == pytest.approx(1.55)
    assert col.numeric.p95 == pytest.approx(2.45)
    # min/max stay raw and truthful
    assert col.min_value == "1.5"
    assert col.max_value == "nan"
    round_tripped = ProfileReport.model_validate_json(report.to_json())
    assert round_tripped == report


def test_yearly_grain(session: InvestigationSession, tmp_path: Path) -> None:
    years = [2019, 2020, 2021, 2023, 2024]  # 2022 missing
    lines = ["y,v"] + [f"{y}-06-01,{y}" for y in years]
    path = tmp_path / "yearly.csv"
    path.write_text("\n".join(lines) + "\n")

    report = _profile(session, path)
    coverage = report.time_coverage[0]
    assert coverage.native_grain == "year"
    assert coverage.gap_count == 1
    assert coverage.example_gaps == ["2022"]


def test_single_timestamp_has_unknown_grain(session: InvestigationSession, tmp_path: Path) -> None:
    path = tmp_path / "one_ts.csv"
    path.write_text("d,v\n2024-03-01,1\n2024-03-01,2\n")

    report = _profile(session, path)
    coverage = report.time_coverage[0]
    assert coverage.native_grain is None  # one distinct timestamp → no deltas
    assert coverage.gap_count is None
    assert coverage.example_gaps == []
    assert coverage.min_value == coverage.max_value
    assert "unknown grain" in report.to_markdown()


def test_untrusted_text_capped(session: InvestigationSession, tmp_path: Path) -> None:
    long_value = "v" * 500
    path = tmp_path / "long.csv"
    path.write_text(f"weird|name\n{long_value}\nshort\n")

    report = _profile(session, path)
    col = report.columns[0]
    assert col.original_name == "weird|name"
    assert col.normalized_name == "weird_name"
    assert col.max_value is not None and len(col.max_value) == CELL_TEXT_CAP
    assert all(len(tv.value) <= CELL_TEXT_CAP for tv in col.top_values)
    markdown = report.to_markdown()
    assert "weird\\|name" in markdown  # pipes escaped so tables stay intact
    assert long_value not in markdown


def test_markdown_and_json_render_from_same_model(session: InvestigationSession) -> None:
    record = ingest_file(session, FIXTURES / "duplicate_cols.csv")
    report = profile_dataset(session, record.dataset_id)

    markdown = report.to_markdown()
    assert "# Profile:" in markdown
    assert "revenue (as `revenue_2`)" in markdown  # original name shown with normalized alias
    assert "`day`" in markdown

    round_tripped = ProfileReport.model_validate_json(report.to_json())
    assert round_tripped == report


def test_large_generated_dataset(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(42)
    n = 100_000
    values = rng.normal(100, 15, n).round(4)
    categories = rng.choice(list("abcde"), n)
    lines = ["id,value,category"]
    for i in range(n):
        value = "" if i % 100 == 0 else repr(float(values[i]))
        lines.append(f"{i},{value},{categories[i]}")
    path = tmp_path / "big.csv"
    path.write_text("\n".join(lines) + "\n")

    record = ingest_file(session, path, name="big")
    assert record.row_count == n

    report = profile_dataset(session, record.dataset_id)
    value_col = _column(report, "value")
    assert value_col.kind == "numeric"
    assert value_col.null_count == 1_000
    assert value_col.null_rate == pytest.approx(0.01)
    assert value_col.numeric is not None
    assert value_col.numeric.mean == pytest.approx(100, abs=0.5)
    assert value_col.numeric.std == pytest.approx(15, abs=0.5)
    assert _column(report, "category").distinct_count == 5
