"""In-process unit tests for the sandbox child's importable building blocks.

These exercise the security- and result-handling logic directly (the subprocess
``main`` is glue and excluded from coverage), so the network block, limits, dataset
loading, and execution paths are all covered without a subprocess round-trip.
"""

from __future__ import annotations

import resource
import socket
import sys
from pathlib import Path

import duckdb
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import pytest

from veritas._sandbox_child import (
    _save_figures,
    apply_limits,
    block_network,
    load_datasets,
    run_in_namespace,
)

matplotlib.use("Agg")


def test_block_network_blocks_sockets_and_connections() -> None:
    saved = {name: getattr(socket, name) for name in ("socket", "create_connection", "getaddrinfo")}
    try:
        block_network()
        with pytest.raises(OSError, match="network access is disabled"):
            socket.create_connection(("127.0.0.1", 80))
        with pytest.raises(OSError, match="network access is disabled"):
            socket.socket()
    finally:
        for name, value in saved.items():
            setattr(socket, name, value)


def test_apply_limits_executes_all_branches() -> None:
    # Infinity imposes no real constraint on the test process, while still running both
    # setrlimit calls; the None path exercises the skip branches.
    inf = resource.RLIM_INFINITY
    apply_limits(inf, inf)
    apply_limits(None, None)


def test_load_datasets_reads_parquets(tmp_path: Path) -> None:
    path = tmp_path / "ds.parquet"
    con = duckdb.connect()
    con.sql("SELECT 1 AS a UNION ALL SELECT 2 AS a").write_parquet(str(path))
    con.close()
    frames = load_datasets({"ds_x": str(path)})
    assert list(frames) == ["ds_x"]
    assert frames["ds_x"]["a"].tolist() == [1, 2]


def test_load_datasets_empty() -> None:
    assert load_datasets({}) == {}


def test_run_in_namespace_writes_dataframe_result(tmp_path: Path) -> None:
    result_path = tmp_path / "r.parquet"
    manifest = run_in_namespace(
        "result = df", {"df": pd.DataFrame({"x": [1, 2, 3]})}, str(result_path), str(tmp_path)
    )
    assert manifest["status"] == "ok"
    assert manifest["data"] is True
    assert result_path.exists()


def test_run_in_namespace_series_result(tmp_path: Path) -> None:
    result_path = tmp_path / "r.parquet"
    manifest = run_in_namespace(
        "result = s", {"s": pd.Series([1, 2, 3], name="v")}, str(result_path), str(tmp_path)
    )
    assert manifest["data"] is True
    assert result_path.exists()


def test_run_in_namespace_tz_result_is_normalized(tmp_path: Path) -> None:
    result_path = tmp_path / "r.parquet"
    code = "result = pd.DataFrame({'t': pd.to_datetime(['2024-01-01']).tz_localize('UTC')})"
    manifest = run_in_namespace(code, {"pd": pd}, str(result_path), str(tmp_path))
    assert manifest["data"] is True
    assert result_path.exists()


def test_run_in_namespace_captures_stdout(tmp_path: Path) -> None:
    manifest = run_in_namespace(
        "print('hello world')", {}, str(tmp_path / "r.parquet"), str(tmp_path)
    )
    assert manifest["stdout"] == "hello world\n"
    assert manifest["data"] is False


def test_run_in_namespace_reports_user_error_with_partial_stdout(tmp_path: Path) -> None:
    manifest = run_in_namespace(
        "print('before')\nraise ValueError('boom')", {}, str(tmp_path / "r.parquet"), str(tmp_path)
    )
    assert manifest["status"] == "error"
    assert "ValueError: boom" in manifest["error"]
    assert manifest["stdout"] == "before\n"


def test_run_in_namespace_captures_scalar_repr(tmp_path: Path) -> None:
    manifest = run_in_namespace("result = {'a': 1}", {}, str(tmp_path / "r.parquet"), str(tmp_path))
    assert manifest["data"] is False
    assert manifest["result_repr"] is not None and "'a'" in manifest["result_repr"]


def test_save_figures_writes_png(tmp_path: Path) -> None:
    plt.close("all")
    plt.figure()
    plt.plot([1, 2, 3])
    try:
        paths = _save_figures(tmp_path)
        assert len(paths) == 1
        assert Path(paths[0]).exists()
    finally:
        plt.close("all")


def test_save_figures_empty_when_no_open_figures(tmp_path: Path) -> None:
    plt.close("all")  # pyplot imported but no figures open
    assert _save_figures(tmp_path) == []


def test_save_figures_none_when_pyplot_not_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, "matplotlib.pyplot", raising=False)
    assert _save_figures(tmp_path) == []
