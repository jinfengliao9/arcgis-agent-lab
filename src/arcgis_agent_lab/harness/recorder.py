"""Trajectory recording: one JSONL event per tool call.

Why record the trajectory, not just the final answer
----------------------------------------------------
A single accuracy number cannot distinguish "picked the wrong tool" from "picked
the right tool and passed a bad parameter" -- and those failures call for
completely different fixes (rewrite the tool description vs tighten the
parameter schema). Recording the call sequence is what makes a failure
*diagnosable* rather than merely *countable*.

``error_kind`` reuses the bridge's own closed set of worker error kinds
(``validation`` / ``security`` / ``geoprocessing`` / ``license`` / ``internal``)
instead of inventing a parallel taxonomy. Attribution therefore stays aligned
with the system's own diagnostics by construction, rather than by careful
maintenance of two lists that drift apart.

Writing is flushed per event: an evaluation that crashes halfway still leaves
usable data, which matters when a single run costs tens of minutes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from collections.abc import Iterator

#: The bridge's closed error-kind set, plus one harness-only kind for calls the
#: harness refuses before they reach the executor.
WORKER_ERROR_KINDS: frozenset[str] = frozenset(
    {"validation", "security", "geoprocessing", "license", "internal"}
)
HARNESS_REFUSAL_KIND = "refused_by_harness"


@dataclass(frozen=True)
class ToolCallEvent:
    """One tool invocation, as observed by the harness."""

    run_id: str
    task_id: str
    turn: int
    tool_name: str
    arguments: dict[str, Any]
    ok: bool
    error_kind: str | None
    error_message: str | None
    duration_ms: float
    timestamp: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class TrajectoryRecorder:
    """Append-only JSONL writer for tool-call events."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" keeps the output byte-identical across platforms, so a
        # recorded trajectory can be fingerprinted and compared.
        self._handle = self._path.open("w", encoding="utf-8", newline="\n")
        self._count = 0

    def record(self, event: ToolCallEvent) -> None:
        self._handle.write(event.to_json() + "\n")
        self._handle.flush()
        self._count += 1

    @property
    def path(self) -> Path:
        return self._path

    @property
    def count(self) -> int:
        return self._count

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> TrajectoryRecorder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_trajectory(path: Path) -> list[ToolCallEvent]:
    """Load a recorded trajectory back into events (used by the metrics stage)."""
    events: list[ToolCallEvent] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(ToolCallEvent(**json.loads(line)))
    return events


def group_by_task(events: Iterator[ToolCallEvent] | list[ToolCallEvent]) -> dict[str, list[ToolCallEvent]]:
    """Bucket events by task id, preserving the order they were recorded in."""
    buckets: dict[str, list[ToolCallEvent]] = {}
    for event in events:
        buckets.setdefault(event.task_id, []).append(event)
    return buckets
