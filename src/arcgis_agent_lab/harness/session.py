"""One evaluation step: execute a tool call and record what happened.

Where the harness deliberately diverges from the MCP transport
--------------------------------------------------------------
The bridge speaks JSON-RPC over stdio. The harness instead drives the same
registry + guard + backend directly.

The transport only moves bytes: the tool surface, the input contracts and the
execution semantics all live in ``registry`` / ``contracts`` / the backend,
which the harness uses verbatim. Driving them directly buys

* precise control over the exposed surface (see :mod:`toolset`),
* per-call timing and structured error capture without framing a protocol,
* no FastMCP dependency sitting in the measurement path.

What it does *not* change: the model still faces the same tools with the same
schemas and the same validation, and the calls still execute through the same
executor. **That equivalence is argued, not proven** -- there is no parity test
against the real MCP client in this repository. An earlier version of this
docstring claimed one existed; a red-team review checked and found none, so the
claim was removed rather than the test invented after the fact. Building that
parity test is an open item.

(Wording note: this project's whole subject is failures that do not announce
themselves, so a docstring asserting a check that does not exist would be a
little too on the nose.)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

import arcgis_mcp.tools  # noqa: F401  -- importing populates the registry
from arcgis_mcp.contracts import WorkerJob
from arcgis_mcp.execution import ExecutionBackend, new_job_id
from arcgis_mcp.registry import apply_path_guard, get as registry_get
from arcgis_mcp.security import PathGuard, PathSecurityError

from .recorder import (
    HARNESS_REFUSAL_KIND,
    ToolCallEvent,
    TrajectoryRecorder,
)
from .toolset import ESCAPE_HATCH_TOOLS


@dataclass(frozen=True)
class ToolOutcome:
    """What a model receives back for one tool call."""

    ok: bool
    payload: dict[str, Any] | None
    error_kind: str | None
    error_message: str | None


class EvalSession:
    """Execute model-requested tool calls against the bridge, recording each one.

    Args:
        backend: Any ``ExecutionBackend`` -- upstream's ``SubprocessBackend`` or
            this project's ``WarmPoolBackend``. The harness does not care which,
            which is what makes the backend comparison a controlled experiment.
        guard: The same ``PathGuard`` the server would construct.
        recorder: Where each call is written.
        run_id: Groups one model x one dataset execution.
        allowed_tools: The accepted surface. A call outside it is refused and
            recorded, never executed -- see :mod:`toolset` for why.
        timeout_s: Per-call ceiling. Larger than upstream's default because a
            cold worker can spend minutes in ``import arcpy``.
    """

    def __init__(
        self,
        backend: ExecutionBackend,
        guard: PathGuard,
        recorder: TrajectoryRecorder,
        *,
        run_id: str,
        allowed_tools: frozenset[str],
        timeout_s: int = 300,
    ) -> None:
        self._backend = backend
        self._guard = guard
        self._recorder = recorder
        self._run_id = run_id
        self._allowed = allowed_tools
        self._timeout_s = timeout_s
        self._turn = 0
        #: Local mirror of what this session recorded, so the runner can count
        #: per-task failures without re-reading the JSONL file.
        self._events: list[ToolCallEvent] = []

    @property
    def turn(self) -> int:
        return self._turn

    def recent_events(self, count: int) -> list[ToolCallEvent]:
        """The last ``count`` events, oldest first.

        The runner uses this to count failures per task from what actually
        happened, rather than trusting the agent's own account of itself.
        """
        if count <= 0:
            return []
        return self._events[-count:]

    async def call_tool(
        self, *, task_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> ToolOutcome:
        """Validate, execute and record one call.

        Never raises for model-side problems: a bad tool name, a malformed
        argument set or a rejected path all come back as a ``ToolOutcome`` with
        ``ok=False``. The model is expected to read that and adapt -- which is
        also the behaviour the failure-attribution stage needs to observe.
        """
        started = time.perf_counter()
        self._turn += 1

        outcome = await self._dispatch(tool_name, arguments)
        duration_ms = (time.perf_counter() - started) * 1000.0

        event = ToolCallEvent(
            run_id=self._run_id,
            task_id=task_id,
            turn=self._turn,
            tool_name=tool_name,
            arguments=arguments,
            ok=outcome.ok,
            error_kind=outcome.error_kind,
            error_message=outcome.error_message,
            duration_ms=round(duration_ms, 3),
            timestamp=datetime.now(UTC).isoformat(),
        )
        self._recorder.record(event)
        self._events.append(event)
        return outcome

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _dispatch(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> ToolOutcome:
        # Ordering matters: "a tool we deliberately withheld" and "a tool that
        # does not exist" are *different failures* and must not collapse into
        # one another. The first is the model probing the escape hatch this
        # evaluation removed; the second is a hallucination. The attribution
        # stage needs to tell them apart, so resolve the name first.
        spec = registry_get(tool_name)

        if tool_name not in self._allowed:
            withheld = tool_name in ESCAPE_HATCH_TOOLS or spec is not None
            return ToolOutcome(
                ok=False,
                payload=None,
                error_kind=HARNESS_REFUSAL_KIND if withheld else "validation",
                error_message=(
                    f"{tool_name!r} was not part of the offered tool surface."
                    + (
                        " It exists in the catalog but is deliberately withheld:"
                        " this evaluation measures tool selection over the named"
                        " surface, so arbitrary-execution escape hatches are excluded."
                        if withheld
                        else " The name does not exist in the catalog."
                    )
                ),
            )

        if spec is None:  # defensive: `allowed` must be a subset of the catalog
            return ToolOutcome(
                ok=False, payload=None, error_kind="validation",
                error_message=f"Unknown tool: {tool_name!r}",
            )

        # 3) Contract validation -- the same Pydantic model the MCP layer uses.
        try:
            validated = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolOutcome(False, None, "validation", f"[validation] {exc}")

        # 4) Path discipline in the Layer-A position: reject before spawning.
        try:
            validated = apply_path_guard(validated, self._guard)
        except PathSecurityError as exc:
            return ToolOutcome(False, None, "security", f"[security] {exc}")

        # 5) Execute through the same job envelope the server would send.
        job = WorkerJob(
            op="run_tool",
            payload={"tool": spec.name, "args": validated.model_dump(mode="json")},
            job_id=new_job_id(),
        )
        result = await self._backend.run_job(job, timeout_s=self._timeout_s)

        if result.ok:
            return ToolOutcome(True, dict(result.result or {}), None, None)

        error = result.error
        return ToolOutcome(
            ok=False,
            payload=None,
            error_kind=error.kind if error else "internal",
            error_message=error.message if error else "worker returned an empty failure frame",
        )


__all__ = ["EvalSession", "ToolOutcome"]
