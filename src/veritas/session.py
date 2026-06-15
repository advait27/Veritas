"""Investigation session state: session directory, DuckDB database, dataset registry.

An :class:`InvestigationSession` owns everything produced during one investigation: a
directory on disk, the DuckDB database file inside it, and the registry of ingested
datasets. Later milestones add artifacts (M2) and findings (M3) to the same session.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Literal

import duckdb
from pydantic import BaseModel, Field, model_validator

DEFAULT_SESSIONS_BASE_DIR = Path(".veritas-sessions")
"""Default parent directory for session directories, relative to the working directory."""

SourceFormat = Literal["csv", "parquet", "xlsx"]
ArtifactKind = Literal["sql", "python"]
FindingStatus = Literal["unverified", "verified", "refuted"]


def new_id(prefix: str) -> str:
    """Return a short unique identifier such as ``ds_1f2e3d4c5b6a``.

    Example:
        >>> new_id("ds").startswith("ds_")
        True
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def quote_identifier(identifier: str) -> str:
    """Quote a string for safe use as a SQL identifier in DuckDB.

    Example:
        >>> quote_identifier('we"ird')
        '"we""ird"'
    """
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


class ColumnSchema(BaseModel):
    """One column's identity: original (untrusted) name and SQL-safe normalized name."""

    position: int
    original_name: str
    normalized_name: str
    duckdb_type: str


class SchemaRecord(BaseModel):
    """Bidirectional column-name mapping plus engine types for one dataset.

    ``original_name`` values are preserved byte-for-byte from the source file and are
    untrusted input. ``normalized_name`` values are unique, SQL-safe identifiers used in
    generated SQL (see :func:`veritas.ingest.normalize_column_names`). Duplicate original
    names map to several normalized names, so original→normalized is one-to-many while
    normalized→original is unique.
    """

    columns: list[ColumnSchema]

    @model_validator(mode="after")
    def _check_normalized_unique(self) -> SchemaRecord:
        names = [column.normalized_name for column in self.columns]
        if len(set(names)) != len(names):
            msg = f"normalized column names must be unique, got {names!r}"
            raise ValueError(msg)
        return self

    def normalized_for(self, original_name: str) -> list[str]:
        """Return the normalized identifiers for an original column name, in column order.

        Example:
            a file with header ``revenue,revenue`` yields
            ``normalized_for("revenue") == ["revenue", "revenue_2"]``.
        """
        return [c.normalized_name for c in self.columns if c.original_name == original_name]

    def original_for(self, normalized_name: str) -> str:
        """Return the original column name behind a normalized identifier.

        Example:
            ``original_for("revenue_2") == "revenue"`` for a ``revenue,revenue`` header.

        Raises:
            KeyError: if no column has that normalized name.
        """
        for column in self.columns:
            if column.normalized_name == normalized_name:
                return column.original_name
        raise KeyError(normalized_name)


class DatasetRecord(BaseModel):
    """Metadata for one ingested dataset, persisted under the session directory."""

    dataset_id: str
    name: str
    source_path: str
    source_format: SourceFormat
    ingested_at: datetime
    row_count: int
    column_count: int
    table_name: str
    schema_record: SchemaRecord


class ArtifactRecord(BaseModel):
    """Metadata for one execution artifact: the receipt for a ``run_sql``/``run_python``.

    Every execution — successful or failed — is recorded so that later milestones can
    verify numeric claims against it (M3). ``data_path``/``figure_paths`` are paths
    relative to the session directory; the full result lives only on disk (the
    ``preview`` is a bounded, sanitized excerpt for model context).
    """

    artifact_id: str
    kind: ArtifactKind
    created_at: datetime
    source: str
    status: Literal["ok", "error"]
    row_count: int | None = None
    columns: list[str] = Field(default_factory=list)
    column_types: list[str] = Field(default_factory=list)
    data_path: str | None = None
    figure_paths: list[str] = Field(default_factory=list)
    stdout: str | None = None
    error: str | None = None
    preview: str = ""


class UnknownDatasetError(KeyError):
    """Raised when a ``dataset_id`` is not present in the session registry."""


class UnknownArtifactError(KeyError):
    """Raised when an ``artifact_id`` is not present in the session registry."""


class NumericClaim(BaseModel):
    """One numeric assertion in a finding, pinned to a cell in an executed artifact.

    The cell is located by ``column`` plus an optional ``where`` map of
    ``column -> value`` equality filters (matched as text, so types never matter); with
    no filters the artifact result must be a single row. This keyed lookup is order-free
    and therefore deterministic — unlike a positional row index, which DuckDB's parallel
    Parquet reader could reorder. ``value`` is the claimed number, compared to the cell
    with :func:`math.isclose` using ``rel_tol``/``abs_tol``.
    """

    description: str
    artifact_id: str
    column: str
    where: dict[str, str] = Field(default_factory=dict)
    value: float
    rel_tol: float = 1e-9
    abs_tol: float = 1e-12


class Finding(BaseModel):
    """A claim destined for a report, with the numeric claims that back it.

    Only a finding whose every :class:`NumericClaim` matches its artifact may be
    reported (``status == 'verified'``); verification is deterministic Python over the
    persisted artifacts, never an LLM judgement (see :mod:`veritas.findings`).
    """

    finding_id: str
    headline: str
    detail: str = ""
    claims: list[NumericClaim] = Field(default_factory=list)
    created_at: datetime
    status: FindingStatus = "unverified"


class UnknownFindingError(KeyError):
    """Raised when a ``finding_id`` is not present in the session registry."""


class InvestigationSession:
    """Owns the session directory, its DuckDB database, and the dataset registry.

    Example:
        >>> import tempfile
        >>> with InvestigationSession(base_dir=Path(tempfile.mkdtemp())) as session:
        ...     session.list_datasets()
        []
    """

    def __init__(self, base_dir: Path | None = None, session_id: str | None = None) -> None:
        """Create (or re-enter) the session directory and open its DuckDB database.

        Args:
            base_dir: parent directory for session directories; defaults to
                :data:`DEFAULT_SESSIONS_BASE_DIR`.
            session_id: reuse an existing identifier instead of generating one.
        """
        self.session_id = session_id if session_id is not None else new_id("sess")
        base = base_dir if base_dir is not None else DEFAULT_SESSIONS_BASE_DIR
        self.session_dir = base / self.session_id
        self._datasets_dir = self.session_dir / "datasets"
        self._datasets_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.session_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._findings_dir = self.session_dir / "findings"
        self._findings_dir.mkdir(parents=True, exist_ok=True)
        self.conn: duckdb.DuckDBPyConnection = duckdb.connect(
            str(self.session_dir / "session.duckdb")
        )
        self._datasets: dict[str, DatasetRecord] = {}
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._findings: dict[str, Finding] = {}

    @classmethod
    def open(cls, session_dir: Path) -> InvestigationSession:
        """Reopen an existing session directory, reloading the dataset registry.

        Example:
            ``InvestigationSession.open(Path(".veritas-sessions/sess_ab12cd34ef56"))``
        """
        session = cls(base_dir=session_dir.parent, session_id=session_dir.name)
        for meta_path in sorted(session._datasets_dir.glob("*.json")):
            record = DatasetRecord.model_validate_json(meta_path.read_text(encoding="utf-8"))
            session._datasets[record.dataset_id] = record
        for art_path in sorted(session.artifacts_dir.glob("*.json")):
            artifact = ArtifactRecord.model_validate_json(art_path.read_text(encoding="utf-8"))
            session._artifacts[artifact.artifact_id] = artifact
        for finding_path in sorted(session._findings_dir.glob("*.json")):
            finding = Finding.model_validate_json(finding_path.read_text(encoding="utf-8"))
            session._findings[finding.finding_id] = finding
        return session

    def register_dataset(self, record: DatasetRecord) -> None:
        """Add a dataset to the registry and persist its metadata as JSON.

        Example:
            ``session.register_dataset(record)`` writes ``datasets/<dataset_id>.json``.
        """
        self._datasets[record.dataset_id] = record
        meta_path = self._datasets_dir / f"{record.dataset_id}.json"
        meta_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    def get_dataset(self, dataset_id: str) -> DatasetRecord:
        """Look up a dataset record by id.

        Example:
            ``session.get_dataset("ds_1f2e3d4c5b6a").row_count``

        Raises:
            UnknownDatasetError: if the id was never registered in this session.
        """
        try:
            return self._datasets[dataset_id]
        except KeyError:
            known = ", ".join(sorted(self._datasets)) or "none"
            msg = f"unknown dataset_id {dataset_id!r} (known: {known})"
            raise UnknownDatasetError(msg) from None

    def list_datasets(self) -> list[DatasetRecord]:
        """Return all registered datasets, oldest first.

        Example:
            ``[record.dataset_id for record in session.list_datasets()]``
        """
        return sorted(self._datasets.values(), key=lambda r: (r.ingested_at, r.dataset_id))

    def register_artifact(self, record: ArtifactRecord) -> None:
        """Add an execution artifact to the registry and persist its metadata as JSON.

        Example:
            ``session.register_artifact(record)`` writes ``artifacts/<artifact_id>.json``.
        """
        self._artifacts[record.artifact_id] = record
        meta_path = self.artifacts_dir / f"{record.artifact_id}.json"
        meta_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        """Look up an execution artifact by id.

        Example:
            ``session.get_artifact("art_1f2e3d4c5b6a").row_count``

        Raises:
            UnknownArtifactError: if the id was never registered in this session.
        """
        try:
            return self._artifacts[artifact_id]
        except KeyError:
            known = ", ".join(sorted(self._artifacts)) or "none"
            msg = f"unknown artifact_id {artifact_id!r} (known: {known})"
            raise UnknownArtifactError(msg) from None

    def list_artifacts(self) -> list[ArtifactRecord]:
        """Return all registered artifacts, oldest first.

        Example:
            ``[record.artifact_id for record in session.list_artifacts()]``
        """
        return sorted(self._artifacts.values(), key=lambda r: (r.created_at, r.artifact_id))

    def register_finding(self, finding: Finding) -> None:
        """Add (or replace) a finding in the registry and persist it as JSON.

        Re-registering the same ``finding_id`` overwrites it — that is how
        :func:`veritas.findings.verify_finding` records a status change.

        Example:
            ``session.register_finding(finding)`` writes ``findings/<finding_id>.json``.
        """
        self._findings[finding.finding_id] = finding
        meta_path = self._findings_dir / f"{finding.finding_id}.json"
        meta_path.write_text(finding.model_dump_json(indent=2), encoding="utf-8")

    def get_finding(self, finding_id: str) -> Finding:
        """Look up a finding by id.

        Example:
            ``session.get_finding("fnd_1f2e3d4c5b6a").status``

        Raises:
            UnknownFindingError: if the id was never registered in this session.
        """
        try:
            return self._findings[finding_id]
        except KeyError:
            known = ", ".join(sorted(self._findings)) or "none"
            msg = f"unknown finding_id {finding_id!r} (known: {known})"
            raise UnknownFindingError(msg) from None

    def list_findings(self) -> list[Finding]:
        """Return all registered findings, oldest first.

        Example:
            ``[f.finding_id for f in session.list_findings() if f.status == "verified"]``
        """
        return sorted(self._findings.values(), key=lambda f: (f.created_at, f.finding_id))

    def close(self) -> None:
        """Close the DuckDB connection (the session directory stays on disk)."""
        self.conn.close()

    def __enter__(self) -> InvestigationSession:
        """Return the session itself for use as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the DuckDB connection on context exit."""
        self.close()
