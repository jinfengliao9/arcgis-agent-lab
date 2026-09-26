"""Task runner: walk the task set, let an agent act, record everything.

The runner owns **no judgement**. It hands each task to an agent and lets the
session record every tool call that agent makes; scoring happens later (stage 4)
from the recorded trajectory.

That separation is deliberate. A recorded run is expensive -- arcpy cold starts,
model tokens, wall-clock -- so it must be re-scorable under different metric
definitions without being repeated. Re-running just to try a new metric would
also make the two runs non-comparable, since the model is not deterministic.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Callable, Sequence

from .session import EvalSession


@dataclass(frozen=True)
class AgentAnswer:
    """What an agent produced for one task."""

    final_answer: Any | None
    notes: str = ""
    #: False when the agent never attempted the task -- e.g. the scripted agent
    #: has no script for it. Unattempted tasks must never be scored as failures:
    #: conflating "did not try" with "tried and failed" understates the model and
    #: simultaneously makes the benchmark look harder than it is. Reports filter
    #: on this field rather than inferring coverage from a zero call count.
    attempted: bool = True


class Agent(Protocol):
    """Anything that can attempt a task against a live session.

    ``solve`` may raise; the runner converts that into a recorded task error so
    one bad task cannot abort a whole suite run.
    """

    name: str

    async def solve(self, task: dict[str, Any], session: EvalSession) -> AgentAnswer:
        ...


@dataclass(frozen=True)
class TaskResult:
    """Per-task outcome, derived from the trajectory the session recorded."""

    task_id: str
    category: str
    answered: bool
    final_answer: Any | None
    turns: int
    failures: int
    notes: str
    error: str | None
    #: Whether the agent actually attempted this task. Defaults to True so that
    #: summaries written before this field existed still deserialize.
    attempted: bool = True


@dataclass
class SuiteResult:
    """One full pass over the task set."""

    run_id: str
    agent: str
    scenario_fingerprint: str
    started_at: str
    finished_at: str = ""
    results: list[TaskResult] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return sum(1 for r in self.results if r.answered)

    @property
    def total_turns(self) -> int:
        return sum(r.turns for r in self.results)

    @property
    def total_failures(self) -> int:
        return sum(r.failures for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "scenario_fingerprint": self.scenario_fingerprint,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": {
                "tasks": len(self.results),
                "answered": self.answered,
                "total_turns": self.total_turns,
                "total_failures": self.total_failures,
            },
            "results": [asdict(r) for r in self.results],
        }


async def run_suite(
    *,
    agent: Agent,
    session: EvalSession,
    tasks: Sequence[dict[str, Any]],
    run_id: str,
    scenario_fingerprint: str = "",
    on_task: Callable[[TaskResult], None] | None = None,
) -> SuiteResult:
    """Run every task in order, recording each attempt.

    Tasks are executed sequentially on purpose: a desktop ArcGIS Pro license
    cannot run parallel automation, and interleaved runs would make the recorded
    timings -- which feed the failure-attribution stage -- incomparable.
    """
    suite = SuiteResult(
        run_id=run_id,
        agent=agent.name,
        scenario_fingerprint=scenario_fingerprint,
        started_at=datetime.now(UTC).isoformat(),
    )

    for task in tasks:
        task_id = str(task.get("id", "?"))
        category = str(task.get("category", "?"))
        turns_before = session.turn
        try:
            answer = await agent.solve(task, session)
            calls = session.turn - turns_before
            # Inspecting the recorder is the only way to know whether the calls
            # that just happened actually succeeded -- the agent is not trusted
            # to report on itself.
            recent = session.recent_events(calls)
            failures = sum(1 for event in recent if not event.ok)
            result = TaskResult(
                task_id=task_id,
                category=category,
                answered=answer.final_answer is not None,
                final_answer=answer.final_answer,
                turns=calls,
                failures=failures,
                notes=answer.notes,
                error=None,
                attempted=answer.attempted,
            )
        except Exception as exc:  # noqa: BLE001 -- one task must not kill the suite
            result = TaskResult(
                task_id=task_id,
                category=category,
                answered=False,
                final_answer=None,
                turns=session.turn - turns_before,
                failures=0,
                notes="",
                error=f"{type(exc).__name__}: {exc}",
            )

        suite.results.append(result)
        if on_task is not None:
            on_task(result)

    suite.finished_at = datetime.now(UTC).isoformat()
    return suite


def write_suite_result(suite: SuiteResult, path: Path) -> Path:
    """Persist the per-task summary next to the trajectory it describes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(suite.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    return path


__all__ = [
    "Agent",
    "AgentAnswer",
    "SuiteResult",
    "TaskResult",
    "run_suite",
    "write_suite_result",
]
