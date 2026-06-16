"""The worked example end to end: orient, falsify, claim, verify — and refuse a stray number.

This is the executable proof behind docs/example-investigation.md: the same loop a client
drives through the MCP tools, run here against a small planted dataset.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from veritas.server import VeritasTools
from veritas.session import NumericClaim

if TYPE_CHECKING:
    from pathlib import Path

    from veritas.session import InvestigationSession


def test_worked_investigation(session: InvestigationSession, tmp_path: Path) -> None:
    tools = VeritasTools(session)
    frame = pd.DataFrame(
        {
            "region": ["east", "west", "east", "west", "east", "west"],
            "month": ["jan", "jan", "feb", "feb", "mar", "mar"],
            "revenue": [120.0, 100.0, 130.0, 90.0, 125.0, 40.0],  # west's total: 230.0
        }
    )
    path = tmp_path / "orders.csv"
    frame.to_csv(path, index=False)

    # 1. orient
    dataset = tools.ingest_dataset(str(path), name="orders")
    assert dataset.row_count == 6
    assert "revenue" in tools.profile_dataset(dataset.dataset_id)

    # 2-3. falsify "which region is weakest?" with a query — the result is a receipt
    result = tools.run_sql(
        f'SELECT region, sum(revenue) AS total FROM "{dataset.dataset_id}" GROUP BY 1'
    )
    assert result.status == "ok"

    # 4-6. a finding whose every number is a claim verifies against that receipt
    claim = NumericClaim(
        description="west total revenue",
        artifact_id=result.artifact_id,
        column="total",
        where={"region": "west"},
        value=230.0,
    )
    backed = tools.record_finding("West's total revenue is 230", [claim])
    assert tools.verify_finding(backed.finding_id).verified

    # the receipts rule has teeth: a stray, unbacked number is refused
    stray = tools.record_finding("West collapsed in 2024", [])
    verdict = tools.verify_finding(stray.finding_id)
    assert not verdict.verified
    assert "2024" in verdict.unbacked_numbers
