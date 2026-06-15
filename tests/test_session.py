"""Tests for InvestigationSession, SchemaRecord, and dataset registry persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pydantic
import pytest

from veritas.ingest import ingest_file
from veritas.profile import profile_dataset
from veritas.session import (
    ArtifactRecord,
    ColumnSchema,
    DatasetRecord,
    Finding,
    InvestigationSession,
    NumericClaim,
    SchemaRecord,
    UnknownArtifactError,
    UnknownDatasetError,
    UnknownFindingError,
    new_id,
    quote_identifier,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _record(dataset_id: str = "ds_abc123abc123", table: str = "t") -> DatasetRecord:
    return DatasetRecord(
        dataset_id=dataset_id,
        name="demo",
        source_path="demo.csv",
        source_format="csv",
        ingested_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        row_count=1,
        column_count=1,
        table_name=table,
        schema_record=SchemaRecord(
            columns=[
                ColumnSchema(
                    position=0, original_name="X", normalized_name="x", duckdb_type="INTEGER"
                )
            ]
        ),
    )


def _artifact(artifact_id: str = "art_abc123abc123") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        kind="sql",
        created_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
        source="SELECT 1",
        status="ok",
        row_count=1,
        columns=["n"],
        column_types=["INTEGER"],
        data_path="artifacts/art_abc123abc123.parquet",
        preview="| n |\n| --- |\n| 1 |",
    )


def _finding(finding_id: str = "fnd_abc123abc123") -> Finding:
    return Finding(
        finding_id=finding_id,
        headline="6 rows",
        claims=[NumericClaim(description="n", artifact_id="art_x", column="n", value=6.0)],
        created_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
    )


def test_new_id_format() -> None:
    first, second = new_id("ds"), new_id("ds")
    assert first.startswith("ds_")
    assert len(first) == 3 + 12
    assert first != second


def test_quote_identifier_escapes_quotes() -> None:
    assert quote_identifier('we"ird') == '"we""ird"'


def test_session_creates_directory_and_database(tmp_path: Path) -> None:
    with InvestigationSession(base_dir=tmp_path) as session:
        assert session.session_dir.is_dir()
        assert (session.session_dir / "session.duckdb").exists()
        assert session.session_id.startswith("sess_")


def test_register_get_list_and_persistence(session: InvestigationSession) -> None:
    record = _record()
    session.register_dataset(record)
    assert session.get_dataset(record.dataset_id) == record
    assert session.list_datasets() == [record]
    meta_path = session.session_dir / "datasets" / f"{record.dataset_id}.json"
    assert DatasetRecord.model_validate_json(meta_path.read_text()) == record


def test_get_unknown_dataset_raises_with_known_ids(session: InvestigationSession) -> None:
    session.register_dataset(_record())
    with pytest.raises(UnknownDatasetError, match=r"ds_missing.*ds_abc123abc123"):
        session.get_dataset("ds_missing")


def test_list_datasets_sorted_oldest_first(session: InvestigationSession) -> None:
    newer = _record(dataset_id="ds_b").model_copy(
        update={"ingested_at": datetime(2026, 6, 11, tzinfo=UTC)}
    )
    older = _record(dataset_id="ds_a")
    session.register_dataset(newer)
    session.register_dataset(older)
    assert [r.dataset_id for r in session.list_datasets()] == ["ds_a", "ds_b"]


def test_artifact_register_get_list_and_persistence(session: InvestigationSession) -> None:
    record = _artifact()
    session.register_artifact(record)
    assert session.get_artifact(record.artifact_id) == record
    assert session.list_artifacts() == [record]
    meta_path = session.artifacts_dir / f"{record.artifact_id}.json"
    assert ArtifactRecord.model_validate_json(meta_path.read_text()) == record


def test_get_unknown_artifact_raises(session: InvestigationSession) -> None:
    session.register_artifact(_artifact())
    with pytest.raises(UnknownArtifactError, match=r"art_missing.*art_abc123abc123"):
        session.get_artifact("art_missing")


def test_list_artifacts_sorted_oldest_first(session: InvestigationSession) -> None:
    newer = _artifact("art_b").model_copy(update={"created_at": datetime(2026, 6, 15, tzinfo=UTC)})
    older = _artifact("art_a")
    session.register_artifact(newer)
    session.register_artifact(older)
    assert [r.artifact_id for r in session.list_artifacts()] == ["art_a", "art_b"]


def test_open_reloads_artifacts(tmp_path: Path) -> None:
    record = _artifact()
    with InvestigationSession(base_dir=tmp_path) as session:
        session.register_artifact(record)
        session_dir = session.session_dir
    reopened = InvestigationSession.open(session_dir)
    try:
        assert reopened.get_artifact(record.artifact_id) == record
    finally:
        reopened.close()


def test_finding_register_get_list_and_persistence(session: InvestigationSession) -> None:
    record = _finding()
    session.register_finding(record)
    assert session.get_finding(record.finding_id) == record
    assert session.list_findings() == [record]
    meta_path = session.session_dir / "findings" / f"{record.finding_id}.json"
    assert Finding.model_validate_json(meta_path.read_text()) == record


def test_register_finding_overwrites_status(session: InvestigationSession) -> None:
    session.register_finding(_finding())
    session.register_finding(_finding().model_copy(update={"status": "verified"}))
    assert session.get_finding("fnd_abc123abc123").status == "verified"


def test_get_unknown_finding_raises(session: InvestigationSession) -> None:
    session.register_finding(_finding())
    with pytest.raises(UnknownFindingError, match=r"fnd_missing.*fnd_abc123abc123"):
        session.get_finding("fnd_missing")


def test_open_reloads_findings(tmp_path: Path) -> None:
    record = _finding()
    with InvestigationSession(base_dir=tmp_path) as session:
        session.register_finding(record)
        session_dir = session.session_dir
    reopened = InvestigationSession.open(session_dir)
    try:
        assert reopened.get_finding(record.finding_id) == record
    finally:
        reopened.close()


def test_open_reloads_registry_and_tables(tmp_path: Path) -> None:
    record = _record()
    with InvestigationSession(base_dir=tmp_path) as session:
        session.conn.execute("CREATE TABLE t AS SELECT 1 AS x")
        session.register_dataset(record)
        session_dir = session.session_dir

    reopened = InvestigationSession.open(session_dir)
    try:
        assert reopened.get_dataset(record.dataset_id) == record
        row = reopened.conn.execute("SELECT count(*) FROM t").fetchone()
        assert row is not None and row[0] == 1
    finally:
        reopened.close()


def test_ingest_close_reopen_profile_end_to_end(tmp_path: Path) -> None:
    # exotic header: unicode, pipe, quoted comma — preserved byte-for-byte through
    # ingest, JSON persistence, and session reopen
    exotic = tmp_path / "exotic.csv"
    exotic.write_text('café,weird|name,"with,comma"\n1,2,3\n4,5,6\n', encoding="utf-8")

    with InvestigationSession(base_dir=tmp_path / "sessions") as session:
        dup_record = ingest_file(session, FIXTURES / "duplicate_cols.csv")
        exotic_record = ingest_file(session, exotic)
        dup_report = profile_dataset(session, dup_record.dataset_id)
        session_dir = session.session_dir

    reopened = InvestigationSession.open(session_dir)
    try:
        assert reopened.get_dataset(dup_record.dataset_id) == dup_record
        reloaded = reopened.get_dataset(exotic_record.dataset_id)
        assert [c.original_name for c in reloaded.schema_record.columns] == [
            "café",
            "weird|name",
            "with,comma",
        ]
        report_after = profile_dataset(reopened, dup_record.dataset_id)
        assert report_after.model_dump(exclude={"generated_at"}) == dup_report.model_dump(
            exclude={"generated_at"}
        )
    finally:
        reopened.close()


def test_close_via_context_manager(tmp_path: Path) -> None:
    with InvestigationSession(base_dir=tmp_path) as session:
        pass
    with pytest.raises(duckdb.Error):
        session.conn.execute("SELECT 1")


def test_schema_record_bidirectional_mapping() -> None:
    schema = SchemaRecord(
        columns=[
            ColumnSchema(
                position=0, original_name="revenue", normalized_name="revenue", duckdb_type="BIGINT"
            ),
            ColumnSchema(
                position=1,
                original_name="revenue",
                normalized_name="revenue_2",
                duckdb_type="BIGINT",
            ),
        ]
    )
    assert schema.normalized_for("revenue") == ["revenue", "revenue_2"]
    assert schema.original_for("revenue_2") == "revenue"
    assert schema.normalized_for("absent") == []
    with pytest.raises(KeyError):
        schema.original_for("absent")


def test_schema_record_rejects_duplicate_normalized_names() -> None:
    with pytest.raises(pydantic.ValidationError, match="unique"):
        SchemaRecord(
            columns=[
                ColumnSchema(
                    position=0, original_name="a", normalized_name="x", duckdb_type="BIGINT"
                ),
                ColumnSchema(
                    position=1, original_name="b", normalized_name="x", duckdb_type="BIGINT"
                ),
            ]
        )
