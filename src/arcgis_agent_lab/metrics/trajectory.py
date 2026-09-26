"""Trajectory-level metrics: the right tools, in the right order?

Why several metrics instead of one accuracy number
--------------------------------------------------
A single "was the answer right" figure cannot separate the failure modes that
matter here:

===============================  =========================
observed behaviour               what actually needs fixing
===============================  =========================
right tools, wrong order         the planning prompt
wrong tool entirely              the tool *description*
right tools, wrong answer        the parameter contract
===============================  =========================

Those call for three different fixes, so a benchmark that collapses them into
one number cannot tell you what to change. The layered metrics keep them apart.

Definitions
-----------
``TAO`` / ``TIO`` / ``TEM`` follow GeoAgentBench (GABench, arXiv 2604.13888),
which introduced trajectory-level scoring for tool-augmented spatial analysis
and defined the PEA metric this project also reuses. **The borrowing is credited
deliberately** -- the contribution here is not a new schema.

Caveat worth stating in any report: this project implements its own version of
each metric. The reference sequence here is a chain of tool *names*, whereas
GABench's ``toolchain_json`` also carries parameters. Where implementations
could disagree, what this module computes is what the reports describe.

``PEA`` (parameter execution accuracy) is declared but not yet scored: it needs
per-call reference parameters, which the task set does not yet carry. It is left
unimplemented rather than approximated, so nobody mistakes a proxy for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections.abc import Iterable, Sequence


def _is_subsequence(needle: Sequence[str], haystack: Sequence[str]) -> bool:
    """True when ``needle`` appears in ``haystack`` in order (gaps allowed)."""
    iterator = iter(haystack)
    return all(item in iterator for item in needle)


@dataclass(frozen=True)
class TrajectoryScore:
    """Sequence-level scoring for one task."""

    task_id: str
    expected: tuple[str, ...]
    actual: tuple[str, ...]
    #: Every expected tool appeared somewhere, order ignored.
    tao: bool
    #: Expected tools appeared in the expected relative order.
    tio: bool
    #: Actual sequence equals the expected one exactly -- no extra, no missing.
    tem: bool
    #: ``len(actual) / len(expected)``. Above ~1.5 usually means wandering.
    step_ratio: float
    missing: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def exact(self) -> bool:
        return self.tem


def score_trajectory(
    task_id: str,
    expected: Sequence[str],
    actual: Iterable[str],
) -> TrajectoryScore:
    """Score one recorded call sequence against the reference sequence.

    Empty reference sequences are handled naturally: a task whose correct
    behaviour is to call nothing scores ``TEM`` only if nothing was called.
    """
    exp = tuple(expected)
    act = tuple(actual)

    expected_set = set(exp)
    actual_set = set(act)

    tao = expected_set <= actual_set
    tio = _is_subsequence(exp, act)
    tem = exp == act

    # Multiplicity matters for `extra`: calling the same right tool three times
    # is wandering even though the set is covered.
    extra = tuple(name for name in act if name not in expected_set)
    missing = tuple(name for name in exp if name not in actual_set)

    ratio = (len(act) / len(exp)) if exp else (0.0 if not act else float("inf"))

    return TrajectoryScore(
        task_id=task_id,
        expected=exp,
        actual=act,
        tao=tao,
        tio=tio,
        tem=tem,
        step_ratio=round(ratio, 4) if ratio != float("inf") else ratio,
        missing=missing,
        extra=extra,
    )


@dataclass(frozen=True)
class SuiteMetrics:
    """Aggregate sequence metrics over a suite run."""

    tasks_scored: int
    tao_rate: float
    tio_rate: float
    tem_rate: float
    mean_step_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks_scored": self.tasks_scored,
            "tao_rate": round(self.tao_rate, 4),
            "tio_rate": round(self.tio_rate, 4),
            "tem_rate": round(self.tem_rate, 4),
            "mean_step_ratio": round(self.mean_step_ratio, 4),
        }


def aggregate(scores: Sequence[TrajectoryScore]) -> SuiteMetrics:
    """Average the sequence metrics across tasks.

    Only tasks with a non-empty reference sequence are aggregated: a task whose
    correct behaviour is to call nothing would otherwise dominate ``step_ratio``
    with a meaningless infinity.
    """
    scored = [s for s in scores if s.expected]
    if not scored:
        return SuiteMetrics(0, 0.0, 0.0, 0.0, 0.0)
    n = len(scored)
    return SuiteMetrics(
        tasks_scored=n,
        tao_rate=sum(1 for s in scored if s.tao) / n,
        tio_rate=sum(1 for s in scored if s.tio) / n,
        tem_rate=sum(1 for s in scored if s.tem) / n,
        mean_step_ratio=sum(s.step_ratio for s in scored) / n,
    )


__all__ = ["SuiteMetrics", "TrajectoryScore", "aggregate", "score_trajectory"]
