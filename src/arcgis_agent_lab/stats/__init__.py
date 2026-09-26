"""Statistical inference for evaluation results.

The purpose of this package is to make one specific kind of claim impossible:
reporting the difference between two point estimates as if it were a finding.

Everything downstream inherits that discipline -- the report quotes intervals,
and the paired tests refuse to call a difference real when the sample cannot
resolve it.

Modules
-------
:mod:`bootstrap`
    Percentile-bootstrap confidence intervals over tasks.
:mod:`paired`
    Paired comparison of two configurations on the same tasks: exact McNemar for
    binary outcomes, paired bootstrap for continuous scores.

No SciPy: the arithmetic is standard-library only, so a library upgrade cannot
silently change a published number.
"""

from .bootstrap import (
    DEFAULT_CONFIDENCE,
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    Interval,
    bootstrap_ci,
)
from .paired import (
    MIN_DISCORDANT_FOR_APPROXIMATION,
    McNemarResult,
    PairedDifference,
    mcnemar_exact,
    paired_bootstrap_difference,
)

__all__ = [
    "DEFAULT_CONFIDENCE",
    "DEFAULT_RESAMPLES",
    "DEFAULT_SEED",
    "MIN_DISCORDANT_FOR_APPROXIMATION",
    "Interval",
    "McNemarResult",
    "PairedDifference",
    "bootstrap_ci",
    "mcnemar_exact",
    "paired_bootstrap_difference",
]
