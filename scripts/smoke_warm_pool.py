"""Minimal smoke test for WarmPoolBackend: every line flushed, short timeouts.

Purpose is diagnosis, not benchmarking: find out exactly which step blocks and
for how long. Run with the project venv from the repository root:

    .venv/Scripts/python.exe scripts/smoke_warm_pool.py
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

os.environ["ARCPY_PYTHON_PATH"] = str(ARCPY_PYTHON)
os.environ["ARCGIS_MCP_ALLOWED_ROOTS"] = str(BENCH_ROOT)
os.environ["ARCGIS_MCP_SCRATCH_GDB"] = str(BENCH_ROOT / "scratch.gdb")

sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(UPSTREAM_ROOT))

from arcgis_mcp.contracts import WorkerJob  # noqa: E402
from arcgis_mcp.execution import new_job_id  # noqa: E402

from arcgis_agent_lab.backends import WarmPoolBackend  # noqa: E402


def step(message: str) -> None:
    """Print immediately: piped stdout is block-buffered by default."""
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def ping_job() -> WorkerJob:
    return WorkerJob(op="ping", payload={}, job_id=new_job_id())


def wkid_job(wkid: int) -> WorkerJob:
    return WorkerJob(
        op="run_tool",
        payload={"tool": "get_spatial_reference", "args": {"wkid": wkid}},
        job_id=new_job_id(),
    )


async def main() -> int:
    step("[i] constructing WarmPoolBackend")
    backend = WarmPoolBackend(arcpy_python=ARCPY_PYTHON, upstream_root=UPSTREAM_ROOT)

    step("[1] ping #1 (worker must finish arcpy warm-up before reading stdin)")
    started = time.perf_counter()
    result = await backend.run_job(ping_job(), timeout_s=600)
    step(
        f"    -> {time.perf_counter() - started:.2f}s ok={result.ok}"
        + (f" err={result.error.kind}" if not result.ok and result.error else "")
    )

    step("[2] ping #2 (should be immediate: same live worker)")
    started = time.perf_counter()
    result = await backend.run_job(ping_job(), timeout_s=60)
    step(f"    -> {time.perf_counter() - started:.3f}s ok={result.ok}")

    step("[3] get_spatial_reference wkid=4547 (first real arcpy work)")
    started = time.perf_counter()
    result = await backend.run_job(wkid_job(4547), timeout_s=300)
    step(
        f"    -> {time.perf_counter() - started:.2f}s ok={result.ok}"
        + (f" err={result.error.kind}" if not result.ok and result.error else "")
    )

    step("[4] get_spatial_reference wkid=4326 (warm repeat)")
    started = time.perf_counter()
    result = await backend.run_job(wkid_job(4326), timeout_s=300)
    step(
        f"    -> {time.perf_counter() - started:.2f}s ok={result.ok}"
        + (f" err={result.error.kind}" if not result.ok and result.error else "")
    )

    step(f"[i] counters: jobs_served={backend.jobs_served} restarts={backend.restarts}")
    await backend.aclose()
    step("[i] closed cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
