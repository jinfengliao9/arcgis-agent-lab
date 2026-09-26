"""Bootstrap confidence intervals for task-level metrics.

Why intervals instead of point estimates
----------------------------------------
"0.72 accuracy on 29 tasks" is not a fact about the model -- it is one draw from
a distribution. Resample those 29 tasks and the number moves by several points.
Any "improvement" smaller than that movement is not an improvement at all.

This module exists because that distinction is the most common way benchmark
claims go wrong: quote a point estimate, then read the difference between two
point estimates as a finding.

Method: percentile bootstrap over tasks -- the same procedure used in this
project author's earlier retrieval work, carried over deliberately so that both
projects' numbers are read the same way.

Zero dependencies on purpose: the whole thing is `random` plus arithmetic, so
the statistics cannot drift with a library upgrade.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import mean
from collections.abc import Callable, Sequence

#: A statistic that reduces a sample to one number (mean, median, rate ...).
Statistic = Callable[[Sequence[float]], float]

DEFAULT_RESAMPLES = 1000
DEFAULT_CONFIDENCE = 0.95
#: Fixed by default: an interval that changes between runs on the same data is
#: not a property of the data. Vary it only to check stability.
DEFAULT_SEED = 20260925


@dataclass(frozen=True)
class Interval:
    """A point estimate with a percentile-bootstrap interval."""

    point: float
    low: float
    high: float
    confidence: float
    n_resamples: int
    n_samples: int

    @property
    def width(self) -> float:
        """Interval width -- the resolution limit of the sample size."""
        return self.high - self.low

    def to_dict(self) -> dict[str, float | int]:
        return {
            "point": round(self.point, 4),
            "low": round(self.low, 4),
            "high": round(self.high, 4),
            "width": round(self.width, 4),
            "confidence": self.confidence,
            "n_resamples": self.n_resamples,
            "n_samples": self.n_samples,
        }


def bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: Statistic = mean,
    n_resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> Interval:
    """Percentile-bootstrap confidence interval for ``statistic``.

    Resampling unit is the *task*, not the individual measurement: tasks are the
    independent draw here, so the interval answers "how much would this score
    move if we had drawn a different set of tasks from the same domain".
    """
    if not values:
        raise ValueError("bootstrap_ci requires at least one value")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    if n_resamples < 1:
        raise ValueError(f"n_resamples must be >= 1, got {n_resamples}")

    sample = list(values)
    rng = random.Random(seed)
    n = len(sample)

    estimates: list[float] = []
    for _ in range(n_resamples):
        resample = [sample[rng.randrange(n)] for _ in range(n)]
        estimates.append(float(statistic(resample)))
    estimates.sort()

    alpha = (1.0 - confidence) / 2.0
    low_index = int(alpha * n_resamples)
    high_index = min(int((1.0 - alpha) * n_resamples), n_resamples - 1)

    return Interval(
        point=float(statistic(sample)),
        low=estimates[low_index],
        high=estimates[high_index],
        confidence=confidence,
        n_resamples=n_resamples,
        n_samples=n,
    )


__all__ = ["DEFAULT_CONFIDENCE", "DEFAULT_RESAMPLES", "DEFAULT_SEED", "Interval", "bootstrap_ci"]
