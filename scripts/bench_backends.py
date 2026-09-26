"""Benchmark: SubprocessBackend (spawn-per-call) vs WarmPoolBackend (persistent).

Answers one question with numbers rather than assertions:

    How much of a tool call is *arcpy start-up*, and how much is the work?

Usage (from the repository root, using the project venv):

    .venv/Scripts/python.exe scripts/bench_backends.py warm
    .venv/Scripts/python.exe scripts/bench_backends.py subprocess

Run ``warm`` first: it is slow only once (the warm-up), then fast. ``subprocess``
re-pays the import tax on every call, so it is deliberately limited to two jobs.

Both backends receive identical jobs, environment and timeout, so the only
independent variable is the worker lifecycle.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
#: Where the bridge checkout lives. Overridable because upstream is NOT
#: vendored into this repository: `.gitignore` excludes it, so a fresh
#: clone must fetch it separately (see README "快速开始"). The env var
#: exists so the checkout can live outside the tree without editing code.
UPSTREAM_ROOT = Path(
    os.environ.get("ARCGIS_MCP_UPSTREAM_ROOT")
    or (LAB_ROOT / "upstream" / "arcgis-mcp-bridge")
)
ARCPY_PYTHON = Path(
    r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
)
BENCH_ROOT = LAB_ROOT / "bench"

# Upstream's Settings.from_environment() reads these; the worker inherits them.
os.environ["ARCPY_PYTHON_PATH"] = str(ARCPY_PYTHON)
os.environ["ARCGIS_MCP_ALLOWED_ROOTS"] = str(BENCH_ROOT)
os.environ["ARCGIS_MCP_SCRATCH_GDB"] = str(BENCH_ROOT / "scratch.gdb")
os.environ["ARCGIS_MCP_TOOL_TIMEOUT"] = "900"

# The lab package lives under src/; upstream is imported from its checkout.
sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(UPSTREAM_ROOT))

from arcgis_mcp.contracts import WorkerJob  # noqa: E402
from arcgis_mcp.execution import SubprocessBackend, new_job_id  # noqa: E402

from arcgis_agent_lab.backends import WarmPoolBackend  # noqa: E402


def run_tool(tool: str, args: dict) -> WorkerJob:
    return WorkerJob(op="run_tool", payload={"tool": tool, "args": args}, job_id=new_job_id())


def ping() -> WorkerJob:
    return WorkerJob(op="ping", payload={}, job_id=new_job_id())


#: Sequenced so the first entry is the cold one and the rest are warm.
JOBS: list[tuple[str, WorkerJob]] = [
    ("01 get_spatial_reference (cold)", run_tool("get_spatial_reference", {"wkid": 4547})),
    ("02 get_spatial_reference (warm)", run_tool("get_spatial_reference", {"wkid": 4326})),
    ("03 get_spatial_reference (warm)", run_tool("get_spatial_reference", {"wkid": 4490})),
    ("04 describe_dataset (warm)", run_tool("describe_dataset", {"dataset": str(BENCH_ROOT / "scratch.gdb")})),
    ("05 ping (no arcpy)", ping()),
    ("06 ping (no arcpy)", ping()),
]


async def bench_warm() -> None:
    backend = WarmPoolBackend(arcpy_python=ARCPY_PYTHON, upstream_root=UPSTREAM_ROOT)
    print("=== WarmPoolBackend (one persistent, pre-imported worker) ===")
    wall = time.perf_counter()
    for label, job in JOBS:
        started = time.perf_counter()
        result = await backend.run_job(job, timeout_s=900)
        elapsed = time.perf_counter() - started
        status = "ok" if result.ok else f"FAIL({result.error.kind})"
        print(f"  {label:<36} {elapsed:8.2f}s  {status}")
        if not result.ok and result.error:
            print(f"       {result.error.message[:120]}")
    print(
        f"  {'TOTAL':<36} {time.perf_counter() - wall:8.2f}s"
        f"   jobs_served={backend.jobs_served} restarts={backend.restarts}"
    )
    await backend.aclose()


async def bench_subprocess() -> None:
    backend = SubprocessBackend(ARCPY_PYTHON, UPSTREAM_ROOT, max_workers=1)
    print("=== SubprocessBackend (upstream default: one worker per job) ===")
    wall = time.perf_counter()
    for label, job in JOBS[:2]:  # two calls is already ~8 minutes here
        started = time.perf_counter()
        result = await backend.run_job(job, timeout_s=900)
        elapsed = time.perf_counter() - started
        status = "ok" if result.ok else f"FAIL({result.error.kind})"
        print(f"  {label:<36} {elapsed:8.2f}s  {status}")
    print(f"  {'TOTAL (2 calls)':<36} {time.perf_counter() - wall:8.2f}s")


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "warm"
    if mode == "warm":
        await bench_warm()
    elif mode == "subprocess":
        await bench_subprocess()
    elif mode == "both":
        await bench_warm()
        await bench_subprocess()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
