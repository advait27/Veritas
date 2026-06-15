"""Integration tests for run_python: real subprocess isolation, results, and failures."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from veritas.ingest import ingest_file
from veritas.sandbox import _persist_figures, _read_outcome, run_python
from veritas.security import UnsafePythonError
from veritas.session import UnknownDatasetError

if TYPE_CHECKING:
    from pathlib import Path

    from veritas.session import InvestigationSession


@pytest.fixture
def loaded(session: InvestigationSession, trio_paths: dict[str, Path]) -> InvestigationSession:
    ingest_file(session, trio_paths["csv"], name="trio")
    return session


def test_run_python_dataframe_result(loaded: InvestigationSession) -> None:
    code = "result = df.groupby('category', as_index=False)['amount'].sum()"
    record = run_python(loaded, code)
    assert record.status == "ok"
    assert record.kind == "python"
    assert record.row_count == 3  # categories a, b, c
    assert "category" in record.columns
    assert record.data_path is not None
    assert (loaded.session_dir / record.data_path).exists()
    assert loaded.get_artifact(record.artifact_id).status == "ok"


def test_run_python_df_is_the_single_dataset(loaded: InvestigationSession) -> None:
    record = run_python(loaded, "result = df")
    assert record.row_count == 6  # the trio fixture has 6 rows


def test_run_python_captures_stdout(loaded: InvestigationSession) -> None:
    record = run_python(loaded, "print('rows', len(df))")
    assert record.status == "ok"
    assert record.stdout is not None and "rows 6" in record.stdout
    assert record.data_path is None


def test_run_python_scalar_result_in_preview(loaded: InvestigationSession) -> None:
    record = run_python(loaded, "result = int((df['amount'] > 0).sum())")
    assert record.status == "ok"
    assert record.data_path is None
    assert "6" in record.preview


def test_run_python_saves_figure(loaded: InvestigationSession) -> None:
    record = run_python(loaded, "import matplotlib.pyplot as plt\nplt.plot(df['amount'])")
    assert record.status == "ok"
    assert len(record.figure_paths) == 1
    assert (loaded.session_dir / record.figure_paths[0]).exists()


def test_run_python_user_exception_is_error_artifact(loaded: InvestigationSession) -> None:
    record = run_python(loaded, "result = 1 / 0")
    assert record.status == "error"
    assert record.error is not None and "ZeroDivisionError" in record.error
    assert loaded.get_artifact(record.artifact_id).status == "error"


def test_run_python_rejects_unsafe_source_before_running(loaded: InvestigationSession) -> None:
    with pytest.raises(UnsafePythonError):
        run_python(loaded, "import os\nresult = os.listdir('/')")
    # nothing was registered for a rejected program
    assert loaded.list_artifacts() == []


def test_run_python_times_out(loaded: InvestigationSession) -> None:
    record = run_python(loaded, "while True:\n    pass", timeout_seconds=2)
    assert record.status == "error"
    assert record.error is not None and "time limit" in record.error


def test_run_python_unknown_dataset_raises(loaded: InvestigationSession) -> None:
    with pytest.raises(UnknownDatasetError):
        run_python(loaded, "result = 1", dataset_ids=["ds_missing"])


def test_read_outcome_missing_manifest_is_error(tmp_path: Path) -> None:
    outcome = _read_outcome(tmp_path / "absent.json", 137, "out of memory")
    assert outcome["status"] == "error"
    assert outcome["manifest"] is None
    assert "out of memory" in outcome["error"]


def test_read_outcome_with_manifest(tmp_path: Path) -> None:
    manifest: dict[str, object] = {
        "status": "ok",
        "stdout": "",
        "error": None,
        "data": False,
        "figures": [],
        "result_repr": None,
    }
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    outcome = _read_outcome(path, 0, "")
    assert outcome["status"] == "ok"
    assert outcome["manifest"] == manifest


def test_persist_figures_skips_missing(session: InvestigationSession) -> None:
    assert _persist_figures(session, "art_x", ["/nonexistent/fig.png"]) == []
