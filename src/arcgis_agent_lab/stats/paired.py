"""Paired comparison of two configurations over the same tasks.

Why pairing matters
-------------------
Two models run on the *same* 29 tasks are not two independent samples. Task
difficulty dominates the score, so comparing two unpaired point estimates
treats a shared cause as noise. Pairing removes it: only the tasks where the two
configurations disagree carry information.

Two test designs, chosen by what the score is
---------------------------------------------
* **binary per task** (pass/fail) -> exact McNemar test on the discordant pairs.
  This is the right test for "did the right tools get called", and it is exact
  rather than asymptotic, which matters at n = 29.
* **continuous per task** (e.g. ``step_ratio``) -> paired bootstrap of the
  difference. Distribution-free, and it yields an interval, so the report can
  say "the gap is somewhere between -0.02 and +0.19" instead of a bare number.

Both are implemented with the standard library only (``math.comb`` for the
binomial), so no SciPy dependency sits between the numbers and the report.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import mean
from collections.abc import Sequence

from .bootstrap import DEFAULT_RESAMPLES, DEFAULT_SEED, Statistic

#: Below this many discordant pairs an exact test is the only defensible choice;
#: the chi-square approximation McNemar is usually quoted with is unreliable here.
MIN_DISCORDANT_FOR_APPROXIMATION = 25


@dataclass(frozen=True)
class McNemarResult:
    """Exact McNemar outcome for two binary configurations."""

    a_only: int  # A passed, B failed
    b_only: int  # B passed, A failed
    both_pass: int
    both_fail: int
    p_value: float
    method: str = "exact-binomial"

    @property
    def discordant(self) -> int:
        return self.a_only + self.b_only

    @property
    def informative(self) -> bool:
        """False when the two configurations never disagreed -- nothing to test."""
        return self.discordant > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "a_only": self.a_only,
            "b_only": self.b_only,
            "both_pass": self.both_pass,
            "both_fail": self.both_fail,
            "discordant": self.discordant,
            "p_value": round(self.p_value, 4),
            "method": self.method,
            "informative": self.informative,
        }


def _binomial_two_sided_p(k: int, n: int) -> float:
    """Exact two-sided p for Binomial(n, 0.5) observing ``k`` successes.

    Two-sided by the "sum of probabilities <= observed" definition, which is the
    convention McNemar's exact test is reported under.
    """
    if n == 0:
        return 1.0
    observed = math.comb(n, k) / (2**n)
    total = 0.0
    for i in range(n + 1):
        p_i = math.comb(n, i) / (2**n)
        if p_i <= observed + 1e-12:
            total += p_i
    return min(1.0, total)


def mcnemar_exact(a: Sequence[bool], b: Sequence[bool]) -> McNemarResult:
    """Exact McNemar test over the same tasks, in the same order.

    Raises:
        ValueError: if the two sequences differ in length -- pairing requires
            that they describe the same tasks.
    """
    if len(a) != len(b):
        raise ValueError(
            f"paired test needs equal-length sequences, got {len(a)} and {len(b)}"
        )

    a_only = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
    b_only = sum(1 for x, y in zip(a, b, strict=True) if y and not x)
    both_pass = sum(1 for x, y in zip(a, b, strict=True) if x and y)
    both_fail = sum(1 for x, y in zip(a, b, strict=True) if not x and not y)

    discordant = a_only + b_only
    p_value = _binomial_two_sided_p(min(a_only, b_only), discordant)

    return McNemarResult(
        a_only=a_only,
        b_only=b_only,
        both_pass=both_pass,
        both_fail=both_fail,
        p_value=p_value,
    )


@dataclass(frozen=True)
class PairedDifference:
    """Paired bootstrap of ``statistic(a) - statistic(b)`` over tasks."""

    point: float
    low: float
    high: float
    confidence: float
    n_resamples: int
    n_pairs: int

    @property
    def excludes_zero(self) -> bool:
        """True when the interval is entirely on one side of zero."""
        return self.low > 0.0 or self.high < 0.0

    def to_dict(self) -> dict[str, float | int | bool]:
        return {
            "point": round(self.point, 4),
            "low": round(self.low, 4),
            "high": round(self.high, 4),
            "confidence": self.confidence,
            "n_resamples": self.n_resamples,
            "n_pairs": self.n_pairs,
            "excludes_zero": self.excludes_zero,
        }


def paired_bootstrap_difference(
    a: Sequence[float],
    b: Sequence[float],
    *,
    statistic: Statistic = mean,
    n_resamples: int = DEFAULT_RESAMPLES,
    confidence: float = 0.95,
    seed: int = DEFAULT_SEED,
) -> PairedDifference:
    """Bootstrap the difference between two paired samples.

    Resamples **task indices** and applies the same indices to both sequences --
    that is what keeps the pairing intact. Resampling each side independently
    would throw away the very structure that makes the comparison sensitive.
    """
    if len(a) != len(b):
        raise ValueError(f"paired test needs equal-length sequences, got {len(a)} and {len(b)}")
    if not a:
        raise ValueError("paired test requires at least one pair")

    left, right = list(a), list(b)
    n = len(left)
    rng = random.Random(seed)

    diffs: list[float] = []
    for _ in range(n_resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        diffs.append(
            float(statistic([left[i] for i in idx]))
            - float(statistic([right[i] for i in idx]))
        )
    diffs.sort()

    alpha = (1.0 - confidence) / 2.0
    low_index = int(alpha * n_resamples)
    high_index = min(int((1.0 - alpha) * n_resamples), n_resamples - 1)

    return PairedDifference(
        point=float(statistic(left)) - float(statistic(right)),
        low=diffs[low_index],
        high=diffs[high_index],
        confidence=confidence,
        n_resamples=n_resamples,
        n_pairs=n,
    )


__all__ = [
    "MIN_DISCORDANT_FOR_APPROXIMATION",
    "McNemarResult",
    "PairedDifference",
    "mcnemar_exact",
    "paired_bootstrap_difference",
]
