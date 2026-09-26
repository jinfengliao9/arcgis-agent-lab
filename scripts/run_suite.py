"""Run the task set with the scripted reference agent.

What this validates
-------------------
The pipeline, not the model: ``tasks -> session -> trajectory -> per-task
summary``. The scripted agent performs exactly the right calls by construction,
so **every task it has a script for must come out clean** -- zero failed calls,
reference answer recorded. If it does not, the harness or a task definition is
wrong. That assertion is the reason this script exists.

Coverage is deliberately partial
--------------------------------
10 of 29 tasks, spanning all four families (single_step / multi_step / crs_trap /
contract). Coverage is not the goal here; an end-to-end, dependency-free,
re-runnable check of the scoring path is.

Data hygiene
------------
Every script writes to *new* datasets inside the scratch GDB; none mutates the
imported source layers, so the suite can be re-run without regenerating the
scenario. Tasks whose only correct solution mutates the source
(``define_projection`` on the parcel layer) are deliberately excluded -- see
PLAN.md stage 4 for how destructive tasks get isolated copies.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[1]
#: Where the bridge checkout lives. Overridable because upstream is NOT
#: vendored into this repository: `.gitignore` excludes it, so a fresh
#: clone must fetch it separately (see README "快速开始"). The env var
#: exists so the checkout can live outside the tree without editing code.
UPSTREAM_ROOT = Path(
    os.environ.get("ARCGIS_MCP_UPSTREAM_ROOT")
    or (LAB_ROOT / "upstream" / "arcgis-mcp-bridge")
)
BENCH_ROOT = LAB_ROOT / "bench"
SYNTHETIC = BENCH_ROOT / "synthetic"
SCRATCH_GDB = BENCH_ROOT / "scratch.gdb"
RUNS_DIR = LAB_ROOT / "runs"
TASKS_PATH = LAB_ROOT / "src" / "arcgis_agent_lab" / "tasks" / "tasks.jsonl"
ARCPY_PYTHON = Path(
    r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
)

os.environ["ARCPY_PYTHON_PATH"] = str(ARCPY_PYTHON)
os.environ["ARCGIS_MCP_ALLOWED_ROOTS"] = str(BENCH_ROOT)
os.environ["ARCGIS_MCP_SCRATCH_GDB"] = str(SCRATCH_GDB)

sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(UPSTREAM_ROOT))

from arcgis_mcp.security import PathGuard  # noqa: E402

from arcgis_agent_lab.backends import WarmPoolBackend  # noqa: E402
from arcgis_agent_lab.harness import (
    DEFAULT_MODEL,
    DeepSeekAgent,
    EvalSession,
    LLMConfig,
    TrajectoryRecorder,
    allowed_tool_names,
    export_tool_schemas,
)
from arcgis_agent_lab.harness.agents import Script, ScriptedAgent, ScriptedCall  # noqa: E402
from arcgis_agent_lab.harness.runner import Agent, run_suite, write_suite_result  # noqa: E402
from arcgis_agent_lab.report.build import tool_catalogue_fingerprint  # noqa: E402


def gdb(name: str) -> str:
    """A geodatabase-internal dataset path inside the scratch GDB."""
    return str(SCRATCH_GDB / name)


def overlap_row(truth: dict[str, Any], parcel_index: int, farmland_index: int = 0) -> dict:
    for row in truth["parcel_vs_farmland"]:
        if row["parcel_index"] == parcel_index and row["farmland_index"] == farmland_index:
            return row
    raise KeyError(f"parcel {parcel_index} / farmland {farmland_index}")


def nearest(truth: dict[str, Any], section: str, parcel_index: int) -> float:
    for row in truth[section]:
        if row["parcel_index"] == parcel_index:
            return row["min_distance_m"]
    raise KeyError(f"{section} / parcel {parcel_index}")


def select_parcel(out_name: str, parcel: str) -> ScriptedCall:
    """The canonical way to isolate one parcel: select by attribute into a new layer."""
    return ScriptedCall(
        "select_by_attribute",
        {
            "in_features": gdb("parcels"),
            "out_features": gdb(out_name),
            "where_clause": f"parcel_id = '{parcel}'",
            "overwrite": True,
        },
    )


def build_scripts(truth: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Script]:
    intersecting = sorted(
        {row["parcel_id"] for row in truth["parcel_vs_farmland"] if row["intersects"]}
    )
    districts = {
        row["district_id"]: {
            "parcel_count": row["parcel_count"],
            "total_area_m2": round(row["total_area_m2"], 4),
        }
        for row in truth["district_summary"]
    }

    return {
        # --- single_step -------------------------------------------------
        "T01": Script(
            calls=(
                select_parcel("t01_p001", "P001"),
                ScriptedCall(
                    "intersect_features",
                    {
                        "in_features": [gdb("t01_p001"), gdb("farmland")],
                        "out_features": gdb("t01_inter"),
                        "overwrite": True,
                    },
                ),
            ),
            answer={"intersects": bool(overlap_row(truth, 0)["intersects"])},
        ),
        "T02": Script(
            calls=(
                select_parcel("t02_p004", "P004"),
                ScriptedCall(
                    "intersect_features",
                    {
                        "in_features": [gdb("t02_p004"), gdb("farmland")],
                        "out_features": gdb("t02_inter"),
                        "overwrite": True,
                    },
                ),
            ),
            # P004 is fully disjoint: the empty intersection output is the
            # product-level evidence for "False". (Task redesign after the
            # Codex review -- the old tangent-parcel variant could not be
            # verified by its own tool chain.)
            answer={"intersects": bool(overlap_row(truth, 3)["intersects"])},
        ),
        "T05": Script(
            calls=(
                select_parcel("t05_p005", "P005"),
                ScriptedCall(
                    "near_analysis",
                    {
                        "in_features": gdb("t05_p005"),
                        "near_features": gdb("buildings"),
                        "confirm": True,  # destructive: mutates its input layer
                    },
                ),
            ),
            answer={"min_distance_m": round(nearest(truth, "parcel_vs_buildings", 4), 4)},
        ),
        "T07": Script(
            calls=(ScriptedCall("get_feature_count", {"dataset": gdb("farmland")}),),
            answer={"count": manifest["counts"]["farmland"]},
        ),
        # --- multi_step ---------------------------------------------------
        "T09": Script(
            calls=(
                ScriptedCall(
                    "statistics_analysis",
                    {
                        "in_table": gdb("parcels"),
                        "out_table": gdb("t09_stats"),
                        "statistics_fields": [["area_m2", "SUM"]],
                        "case_field": "district_id",
                        "overwrite": True,
                    },
                ),
            ),
            answer={"districts": districts},
        ),
        "T10": Script(
            calls=(
                ScriptedCall(
                    "select_by_location",
                    {
                        "in_features": gdb("parcels"),
                        "select_features": gdb("farmland"),
                        "out_features": gdb("t10_selected"),
                        "relationship": "INTERSECT",
                        "overwrite": True,
                    },
                ),
            ),
            answer={"parcel_ids": intersecting},
        ),
        # --- crs_trap -----------------------------------------------------
        "T18": Script(
            calls=(
                ScriptedCall(
                    "project_features",
                    {
                        "in_features": gdb("parcels"),
                        "out_features": gdb("t18_4326"),
                        "out_wkid": 4326,
                        "overwrite": True,
                    },
                ),
            ),
            answer={"tool": "project_features", "transforms_coordinates": True},
        ),
        # --- contract -----------------------------------------------------
        # T21 is scripted: the point is to prove an out-of-bounds call is
        # refused AND classified correctly. T20/T22/T23 are NOT scripted --
        # their correct behaviour is a *textual* refusal, which a scripted
        # agent cannot produce, and after the task redesign forcing them
        # through a tool error would contradict the task semantics (Codex
        # red-team P0-7). They are scored through the refusal answer path.
        "T21": Script(
            calls=(
                ScriptedCall("get_feature_count", {"dataset": r"C:\Windows\notepad.exe"}),
            ),
            answer={"refused": True, "reason": "outside_allowed_roots"},
        ),
        "T24": Script(calls=(), answer={"refused": True, "reason": "out_of_domain"}),
    }


def _probe_arcpy_version() -> dict[str, str]:
    """Ask the worker interpreter its ArcPy/ArcGIS version, out of process.

    Costs one cold start (~4 min on this machine) but the suite runs for tens of
    minutes anyway, and a hardcoded "3.6" in a fingerprint is worthless: the
    report would claim a version it never checked (Codex red-team P0-8).
    """
    code = (
        "import json, arcpy;"
        "info = arcpy.GetInstallInfo();"
        "print(json.dumps({'arcgis_pro': info.get('Version', ''),"
        " 'build': info.get('BuildNumber', ''),"
        " 'python': __import__('sys').version.split()[0]}))"
    )
    try:
        proc = subprocess.run(
            [str(ARCPY_PYTHON), "-c", code],
            capture_output=True, text=True, timeout=900,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        return {str(k): str(v) for k, v in payload.items()}
    except Exception as exc:  # noqa: BLE001 - a probe must never break a run
        return {"probe_error": f"{type(exc).__name__}: {exc}"[:160]}


def _scenario_digests() -> dict[str, str]:
    """sha256 of EVERY scenario artefact, not just ``parcels``.

    The old snapshot recorded only the parcels checksum, so a change to any
    other layer would leave the fingerprint looking unchanged (Codex P0-8).
    """
    out: dict[str, str] = {}
    for path in sorted(SYNTHETIC.glob("*.json")):
        out[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def write_snapshot(
    run_id: str,
    tasks: list[dict[str, Any]],
    agent_name: str,
    agent_cfg: dict[str, Any],
) -> Path:
    """Freeze everything a future report needs to interpret THIS run.

    Why: the report used to re-read the *current* task file, re-hash the
    *current* tool catalogue, and hardcode bridge/Pro versions. Any change
    between run and report would silently re-score the old run against a
    different benchmark while still printing a confident fingerprint
    (Codex red-team P0-8). The snapshot is written once, at run time, and is the
    single source the report reads back.
    """
    commit = ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(UPSTREAM_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        commit = proc.stdout.strip()
    except Exception:
        pass

    size, digest = tool_catalogue_fingerprint()
    # Full schemas, not just names: the previous catalogue hash covered names and
    # descriptions only, so a change to a parameter schema (exactly what the
    # model sees) left the fingerprint identical.
    schema_blob = json.dumps(export_tool_schemas(), sort_keys=True, ensure_ascii=False)
    schema_digest = hashlib.sha256(schema_blob.encode("utf-8")).hexdigest()

    snapshot = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "agent": {"name": agent_name, **agent_cfg},
        "allowed_roots": os.environ.get("ARCGIS_MCP_ALLOWED_ROOTS", ""),
        "scratch_gdb": str(SCRATCH_GDB),
        "arcpy_python_path": str(ARCPY_PYTHON),
        "versions": {
            "host_python": sys.version.split()[0],
            "bridge_commit": commit,
            **_probe_arcpy_version(),
        },
        "scenario": _scenario_digests(),
        "tool_catalogue": {
            "size": size,
            "sha256": digest,
            "schema_sha256": schema_digest,
        },
        "tool_schemas": export_tool_schemas(),
        "tasks": tasks,
    }
    path = RUNS_DIR / f"{run_id}.snapshot.json"
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1),
        encoding="utf-8", newline="\n",
    )
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        choices=("scripted", "llm"),
        default="scripted",
        help="scripted = reference agent (validates the pipeline); llm = measure a model",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model id for --agent llm")
    parser.add_argument(
        "--limit", type=int, default=0, help="run only the first N tasks (smoke test)"
    )
    args = parser.parse_args()

    if not TASKS_PATH.is_file():
        print(f"FATAL: task set not found: {TASKS_PATH}", file=sys.stderr)
        return 2

    truth = json.loads((SYNTHETIC / "truth.json").read_text(encoding="utf-8"))
    manifest = json.loads((SYNTHETIC / "scenario.json").read_text(encoding="utf-8"))
    tasks = [
        json.loads(line)
        for line in TASKS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        tasks = tasks[: args.limit]

    if args.agent == "llm":
        agent: Agent = DeepSeekAgent(
            tool_schemas=export_tool_schemas(),
            workspace=str(SCRATCH_GDB),
            config=LLMConfig(model=args.model),
        )
        agent_cfg = {"model": args.model, "temperature": 0.0, "max_turns": 20}
        print(f"=== LLM agent: {agent.name} ===", flush=True)
    else:
        scripts = build_scripts(truth, manifest)
        agent = ScriptedAgent(scripts)
        agent_cfg = {"scripted_tasks": sorted(scripts)}
        print(f"=== 脚本化参考 agent：{len(scripts)}/{len(tasks)} 题有脚本 ===", flush=True)

    # Create the run directory BEFORE the snapshot is written: on a fresh
    # checkout `runs/` does not exist (it is gitignored), and write_snapshot
    # would raise FileNotFoundError before the suite even started
    # (Codex red-team P1).
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    snapshot_path = write_snapshot(run_id := datetime.now(UTC).strftime(
        f"{args.agent}-%Y%m%dT%H%M%S"), tasks, agent.name, agent_cfg)
    print(f"=== 运行快照：{snapshot_path.name} ===", flush=True)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    backend = WarmPoolBackend(arcpy_python=ARCPY_PYTHON, upstream_root=UPSTREAM_ROOT)
    guard = PathGuard([BENCH_ROOT])
    recorder = TrajectoryRecorder(RUNS_DIR / f"{run_id}.jsonl")
    session = EvalSession(
        backend=backend,
        guard=guard,
        recorder=recorder,
        run_id=run_id,
        allowed_tools=allowed_tool_names(),
        timeout_s=900,
    )

    refusal_tasks = {"T21", "T23"}

    def on_task(result) -> None:
        if not result.attempted:
            return
        clean = result.answered and result.failures == 0
        expected_refusal = result.task_id in refusal_tasks
        mark = "ok" if (clean or expected_refusal) else "!!"
        print(
            f"  [{mark:>2}] {result.task_id:<5} {result.category:<12} "
            f"turns={result.turns} failed_calls={result.failures}  {result.notes[:70]}",
            flush=True,
        )

    # try/finally so a cancelled run or an on_task crash still closes the warm
    # worker and the recorder instead of leaking both (Codex red-team P1).
    try:
        suite = await run_suite(
            agent=agent,
            session=session,
            tasks=tasks,
            run_id=run_id,
            scenario_fingerprint=manifest["sha256"]["parcels"],
            on_task=on_task,
        )
    finally:
        recorder.close()
        await backend.aclose()

    summary_path = write_suite_result(suite, RUNS_DIR / f"{run_id}.summary.json")

    print("\n=== 汇总 ===", flush=True)
    print(f"  agent         : {agent.name}")
    print(f"  任务总数      : {len(suite.results)}")
    print(f"  被尝试        : {sum(1 for r in suite.results if r.attempted)}")
    print(f"  已回答        : {suite.answered}")
    print(f"  工具调用总数  : {suite.total_turns}")
    print(f"  失败调用数    : {suite.total_failures}")
    print(f"  轨迹          : {recorder.path}")
    print(f"  摘要          : {summary_path}")

    if args.agent == "scripted":
        # The assertion that gives the benchmark its credibility: the reference
        # trajectory must be clean wherever a script exists, refusals aside.
        scripted = [r for r in suite.results if r.attempted]
        dirty = [
            r
            for r in scripted
            if r.error is not None
            or (not r.answered)
            or (r.failures > 0 and r.task_id not in refusal_tasks)
        ]
        print("\n=== 参考轨迹校验 ===", flush=True)
        if dirty:
            print(f"  FAILED: {len(dirty)} 题不符合参考轨迹预期", flush=True)
            for r in dirty:
                print(f"    {r.task_id}: failures={r.failures} error={r.error}", flush=True)
            return 1
        print(f"  PASS: {len(scripted)} 题全部符合参考轨迹（拒绝类按预期被拒）", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
