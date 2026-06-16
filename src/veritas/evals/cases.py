"""Synthetic eval datasets with planted root causes and red herrings (M6).

Each :class:`EvalCase` builds a deterministic dataset (one fixed seed) in which the
*only* real relationships are the planted ones in :attr:`EvalCase.planted` — every other
column is a red herring: independent noise, an unrelated segment, or (in the trap case) a
relationship that is statistically significant but too weak to matter. A correct
discovery pass recovers every planted cause and surfaces nothing else; one case plants
nothing at all, so the only correct answer is silence.

Column names are chosen to survive ingestion unchanged (lowercase, ``[a-z0-9_]`` only,
no reserved words), so a planted pair like ``{"driver", "outcome"}`` matches the
normalized column names a :class:`~veritas.discovery.Discovery` reports. Categorical
labels are deliberately non-numeric so DuckDB's CSV sniffer keeps them as text rather
than re-reading them as integers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class EvalCase:
    """One eval scenario: a seeded dataset builder plus its planted root causes.

    ``planted`` is a set of column *pairs* (each an unordered ``frozenset`` of two
    normalized names). A discovery is a recovery when its two columns equal a planted
    pair; any other surfaced discovery is a false discovery. An empty ``planted`` means
    the correct outcome is no discoveries at all.
    """

    name: str
    description: str
    build: Callable[[np.random.Generator], pd.DataFrame]
    seed: int
    planted: frozenset[frozenset[str]] = field(default_factory=frozenset)


def _numeric_driver(rng: np.random.Generator) -> pd.DataFrame:
    """One numeric driver strongly predicts the outcome; three columns are pure noise."""
    n = 300
    driver = rng.normal(size=n)
    return pd.DataFrame(
        {
            "driver": driver,
            "outcome": 3.0 * driver + rng.normal(0, 0.5, n),
            "noise_0": rng.normal(size=n),
            "noise_1": rng.normal(size=n),
            "noise_2": rng.normal(size=n),
        }
    )


def _segment_shift(rng: np.random.Generator) -> pd.DataFrame:
    """A metric's level shifts sharply across one segment; region and extra are unrelated."""
    n = 360
    seg_idx = rng.integers(0, 3, n)
    return pd.DataFrame(
        {
            "segment": np.array(["alpha", "bravo", "charlie"])[seg_idx],
            "metric": seg_idx * 3.0 + rng.normal(0, 1.0, n),
            "region": np.array(["north", "south"])[rng.integers(0, 2, n)],
            "extra": rng.normal(size=n),
        }
    )


def _categorical_link(rng: np.random.Generator) -> pd.DataFrame:
    """Plan tracks region 85% of the time; channel and score are independent."""
    n = 500
    region_idx = rng.integers(0, 3, n)
    plan_idx = region_idx.copy()
    flipped = rng.random(n) < 0.15
    plan_idx[flipped] = rng.integers(0, 3, n)[flipped]
    return pd.DataFrame(
        {
            "region": np.array(["north", "south", "east"])[region_idx],
            "plan_type": np.array(["basic", "plus", "pro"])[plan_idx],
            "channel": np.array(["web", "store"])[rng.integers(0, 2, n)],
            "score": rng.normal(size=n),
        }
    )


def _tiny_effect_trap(rng: np.random.Generator) -> pd.DataFrame:
    """A strong real cause plus a trap: significant at large n but far below the floor."""
    n = 4000
    real_driver = rng.normal(size=n)
    trap_x = rng.normal(size=n)
    return pd.DataFrame(
        {
            "real_driver": real_driver,
            "real_outcome": 2.5 * real_driver + rng.normal(0, 0.6, n),
            "trap_x": trap_x,
            "trap_y": 0.08 * trap_x + rng.normal(0, 1.0, n),  # |rho| ~ 0.08, p tiny, floored
            "noise_a": rng.normal(size=n),
            "noise_b": rng.normal(size=n),
        }
    )


def _pure_noise(rng: np.random.Generator) -> pd.DataFrame:
    """Five independent noise columns: the only correct answer is silence."""
    n = 250
    return pd.DataFrame({f"signal_{i}": rng.normal(size=n) for i in range(5)})


def _pair(*columns: str) -> frozenset[str]:
    """A planted column pair, order-free (matches a discovery's two columns)."""
    return frozenset(columns)


CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="numeric_driver",
        description="One numeric driver predicts the outcome; the rest is noise.",
        build=_numeric_driver,
        seed=101,
        planted=frozenset({_pair("driver", "outcome")}),
    ),
    EvalCase(
        name="segment_shift",
        description="A metric shifts across one segment; other columns are unrelated.",
        build=_segment_shift,
        seed=202,
        planted=frozenset({_pair("segment", "metric")}),
    ),
    EvalCase(
        name="categorical_link",
        description="Plan tracks region; channel and score are independent.",
        build=_categorical_link,
        seed=303,
        planted=frozenset({_pair("region", "plan_type")}),
    ),
    EvalCase(
        name="tiny_effect_trap",
        description="A strong real cause beside a significant-but-trivial trap.",
        build=_tiny_effect_trap,
        seed=404,
        planted=frozenset({_pair("real_driver", "real_outcome")}),
    ),
    EvalCase(
        name="pure_noise",
        description="No real signal anywhere; the correct report is empty.",
        build=_pure_noise,
        seed=505,
        planted=frozenset(),
    ),
)
