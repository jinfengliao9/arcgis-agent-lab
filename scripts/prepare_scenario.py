"""Import the synthetic scene into the scratch geodatabase, then project it.

Why this is now a two-stage step
--------------------------------
GeoJSON (RFC 7946) is WGS 84 by definition and carries no CRS member. The
generator therefore writes EPSG:4326, which is legal and lossless.

But the evaluation assumes **metres**: distances in metres, areas in square
metres, and a truth table computed with Shapely on a projected plane. Degrees
cannot carry that.

So the pipeline is:

    generate  ->  GeoJSON in EPSG:4326      (nothing is silently dropped)
    prepare   ->  import, project to 4547   (metres, as the truth assumes)

An earlier version skipped this and wrote projected metre coordinates directly
into GeoJSON. ArcGIS read them as degrees: layers whose coordinates stayed
inside the valid degree domain imported with a **wrong CRS**, and layers whose
coordinates exceeded it -- a longitude of 874, a latitude of 535 -- were
**silently dropped**. Two of six layers came in empty, and the task set built on
them was unsolvable. Nothing raised an error at any point.

Two consequences worth noting:

* ``parcels_geographic`` is kept in EPSG:4326 on purpose. It is the same ground
  geometry as ``parcels`` expressed in degrees, which is what makes the CRS-trap
  tasks genuine rather than hypothetical.
* The intermediate ``*_wgs84`` layers are removed after projection. Leaving them
  would pollute any task that enumerates layers, and the deletion is a
  preparation concern rather than an evaluated operation -- so it goes through
  arcpy directly instead of through the harness.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime
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
BENCH_ROOT = LAB_ROOT / "bench"
SYNTHETIC = BENCH_ROOT / "synthetic"
SCRATCH_GDB = BENCH_ROOT / "scratch.gdb"
RUNS_DIR = LAB_ROOT / "runs"
ARCPY_PYTHON = Path(
    r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
)

#: Layers imported from GeoJSON and projected to EPSG:4547 for analysis.
PROJECTED_LAYERS = ("parcels", "farmland", "buildings", "roads", "facilities")

#: The trap layer: same parcels, deliberately left in EPSG:4326 (degrees).
GEOGRAPHIC_LAYER = "parcels_geographic"

TARGET_EPSG = 4547
SOURCE_EPSG = 4326

os.environ["ARCPY_PYTHON_PATH"] = str(ARCPY_PYTHON)
os.environ["ARCGIS_MCP_ALLOWED_ROOTS"] = str(BENCH_ROOT)
os.environ["ARCGIS_MCP_SCRATCH_GDB"] = str(SCRATCH_GDB)

sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(UPSTREAM_ROOT))

from arcgis_mcp.security import PathGuard  # noqa: E402

from arcgis_agent_lab.backends import WarmPoolBackend  # noqa: E402
from arcgis_agent_lab.harness import (  # noqa: E402
    EvalSession,
    TrajectoryRecorder,
    allowed_tool_names,
)


def dataset(name: str) -> str:
    """A geodatabase-internal path."""
    return str(SCRATCH_GDB / name)


def run_arcpy(script: str) -> tuple[int, str]:
    """Run a short arcpy snippet in the worker interpreter.

    Used only for preparation bookkeeping (resetting the workspace, patching
    missing CRS metadata, removing intermediates, reporting the resulting state)
    -- never for anything the evaluation measures.
    """
    completed = subprocess.run(
        [str(ARCPY_PYTHON), "-c", script],
        capture_output=True,
        timeout=1800,
    )
    out = completed.stdout.decode("utf-8", "replace")
    err = completed.stderr.decode("utf-8", "replace")
    return completed.returncode, (out + err)


def reset_workspace() -> None:
    """Move the scratch geodatabase aside and create a fresh one.

    Why wholesale rather than selective: a model run creates dozens of
    intermediate datasets in here -- one evaluation left **347** feature classes
    named things like ``p001_sel``, ``chk016`` and ``q7``. They are not just
    clutter; they break the evaluation itself:

    * tasks that enumerate layers would report them as part of the scene;
    * a re-run collides with them, producing ``ERROR 000725: 数据集已存在`` --
      failures caused by leftover state rather than by the model.

    The GDB *is* the scratch space, so recreating it is the cheap correct move.

    **It is moved, not deleted.** The container holds ~2000 files, which trips a
    bulk-deletion guard, and rightly so -- an irreversible wipe of a directory
    tree is not something a preparation script should do unilaterally. Renaming
    sidesteps that, keeps a rollback path, and leaves the disposal decision with
    the person who owns the disk. Old backups are listed at the end of the run
    so they are easy to find.
    """
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = SCRATCH_GDB.with_name(f"{SCRATCH_GDB.name}.stale-{stamp}")
    src = f"""
import arcpy, os
gdb = r'{SCRATCH_GDB}'
backup = r'{backup}'
parent = os.path.dirname(gdb)
name = os.path.basename(gdb)
if os.path.isdir(gdb):
    os.rename(gdb, backup)
    print('MARKER_ARCHIVED', backup)
arcpy.management.CreateFileGDB(parent, name)
print('MARKER_RESET', arcpy.Exists(gdb))
"""
    code, output = run_arcpy(src)
    print(output.strip(), flush=True)
    if "MARKER_RESET True" not in output:
        raise SystemExit("FATAL: could not recreate the scratch geodatabase")


def missing_crs_layers(names: list[str]) -> list[str]:
    """Which of ``names`` came in without a coordinate system defined.

    ``arcpy.JSONToFeatures`` sets the spatial reference for polygons but **not**
    for points or polylines -- verified by inspection, not assumption. Those
    layers land as ``Unknown``, and ``Project`` then refuses them outright with
    ``ERROR 000517: 没有为输入数据集定义坐标系``.

    The remedy is ``define_projection``: the coordinates are already correct
    degrees, only the declaration is missing. That is its exact purpose, and
    having it arise from the data pipeline rather than from a contrived task is
    a better version of the coordinate-system lesson.
    """
    quoted = ", ".join(f"'{n}'" for n in names)
    src = f"""
import arcpy
gdb = r'{SCRATCH_GDB}'
for name in [{quoted}]:
    desc = arcpy.Describe(gdb + chr(92) + name)
    code = desc.spatialReference.factoryCode or 0
    print('CRS %s %s' % (name, code))
"""
    code, output = run_arcpy(src)
    undefined: list[str] = []
    for line in output.splitlines():
        if not line.startswith("CRS "):
            continue
        _tag, name, value = line.split()
        if int(value) == 0:
            undefined.append(name)
    return undefined


def verify_state() -> None:
    """Report every layer's CRS and feature count -- the acceptance check."""
    code, output = run_arcpy(
        f"""
import arcpy
gdb = r'{SCRATCH_GDB}'
arcpy.env.workspace = gdb
names = []
for fc in arcpy.ListFeatureClasses() or []:
    names.append(fc)
print('LAYERS:', len(names))
for name in sorted(names):
    path = gdb + chr(92) + name
    desc = arcpy.Describe(path)
    sr = desc.spatialReference
    try:
        n = int(arcpy.management.GetCount(path)[0])
    except Exception:
        n = -1
    print('  %-22s CRS=%-6s %-20s features=%d' % (
        name, sr.factoryCode, (sr.name or '')[:20], n))
"""
    )
    print(output.strip())


async def main() -> int:
    if not SCRATCH_GDB.is_dir():
        print(f"FATAL: scratch GDB not found: {SCRATCH_GDB}", file=sys.stderr)
        return 2
    missing = [
        name
        for name in (*PROJECTED_LAYERS, "parcels")
        if not (SYNTHETIC / f"{name}.json").is_file()
    ]
    if missing:
        print(
            f"FATAL: missing scenario layers {sorted(set(missing))}; run "
            "`python -m arcgis_agent_lab.data.generate --out bench/synthetic` first",
            file=sys.stderr,
        )
        return 2

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    backend = WarmPoolBackend(arcpy_python=ARCPY_PYTHON, upstream_root=UPSTREAM_ROOT)
    guard = PathGuard([BENCH_ROOT])
    recorder = TrajectoryRecorder(RUNS_DIR / "prepare.jsonl")
    session = EvalSession(
        backend=backend,
        guard=guard,
        recorder=recorder,
        run_id="prepare",
        allowed_tools=allowed_tool_names(),
        timeout_s=900,
    )

    failures = 0

    async def call(task_id: str, tool: str, args: dict) -> dict | None:
        nonlocal failures
        outcome = await session.call_tool(task_id=task_id, tool_name=tool, arguments=args)
        if outcome.ok:
            return outcome.payload or {}
        failures += 1
        print(
            f"  [FAIL] {task_id:<24} {outcome.error_kind}: "
            f"{(outcome.error_message or '')[:140]}",
            flush=True,
        )
        return None

    # --- 0. start from an empty workspace ---------------------------------
    print("=== 0/5 重置 scratch GDB（清除上一轮运行留下的中间数据集）===", flush=True)
    reset_workspace()

    # --- 1. import every layer as EPSG:4326 --------------------------------
    print(f"\n=== 1/5 导入 {len(PROJECTED_LAYERS)} 个图层（EPSG:{SOURCE_EPSG}）===", flush=True)
    for name in PROJECTED_LAYERS:
        payload = await call(
            f"import:{name}",
            "import_from_geojson",
            {
                "in_json": str(SYNTHETIC / f"{name}.json"),
                "out_features": dataset(f"{name}_wgs84"),
                "overwrite": True,
            },
        )
        if payload is not None:
            print(f"  [ok]   {name}_wgs84", flush=True)

    # --- 2. patch layers that arrived without a CRS ------------------------
    staged_names = [f"{n}_wgs84" for n in PROJECTED_LAYERS]
    undefined = missing_crs_layers(staged_names)
    print(
        f"\n=== 2/5 补齐坐标系声明：{len(undefined)}/{len(staged_names)} 个图层缺少 CRS ===",
        flush=True,
    )
    for name in undefined:
        payload = await call(
            f"define_crs:{name}",
            "define_projection",
            {"dataset": dataset(name), "wkid": SOURCE_EPSG, "confirm": True},
        )
        if payload is not None:
            print(f"  [ok]   {name} 声明为 EPSG:{SOURCE_EPSG}", flush=True)
    if not undefined:
        print("  （无需补齐）", flush=True)

    # --- 3. project them to metres ----------------------------------------
    print(f"\n=== 3/5 投影到 EPSG:{TARGET_EPSG}（米制）===", flush=True)
    for name in PROJECTED_LAYERS:
        payload = await call(
            f"project:{name}",
            "project_features",
            {
                "in_features": dataset(f"{name}_wgs84"),
                "out_features": dataset(name),
                "out_wkid": TARGET_EPSG,
                "overwrite": True,
            },
        )
        if payload is not None:
            print(f"  [ok]   {name}  <- {name}_wgs84", flush=True)

    # --- 4. a second copy of parcels, left in degrees (the CRS trap) -------
    print(f"\n=== 4/5 保留一份 EPSG:{SOURCE_EPSG} 的宗地作为坐标陷阱组 ===", flush=True)
    payload = await call(
        f"import:{GEOGRAPHIC_LAYER}",
        "import_from_geojson",
        {
            "in_json": str(SYNTHETIC / "parcels.json"),
            "out_features": dataset(GEOGRAPHIC_LAYER),
            "overwrite": True,
        },
    )
    if payload is not None:
        print(f"  [ok]   {GEOGRAPHIC_LAYER} (保持经纬度)", flush=True)

    recorder.close()
    await backend.aclose()

    # --- 5. drop the intermediates, then verify ---------------------------
    print("\n=== 5/5 清理中间图层并核实 ===", flush=True)
    to_drop = ", ".join(f"'{n}'" for n in staged_names)
    code, output = run_arcpy(
        f"""
import arcpy
gdb = r'{SCRATCH_GDB}'
for name in [{to_drop}]:
    path = gdb + chr(92) + name
    if arcpy.Exists(path):
        arcpy.management.Delete(path)
print('MARKER_DONE')
"""
    )
    if "MARKER_DONE" not in output:
        print("  清理未确认完成：", output[-400:], flush=True)
        failures += 1

    verify_state()

    stale = sorted(BENCH_ROOT.glob("scratch.gdb.stale-*"))
    if stale:
        print(
            "\n注意：旧的 scratch GDB 已归档（未删除），确认无用后请自行清理：",
            flush=True,
        )
        for path in stale:
            print(f"  {path}", flush=True)

    print(f"\n导入完成；轨迹 -> {recorder.path}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
