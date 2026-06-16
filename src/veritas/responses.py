"""Lean, model-context-friendly views returned by the MCP tools (M5).

The internal records (:class:`~veritas.session.DatasetRecord`,
:class:`~veritas.session.ArtifactRecord`, :class:`~veritas.session.Finding`) carry
on-disk paths and bookkeeping that the model should not have to wade through. These
view models are the *outward* shapes the tools return: bounded, already-sanitized (the
previews and errors were sanitized when their artifacts were written), and centred on
the identifiers the model needs to keep investigating — above all the ``artifact_id``
that turns a number into a receipt.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ColumnInfo(BaseModel):
    """One column's identity in a dataset summary: normalized name, original, type."""

    position: int
    name: str
    original_name: str
    type: str


class DatasetSummary(BaseModel):
    """What ``ingest_dataset`` returns: the id to query by, plus the column map."""

    dataset_id: str
    name: str
    row_count: int
    column_count: int
    columns: list[ColumnInfo]


class ExecutionResult(BaseModel):
    """The receipt for a ``run_sql``/``run_python`` call: id, schema, bounded preview.

    ``artifact_id`` is the load-bearing field — a later :class:`NumericClaim` cites it to
    prove a number. ``status`` is ``ok`` or ``error``; on error ``preview`` is empty and
    ``error`` carries the sanitized message (a failed run is still a recorded receipt).
    """

    artifact_id: str
    kind: str
    status: str
    row_count: int | None = None
    columns: list[str] = Field(default_factory=list)
    column_types: list[str] = Field(default_factory=list)
    preview: str = ""
    stdout: str | None = None
    figure_count: int = 0
    error: str | None = None


class FindingView(BaseModel):
    """A finding as the model sees it: headline, status, and how many claims back it."""

    finding_id: str
    headline: str
    detail: str
    status: str
    claim_count: int


class ClaimCheckView(BaseModel):
    """One claim's verification outcome: did the artifact cell match the claimed value."""

    description: str
    artifact_id: str
    column: str
    ok: bool
    actual_value: float | None = None
    reason: str = ""


class VerificationView(BaseModel):
    """The deterministic verdict for a finding: per-claim checks and unbacked prose.

    ``verified`` is true only when every claim matched *and* ``unbacked_numbers`` is
    empty — a prose number with no backing claim fails the finding (receipts, or it
    didn't happen). ``status`` is the persisted result (``verified``/``refuted``).
    """

    finding_id: str
    verified: bool
    status: str
    claim_checks: list[ClaimCheckView]
    unbacked_numbers: list[str]


class ArtifactDigest(BaseModel):
    """A one-line artifact entry for the investigation overview (source truncated)."""

    artifact_id: str
    kind: str
    status: str
    source: str
    row_count: int | None = None
    created_at: datetime


class ArtifactView(BaseModel):
    """Full drill-down on one artifact: the receipt behind a claim, in detail.

    ``data_path``/``figure_paths`` are relative to the session directory; the full
    result lives only on disk, while ``preview`` is the bounded excerpt the model reads.
    """

    artifact_id: str
    kind: str
    status: str
    source: str
    row_count: int | None = None
    columns: list[str] = Field(default_factory=list)
    column_types: list[str] = Field(default_factory=list)
    preview: str = ""
    stdout: str | None = None
    data_path: str | None = None
    figure_paths: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime


class InvestigationState(BaseModel):
    """The whole investigation at a glance: datasets, artifacts, and findings."""

    session_id: str
    datasets: list[DatasetSummary]
    artifacts: list[ArtifactDigest]
    findings: list[FindingView]
