"""Agents that drive the harness.

Only one exists so far: :class:`ScriptedAgent`, which replays a configured call
sequence. It is not a model and makes no decisions.

Why a scripted agent is worth writing
-------------------------------------
It validates the **scoring** pipeline with no model in the loop. The chain
``tasks -> trajectory -> metrics`` can be exercised end to end, and -- this is
the whole point -- **the reference trajectory must score full marks**. If the
scripted agent, which by construction does exactly the right thing, does not
score 100% on the tasks it has scripts for, then the *metric* is wrong, not the
agent.

That property is what makes the rest of the benchmark trustworthy: it separates
"the model failed" from "the benchmark is broken". Without it, every unexpected
score is ambiguous -- and a benchmark whose failures cannot be interpreted is
not measuring anything.

Scope honesty
-------------
Scripts cover a representative subset rather than all 29 tasks, because the
immediate goal is validating the pipeline, not achieving coverage. Tasks without
a script are reported as *unanswered* rather than silently skipped: an
incomplete run that looks complete is worse than an obviously incomplete one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections.abc import Mapping

from .runner import AgentAnswer
from .session import EvalSession


@dataclass(frozen=True)
class ScriptedCall:
    """One tool call in a scripted sequence."""

    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Script:
    """The reference behaviour for one task: the calls, then the answer."""

    calls: tuple[ScriptedCall, ...]
    answer: Any


class ScriptedAgent:
    """Replay a configured call sequence per task, then emit a fixed answer.

    Args:
        scripts: task id -> :class:`Script`. Built by the caller so that data
            paths can be resolved at run time instead of baked into this module.
        name: Reported in the suite result; keep it distinct from model names so
            a scripted run is never mistaken for a model run.
    """

    def __init__(self, scripts: Mapping[str, Script], *, name: str = "scripted-reference") -> None:
        self.name = name
        self._scripts: dict[str, Script] = dict(scripts)

    @property
    def covered_tasks(self) -> frozenset[str]:
        return frozenset(self._scripts)

    def has_script(self, task_id: str) -> bool:
        return task_id in self._scripts

    async def solve(self, task: dict[str, Any], session: EvalSession) -> AgentAnswer:
        task_id = str(task.get("id", "?"))
        script = self._scripts.get(task_id)
        if script is None:
            return AgentAnswer(
                final_answer=None,
                notes="no script for this task; scripted agent covers a subset",
                attempted=False,
            )

        delivered: list[str] = []
        for call in script.calls:
            outcome = await session.call_tool(
                task_id=task_id, tool_name=call.tool, arguments=call.args
            )
            delivered.append(f"{call.tool}:{'ok' if outcome.ok else outcome.error_kind}")

        # The scripted agent answers regardless of whether the calls succeeded.
        # That is intentional: if a call fails, the *metric* must notice the
        # discrepancy between the recorded trajectory and the reference one.
        # Adapting the answer to the failures would hide the very thing the
        # reference run exists to expose.
        return AgentAnswer(
            final_answer=script.answer,
            notes="scripted [" + ", ".join(delivered) + "]",
        )


__all__ = ["Script", "ScriptedAgent", "ScriptedCall"]
