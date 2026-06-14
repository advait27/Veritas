"""Shared fixtures: a temp-dir session and a cross-format (csv/parquet/xlsx) trio."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from veritas.session import InvestigationSession


@pytest.fixture
def session(tmp_path: Path) -> Iterator[InvestigationSession]:
    with InvestigationSession(base_dir=tmp_path / "sessions") as s:
        yield s


def _trio_frame() -> pd.DataFrame:
    """Cross-format fixture: includes a date column and a nullable integer column."""
    return pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5, 6],
            "category": ["a", "b", "a", "c", "b", "a"],
            "amount": [10.5, 20.25, 10.5, 99.0, 0.5, 7.75],
            "day": [date(2024, 1, n) for n in range(1, 7)],
            "qty": pd.array([3, None, 7, 2, None, 5], dtype="Int64"),
        }
    )


@pytest.fixture
def trio_paths(tmp_path: Path) -> dict[str, Path]:
    """Write the same small table as .csv, .parquet, and .xlsx (no committed binaries)."""
    frame = _trio_frame()
    csv_path = tmp_path / "trio.csv"
    frame.to_csv(csv_path, index=False)

    parquet_path = tmp_path / "trio.parquet"
    scratch = duckdb.connect()
    scratch.register("frame", frame)
    escaped = parquet_path.as_posix().replace("'", "''")
    scratch.execute(f"COPY (SELECT * FROM frame) TO '{escaped}' (FORMAT PARQUET)")
    scratch.close()

    xlsx_path = tmp_path / "trio.xlsx"
    frame.to_excel(xlsx_path, index=False)
    return {"csv": csv_path, "parquet": parquet_path, "xlsx": xlsx_path}
