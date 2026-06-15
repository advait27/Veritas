"""Deterministic verification that a finding's numbers trace to executed artifacts (M3).

Veritas's core promise — *receipts, or it didn't happen* — is enforced here, in plain
Python, never by an LLM judge. :func:`verify_finding` re-reads each :class:`NumericClaim`
straight from its artifact's persisted Parquet and compares it to the claimed value; it
also scans the finding's prose and flags any number that no claim backs. Only a finding
whose every claim matches *and* whose prose is fully backed is ``verified`` — and only a
verified finding may enter a report.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel

from veritas.session import Finding, NumericClaim, UnknownArtifactError, new_id, quote_identifier

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import duckdb

    from veritas.session import InvestigationSession

# Optional sign, optional ``$``, digits with optional thousands commas, optional decimal,
# optional trailing ``%`` — every numeric literal a finding's prose might contain.
_NUMBER_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


class ClaimCheck(BaseModel):
    """The outcome of verifying one :class:`NumericClaim` against its artifact."""

    claim: NumericClaim
    ok: bool
    actual_value: float | None = None
    reason: str = ""


class FindingVerification(BaseModel):
    """The deterministic verdict for a finding: per-claim checks plus prose coverage."""

    verified: bool
    claim_checks: list[ClaimCheck]
    unbacked_numbers: list[str]


def add_finding(
    session: InvestigationSession,
    headline: str,
    claims: Sequence[NumericClaim],
    detail: str = "",
) -> Finding:
    """Create an ``unverified`` finding, persist it, and return it.

    Args:
        session: the investigation session that owns the findings registry.
        headline: the one-line claim ("Category A's mean amount is 45.2").
        claims: the numeric claims that back the finding's numbers.
        detail: optional supporting narrative.

    Returns:
        The registered :class:`~veritas.session.Finding` (status ``unverified``).

    Example:
        ``finding = add_finding(session, "6 rows ingested", [claim])``
    """
    finding = Finding(
        finding_id=new_id("fnd"),
        headline=headline,
        detail=detail,
        claims=list(claims),
        created_at=datetime.now(UTC),
    )
    session.register_finding(finding)
    return finding


def verify_finding(session: InvestigationSession, finding: Finding) -> FindingVerification:
    """Deterministically verify a finding's claims and prose against its artifacts.

    Each claim is checked against the value re-read from its artifact's Parquet; the
    prose (headline + detail) is scanned and any numeric literal that no claim backs is
    reported. The finding is ``verified`` only if every claim matches and no prose number
    is unbacked (a finding with no numbers is vacuously verified — it makes no claims).

    Args:
        session: the session whose artifacts back the claims.
        finding: the finding to verify (not mutated; see :func:`verify_and_record`).

    Returns:
        A :class:`FindingVerification` with the per-claim checks and unbacked numbers.

    Example:
        ``verify_finding(session, finding).verified``
    """
    checks = [_check_claim(session, claim) for claim in finding.claims]
    prose = f"{finding.headline}\n{finding.detail}"
    unbacked = unbacked_prose_numbers(prose, finding.claims)
    verified = all(check.ok for check in checks) and not unbacked
    return FindingVerification(verified=verified, claim_checks=checks, unbacked_numbers=unbacked)


def verify_and_record(session: InvestigationSession, finding: Finding) -> FindingVerification:
    """Verify a finding and persist its resulting ``verified``/``refuted`` status.

    Example:
        ``result = verify_and_record(session, finding)``
    """
    result = verify_finding(session, finding)
    status = "verified" if result.verified else "refuted"
    session.register_finding(finding.model_copy(update={"status": status}))
    return result


def verified_findings(session: InvestigationSession) -> list[Finding]:
    """Return the session's findings that passed verification, oldest first.

    Example:
        ``[f.headline for f in verified_findings(session)]``
    """
    return [finding for finding in session.list_findings() if finding.status == "verified"]


def unbacked_prose_numbers(text: str, claims: Sequence[NumericClaim]) -> list[str]:
    """Return numeric literals in ``text`` that no claim's value backs.

    A literal is backed when some claim's ``value``, rounded to the literal's displayed
    precision, equals it — so a claim of ``45.234`` backs the prose ``45.2``. Percent and
    currency markers and thousands commas are stripped before comparison, so a percentage
    must be claimed in percent units (``23%`` is backed by a claim value of ``23``).

    Example:
        >>> unbacked_prose_numbers("up 23%", [])
        ['23%']
    """
    unbacked: list[str] = []
    for token in _NUMBER_RE.findall(text):
        parsed = _parse_number(token)
        if parsed is None:  # pragma: no cover - every _NUMBER_RE match has a digit and parses
            continue
        value, decimals = parsed
        if not any(_rounds_to(claim.value, value, decimals) for claim in claims):
            unbacked.append(token)
    return unbacked


def _parse_number(token: str) -> tuple[float, int] | None:
    """Parse a prose numeric token into (value, displayed-decimal-count)."""
    cleaned = token.replace("$", "").replace(",", "").rstrip("%")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
    return value, decimals


def _rounds_to(claim_value: float, prose_value: float, decimals: int) -> bool:
    """True when ``claim_value`` rounded to ``decimals`` places equals ``prose_value``."""
    return math.isclose(round(claim_value, decimals), prose_value, abs_tol=1e-9)


def _check_claim(session: InvestigationSession, claim: NumericClaim) -> ClaimCheck:
    """Verify one claim against the value re-read from its artifact's Parquet."""
    try:
        artifact = session.get_artifact(claim.artifact_id)
    except UnknownArtifactError:
        return ClaimCheck(claim=claim, ok=False, reason=f"no artifact {claim.artifact_id!r}")
    if artifact.data_path is None:
        return ClaimCheck(claim=claim, ok=False, reason="artifact has no tabular result")
    path = session.session_dir / artifact.data_path
    actual, reason = _read_claim_cell(session.conn, path, claim.column, claim.where)
    if reason:
        return ClaimCheck(claim=claim, ok=False, reason=reason)
    actual_float = _as_float(actual)
    if actual_float is None:
        return ClaimCheck(claim=claim, ok=False, reason=f"cell value {actual!r} is not numeric")
    ok = math.isclose(actual_float, claim.value, rel_tol=claim.rel_tol, abs_tol=claim.abs_tol)
    detail = "" if ok else f"claimed {claim.value} but artifact cell is {actual_float}"
    return ClaimCheck(claim=claim, ok=ok, actual_value=actual_float, reason=detail)


def _read_claim_cell(
    conn: duckdb.DuckDBPyConnection, path: Path, column: str, where: dict[str, str]
) -> tuple[object, str]:
    """Read the single cell a claim points to; return ``(value, "")`` or ``(None, reason)``.

    The cell is located by ``column`` filtered by ``where`` equality predicates (matched
    as text). Exactly one row must match — zero or many is an unverifiable claim.
    """
    described = conn.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
    columns = {str(row[0]) for row in described}
    unknown = sorted(name for name in (column, *where) if name not in columns)
    if unknown:
        return None, f"unknown column(s) {unknown} in artifact result"
    sql = f"SELECT {quote_identifier(column)} FROM read_parquet(?)"
    params: list[object] = [str(path)]
    if where:
        clauses = " AND ".join(f"CAST({quote_identifier(key)} AS VARCHAR) = ?" for key in where)
        sql += f" WHERE {clauses}"
        params.extend(where.values())
    rows = conn.execute(sql, params).fetchall()
    if len(rows) != 1:
        return None, f"expected exactly 1 matching row, got {len(rows)}"
    return rows[0][0], ""


def _as_float(value: object) -> float | None:
    """Coerce a cell value to a finite float, or ``None`` if it is not a real number."""
    if isinstance(value, bool):
        return None  # a boolean is not a numeric claim, even though bool is an int
    if isinstance(value, int | float | Decimal):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
        return result if math.isfinite(result) else None
    return None
