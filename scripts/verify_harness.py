"""Verify the harness itself, without touching arcpy.

Checks the four decisions that make the harness trustworthy, in the order they
matter. Each one is an assertion, not a print -- if a decision ever regresses,
this script fails loudly instead of quietly changing what the benchmark means.

1. surface    the escape hatch is absent from what the model sees
2. discipline calling it anyway is refused and recorded, not executed
3. taxonomy   a hallucinated tool and an out-of-bounds path produce *different*
              error kinds (validation vs security) -- conflating them would make
              the attribution stage unable to tell "wrong tool" from "bad path"
4. recording  every event lands in the JSONL with the fields the metrics stage
              will need

None of these require ArcGIS: refusals are decided before any worker spawns,
which is also why refusal tasks are cheap to evaluate (0.00 s in the smoke
benchmark).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
#: Where the bridge checkout lives. Overridable because upstream is NOT
#: vendored into this repository: `.gitignore` excludes it, so a fresh
#: clone must fetch it separately (see README "快速开始").
UPSTREAM_ROOT = Path(
    os.environ.get("ARCGIS_MCP_UPSTREAM_ROOT")
    or (LAB_ROOT / "upstream" / "arcgis-mcp-bridge")
)
BENCH_ROOT = LAB_ROOT / "bench"
RECORD_PATH = LAB_ROOT / "runs" / "_harness_verify.jsonl"

sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(UPSTREAM_ROOT))

from arcgis_mcp.contracts import WorkerResult  # noqa: E402
from arcgis_mcp.security import PathGuard  # noqa: E402

from arcgis_agent_lab.harness import (  # noqa: E402
    ESCAPE_HATCH_TOOLS,
    EvalSession,
    HARNESS_REFUSAL_KIND,
    TrajectoryRecorder,
    allowed_tool_names,
    export_tool_schemas,
    read_trajectory,
)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail else ""), flush=True)
    if not condition:
        FAILURES.append(label)


class _NoopBackend:
    """Fails if anything actually reaches execution -- these checks must not."""

    async def run_job(self, job, *, timeout_s: int) -> WorkerResult:  # pragma: no cover
        raise AssertionError(
            f"harness reached execution during a refusal check (op={job.op!r})"
        )


async def main() -> int:
    print("=== 1. 工具面：逃生舱必须缺席 ===", flush=True)
    schemas = export_tool_schemas()
    names = allowed_tool_names()
    schema_names = {s["function"]["name"] for s in schemas}
    check("schema 数量与允许集合一致", len(schemas) == len(names), f"{len(schemas)} tools")
    check(
        "execute_spatial_tool 不在模型可见面内",
        not (schema_names & ESCAPE_HATCH_TOOLS),
        f"withheld: {sorted(ESCAPE_HATCH_TOOLS)}",
    )
    check(
        "逃生舱也不在允许执行集合内",
        not (names & ESCAPE_HATCH_TOOLS),
    )
    from arcgis_mcp.registry import get as _registry_get

    check(
        "逃生舱不在 catalog 中（bridge 的 core tools 不走 registry）",
        _registry_get("execute_spatial_tool") is None,
        "它天然缺席；过滤器是防止它未来迁移进 catalog 的守卫",
    )

    print("\n=== 2-4. 执行与记录（全部在 spawn 之前被拒） ===", flush=True)
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    guard = PathGuard([BENCH_ROOT])
    recorder = TrajectoryRecorder(RECORD_PATH)
    session = EvalSession(
        backend=_NoopBackend(),  # type: ignore[arg-type]
        guard=guard,
        recorder=recorder,
        run_id="verify",
        allowed_tools=names,
    )

    # 2) discipline: the escape hatch is refused
    hatch = await session.call_tool(
        task_id="V01",
        tool_name="execute_spatial_tool",
        arguments={"tool": "Buffer_analysis", "in_features": "x", "parameters": {}},
    )
    check(
        "调用逃生舱被拒（未执行）",
        (not hatch.ok) and hatch.error_kind == HARNESS_REFUSAL_KIND,
        f"error_kind={hatch.error_kind}",
    )

    # 3a) hallucinated tool -> validation
    ghost = await session.call_tool(
        task_id="V02", tool_name="buffer_features", arguments={"in_features": "x"}
    )
    check(
        "幻觉工具 -> validation",
        (not ghost.ok) and ghost.error_kind == "validation",
        f"error_kind={ghost.error_kind}",
    )

    # 3b) out-of-bounds path -> security (and NOT validation)
    outside = await session.call_tool(
        task_id="V03",
        tool_name="get_feature_count",
        arguments={"dataset": r"C:\Windows\System32\notepad.exe"},
    )
    check(
        "越界路径 -> security",
        (not outside.ok) and outside.error_kind == "security",
        f"error_kind={outside.error_kind}",
    )
    check(
        "越界与幻觉的 error_kind 不同（归因才能区分）",
        ghost.error_kind != outside.error_kind,
    )

    # 3c) malformed arguments -> validation
    malformed = await session.call_tool(
        task_id="V04", tool_name="get_feature_count", arguments={"dataset": 12345}
    )
    check(
        "参数类型错误 -> validation",
        (not malformed.ok) and malformed.error_kind == "validation",
        f"error_kind={malformed.error_kind}",
    )

    recorder.close()

    print("\n=== 5. 轨迹记录 ===", flush=True)
    events = read_trajectory(RECORD_PATH)
    check("事件数正确", len(events) == 4, f"{len(events)} events")
    required = {
        "run_id", "task_id", "turn", "tool_name", "arguments",
        "ok", "error_kind", "error_message", "duration_ms", "timestamp",
    }
    check("字段齐全", required <= set(events[0].__dict__), f"{sorted(required)}")
    check(
        "turn 递增",
        [e.turn for e in events] == [1, 2, 3, 4],
        f"{[e.turn for e in events]}",
    )
    check("全部标记为失败", all(not e.ok for e in events))

    print("\n=== 结论 ===", flush=True)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s) -> {FAILURES}", flush=True)
        return 1
    print("全部通过 —— harness 的工具面纪律、错误分类与轨迹记录均符合设计。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
