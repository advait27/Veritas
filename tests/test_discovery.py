"""Tests for the discovery pass: planted signal surfaces, noise stays silent."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from veritas.discovery import DiscoveryConfig, _effect_label, discover
from veritas.findings import add_finding, verify_finding
from veritas.ingest import ingest_file
from veritas.session import NumericClaim

if TYPE_CHECKING:
    from pathlib import Path

    from veritas.session import InvestigationSession


def _ingest(session: InvestigationSession, tmp_path: Path, frame: pd.DataFrame, name: str) -> str:
    path = tmp_path / f"{name}.csv"
    frame.to_csv(path, index=False)
    return ingest_file(session, path, name=name).dataset_id


def test_planted_correlation_and_group_difference_surface(
    session: InvestigationSession, tmp_path: Path
) -> None:
    rng = np.random.default_rng(42)
    n = 300
    x = rng.normal(0, 1, n)
    group = rng.integers(0, 3, n)
    frame = pd.DataFrame(
        {
            "x": x,
            "y": 2 * x + rng.normal(0, 0.5, n),  # strong correlation with x
            "grp": np.array(["g0", "g1", "g2"])[group],  # non-numeric so it stays categorical
            "val": group * 3.0 + rng.normal(0, 1, n),  # strongly driven by grp
        }
    )
    dataset_id = _ingest(session, tmp_path, frame, "planted")
    report = discover(session, dataset_id)
    kinds = {d.kind for d in report.discoveries}
    assert "spearman" in kinds  # x~y
    assert "kruskal" in kinds  # val by grp
    assert all(d.effect_label in {"weak", "moderate", "strong"} for d in report.discoveries)
    assert report.discoveries == sorted(
        report.discoveries, key=lambda d: d.effect_size, reverse=True
    )


def test_categorical_association_surfaces(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    n = 400
    a = rng.integers(0, 2, n)
    b = np.where(rng.random(n) < 0.85, a, 1 - a)  # b agrees with a 85% of the time
    labels = np.array(["lo", "hi"])
    frame = pd.DataFrame({"a": labels[a], "b": labels[b]})  # non-numeric -> categorical
    report = discover(session, _ingest(session, tmp_path, frame, "cats"))
    assert [d.kind for d in report.discoveries] == ["chi_square"]
    assert report.discoveries[0].effect_metric == "Cramers V"


def test_pure_noise_is_silent(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    frame = pd.DataFrame({f"c{i}": rng.normal(size=250) for i in range(4)})
    report = discover(session, _ingest(session, tmp_path, frame, "noise"))
    assert report.discoveries == []
    assert report.summary.surfaced == 0
    assert report.summary.probes_run == 6  # C(4,2) Spearman probes all ran...
    assert report.summary.dropped_not_significant + report.summary.dropped_below_effect_floor == 6


def test_finding_cap_limits_surfaced_to_five(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    base = rng.normal(size=300)
    frame = pd.DataFrame({f"v{i}": base + rng.normal(0, 0.3, 300) for i in range(6)})
    report = discover(session, _ingest(session, tmp_path, frame, "many"))  # all 15 pairs strong
    assert report.summary.surfaced == 5
    assert report.summary.dropped_finding_cap > 0


def test_significant_but_tiny_effect_is_floored(
    session: InvestigationSession, tmp_path: Path
) -> None:
    rng = np.random.default_rng(2)
    n = 5000
    x = rng.normal(size=n)
    frame = pd.DataFrame({"x": x, "y": 0.05 * x + rng.normal(size=n)})  # tiny but significant
    report = discover(session, _ingest(session, tmp_path, frame, "tiny"))
    assert report.summary.surfaced == 0
    assert report.summary.dropped_below_effect_floor == 1


def test_effect_label_bands() -> None:
    assert _effect_label(0.2) == "weak"
    assert _effect_label(0.4) == "moderate"
    assert _effect_label(0.6) == "strong"


def test_temporal_trend_surfaces(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(17)
    n = 200
    frame = pd.DataFrame(
        {
            "day": pd.date_range("2024-01-01", periods=n, freq="D"),
            "value": np.arange(n) * 1.0 + rng.normal(0, 5, n),  # rises over time
        }
    )
    report = discover(session, _ingest(session, tmp_path, frame, "trend"))
    assert any(d.kind == "spearman" for d in report.discoveries)  # value ~ time


def test_small_samples_are_insufficient(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(19)
    labels = np.array(["lo", "hi"])
    frame = pd.DataFrame(
        {
            "x": rng.normal(size=10),
            "y": rng.normal(size=10),
            "c1": labels[rng.integers(0, 2, 10)],
            "c2": labels[rng.integers(0, 2, 10)],
        }
    )
    report = discover(session, _ingest(session, tmp_path, frame, "tiny_n"))
    # spearman, kruskal, and chi-square probes are all generated but none has enough data
    assert report.summary.probes_generated > 0
    assert report.summary.probes_run == 0
    assert report.summary.surfaced == 0


def test_constant_columns_yield_no_probes(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    frame = pd.DataFrame({"const": [5] * 200, "x": rng.normal(size=200)})
    report = discover(session, _ingest(session, tmp_path, frame, "degenerate"))
    assert report.summary.probes_generated == 0
    assert report.discoveries == []


def test_probe_cap_is_reported(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(9)
    frame = pd.DataFrame({f"c{i}": rng.normal(size=120) for i in range(6)})  # 15 spearman pairs
    report = discover(
        session, _ingest(session, tmp_path, frame, "capped"), DiscoveryConfig(max_probes=10)
    )
    assert report.summary.probes_generated == 15
    assert report.summary.dropped_probe_cap == 5


def test_summary_counts_are_conserved(session: InvestigationSession, tmp_path: Path) -> None:
    rng = np.random.default_rng(11)
    x = rng.normal(size=300)
    frame = pd.DataFrame(
        {"x": x, "y": 2 * x + rng.normal(0, 0.3, 300), "noise": rng.normal(size=300)}
    )
    s = discover(session, _ingest(session, tmp_path, frame, "conserve")).summary
    assert s.probes_generated == s.dropped_probe_cap + s.probes_run
    surfaced_path = (
        s.dropped_not_significant
        + s.dropped_below_effect_floor
        + s.dropped_finding_cap
        + s.surfaced
    )
    assert s.probes_run == surfaced_path


def test_surfaced_discovery_is_a_verifiable_receipt(
    session: InvestigationSession, tmp_path: Path
) -> None:
    rng = np.random.default_rng(13)
    x = rng.normal(size=300)
    frame = pd.DataFrame({"x": x, "y": 2 * x + rng.normal(0, 0.3, 300)})
    discovery = discover(session, _ingest(session, tmp_path, frame, "receipt")).discoveries[0]
    artifact = session.get_artifact(discovery.artifact_id)
    assert artifact.kind == "probe"
    # the discovery's effect size is backed by its persisted probe artifact
    claim = NumericClaim(
        description="effect",
        artifact_id=discovery.artifact_id,
        column="effect_size",
        value=discovery.effect_size,
    )
    finding = add_finding(session, "effect size is backed by the probe artifact", [claim])
    assert verify_finding(session, finding).verified
