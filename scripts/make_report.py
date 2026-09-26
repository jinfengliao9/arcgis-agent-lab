"""Render an evaluation report from an already-recorded run.

Needs no ArcGIS, no licence, no model: everything comes from the trajectory and
summary files the runner wrote. That is the payoff of keeping recording and
judgement apart -- a report can be regenerated, or re-rendered under different
metric definitions, on any machine.

Usage:

    .venv/Scripts/python.exe scripts/make_report.py            # latest run
    .venv/Scripts/python.exe scripts/make_report.py --run <stem>
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any
from collections.abc import Sequence

LAB_ROOT = Path(__file__).resolve().parents[1]
#: Where the bridge checkout lives. Overridable because upstream is NOT
#: vendored into this repository: `.gitignore` excludes it, so a fresh
#: clone must fetch it separately (see README "快速开始").
UPSTREAM_ROOT = Path(
    os.environ.get("ARCGIS_MCP_UPSTREAM_ROOT")
    or (LAB_ROOT / "upstream" / "arcgis-mcp-bridge")
)
RUNS_DIR = LAB_ROOT / "runs"
REPORTS_DIR = LAB_ROOT / "reports"
TASKS_PATH = LAB_ROOT / "src" / "arcgis_agent_lab" / "tasks" / "tasks.jsonl"

sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(UPSTREAM_ROOT))

from arcgis_agent_lab.harness.recorder import read_trajectory  # noqa: E402
from arcgis_agent_lab.harness.toolset import BRIDGE_TOOL_UNIVERSE  # noqa: E402
from arcgis_agent_lab.metrics.answer_check import verify_answer  # noqa: E402
from arcgis_agent_lab.metrics.attribution import TaskObservation, attribute_task  # noqa: E402
from arcgis_agent_lab.metrics.trajectory import score_trajectory  # noqa: E402
from arcgis_agent_lab.report import (  # noqa: E402
    Fingerprint,
    ReportInputs,
    render_report,
    write_report,
)
from arcgis_agent_lab.report.build import tool_catalogue_fingerprint  # noqa: E402

LIMITATIONS = (
    "**合成数据，非真实业务数据。** 真实宗地/耕地数据没有 ground truth —— "
    "无法验证「这块地到底有没有占压」，所以评测用自带真值的合成场景。"
    "演示可以接真实 OSM 数据，评测不行。",
    "**规模小。** 29 题，而 GABench 53 题、GISAgentBench 349 题。"
    "规模小是刻意取舍：这个领域以前没人做评测，不是因为没人想到，"
    "而是因为商业桌面 GIS 天然不可复现（许可 + Windows 独占 + 引擎状态）。"
    "本项目先解决「可复现」，规模是第二步。",
    "**单机单许可。** 评测串行执行（`ARCGIS_MCP_MAX_WORKERS=1`），"
    "因为桌面版 ArcGIS Pro 许可不允许并行自动化。不反映并发场景下的表现。",
    "**被测工具面经过裁剪。** 任意代码执行入口（`execute_spatial_tool`）被刻意排除，"
    "见 harness/toolset.py。这是方法论决定：允许它会退化成「写代码」，"
    "而那是已有基准测过的东西。",
    "**PEA（参数级指标）未实现。** 需要每次调用的参考参数，当前任务集只带工具名链。"
    "已声明未实现，而不是用近似值顶替。",
    "**F7（多步状态污染）只有一条错误码触发规则。** ArcPy `000732`（输入数据集不存在）"
    "会归入 F7，但**没有产物级校验** —— 中间产物是否真的支持下游引用，不从产物验证。"
    "故意不折进 F6，否则会夸大「不是模型的错」的比例。",
)


def latest_run_stem() -> str:
    """Most recent recorded run, whichever agent produced it.

    Deliberately not filtered by agent prefix: an earlier version matched only
    ``scripted-*``, so after a model run the report silently rendered the
    *previous* reference run instead -- ten tasks and a zero failure rate,
    presented as if it described the model. Picking the newest summary of any
    kind removes the failure mode.
    """
    candidates = sorted(RUNS_DIR.glob("*.summary.json"))
    if not candidates:
        raise SystemExit(f"no recorded runs found in {RUNS_DIR}")
    return candidates[-1].name.removesuffix(".summary.json")


def load_tasks() -> dict[str, dict[str, Any]]:
    return {
        task["id"]: task
        for task in (
            json.loads(line)
            for line in TASKS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


def expected_refusal_kind(task: dict[str, Any]) -> str | None:
    """Map a contract task's declared outcome to the error kind it must produce.

    Only out-of-bounds paths force a specific tool-layer error now. The
    data-missing and hallucinated-tool tasks (T22/T23) were **deliberately
    relaxed** after the Codex red-team review: their notes accept an honest
    textual refusal, so requiring a `validation` error would score correct
    behaviour as a failure. Those tasks are verified through the refusal
    *answer* path instead.
    """
    if task.get("answer_type") != "refusal":
        return None
    answer = task.get("expected_answer") or {}

    by_reason = {"outside_allowed_roots": "security"}
    if answer.get("reason") in by_reason:
        return by_reason[answer["reason"]]

    return None


def is_sequence_scorable(task: dict[str, Any]) -> bool:
    """Whether TAO/TIO/TEM apply to this task at all.

    Refusal tasks are excluded: their reference sequence may legitimately be
    empty while the model still has to *attempt* something to discover the
    refusal, which makes "exact sequence match" the wrong question.
    """
    return task.get("answer_type") != "refusal"


def build_inputs(stem: str) -> ReportInputs:
    summary = json.loads((RUNS_DIR / f"{stem}.summary.json").read_text(encoding="utf-8"))
    events = read_trajectory(RUNS_DIR / f"{stem}.jsonl")
    # --- Run snapshot (Codex red-team P0-8) ---
    # A report must describe the run it belongs to, not the repo as it happens
    # to be right now. Tasks, tool catalogue and versions come from the
    # snapshot written at run time; legacy runs without one fall back to the
    # current files and SAY SO in the notes.
    snap_path = RUNS_DIR / f"{stem}.snapshot.json"
    snapshot: dict[str, Any] | None = None
    if snap_path.is_file():
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))

    if snapshot and snapshot.get("tasks"):
        tasks = {t["id"]: t for t in snapshot["tasks"]}
    else:
        tasks = load_tasks()

    by_task: dict[str, list[Any]] = {}
    for event in events:
        by_task.setdefault(event.task_id, []).append(event)

    # --- Attribution FIRST, then sequence metrics. ---
    # (Codex red-team P0: scores used to be collected before attribution, so a
    # task excluded from scoring as environment noise (F6) still sat inside the
    # TAO/TIO/TEM denominators -- the report's sequence metrics and its claimed
    # scoring basis disagreed.)
    scores = []
    attributions = []
    for row in summary["results"]:
        if not row.get("attempted", True):
            # Unattempted tasks are reported, never scored. Counting "did not
            # try" as failure would understate any agent that covers a subset --
            # including the scripted reference, whose scores exist only to
            # validate the scoring path in the first place.
            continue
        task_id = row["task_id"]
        task = tasks.get(task_id, {})
        observed = tuple(by_task.get(task_id, ()))
        expected_tools = tuple(task.get("expected_tools", ()))

        # --- Answer verification by declared type (Codex red-team P0). ---
        # The old code compared prose to structured answers with a bare
        # equality check, scoring "共有 6 个要素" as WRONG against
        # {"count": 6}. Unverifiable answers now return None and are counted
        # as neither pass nor fail.
        expected_answer = task.get("expected_answer")
        answer_correct: bool | None
        if expected_answer is None or row["final_answer"] is None:
            answer_correct = None
        else:
            answer_correct = verify_answer(
                row["final_answer"],
                expected_answer,
                contract=task.get("answer_contract"),
                answer_type=str(task.get("answer_type", "")),
                tolerance=task.get("tolerance"),
            )

        attribution = attribute_task(
            TaskObservation(
                task_id=task_id,
                expected_tools=expected_tools,
                events=observed,
                known_tools=BRIDGE_TOOL_UNIVERSE,
                answer_correct=answer_correct,
                expected_error_kind=expected_refusal_kind(task),
                answered=bool(row.get("answered", False)),
                has_expected_answer=expected_answer is not None,
                forbidden_tools=frozenset(task.get("forbidden_tools") or ()),
                allow_tool_probe=frozenset(task.get("allow_tool_probe") or ()),
            )
        )
        attributions.append(attribution)

        # Sequence metrics: attempted, type-scorable, and NOT excluded from
        # scoring. F9 (unanswered) stays in -- its calls really happened, and
        # an unfinished task SHOULD drag the sequence metrics down.
        if (
            is_sequence_scorable(task)
            and not attribution.excluded_from_score
        ):
            scores.append(
                score_trajectory(task_id, expected_tools, (e.tool_name for e in observed))
            )

    if snapshot:
        size = int(snapshot["tool_catalogue"]["size"])
        digest = str(snapshot["tool_catalogue"]["sha256"])
        schema_digest = snapshot["tool_catalogue"].get("schema_sha256", "")
    else:
        size, digest = tool_catalogue_fingerprint()
        schema_digest = ""

    versions = (snapshot or {}).get("versions", {}) or {}
    scenarios = (snapshot or {}).get("scenario", {}) or {}

    fingerprint = Fingerprint(
        scenario_sha256=summary.get("scenario_fingerprint", ""),
        bridge_commit=versions.get("bridge_commit", ""),
        bridge_version="0.6.6",
        tool_catalogue_size=size,
        tool_catalogue_sha256=digest,
        # Host Python comes from the snapshot when available: reading the
        # current interpreter at report time describes the machine generating
        # the report, not the machine that ran the benchmark (Codex P0-8).
        host_python=versions.get("host_python") or platform.python_version(),
        arcgis_pro_version=versions.get("arcgis_pro", "") or "—",
        agent=summary.get("agent", "unknown"),
    )

    legacy_note = (
        "⚠️ 本次运行没有运行时快照，报告按**当前**任务集与工具目录重建 —— "
        "若运行之后任务或工具面发生过变化，指标可能与当时实际测得的不一致。"
    ) if snapshot is None else None

    integrity_note = None
    if snapshot:
        integrity_note = (
            f"场景文件校验和：{len(scenarios)} 个；"
            f"工具参数 schema sha256：`{schema_digest[:16]}…`"
        )

    return ReportInputs(
        suite=_Suite.from_dict(summary),
        fingerprint=fingerprint,
        trajectory_scores=scores,
        attributions=attributions,
        limitations=LIMITATIONS,
        notes=[n for n in (
            "本次运行使用脚本化参考 agent：它按构造执行正确序列，"
            "用途是验证评分链路（参考轨迹必须干净），**不是**模型能力测量。"
            if summary.get("agent") == "scripted-reference" else None,
            f"轨迹文件：runs/{stem}.jsonl",
            f"运行快照：runs/{stem}.snapshot.json" if snapshot else None,
            integrity_note,
            legacy_note,
        ) if n],
    )


class _Suite:
    """Minimal adapter so the renderer does not need the live runner types."""

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> Any:
        from arcgis_agent_lab.harness.runner import SuiteResult

        results = []
        for row in payload["results"]:
            results.append(
                __import__(
                    "arcgis_agent_lab.harness.runner", fromlist=["TaskResult"]
                ).TaskResult(**row)
            )
        return SuiteResult(
            run_id=payload["run_id"],
            agent=payload["agent"],
            scenario_fingerprint=payload.get("scenario_fingerprint", ""),
            started_at=payload["started_at"],
            finished_at=payload["finished_at"],
            results=results,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default=None, help="run stem (default: latest)")
    args = parser.parse_args(argv)

    stem = args.run or latest_run_stem()
    inputs = build_inputs(stem)
    text = render_report(inputs)

    out = write_report(text, REPORTS_DIR / f"{stem}.md")
    print(f"report -> {out}")
    print()
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
