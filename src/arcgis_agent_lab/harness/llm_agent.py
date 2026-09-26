"""A model-backed agent that drives the harness.

Design notes
------------
* OpenAI-compatible chat completions with tool calling, over ``urllib`` only --
  no SDK. The client is ~40 lines and cannot drift with a dependency upgrade,
  which matters for a number the project intends to publish.
* Tool schemas come from :func:`toolset.export_tool_schemas`, i.e. the same
  surface the report describes -- including the withheld escape hatch.
* **No scaffolding.** No few-shot examples, no recipe list, no planner. The
  benchmark measures what the model does with the tool descriptions as given; a
  bespoke orchestration layer would measure the orchestration instead.
* Temperature 0. Note that closed APIs are not fully deterministic even so --
  which is exactly why results are reported with confidence intervals rather
  than as exact numbers.
* The key is read from a path outside the repository and never logged.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

from .runner import AgentAnswer
from .session import EvalSession, ToolOutcome

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

#: Key location. Resolved from an env var when set, else ``<home>/.workbuddy``.
#: Deliberately NOT a hard-coded absolute path: the repository is public, and a
#: user-profile path inside the source leaks the author's Windows account name
#: for no benefit. ``Path.home()`` carries no such leak and works on any machine.
KEY_FILE_NAME = "deepseek-key.txt"
KEY_DIR_ENV = "ARCGIS_AGENT_LAB_KEY_DIR"


def default_key_path() -> Path:
    import os

    override = os.environ.get(KEY_DIR_ENV)
    base = Path(override) if override else Path.home() / ".workbuddy"
    return base / KEY_FILE_NAME

#: What the workspace contains. Described to the model as data context -- see
#: :func:`build_system_prompt` for why this is not "help".
LAYER_CATALOGUE: tuple[tuple[str, str], ...] = (
    ("parcels", "宗地面，字段 parcel_id / district_id / area_m2"),
    ("farmland", "耕地图层"),
    ("buildings", "建筑"),
    ("roads", "道路"),
    ("facilities", "设施点"),
    ("parcels_geographic", "同一批宗地，但坐标是经纬度（EPSG:4326）"),
)


def build_system_prompt(
    workspace: str, layers: Sequence[tuple[str, str]] = LAYER_CATALOGUE
) -> str:
    """Compose the system prompt, including the data context.

    Why the data context belongs here
    ---------------------------------
    The task set asks business questions ("does parcel P001 overlap farmland?"),
    which presuppose that data is already available. Withholding its location
    does not test spatial reasoning -- it tests *path guessing*. The first real
    model run proved exactly that: 122 tool calls, every one rejected by
    PathGuard, as the model probed ``C:\\``, ``D:\\`` and
    ``C:\\Users\\Public\\Documents`` in turn.

    A real MCP host describes its available resources the same way, so this is
    realistic context rather than a hint. It gives no help with the parts being
    measured: which tool to pick, in what order, with which parameters, and how
    to interpret a coordinate system.
    """
    lines = [
        "You are operating a GIS toolset over a prepared workspace.",
        "",
        f"Workspace (a file geodatabase): {workspace}",
        "Datasets inside it -- pass the full path, e.g. "
        f"{workspace}\\parcels:",
    ]
    lines += [f"  - {name}: {desc}" for name, desc in layers]
    lines += [
        "",
        "Every path you pass to a tool must be an absolute path inside that "
        "workspace. Paths outside it are rejected.",
        "",
        "Answer the user's question by calling the tools available to you. "
        "Every factual claim about the data must come from a tool result -- "
        "never invent coordinates, distances, areas or feature counts. "
        "If the available tools cannot answer the question, or if the data "
        "being asked about does not exist, say so plainly instead of guessing. "
        "When you are finished, reply with a short final answer.",
    ]
    return "\n".join(lines)


def load_api_key(path: Path | None = None) -> str:
    """Read the API key from outside the repository.

    Raises:
        FileNotFoundError: with an actionable message rather than letting a
            missing credential surface as a confusing HTTP 401 mid-run.
    """
    resolved = path or default_key_path()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"DeepSeek API key not found at {resolved}. Write the key on a single "
            "line into that file (or set "
            f"{KEY_DIR_ENV} to its containing directory). It is deliberately kept "
            "out of the repository."
        )
    key = resolved.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"API key file is empty: {resolved}")
    return key


@dataclass(frozen=True)
class LLMConfig:
    """Model and request settings for one run."""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    temperature: float = 0.0
    #: Hard ceiling on tool-calling rounds per task. Bounds both cost and the
    #: damage a looping model can do.
    #:
    #: Raised from 8 to 20 after the first real run: the model issues several
    #: calls per round while orienting itself (field lists, extents, counts
    #: before the actual analysis), so eight rounds ran out mid-exploration.
    #: A budget that truncates before the model can answer measures the budget,
    #: not the model -- how *many* calls it needs is itself a signal, and one
    #: that only exists if it is allowed to finish.
    max_turns: int = 20
    request_timeout_s: int = 180
    max_retries: int = 2

    def describe(self) -> str:
        return f"{self.model}@temp{self.temperature}"


def outcome_as_tool_content(outcome: ToolOutcome) -> str:
    """Render a tool outcome as the text the model will read.

    Errors are surfaced verbatim, including the bridge's ``error_kind``. The
    model is expected to read a refusal and adapt -- that behaviour is what the
    F4/F5 attribution classes exist to observe, so hiding it would blind the
    benchmark.
    """
    if outcome.ok:
        return json.dumps({"ok": True, "result": outcome.payload}, ensure_ascii=False)
    return json.dumps(
        {
            "ok": False,
            "error_kind": outcome.error_kind,
            "error": outcome.error_message,
        },
        ensure_ascii=False,
    )


class DeepSeekAgent:
    """Drives a task to completion with a tool-calling chat model."""

    def __init__(
        self,
        *,
        tool_schemas: Sequence[Mapping[str, Any]],
        workspace: str,
        config: LLMConfig | None = None,
        api_key: str | None = None,
        name: str | None = None,
    ) -> None:
        self._config = config or LLMConfig()
        self._api_key = api_key or load_api_key()
        self._tools = list(tool_schemas)
        self._system_prompt = build_system_prompt(workspace)
        self.name = name or self._config.describe()
        #: Counters for the run summary; the report quotes them.
        self.requests = 0
        self.tool_rounds = 0

    # ------------------------------------------------------------------ #
    # Agent protocol
    # ------------------------------------------------------------------ #

    async def solve(self, task: dict[str, Any], session: EvalSession) -> AgentAnswer:
        task_id = str(task.get("id", "?"))
        question = str(task.get("question", ""))

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": question},
        ]

        for _round in range(self._config.max_turns):
            message = await self._chat(messages)
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                content = message.get("content")
                return AgentAnswer(
                    final_answer=content if isinstance(content, str) else None,
                    notes=f"model answered after {self.tool_rounds} tool round(s)",
                )

            # The assistant turn must be echoed back before the tool results,
            # otherwise the API cannot correlate the tool_call ids.
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )

            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                arguments = _parse_arguments(function.get("arguments"))
                outcome = await session.call_tool(
                    task_id=task_id, tool_name=name, arguments=arguments
                )
                self.tool_rounds += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": outcome_as_tool_content(outcome),
                    }
                )

        return AgentAnswer(
            final_answer=None,
            notes=f"turn budget exhausted ({self._config.max_turns} rounds)",
        )

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    async def _chat(self, messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """One completion. Retries transient failures; never logs the key."""
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                return await asyncio.to_thread(self._chat_sync, messages)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "ignore")[:200]
                # 4xx other than 429 are the caller's problem; retrying cannot help.
                if exc.code != 429 and 400 <= exc.code < 500:
                    raise RuntimeError(f"DeepSeek HTTP {exc.code}: {body}") from exc
                last_error = RuntimeError(f"HTTP {exc.code}: {body}")
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc

            if attempt < self._config.max_retries:
                await asyncio.sleep(2.0 * (attempt + 1))

        raise RuntimeError(f"DeepSeek request failed after retries: {last_error}")

    def _chat_sync(self, messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        payload = {
            "model": self._config.model,
            "messages": list(messages),
            "tools": self._tools,
            "temperature": self._config.temperature,
        }
        request = urllib.request.Request(
            f"{self._config.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self.requests += 1
        with urllib.request.urlopen(request, timeout=self._config.request_timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"DeepSeek returned no choices: {str(body)[:200]}")
        return dict(choices[0].get("message") or {})

    def run_summary(self) -> dict[str, Any]:
        return {
            "model": self._config.model,
            "temperature": self._config.temperature,
            "max_turns": self._config.max_turns,
            "http_requests": self.requests,
            "tool_rounds": self.tool_rounds,
        }


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as a JSON *string*; tolerate malformed payloads.

    A model that emits invalid JSON should produce a recorded validation
    failure, not an exception that aborts the task -- so parsing failure yields
    an empty argument set and lets the contract layer reject it.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "KEY_DIR_ENV",
    "KEY_FILE_NAME",
    "LAYER_CATALOGUE",
    "DeepSeekAgent",
    "LLMConfig",
    "build_system_prompt",
    "default_key_path",
    "load_api_key",
    "outcome_as_tool_content",
]
