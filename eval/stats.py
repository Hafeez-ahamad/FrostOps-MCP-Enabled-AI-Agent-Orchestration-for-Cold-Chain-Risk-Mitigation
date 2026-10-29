"""Bootstrap confidence intervals (eval/EVAL.md: 10,000 resamples, seed 20261029)."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261029


@dataclass(frozen=True)
class Estimate:
    value: float
    low: float
    high: float
    n: int

    def fmt(self, digits: int = 3) -> str:
        return f"{self.value:.{digits}f} [{self.low:.{digits}f}, {self.high:.{digits}f}]"


def _ci(samples: np.ndarray, value: float, n: int) -> Estimate:
    low, high = np.percentile(samples, [2.5, 97.5])
    return Estimate(round(value, 4), round(float(low), 4), round(float(high), 4), n)


def mean_ci(values: Sequence[float], seed: int = BOOTSTRAP_SEED) -> Estimate:
    data = np.asarray(values, dtype=float)
    if data.size == 0:
        return Estimate(float("nan"), float("nan"), float("nan"), 0)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, data.size, size=(RESAMPLES, data.size))
    return _ci(data[idx].mean(axis=1), float(data.mean()), int(data.size))


def paired_diff_ci(a: Sequence[float], b: Sequence[float], seed: int = BOOTSTRAP_SEED) -> Estimate:
    """Mean of (a - b) over paired items (same incident or run), with a bootstrap CI."""
    if len(a) != len(b):
        raise ValueError("paired samples must have the same length")
    return mean_ci([x - y for x, y in zip(a, b, strict=True)], seed)


def unpaired_diff_ci(
    a: Sequence[float], b: Sequence[float], seed: int = BOOTSTRAP_SEED
) -> Estimate:
    """Mean(a) - mean(b) for independent samples, resampling each side separately."""
    xa, xb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if xa.size == 0 or xb.size == 0:
        return Estimate(float("nan"), float("nan"), float("nan"), 0)
    rng = np.random.default_rng(seed)
    sa = xa[rng.integers(0, xa.size, size=(RESAMPLES, xa.size))].mean(axis=1)
    sb = xb[rng.integers(0, xb.size, size=(RESAMPLES, xb.size))].mean(axis=1)
    return _ci(sa - sb, float(xa.mean() - xb.mean()), int(xa.size + xb.size))
