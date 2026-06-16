"""Tests for the MCP server: the nine tools, their wiring, and env configuration."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pandas as pd
import pytest
from mcp.server.fastmcp import FastMCP

from veritas.ingest import IngestError
from veritas.responses import ArtifactView, DatasetSummary, ExecutionResult
from veritas.security import UnsafePythonError, UnsafeSqlError
from veritas.server import (
    _TOOL_ORDER,
    SERVER_NAME_ENV,
    VeritasTools,
    create_server,
    server_name,
)
from veritas.session import (
    NumericClaim,
    UnknownArtifactError,
    UnknownFindingError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from veritas.session import InvestigationSession


def _csv(tmp_path: Path, frame: pd.DataFrame, name: str = "data") -> str:
    path = tmp_path / f"{name}.csv"
    frame.to_csv(path, index=False)
    return str(path)


def _small(tmp_path: Path) -> str:
    return _csv(tmp_path, pd.DataFrame({"category": ["a", "b", "a"], "amount": [1.0, 2.0, 3.0]}))


def _call(server: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool through FastMCP and return its structured result.

    ``call_tool`` returns ``(content_blocks, structured_dict)`` at runtime; the cast
    pins that shape, which the SDK's published return annotation does not capture.
    """
    result = asyncio.run(server.call_tool(name, arguments))
    _, structured = cast("tuple[Any, dict[str, Any]]", result)
    return structured


@pytest.fixture
def tools(session: InvestigationSession) -> VeritasTools:
    return VeritasTools(session)


# --- ingest / profile -------------------------------------------------------------------


def test_ingest_returns_summary_with_columns(tools: VeritasTools, tmp_path: Path) -> None:
    summary = tools.ingest_dataset(_small(tmp_path), name="orders")
    assert isinstance(summary, DatasetSummary)
    assert summary.name == "orders"
    assert summary.row_count == 3
    assert [c.name for c in summary.columns] == ["category", "amount"]
    assert summary.columns[0].type == "VARCHAR"


def test_ingest_unsupported_file_raises(tools: VeritasTools, tmp_path: Path) -> None:
    bad = tmp_path / "data.txt"
    bad.write_text("nope", encoding="utf-8")
    with pytest.raises(IngestError):
        tools.ingest_dataset(str(bad))


def test_profile_returns_markdown(tools: VeritasTools, tmp_path: Path) -> None:
    dataset_id = tools.ingest_dataset(_small(tmp_path)).dataset_id
    markdown = tools.profile_dataset(dataset_id)
    assert markdown.startswith("# Profile:")
    assert "amount" in markdown


# --- run_sql ----------------------------------------------------------------------------


def test_run_sql_ok_persists_artifact(tools: VeritasTools, tmp_path: Path) -> None:
    dataset_id = tools.ingest_dataset(_small(tmp_path)).dataset_id
    result = tools.run_sql(f'SELECT sum(amount) AS total FROM "{dataset_id}"')
    assert isinstance(result, ExecutionResult)
    assert result.status == "ok"
    assert result.artifact_id.startswith("art_")
    assert "total" in result.preview


def test_run_sql_unsafe_query_raises_before_execution(tools: VeritasTools) -> None:
    with pytest.raises(UnsafeSqlError):
        tools.run_sql("DROP TABLE something")


def test_run_sql_runtime_error_is_an_error_artifact(tools: VeritasTools) -> None:
    result = tools.run_sql("SELECT * FROM no_such_table")
    assert result.status == "error"
    assert result.error is not None


# --- run_python -------------------------------------------------------------------------


def test_run_python_result_frame_and_figure(tools: VeritasTools, tmp_path: Path) -> None:
    dataset_id = tools.ingest_dataset(_small(tmp_path)).dataset_id
    code = (
        "import matplotlib.pyplot as plt\n"
        "result = df.groupby('category', as_index=False)['amount'].sum()\n"
        "plt.figure(); plt.plot([1, 2, 3])\n"
    )
    result = tools.run_python(code, [dataset_id])
    assert result.status == "ok"
    assert result.row_count == 2  # two categories
    assert result.figure_count == 1


def test_run_python_policy_violation_raises(tools: VeritasTools) -> None:
    with pytest.raises(UnsafePythonError):
        tools.run_python("import os\nresult = os")


def test_run_python_user_exception_is_an_error_artifact(tools: VeritasTools) -> None:
    result = tools.run_python("result = 1 / 0")
    assert result.status == "error"
    assert result.error is not None
    assert result.figure_count == 0


# --- discover ---------------------------------------------------------------------------


def test_discover_surfaces_planted_signal(tools: VeritasTools, tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=300)
    frame = pd.DataFrame({"x": x, "y": 2 * x + rng.normal(0, 0.3, 300)})
    dataset_id = tools.ingest_dataset(_csv(tmp_path, frame, "corr")).dataset_id
    report = tools.discover(dataset_id)
    assert report.summary.surfaced >= 1
    assert report.discoveries[0].artifact_id.startswith("art_")  # a verifiable receipt


def test_discover_overrides_cap(tools: VeritasTools, tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    base = rng.normal(size=300)
    frame = pd.DataFrame({f"v{i}": base + rng.normal(0, 0.3, 300) for i in range(6)})
    dataset_id = tools.ingest_dataset(_csv(tmp_path, frame, "many")).dataset_id
    report = tools.discover(dataset_id, fdr_alpha=0.05, max_findings=2)
    assert report.summary.surfaced == 2
    assert report.summary.fdr_alpha == 0.05


# --- findings / verification ------------------------------------------------------------


def _ingested_sum(tools: VeritasTools, tmp_path: Path) -> tuple[str, str]:
    """Ingest the small frame, run a SUM, and return (artifact_id, finding-ready column)."""
    dataset_id = tools.ingest_dataset(_small(tmp_path)).dataset_id
    result = tools.run_sql(f'SELECT sum(amount) AS total FROM "{dataset_id}"')
    return result.artifact_id, "total"


def test_record_then_verify_passes(tools: VeritasTools, tmp_path: Path) -> None:
    artifact_id, column = _ingested_sum(tools, tmp_path)
    claim = NumericClaim(description="sum", artifact_id=artifact_id, column=column, value=6.0)
    finding = tools.record_finding("total amount is 6", [claim])
    assert finding.status == "unverified"
    assert finding.claim_count == 1
    verdict = tools.verify_finding(finding.finding_id)
    assert verdict.verified
    assert verdict.status == "verified"
    assert verdict.claim_checks[0].ok
    assert verdict.unbacked_numbers == []


def test_verify_refutes_wrong_claim(tools: VeritasTools, tmp_path: Path) -> None:
    artifact_id, column = _ingested_sum(tools, tmp_path)
    claim = NumericClaim(description="sum", artifact_id=artifact_id, column=column, value=999.0)
    finding = tools.record_finding("total amount is 999", [claim])
    verdict = tools.verify_finding(finding.finding_id)
    assert not verdict.verified
    assert verdict.status == "refuted"
    assert not verdict.claim_checks[0].ok


def test_verify_flags_unbacked_prose_number(tools: VeritasTools, tmp_path: Path) -> None:
    artifact_id, column = _ingested_sum(tools, tmp_path)
    claim = NumericClaim(description="sum", artifact_id=artifact_id, column=column, value=6.0)
    finding = tools.record_finding("total is 6 across 42 stores", [claim])
    verdict = tools.verify_finding(finding.finding_id)
    assert not verdict.verified  # "42" is backed by no claim
    assert "42" in verdict.unbacked_numbers


def test_verify_unknown_finding_raises(tools: VeritasTools) -> None:
    with pytest.raises(UnknownFindingError):
        tools.verify_finding("fnd_does_not_exist")


# --- artifact drill-down / state --------------------------------------------------------


def test_get_artifact_returns_full_view(tools: VeritasTools, tmp_path: Path) -> None:
    artifact_id, _ = _ingested_sum(tools, tmp_path)
    view = tools.get_artifact(artifact_id)
    assert isinstance(view, ArtifactView)
    assert view.kind == "sql"
    assert view.data_path is not None and view.data_path.endswith(".parquet")


def test_get_artifact_unknown_raises(tools: VeritasTools) -> None:
    with pytest.raises(UnknownArtifactError):
        tools.get_artifact("art_does_not_exist")


def test_artifact_digest_truncates_long_source(tools: VeritasTools, tmp_path: Path) -> None:
    dataset_id = tools.ingest_dataset(_small(tmp_path)).dataset_id
    long_alias = "x" * 300
    tools.run_sql(f'SELECT amount AS {long_alias} FROM "{dataset_id}"')
    digest = tools.investigation_state().artifacts[0]
    assert digest.source.endswith("…")
    assert len(digest.source) <= 160


def test_investigation_state_aggregates_everything(tools: VeritasTools, tmp_path: Path) -> None:
    artifact_id, column = _ingested_sum(tools, tmp_path)
    claim = NumericClaim(description="sum", artifact_id=artifact_id, column=column, value=6.0)
    tools.record_finding("total is 6", [claim])
    state = tools.investigation_state()
    assert state.session_id == tools.session.session_id
    assert len(state.datasets) == 1
    assert len(state.artifacts) == 1
    assert len(state.findings) == 1
    assert state.findings[0].status == "unverified"


# --- server wiring + configuration ------------------------------------------------------


def test_create_server_registers_nine_tools(tools: VeritasTools) -> None:
    server = create_server(tools, "veritas")
    registered = asyncio.run(server.list_tools())
    assert [tool.name for tool in registered] == list(_TOOL_ORDER)
    assert len(_TOOL_ORDER) == 9
    by_name = {tool.name: tool for tool in registered}
    assert "claims" in by_name["record_finding"].inputSchema["properties"]


def test_call_tool_runs_the_full_loop(tools: VeritasTools, tmp_path: Path) -> None:
    server = create_server(tools)
    ingest = _call(server, "ingest_dataset", {"path": _small(tmp_path)})
    dataset_id = ingest["dataset_id"]
    sql = _call(server, "run_sql", {"sql": f'SELECT sum(amount) AS total FROM "{dataset_id}"'})
    claim = {
        "description": "sum",
        "artifact_id": sql["artifact_id"],
        "column": "total",
        "value": 6.0,
    }
    finding = _call(server, "record_finding", {"headline": "total is 6", "claims": [claim]})
    verdict = _call(server, "verify_finding", {"finding_id": finding["finding_id"]})
    assert verdict["verified"] is True


def test_server_name_defaults_and_overrides() -> None:
    assert server_name({}) == "veritas"
    assert server_name({SERVER_NAME_ENV: "audit"}) == "audit"


def test_create_server_uses_default_name(tools: VeritasTools) -> None:
    assert create_server(tools).name == "veritas"
