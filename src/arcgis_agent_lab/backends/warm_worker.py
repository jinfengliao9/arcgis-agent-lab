"""warm_worker — a persistent, pre-imported variant of ``arcgis_mcp.worker``.

Why this exists
---------------
Upstream's ``SubprocessBackend`` spawns one worker process per job. That buys
maximum crash isolation and zero state leakage between jobs, but it pays the
``import arcpy`` tax on **every** call. Upstream documents the expected cost as
roughly 10-30 s (class docstring, ``arcgis_mcp/execution.py``).

Measured on the machine this project was built on (ArcGIS Pro 3.6, Python
3.13.7), that tax was **223-239 s** across three independent processes -- with
real-time antivirus scanning ArcGIS's native DLLs as the confirmed suspect.
At that cost a single tool call can exceed a client-side 240 s request timeout,
which is exactly what happened to two of the eight upstream smoke-benchmark
cases. Measurements live in ``docs/warm-pool.md``.

This worker keeps upstream's frame protocol unchanged (one NDJSON ``WorkerJob``
in, one NDJSON ``WorkerResult`` out) but processes frames in a **loop**, so the
arcpy import is paid once per process lifetime instead of once per job.

Deliberate trade-off (why this is opt-in)
-----------------------------------------
A long-lived worker does **not** provide upstream's "zero state leakage between
jobs" guarantee: ``arcpy.env`` settings, geoprocessing history and checked-out
extension license seats persist across frames. Choosing a warm pool means
deliberately accepting weaker isolation in exchange for predictable latency.
It is not a free win, and this project documents it as such rather than
quietly optimising it away. ``SubprocessBackend`` stays the default.

Reuse, not reimplementation
---------------------------
Per-frame logic is upstream's ``arcgis_mcp.worker.process_frame``. This module
changes only the *lifecycle*, never the semantics: every error class keeps its
``WorkerError.kind`` mapping, and the stdout-shielding discipline is identical.

Exit codes (same meaning as upstream, plus one addition)
--------------------------------------------------------
0 = clean shutdown after stdin reached EOF.
2 = protocol failure (upstream's ``_EXIT_PROTOCOL``).
3 = warm-up or configuration failure -- no frame was ever served.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Final

LOG: Final[logging.Logger] = logging.getLogger("arcgis_agent_lab.warm_worker")

_EXIT_OK: Final[int] = 0
_EXIT_WARMUP_FAILED: Final[int] = 3


class UpstreamRootError(RuntimeError):
    """ARCGIS_UPSTREAM_ROOT is missing or does not hold an ``arcgis_mcp`` package."""


def _resolve_upstream_root() -> Path:
    """Locate the source checkout that contains the ``arcgis_mcp`` package.

    The parent process passes this explicitly via ``ARCGIS_UPSTREAM_ROOT``
    instead of mutating ``PYTHONPATH``. Upstream deliberately avoids
    ``PYTHONPATH`` (see the ``project_root`` argument in
    ``SubprocessBackend.__init__``); keeping the path explicit here means the
    parent remains the single owner of ``sys.path`` decisions.
    """
    raw = os.environ.get("ARCGIS_UPSTREAM_ROOT", "").strip()
    if not raw:
        raise UpstreamRootError(
            "ARCGIS_UPSTREAM_ROOT is not set. WarmPoolBackend sets it to the "
            "arcgis-mcp-bridge source checkout before spawning this worker."
        )
    root = Path(raw).expanduser().resolve()
    if not (root / "arcgis_mcp" / "worker.py").is_file():
        raise UpstreamRootError(
            f"ARCGIS_UPSTREAM_ROOT={root} does not contain arcgis_mcp/worker.py."
        )
    return root


def main() -> int:
    """Warm up arcpy once, then serve NDJSON frames until stdin closes.

    Unlike upstream's one-shot ``main()``, stdin staying open is the normal
    case: the worker is expected to outlive many jobs.
    """
    # --- stdout shielding, strengthened for a long-lived worker. ---
    #
    # Upstream rebinds sys.stdout to sys.stderr, which stops *Python* prints
    # from corrupting the protocol. It does not stop ArcPy's native layer, which
    # writes to file descriptor 1 directly. In a one-shot worker that is
    # survivable: the parent reads a single frame and a stray warning line is
    # simply skipped. In a long-lived worker it is fatal -- one misplaced line
    # desynchronises every subsequent read, and the session never recovers.
    #
    # So redirect at the fd level too, and keep a duplicate of the original for
    # the sanctioned writes.
    real_stdout_fd = os.dup(1)
    os.dup2(2, 1)  # anything writing to fd 1 now lands on stderr
    sys.stdout = sys.stderr

    def emit(payload: str) -> None:
        """The single sanctioned stdout write: one NDJSON frame, flushed."""
        os.write(real_stdout_fd, (payload + "\n").encode("utf-8"))

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [warm-worker:%(levelname)s] %(name)s: %(message)s",
    )

    try:
        upstream_root = _resolve_upstream_root()
    except UpstreamRootError as exc:
        LOG.error("fatal: %s", exc)
        return _EXIT_WARMUP_FAILED

    # Importing arcgis_mcp.worker is cheap: arcpy itself is deferred behind
    # _get_arcpy(), so this does not pay the expensive tax yet.
    sys.path.insert(0, str(upstream_root))
    try:
        from arcgis_mcp.worker import _build_guard, _get_arcpy, process_frame
    except Exception as exc:  # noqa: BLE001 -- any import failure is fatal here
        LOG.error("fatal: could not import arcgis_mcp.worker: %s", exc)
        return _EXIT_WARMUP_FAILED

    # --- Warm-up: pay the arcpy tax exactly once, before serving any frame. ---
    # This is the entire point of the warm pool. Upstream pays it per job.
    warmup_started = time.perf_counter()
    try:
        _get_arcpy()
    except Exception as exc:  # noqa: BLE001 -- license/import failure is fatal
        LOG.error("fatal: arcpy warm-up failed: %s", exc)
        return _EXIT_WARMUP_FAILED
    LOG.info(
        "arcpy warm-up complete in %.1f s; entering frame loop",
        time.perf_counter() - warmup_started,
    )

    try:
        guard = _build_guard()
    except Exception as exc:  # noqa: BLE001 -- config failure is fatal
        LOG.error("fatal: worker configuration error: %s", exc)
        return _EXIT_WARMUP_FAILED

    # --- Frame loop: one NDJSON request in, one NDJSON result out, forever. ---
    served = 0
    while True:
        # Read bytes and decode explicitly: the parent always writes UTF-8
        # (warm_pool encodes the frame itself), but sys.stdin on Windows uses
        # the console code page (e.g. GBK), which would corrupt non-ASCII
        # paths -- silently mis-routing them to PathGuard or the tool.
        # (Codex red-team P1.)
        raw_line = sys.stdin.buffer.readline().decode("utf-8")
        if not raw_line:  # EOF: the parent closed the pipe (shutdown or restart)
            break
        if not raw_line.strip():  # tolerate keep-alive blank lines
            continue

        result = process_frame(raw_line, guard)

        # The single sanctioned stdout write per frame. Written straight to the
        # duplicated fd: the buffered `print(...)` path is not used because
        # fd-level writes are what survive ArcPy's native stdout traffic.
        emit(result.model_dump_json())
        served += 1

    LOG.info("stdin closed after serving %d frame(s); exiting cleanly", served)
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
