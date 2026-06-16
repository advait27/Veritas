"""Autonomous opportunity/risk discovery: generate -> test -> suppress -> rank (M4).

Discovery enumerates statistical probes over a dataset (categorical associations,
group differences, numeric correlations), tests each, and then *suppresses* aggressively:
Benjamini-Hochberg false-discovery-rate control across all p-values, an effect-size
floor per probe type, and a hard cap on how many findings surface. Silence is a feature
-- a dataset with no real signal yields an empty report, and the report always says how
many probes were run and why each was dropped (no silent truncation). Every surfaced
discovery's statistics are persisted as a ``probe`` artifact, so the numbers are
receipts that :mod:`veritas.findings` can later verify.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from scipy import stats
from statsmodels.stats.multitest import multipletests

from veritas.execute import tabular_preview
from veritas.session import ArtifactRecord, new_id, quote_identifier

if TYPE_CHECKING:
    from veritas.session import DatasetRecord, InvestigationSession

_NUMERIC_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
        "DOUBLE",
        "FLOAT",
        "REAL",
    }
)
_TEMPORAL_PREFIXES = ("DATE", "TIMESTAMP")


class DiscoveryConfig(BaseModel):
    """Tunable thresholds for the discovery pass (sensible defaults baked in)."""

    fdr_alpha: float = 0.1
    max_findings: int = 5
    max_cardinality: int = 30
    min_observations: int = 20
    min_group: int = 5
    max_probes: int = 200
    cramers_v_floor: float = 0.1
    epsilon_sq_floor: float = 0.06
    rho_floor: float = 0.3


class Discovery(BaseModel):
    """One surfaced discovery: a tested relationship that survived suppression."""

    headline: str
    kind: str
    columns: list[str]
    test: str
    statistic: float
    p_value: float
    effect_metric: str
    effect_size: float
    effect_label: str
    n: int
    artifact_id: str


class DiscoverySummary(BaseModel):
    """The bookkeeping behind a report: what ran and why things were dropped."""

    probes_generated: int
    probes_run: int
    dropped_insufficient_data: int
    dropped_probe_cap: int
    dropped_not_significant: int
    dropped_below_effect_floor: int
    dropped_finding_cap: int
    surfaced: int
    fdr_alpha: float


class DiscoveryReport(BaseModel):
    """Surfaced discoveries (ranked, strongest first) plus the suppression summary."""

    discoveries: list[Discovery] = Field(default_factory=list)
    summary: DiscoverySummary


@dataclass(frozen=True)
class _Probe:
    kind: str  # "chi_square" | "kruskal" | "spearman"
    columns: tuple[str, str]


@dataclass(frozen=True)
class _Result:
    probe: _Probe
    test: str
    statistic: float
    p_value: float
    effect_metric: str
    effect_size: float
    n: int


_EFFECT_FLOOR = {
    "chi_square": "cramers_v_floor",
    "kruskal": "epsilon_sq_floor",
    "spearman": "rho_floor",
}


def discover(
    session: InvestigationSession, dataset_id: str, config: DiscoveryConfig | None = None
) -> DiscoveryReport:
    """Run the discovery pass over a dataset and return the surfaced findings.

    Args:
        session: the investigation session that owns the dataset and artifact store.
        dataset_id: the dataset to probe.
        config: thresholds; defaults to :class:`DiscoveryConfig`.

    Returns:
        A :class:`DiscoveryReport`: discoveries that survived FDR control, the
        effect-size floor, and the finding cap, plus a full suppression summary.

    Example:
        ``report = discover(session, dataset_id); report.summary.surfaced``
    """
    cfg = config or DiscoveryConfig()
    record = session.get_dataset(dataset_id)
    frame = session.conn.execute(f"SELECT * FROM {quote_identifier(record.table_name)}").df()
    probes, generated = _generate_probes(frame, record, cfg)
    results = [run for probe in probes if (run := _run_probe(frame, probe, cfg)) is not None]
    return _suppress_and_build(session, results, generated, len(probes), cfg)


def _column_kinds(record: DatasetRecord) -> dict[str, str]:
    """Classify each column as numeric, temporal, or categorical from its DuckDB type."""
    kinds: dict[str, str] = {}
    for column in record.schema_record.columns:
        upper = column.duckdb_type.upper()
        if upper in _NUMERIC_TYPES or upper.startswith(("DECIMAL", "NUMERIC")):
            kinds[column.normalized_name] = "numeric"
        elif upper.startswith(_TEMPORAL_PREFIXES):
            kinds[column.normalized_name] = "temporal"
        else:
            kinds[column.normalized_name] = "categorical"
    return kinds


def _generate_probes(
    frame: pd.DataFrame, record: DatasetRecord, cfg: DiscoveryConfig
) -> tuple[list[_Probe], int]:
    """Enumerate candidate probes from column kinds; return (capped probes, generated)."""
    kinds = _column_kinds(record)
    numeric = [
        c for c, k in kinds.items() if k in {"numeric", "temporal"} and frame[c].nunique() >= 2
    ]
    categorical = [
        c
        for c, k in kinds.items()
        if k == "categorical" and 2 <= frame[c].nunique(dropna=True) <= cfg.max_cardinality
    ]
    probes: list[_Probe] = []
    for i, left in enumerate(numeric):
        for right in numeric[i + 1 :]:
            probes.append(_Probe("spearman", (left, right)))
    for num in numeric:
        for cat in categorical:
            probes.append(_Probe("kruskal", (num, cat)))
    for i, left in enumerate(categorical):
        for right in categorical[i + 1 :]:
            probes.append(_Probe("chi_square", (left, right)))
    return probes[: cfg.max_probes], len(probes)


def _run_probe(frame: pd.DataFrame, probe: _Probe, cfg: DiscoveryConfig) -> _Result | None:
    """Run one probe, returning its result or ``None`` when the data is insufficient."""
    runner = {"spearman": _spearman, "kruskal": _kruskal, "chi_square": _chi_square}[probe.kind]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # constant-input / small-sample warnings -> we guard anyway
        return runner(frame, probe, cfg)


def _to_numeric(series: pd.Series) -> pd.Series:
    """Coerce a column to float, mapping temporal values to epoch nanoseconds (NaT->NaN)."""
    if pd.api.types.is_datetime64_any_dtype(series):
        epochs = series.to_numpy(dtype="datetime64[ns]").view("int64").astype("float64")
        epochs[series.isna().to_numpy()] = np.nan
        return pd.Series(epochs, index=series.index)
    return pd.to_numeric(series, errors="coerce")


def _spearman(frame: pd.DataFrame, probe: _Probe, cfg: DiscoveryConfig) -> _Result | None:
    left, right = (_to_numeric(frame[col]) for col in probe.columns)
    pair = pd.DataFrame({"a": left, "b": right}).dropna()
    if len(pair) < cfg.min_observations or pair["a"].nunique() < 2 or pair["b"].nunique() < 2:
        return None
    result = stats.spearmanr(pair["a"], pair["b"])
    rho, p_value = float(result.statistic), float(result.pvalue)
    if not (np.isfinite(rho) and np.isfinite(p_value)):
        return None
    return _Result(
        probe, "Spearman correlation", rho, p_value, "abs Spearman rho", abs(rho), len(pair)
    )


def _kruskal(frame: pd.DataFrame, probe: _Probe, cfg: DiscoveryConfig) -> _Result | None:
    num_col, cat_col = probe.columns
    pair = pd.DataFrame({"v": _to_numeric(frame[num_col]), "g": frame[cat_col]}).dropna()
    groups = [
        g["v"].to_numpy() for _, g in pair.groupby("g", observed=True) if len(g) >= cfg.min_group
    ]
    total = sum(len(g) for g in groups)
    if len(groups) < 2 or total < cfg.min_observations or pair["v"].nunique() < 2:
        return None
    statistic, p_value = stats.kruskal(*groups)
    if not (np.isfinite(statistic) and np.isfinite(p_value)):
        return None
    epsilon_sq = max((float(statistic) - len(groups) + 1) / (total - len(groups)), 0.0)
    return _Result(
        probe,
        "Kruskal-Wallis",
        float(statistic),
        float(p_value),
        "epsilon-squared",
        epsilon_sq,
        total,
    )


def _chi_square(frame: pd.DataFrame, probe: _Probe, cfg: DiscoveryConfig) -> _Result | None:
    table = pd.crosstab(frame[probe.columns[0]], frame[probe.columns[1]])
    total = int(table.to_numpy().sum())
    if min(table.shape) < 2 or total < cfg.min_observations:
        return None
    chi2, p_value, _, _ = stats.chi2_contingency(table)
    if not (np.isfinite(chi2) and np.isfinite(p_value)):
        return None
    cramers_v = float(np.sqrt(chi2 / (total * (min(table.shape) - 1))))
    return _Result(probe, "chi-square", float(chi2), float(p_value), "Cramers V", cramers_v, total)


def _suppress_and_build(
    session: InvestigationSession,
    results: list[_Result],
    generated: int,
    run_count: int,
    cfg: DiscoveryConfig,
) -> DiscoveryReport:
    """Apply FDR control, the effect-size floor, and the finding cap, then persist.

    Conservation holds at every stage: ``generated = probe_cap + run`` and
    ``run = not_significant + below_floor + finding_cap + surfaced``.
    """
    significant = _fdr_significant(results, cfg.fdr_alpha)
    above_floor = [
        r for r in significant if r.effect_size >= getattr(cfg, _EFFECT_FLOOR[r.probe.kind])
    ]
    ranked = sorted(above_floor, key=lambda r: r.effect_size, reverse=True)
    surfaced = ranked[: cfg.max_findings]
    discoveries = [_persist_discovery(session, result) for result in surfaced]
    summary = DiscoverySummary(
        probes_generated=generated,
        probes_run=len(results),
        dropped_insufficient_data=run_count - len(results),
        dropped_probe_cap=generated - run_count,
        dropped_not_significant=len(results) - len(significant),
        dropped_below_effect_floor=len(significant) - len(above_floor),
        dropped_finding_cap=len(ranked) - len(surfaced),
        surfaced=len(discoveries),
        fdr_alpha=cfg.fdr_alpha,
    )
    return DiscoveryReport(discoveries=discoveries, summary=summary)


def _fdr_significant(results: list[_Result], alpha: float) -> list[_Result]:
    """Return the results significant under Benjamini-Hochberg FDR control at ``alpha``."""
    if not results:
        return []
    reject, _, _, _ = multipletests([r.p_value for r in results], alpha=alpha, method="fdr_bh")
    return [result for result, keep in zip(results, reject, strict=True) if keep]


def _effect_label(effect_size: float) -> str:
    """A coarse strength label for any of the [0, 1]-scaled effect metrics."""
    if effect_size < 0.3:
        return "weak"
    return "moderate" if effect_size < 0.5 else "strong"


def _persist_discovery(session: InvestigationSession, result: _Result) -> Discovery:
    """Persist a probe's statistics as an artifact and return the Discovery."""
    artifact_id = _persist_probe_artifact(session, result)
    left, right = result.probe.columns
    headline = (
        f"{left} relates to {right}: {result.test}, {result.effect_metric}="
        f"{result.effect_size:.3g}, p={result.p_value:.3g} (n={result.n})"
    )
    return Discovery(
        headline=headline,
        kind=result.probe.kind,
        columns=[left, right],
        test=result.test,
        statistic=result.statistic,
        p_value=result.p_value,
        effect_metric=result.effect_metric,
        effect_size=result.effect_size,
        effect_label=_effect_label(result.effect_size),
        n=result.n,
        artifact_id=artifact_id,
    )


def _persist_probe_artifact(session: InvestigationSession, result: _Result) -> str:
    """Write a 1-row Parquet of the probe statistics and register it as a ``probe`` artifact."""
    artifact_id = new_id("art")
    frame = pd.DataFrame(
        [
            {
                "statistic": result.statistic,
                "p_value": result.p_value,
                "effect_size": result.effect_size,
                "n": result.n,
            }
        ]
    )
    path = session.artifacts_dir / f"{artifact_id}.parquet"
    view = f"_veritas_probe_{artifact_id}"
    session.conn.register(view, frame)
    try:
        session.conn.sql(f"SELECT * FROM {quote_identifier(view)}").write_parquet(str(path))
    finally:
        session.conn.unregister(view)
    columns, types, row_count, preview = tabular_preview(session.conn, path)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        kind="probe",
        created_at=datetime.now(UTC),
        source=f"{result.test}({', '.join(result.probe.columns)})",
        status="ok",
        row_count=row_count,
        columns=columns,
        column_types=types,
        data_path=str(path.relative_to(session.session_dir)),
        preview=preview,
    )
    session.register_artifact(record)
    return artifact_id
