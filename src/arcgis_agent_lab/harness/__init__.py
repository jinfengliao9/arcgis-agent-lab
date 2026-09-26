"""Evaluation harness: drive the bridge's tools, record every step.

Pipeline position (see PLAN.md)::

    bench/synthetic + tasks.jsonl      <- stage 2 (data + gold tasks)
        |
    harness/  (this package)           <- stage 3: execute and record
        |
    metrics/                           <- stage 4: score and attribute failures
        |
    stats/ + report/                   <- stage 5: confidence intervals, report

The harness deliberately does **not** measure anything. It executes and
records; all judgement lives in the metrics stage. Keeping those separate is
what allows the same recorded trajectory to be re-scored under different
metric definitions without re-running a single (expensive) tool call.
"""

from .agents import Script, ScriptedAgent, ScriptedCall
from .llm_agent import DEFAULT_MODEL, DeepSeekAgent, LLMConfig, load_api_key
from .recorder import (
    HARNESS_REFUSAL_KIND,
    WORKER_ERROR_KINDS,
    ToolCallEvent,
    TrajectoryRecorder,
    group_by_task,
    read_trajectory,
)
from .session import EvalSession, ToolOutcome
from .toolset import (
    ESCAPE_HATCH_TOOLS,
    allowed_tool_names,
    export_tool_schemas,
    spec_for,
)

__all__ = [
    "DEFAULT_MODEL",
    "ESCAPE_HATCH_TOOLS",
    "HARNESS_REFUSAL_KIND",
    "WORKER_ERROR_KINDS",
    "DeepSeekAgent",
    "EvalSession",
    "LLMConfig",
    "Script",
    "ScriptedAgent",
    "ScriptedCall",
    "ToolCallEvent",
    "ToolOutcome",
    "TrajectoryRecorder",
    "allowed_tool_names",
    "export_tool_schemas",
    "group_by_task",
    "load_api_key",
    "read_trajectory",
    "spec_for",
]
