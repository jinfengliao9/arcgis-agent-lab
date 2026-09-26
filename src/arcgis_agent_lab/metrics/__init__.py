"""Scoring and failure attribution.

Two layers, deliberately separate:

:mod:`trajectory`
    Sequence-level metrics -- were the right tools used, in the right order?
    Answers "how well did it do".

:mod:`attribution`
    ``F1``-``F8`` classification of each failure, each mapped to a fix.
    Answers "what should change".

The split exists because they consume the *same* recorded trajectory. Once a
run is recorded, both can be recomputed with different definitions without
re-running a single (expensive) tool call.

Neither module measures on its own. The runner and CLI do the running, the
metrics do the judging, and the report stage renders the result. See PLAN.md.
"""

from .attribution import (
    ENVIRONMENT_CLASSES,
    REMEDIATION,
    Attribution,
    AttributionSummary,
    FailureClass,
    TaskObservation,
    attribute_task,
    summarize,
)
from .trajectory import SuiteMetrics, TrajectoryScore, aggregate, score_trajectory

__all__ = [
    "ENVIRONMENT_CLASSES",
    "REMEDIATION",
    "Attribution",
    "AttributionSummary",
    "FailureClass",
    "SuiteMetrics",
    "TaskObservation",
    "TrajectoryScore",
    "aggregate",
    "attribute_task",
    "score_trajectory",
    "summarize",
]
