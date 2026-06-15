"""Tests for deterministic claim verification: receipts, or it didn't happen."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from veritas.execute import run_sql
from veritas.findings import (
    _parse_number,
    add_finding,
    unbacked_prose_numbers,
    verified_findings,
    verify_and_record,
    verify_finding,
)
from veritas.ingest import ingest_file
from veritas.session import NumericClaim

if TYPE_CHECKING:
    from pathlib import Path

    from veritas.session import InvestigationSession


@pytest.fixture
def loaded(session: InvestigationSession, trio_paths: dict[str, Path]) -> InvestigationSession:
    ingest_file(session, trio_paths["csv"], name="trio")
    return session


def _table(session: InvestigationSession) -> str:
    return session.list_datasets()[0].table_name


def _claim(
    artifact_id: str, column: str, value: float, where: dict[str, str] | None = None
) -> NumericClaim:
    return NumericClaim(
        description="c", artifact_id=artifact_id, column=column, value=value, where=where or {}
    )


# --- the happy path ------------------------------------------------------------


def test_add_finding_starts_unverified(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT count(*) AS n FROM {_table(loaded)}")
    finding = add_finding(loaded, "There are 6 rows", [_claim(art.artifact_id, "n", 6)])
    assert finding.status == "unverified"
    assert loaded.get_finding(finding.finding_id) == finding


def test_verify_scalar_claim_passes(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT count(*) AS n FROM {_table(loaded)}")
    finding = add_finding(loaded, "There are 6 rows", [_claim(art.artifact_id, "n", 6)])
    result = verify_finding(loaded, finding)
    assert result.verified
    assert result.claim_checks[0].ok
    assert result.claim_checks[0].actual_value == 6


def test_verify_keyed_lookup_with_where(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT category, count(*) AS n FROM {_table(loaded)} GROUP BY 1")
    claim = _claim(art.artifact_id, "n", 3, where={"category": "a"})
    finding = add_finding(loaded, "Category a appears 3 times", [claim])
    assert verify_finding(loaded, finding).verified


def test_verify_rounding_and_decimal_and_str_coercion(loaded: InvestigationSession) -> None:
    art = run_sql(
        loaded, f"SELECT avg(amount) AS mean_amount FROM {_table(loaded)} WHERE category='a'"
    )
    # exact mean is 9.58333…; the claim carries full precision, the prose shows 9.58
    claim = _claim(art.artifact_id, "mean_amount", 9.583333333333334)
    finding = add_finding(loaded, "Category a's mean amount is 9.58", [claim])
    assert verify_finding(loaded, finding).verified


def test_verify_decimal_and_string_numeric_cells(loaded: InvestigationSession) -> None:
    dec = run_sql(loaded, "SELECT 1.5::DECIMAL(4,2) AS d")
    assert verify_finding(
        loaded, add_finding(loaded, "d=1.5", [_claim(dec.artifact_id, "d", 1.5)])
    ).verified
    text = run_sql(loaded, "SELECT '42' AS s")
    assert verify_finding(
        loaded, add_finding(loaded, "s=42", [_claim(text.artifact_id, "s", 42)])
    ).verified


# --- refutation paths ----------------------------------------------------------


def test_verify_refutes_wrong_value(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT count(*) AS n FROM {_table(loaded)}")
    finding = add_finding(loaded, "There are 5 rows", [_claim(art.artifact_id, "n", 5)])
    result = verify_finding(loaded, finding)
    assert not result.verified
    assert not result.claim_checks[0].ok
    assert "claimed 5" in result.claim_checks[0].reason


def test_verify_unknown_artifact(loaded: InvestigationSession) -> None:
    finding = add_finding(loaded, "x=1", [_claim("art_missing", "n", 1)])
    check = verify_finding(loaded, finding).claim_checks[0]
    assert not check.ok and "no artifact" in check.reason


def test_verify_artifact_without_tabular_result(loaded: InvestigationSession) -> None:
    err = run_sql(loaded, f"SELECT nope FROM {_table(loaded)}")  # error artifact, no data_path
    finding = add_finding(loaded, "x=1", [_claim(err.artifact_id, "n", 1)])
    check = verify_finding(loaded, finding).claim_checks[0]
    assert not check.ok and "no tabular result" in check.reason


def test_verify_unknown_column(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT count(*) AS n FROM {_table(loaded)}")
    check = verify_finding(
        loaded, add_finding(loaded, "x", [_claim(art.artifact_id, "missing", 1)])
    )
    assert not check.claim_checks[0].ok and "unknown column" in check.claim_checks[0].reason


def test_verify_where_matches_no_rows(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT category, count(*) AS n FROM {_table(loaded)} GROUP BY 1")
    claim = _claim(art.artifact_id, "n", 1, where={"category": "z"})
    check = verify_finding(loaded, add_finding(loaded, "x", [claim])).claim_checks[0]
    assert not check.ok and "got 0" in check.reason


def test_verify_scalar_claim_on_multi_row_artifact(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT category FROM {_table(loaded)}")  # 6 rows, no where
    check = verify_finding(
        loaded, add_finding(loaded, "x", [_claim(art.artifact_id, "category", 1)])
    )
    assert not check.claim_checks[0].ok and "got 6" in check.claim_checks[0].reason


@pytest.mark.parametrize(
    ("sql", "column"),
    [
        ("SELECT 'abc' AS t", "t"),  # non-numeric text
        ("SELECT NULL::INTEGER AS n", "n"),  # NULL
        ("SELECT true AS flag", "flag"),  # boolean is not a numeric claim
        ("SELECT 'inf'::DOUBLE AS x", "x"),  # non-finite float
        ("SELECT 'nan' AS s", "s"),  # non-finite string
    ],
)
def test_verify_rejects_non_numeric_cells(
    loaded: InvestigationSession, sql: str, column: str
) -> None:
    art = run_sql(loaded, sql)
    check = verify_finding(loaded, add_finding(loaded, "x", [_claim(art.artifact_id, column, 1)]))
    assert not check.claim_checks[0].ok
    assert "not numeric" in check.claim_checks[0].reason


# --- prose coverage ------------------------------------------------------------


def test_prose_number_without_a_claim_blocks_verification(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT count(*) AS n FROM {_table(loaded)}")
    # the claim backs 6, but the prose also asserts an unbacked 99%
    finding = add_finding(loaded, "6 rows, revenue up 99%", [_claim(art.artifact_id, "n", 6)])
    result = verify_finding(loaded, finding)
    assert not result.verified
    assert result.unbacked_numbers == ["99%"]


def test_qualitative_finding_with_no_numbers_is_verified(loaded: InvestigationSession) -> None:
    assert verify_finding(loaded, add_finding(loaded, "Category a dominates", [])).verified


def test_unbacked_prose_numbers_direct() -> None:
    assert unbacked_prose_numbers("up 23%", []) == ["23%"]
    assert unbacked_prose_numbers("up 23%", [_claim("a", "c", 23)]) == []
    # a claim carrying full precision backs the rounded prose number
    assert unbacked_prose_numbers("mean 9.58", [_claim("a", "c", 9.583)]) == []


def test_parse_number_rejects_unparseable() -> None:
    assert _parse_number(".") is None


# --- recording + filtering -----------------------------------------------------


def test_verify_and_record_persists_status(loaded: InvestigationSession) -> None:
    art = run_sql(loaded, f"SELECT count(*) AS n FROM {_table(loaded)}")
    good = add_finding(loaded, "6 rows", [_claim(art.artifact_id, "n", 6)])
    bad = add_finding(loaded, "5 rows", [_claim(art.artifact_id, "n", 5)])
    assert verify_and_record(loaded, good).verified
    assert not verify_and_record(loaded, bad).verified
    assert loaded.get_finding(good.finding_id).status == "verified"
    assert loaded.get_finding(bad.finding_id).status == "refuted"
    assert [f.finding_id for f in verified_findings(loaded)] == [good.finding_id]
