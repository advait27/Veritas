"""The Veritas MCP server: nine tools over stdio that drive an investigation (M5).

The investigation core (ingest, profile, ``run_sql``, ``run_python``, discovery,
findings/verification) is wired here into FastMCP tools, one investigation session per
server process. Each tool is a method of :class:`VeritasTools` so the logic is plain,
synchronous, and unit-testable; :func:`create_server` registers those methods on a
:class:`~mcp.server.fastmcp.FastMCP` instance, and :func:`main` is the ``veritas``
console entry point (DECISIONS.md, D-001/D-002/D-029).

The tools return the lean views in :mod:`veritas.responses`, never raw on-disk records.
The load-bearing field throughout is ``artifact_id``: every number a report will make
must cite the artifact it came from, and ``verify_finding`` re-checks that citation in
deterministic Python — never an LLM judgement.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from veritas import DEFAULT_SERVER_NAME
from veritas.discovery import DiscoveryConfig, DiscoveryReport
from veritas.discovery import discover as run_discovery
from veritas.execute import run_sql as run_sql_query
from veritas.findings import add_finding, verify_and_record
from veritas.ingest import ingest_file
from veritas.methodology import methodology
from veritas.profile import profile_dataset as build_profile
from veritas.responses import (
    ArtifactDigest,
    ArtifactView,
    ClaimCheckView,
    ColumnInfo,
    DatasetSummary,
    ExecutionResult,
    FindingView,
    InvestigationState,
    VerificationView,
)
from veritas.sandbox import run_python as run_python_sandbox
from veritas.session import InvestigationSession, NumericClaim

if TYPE_CHECKING:
    from collections.abc import Mapping

    from veritas.findings import FindingVerification
    from veritas.session import ArtifactRecord, DatasetRecord, Finding

SERVER_NAME_ENV = "VERITAS_SERVER_NAME"
"""Environment variable overriding the server's display name (DECISIONS.md, D-002)."""

SESSION_DIR_ENV = "VERITAS_SESSION_DIR"
"""Environment variable setting the parent directory for session directories (D-029)."""

_SOURCE_PREVIEW_CHARS = 160


def _dataset_summary(record: DatasetRecord) -> DatasetSummary:
    """Project a dataset record into its outward summary view."""
    columns = [
        ColumnInfo(
            position=column.position,
            name=column.normalized_name,
            original_name=column.original_name,
            type=column.duckdb_type,
        )
        for column in record.schema_record.columns
    ]
    return DatasetSummary(
        dataset_id=record.dataset_id,
        name=record.name,
        row_count=record.row_count,
        column_count=record.column_count,
        columns=columns,
    )


def _execution_result(record: ArtifactRecord) -> ExecutionResult:
    """Project an execution artifact into the receipt the model reads."""
    return ExecutionResult(
        artifact_id=record.artifact_id,
        kind=record.kind,
        status=record.status,
        row_count=record.row_count,
        columns=record.columns,
        column_types=record.column_types,
        preview=record.preview,
        stdout=record.stdout,
        figure_count=len(record.figure_paths),
        error=record.error,
    )


def _finding_view(finding: Finding) -> FindingView:
    """Project a finding into its overview view."""
    return FindingView(
        finding_id=finding.finding_id,
        headline=finding.headline,
        detail=finding.detail,
        status=finding.status,
        claim_count=len(finding.claims),
    )


def _verification_view(finding: Finding, result: FindingVerification) -> VerificationView:
    """Project a verification verdict into its outward view (status from the verdict)."""
    checks = [
        ClaimCheckView(
            description=check.claim.description,
            artifact_id=check.claim.artifact_id,
            column=check.claim.column,
            ok=check.ok,
            actual_value=check.actual_value,
            reason=check.reason,
        )
        for check in result.claim_checks
    ]
    return VerificationView(
        finding_id=finding.finding_id,
        verified=result.verified,
        status="verified" if result.verified else "refuted",
        claim_checks=checks,
        unbacked_numbers=result.unbacked_numbers,
    )


def _artifact_digest(record: ArtifactRecord) -> ArtifactDigest:
    """Project an artifact into a one-line overview entry (source truncated)."""
    source = record.source
    truncated = (
        source
        if len(source) <= _SOURCE_PREVIEW_CHARS
        else source[: _SOURCE_PREVIEW_CHARS - 1] + "…"
    )
    return ArtifactDigest(
        artifact_id=record.artifact_id,
        kind=record.kind,
        status=record.status,
        source=truncated,
        row_count=record.row_count,
        created_at=record.created_at,
    )


def _artifact_view(record: ArtifactRecord) -> ArtifactView:
    """Project an artifact into its full drill-down view."""
    return ArtifactView(
        artifact_id=record.artifact_id,
        kind=record.kind,
        status=record.status,
        source=record.source,
        row_count=record.row_count,
        columns=record.columns,
        column_types=record.column_types,
        preview=record.preview,
        stdout=record.stdout,
        data_path=record.data_path,
        figure_paths=record.figure_paths,
        error=record.error,
        created_at=record.created_at,
    )


@dataclass
class VeritasTools:
    """The nine MCP tools, bound to one :class:`InvestigationSession`.

    Holding the session here keeps every tool a plain, synchronous method that can be
    called and asserted on directly in tests; :func:`create_server` is the only place
    that knows about FastMCP.

    Example:
        ``tools = VeritasTools(session); tools.run_sql("SELECT 1 AS n")``
    """

    session: InvestigationSession

    def ingest_dataset(self, path: str, name: str | None = None) -> DatasetSummary:
        """Load a CSV/Parquet/Excel file into the session and register it as a dataset.

        Args:
            path: filesystem path to a ``.csv``, ``.parquet``, or ``.xlsx`` file.
            name: optional human label; defaults to the file stem.

        Returns:
            A :class:`~veritas.responses.DatasetSummary` with the ``dataset_id`` to query
            by and the original-to-normalized column map.

        Example:
            ``tools.ingest_dataset("orders.csv", name="orders")``
        """
        return _dataset_summary(ingest_file(self.session, path, name))

    def profile_dataset(self, dataset_id: str) -> str:
        """Profile a dataset (per-column stats, date candidates, time coverage).

        Args:
            dataset_id: the dataset to profile (from :meth:`ingest_dataset`).

        Returns:
            A human-readable markdown report; cell-derived text is length-capped.

        Example:
            ``print(tools.profile_dataset(dataset_id))``
        """
        return build_profile(self.session, dataset_id).to_markdown()

    def run_sql(self, sql: str) -> ExecutionResult:
        """Run a read-only ``SELECT`` and persist its full result as an artifact.

        The query passes the read-only gate before any execution; its full result streams
        to a Parquet artifact and only a bounded, sanitized preview returns here. Cite the
        returned ``artifact_id`` in a claim to make any number from it a receipt.

        Args:
            sql: a single read-only ``SELECT`` statement.

        Returns:
            An :class:`~veritas.responses.ExecutionResult`; ``status='error'`` (with a
            sanitized message) if DuckDB rejected the query at runtime.

        Raises:
            UnsafeSqlError: if ``sql`` is not a safe read-only query (before execution).

        Example:
            ``tools.run_sql("SELECT category, count(*) AS n FROM ds GROUP BY 1")``
        """
        return _execution_result(run_sql_query(self.session, sql))

    def run_python(
        self,
        code: str,
        dataset_ids: Sequence[str] | None = None,
        timeout_seconds: int = 30,
    ) -> ExecutionResult:
        """Run sandboxed Python over the session's datasets and persist the result.

        The code is statically vetted, then run in an isolated subprocess (no network,
        CPU/memory limits, only the requested datasets exposed as ``df``/``datasets``). A
        ``result`` DataFrame becomes a Parquet artifact; figures are saved as PNGs.

        Args:
            code: the Python source to execute; assign ``result`` for a tabular receipt.
            dataset_ids: dataset ids to expose; defaults to every registered dataset.
            timeout_seconds: wall-clock limit for the subprocess.

        Returns:
            An :class:`~veritas.responses.ExecutionResult` describing the run.

        Raises:
            UnsafePythonError: if ``code`` fails the static policy (before execution).

        Example:
            ``tools.run_python("result = df.describe()", [dataset_id])``
        """
        record = run_python_sandbox(
            self.session, code, dataset_ids, timeout_seconds=timeout_seconds
        )
        return _execution_result(record)

    def discover(
        self,
        dataset_id: str,
        fdr_alpha: float | None = None,
        max_findings: int | None = None,
    ) -> DiscoveryReport:
        """Run the autonomous discovery pass: generate, test, suppress, rank.

        Probes (numeric correlations, group differences, categorical associations) are
        tested, then suppressed hard — Benjamini-Hochberg FDR control, per-metric
        effect-size floors, and a finding cap. Silence is a feature: a no-signal dataset
        yields no discoveries and a summary saying how many probes were dropped and why.

        Args:
            dataset_id: the dataset to probe.
            fdr_alpha: false-discovery-rate level (defaults to the project default).
            max_findings: hard cap on surfaced discoveries (defaults to the default).

        Returns:
            A :class:`~veritas.discovery.DiscoveryReport`; each discovery carries the
            ``artifact_id`` of its persisted statistics, so its numbers are receipts.

        Example:
            ``tools.discover(dataset_id, max_findings=3)``
        """
        defaults = DiscoveryConfig()
        config = DiscoveryConfig(
            fdr_alpha=defaults.fdr_alpha if fdr_alpha is None else fdr_alpha,
            max_findings=defaults.max_findings if max_findings is None else max_findings,
        )
        return run_discovery(self.session, dataset_id, config)

    def record_finding(
        self,
        headline: str,
        claims: Sequence[NumericClaim],
        detail: str = "",
    ) -> FindingView:
        """Register an (unverified) finding with the numeric claims that back it.

        Each claim pins a number to a cell in an executed artifact by ``column`` and
        optional ``where`` filters. The finding is created ``unverified``; call
        :meth:`verify_finding` to check it before it may enter a report.

        Args:
            headline: the one-line claim ("Category A's mean amount is 45.2").
            claims: the numeric claims backing the finding's numbers.
            detail: optional supporting narrative (also scanned for unbacked numbers).

        Returns:
            A :class:`~veritas.responses.FindingView` with the new ``finding_id``.

        Example:
            ``tools.record_finding("6 rows ingested", [claim])``
        """
        return _finding_view(add_finding(self.session, headline, claims, detail))

    def verify_finding(self, finding_id: str) -> VerificationView:
        """Deterministically verify a finding and persist its verified/refuted status.

        Every claim is re-read straight from its artifact's Parquet and compared to the
        claimed value; the prose is scanned and any number no claim backs is reported.
        Only a finding that passes both checks is ``verified`` — receipts, or it didn't
        happen. This is plain Python, never an LLM judgement.

        Args:
            finding_id: the finding to verify (from :meth:`record_finding`).

        Returns:
            A :class:`~veritas.responses.VerificationView` with per-claim checks and any
            unbacked prose numbers.

        Raises:
            UnknownFindingError: if no such finding is registered in this session.

        Example:
            ``tools.verify_finding(finding_id).verified``
        """
        finding = self.session.get_finding(finding_id)
        return _verification_view(finding, verify_and_record(self.session, finding))

    def get_artifact(self, artifact_id: str) -> ArtifactView:
        """Return the full detail of one execution artifact: the receipt behind a claim.

        Args:
            artifact_id: the artifact to inspect (from a ``run_sql``/``run_python`` call).

        Returns:
            An :class:`~veritas.responses.ArtifactView` with the schema, preview, stdout,
            and on-disk result/figure paths.

        Raises:
            UnknownArtifactError: if no such artifact is registered in this session.

        Example:
            ``tools.get_artifact(artifact_id).preview``
        """
        return _artifact_view(self.session.get_artifact(artifact_id))

    def investigation_state(self) -> InvestigationState:
        """Return the whole investigation at a glance: datasets, artifacts, findings.

        Returns:
            An :class:`~veritas.responses.InvestigationState` listing every dataset
            summary, a one-line digest of every artifact, and every finding's status.

        Example:
            ``tools.investigation_state().findings``
        """
        return InvestigationState(
            session_id=self.session.session_id,
            datasets=[_dataset_summary(record) for record in self.session.list_datasets()],
            artifacts=[_artifact_digest(record) for record in self.session.list_artifacts()],
            findings=[_finding_view(finding) for finding in self.session.list_findings()],
        )


_TOOL_ORDER = (
    "ingest_dataset",
    "profile_dataset",
    "run_sql",
    "run_python",
    "discover",
    "record_finding",
    "verify_finding",
    "get_artifact",
    "investigation_state",
)


def server_name(env: Mapping[str, str]) -> str:
    """Return the configured MCP server display name (DECISIONS.md, D-002).

    Example:
        >>> server_name({"VERITAS_SERVER_NAME": "audit"})
        'audit'
    """
    return env.get(SERVER_NAME_ENV, DEFAULT_SERVER_NAME)


METHODOLOGY_PROMPT_NAME = "investigation_methodology"
"""Name of the MCP prompt that serves the investigator methodology (M7)."""


def _methodology_prompt() -> str:
    """How to run a rigorous, receipts-backed investigation with Veritas."""
    return methodology()


def create_server(tools: VeritasTools, name: str | None = None) -> FastMCP:
    """Build a FastMCP server exposing the nine tools and the methodology prompt.

    The nine :class:`VeritasTools` methods register as tools; the investigator
    methodology registers as an MCP prompt (``investigation_methodology``), so a client
    that only speaks to the server still receives the receipts-or-it-didn't-happen
    workflow without needing the separate Agent skill.

    Args:
        tools: the :class:`VeritasTools` whose methods become the MCP tools.
        name: the server display name; defaults to :data:`DEFAULT_SERVER_NAME`.

    Returns:
        A configured :class:`~mcp.server.fastmcp.FastMCP` ready to ``run()`` over stdio.

    Example:
        ``server = create_server(VeritasTools(session), "veritas")``
    """
    mcp = FastMCP(name or DEFAULT_SERVER_NAME)
    for tool_name in _TOOL_ORDER:
        mcp.tool()(getattr(tools, tool_name))
    mcp.prompt(name=METHODOLOGY_PROMPT_NAME)(_methodology_prompt)
    return mcp


def main() -> None:  # pragma: no cover - process entry point, run via the console script
    """Run the Veritas MCP server over stdio (the ``veritas`` console entry point)."""
    base = os.environ.get(SESSION_DIR_ENV)
    session = InvestigationSession(base_dir=Path(base) if base else None)
    try:
        create_server(VeritasTools(session), server_name(os.environ)).run()
    finally:
        session.close()
