"""WarmPoolBackend — a persistent-worker execution backend.

Drop-in alternative to upstream's ``arcgis_mcp.execution.SubprocessBackend``:
same structural ``ExecutionBackend`` Protocol, same contract (never raises for
worker-side problems -- every failure becomes a structured ``WorkerResult``),
different latency/isolation trade-off.

==================  =========================  ==========================
                    SubprocessBackend          WarmPoolBackend
==================  =========================  ==========================
worker lifetime     one per job                one per backend lifetime
arcpy import tax    paid on every call         paid once, at startup
job isolation       full (no state leakage)    **weaker** (state persists)
crash blast radius  contained to one job       contained to one restart
==================  =========================  ==========================

The isolation weakening is real and is why this backend is opt-in. A warm
worker keeps ``arcpy.env`` settings, geoprocessing history and checked-out
extension license seats alive between jobs; upstream's spawn-per-call design
deliberately avoids all of that. Use this backend when predictable latency
matters more than job-to-job purity (batch evaluation is the motivating case),
and say so explicitly when reporting results.

Why a single worker
-------------------
A desktop ArcGIS Pro license cannot run supervised automation on more than one
machine and its GP tools are not thread-safe; upstream's own benchmark harness
pins ``ARCGIS_MCP_MAX_WORKERS=1`` for exactly this reason. Maintaining one warm
worker is therefore aligned with the environment constraint rather than a
limitation of this implementation. Requests serialize on an internal gate.

Measured motivation
-------------------
On the development machine, ``import arcpy`` cost 223-239 s per process
(ArcGIS Pro 3.6 / Python 3.13.7, real-time AV scanning the native DLLs).
Upstream documents an expected 10-30 s. Two of eight upstream smoke-benchmark
cases exceeded a client's 240 s request timeout as a direct result. See
``docs/warm-pool.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from arcgis_mcp.contracts import WorkerError, WorkerJob, WorkerResult

LOG: Final[logging.Logger] = logging.getLogger("arcgis_agent_lab.warm_pool")

#: Same ceiling upstream applies, so a runaway GP message stack cannot exhaust
#: the server's memory here either.
_MAX_STREAM_BYTES: Final[int] = 8 * 1024 * 1024

#: Grace period between kill() and giving up on wait() (mirrors upstream).
_KILL_GRACE_S: Final[float] = 10.0

_WORKER_SCRIPT: Final[Path] = Path(__file__).resolve().parent / "warm_worker.py"


class WarmWorkerError(RuntimeError):
    """Base class for infrastructure problems on the warm-worker channel."""


class WarmWorkerGoneError(WarmWorkerError):
    """The worker died or closed its pipe; it has been discarded."""


class WarmWorkerTimeoutError(WarmWorkerError):
    """The worker exceeded the per-job timeout; it has been discarded."""


def _failure(job_id: str, kind: str, message: str) -> WorkerResult:
    """Build a structured failure frame (single construction point, mirrors upstream)."""
    return WorkerResult(
        job_id=job_id,
        ok=False,
        error=WorkerError(kind=kind, message=message),
    )


class WarmPoolBackend:
    """Persistent, pre-imported arcpy worker behind the ``ExecutionBackend`` Protocol.

    Args:
        arcpy_python: Interpreter that can import both ``arcpy`` and
            ``arcgis_mcp.worker`` (the licensed ArcGIS Pro environment).
        upstream_root: Source checkout containing the ``arcgis_mcp`` package.
            Passed to the worker as ``ARCGIS_UPSTREAM_ROOT`` and used as its
            ``cwd``, so the package resolves without mutating ``PYTHONPATH``.
        worker_script: Override for the warm-worker entry point (tests use this).
    """

    def __init__(
        self,
        arcpy_python: Path,
        upstream_root: Path,
        worker_script: Path | None = None,
    ) -> None:
        if not arcpy_python.is_file():
            raise FileNotFoundError(f"Worker interpreter not found: {arcpy_python}")
        if not (upstream_root / "arcgis_mcp" / "worker.py").is_file():
            raise FileNotFoundError(
                f"No arcgis_mcp package under upstream_root: {upstream_root}"
            )
        script = (worker_script or _WORKER_SCRIPT).resolve()
        if not script.is_file():
            raise FileNotFoundError(f"Warm-worker script not found: {script}")

        self._python: Final[Path] = arcpy_python
        self._upstream_root: Final[Path] = upstream_root
        self._worker_script: Final[Path] = script

        #: Serializes requests: one live arcpy interpreter, matching the
        #: single-seat desktop-license constraint.
        self._gate: Final[asyncio.Semaphore] = asyncio.Semaphore(1)

        #: Guards worker creation/teardown so concurrent callers cannot race
        #: into spawning two interpreters.
        self._lifecycle: Final[asyncio.Lock] = asyncio.Lock()
        #: Set by ``aclose()``. Once closed, queued or later ``run_job`` calls
        #: fail explicitly instead of starting a new worker behind the caller's
        #: back (Codex red-team round 4, P1).
        self._closed: bool = False

        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        #: Observability counters (surfaced in evaluation reports).
        self.restarts: int = 0
        self.jobs_served: int = 0

    # ------------------------------------------------------------------ #
    # ExecutionBackend Protocol
    # ------------------------------------------------------------------ #

    async def run_job(self, job: WorkerJob, *, timeout_s: int) -> WorkerResult:
        """Execute one job against the warm worker.

        The timeout measures *geoprocessing*, not the one-off startup warm-up:
        by the time this method returns, arcpy is already imported. A very first
        call issued before warm-up finishes will still block, because the worker
        does not read stdin until its import completes -- that cost is paid once
        per backend lifetime, not per job.
        """
        async with self._gate:
            if self._closed:
                # Checked INSIDE the gate: setting the flag before acquiring it
                # would not stop a request that was already waiting (round 4).
                return _failure(
                    job.job_id, "internal", "Warm pool is closed; no new job accepted."
                )
            try:
                result = await self._exchange(job, timeout_s=timeout_s)
            except WarmWorkerTimeoutError as exc:
                return _failure(job.job_id, "internal", str(exc))
            except WarmWorkerError as exc:
                return _failure(job.job_id, "internal", str(exc))

        if result.ok:
            self.jobs_served += 1
        return result

    async def aclose(self) -> None:
        """Shut the worker down cleanly (closing stdin is the shutdown signal).

        Two locks, in order (Codex red-team P1): the **execution gate** first,
        so an in-flight `run_job` finishes its frame before the pipes close
        (closing mid-job would leave a geoprocessing task half-applied to the
        data), then the **lifecycle lock** for the teardown itself.

        Calls the ``_locked`` variant deliberately. ``asyncio.Lock`` is **not**
        reentrant, so holding ``_lifecycle`` here and then delegating to
        ``_discard_worker()`` -- which acquires it again -- deadlocks forever.
        That bug made every run appear to complete while its process never
        exited; the smoke test only surfaced it as a hang after the last log
        line. Kept as a comment because it is an easy mistake to reintroduce.
        """
        LOG.info("aclose: shutting down the warm worker")
        # Mark closed BEFORE taking the gate: a request already queued behind
        # the gate must see the flag once it gets in, instead of spawning a
        # fresh ArcPy worker after the caller believes everything is shut down
        # (Codex red-team round 4, P1).
        self._closed = True
        async with self._gate:
            async with self._lifecycle:
                await self._discard_worker_locked()
        LOG.info("aclose: worker stopped (restarts=%d)", self.restarts)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _exchange(self, job: WorkerJob, *, timeout_s: int) -> WorkerResult:
        """Send one frame, await its result. Discards the worker on any fault.

        The whole exchange is wrapped for cancellation, not just the read: a
        cancel can land while the worker is starting (``_ensure_worker``) or
        while the frame is being drained into its stdin. In every case the
        worker may already be committed to the job, so it is recycled rather
        than reused (Codex red-team P1: wrapping only ``_read_frame`` left a
        probe with zero recycling).
        """
        try:
            return await self._exchange_locked(job, timeout_s=timeout_s)
        except asyncio.CancelledError:
            await self._discard_worker()
            raise

    async def _exchange_locked(self, job: WorkerJob, *, timeout_s: int) -> WorkerResult:
        proc = await self._ensure_worker()

        if proc.stdin is None or proc.stdout is None:  # pragma: no cover - defensive
            await self._discard_worker()
            raise WarmWorkerGoneError("Warm worker has no usable pipes.")

        frame = (job.model_dump_json() + "\n").encode("utf-8")
        try:
            proc.stdin.write(frame)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            await self._discard_worker()
            raise WarmWorkerGoneError(
                f"Warm worker died while receiving the job frame: {exc}"
            ) from exc

        return await self._read_frame(proc, job.job_id, timeout_s=timeout_s)

    async def _read_frame(
        self, proc: asyncio.subprocess.Process, job_id: str, *, timeout_s: float
    ) -> WorkerResult:
        """Read until a well-formed frame for ``job_id`` arrives.

        Why not simply take the next line: ArcPy's native layer writes warnings
        to the process's stdout regardless of Python-level shielding -- observed
        in practice as ``WARNING 000635: ...`` landing between frames. In a
        one-shot worker the parent reads once and is done; in a long-lived worker
        a single stray line shifts every later read by one and the session never
        recovers.

        Skipping non-frames hardens against the stray-line case, and matching on
        ``job_id`` recovers even when a frame arrives out of order.
        """
        if proc.stdout is None:  # pragma: no cover - defensive
            await self._discard_worker()
            raise WarmWorkerGoneError("Warm worker has no stdout pipe.")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._discard_worker()
                raise WarmWorkerTimeoutError(
                    f"Warm worker exceeded the {timeout_s:g}s timeout and was recycled."
                )
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except TimeoutError as exc:
                # A timeout leaves the worker's internal state unknowable (a GP
                # tool may still be running), so it is recycled rather than
                # reused -- the same reasoning upstream applies on timeout.
                await self._discard_worker()
                raise WarmWorkerTimeoutError(
                    f"Warm worker exceeded the {timeout_s:g}s timeout and was recycled."
                ) from exc

            if not raw:
                await self._discard_worker()
                raise WarmWorkerGoneError(
                    "Warm worker exited without producing a response frame."
                )

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                result = WorkerResult.model_validate_json(line)
            except (ValidationError, ValueError):
                LOG.debug("skipping non-frame stdout line: %s", line[:120])
                continue

            if result.job_id != job_id:
                LOG.warning(
                    "skipping frame for a different job (%s while waiting for %s)",
                    result.job_id,
                    job_id,
                )
                continue

            return result

    async def _ensure_worker(self) -> asyncio.subprocess.Process:
        """Return a live worker, starting one if needed (creation is serialized)."""
        async with self._lifecycle:
            if self._proc is not None and self._proc.returncode is None:
                return self._proc
            if self._proc is not None:
                await self._discard_worker_locked()
            self._proc = await self._spawn()
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))
            return self._proc

    async def _spawn(self) -> asyncio.subprocess.Process:
        env = {**os.environ, "ARCGIS_UPSTREAM_ROOT": str(self._upstream_root)}
        LOG.info("starting persistent warm worker: %s", self._python)
        return await asyncio.create_subprocess_exec(
            str(self._python),
            "-u",  # unbuffered pipes: no half-written frames
            str(self._worker_script),
            cwd=str(self._upstream_root),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_MAX_STREAM_BYTES,
        )

    async def _discard_worker(self) -> None:
        async with self._lifecycle:
            await self._discard_worker_locked()

    async def _discard_worker_locked(self) -> None:
        """Reap the current worker. Caller must hold ``_lifecycle``."""
        proc = self._proc
        self._proc = None
        if self._stderr_task is not None:
            # A cancelled task that is never awaited leaves a pending coroutine
            # on the loop, which keeps the process alive after aclose() returns.
            # Awaiting it (suppressing the cancellation itself) is what makes
            # shutdown actually complete.
            stderr_task, self._stderr_task = self._stderr_task, None
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
        if proc is None:
            return

        self.restarts += 1
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()  # graceful: the worker's stdin loop ends
        except (BrokenPipeError, OSError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_S)
            LOG.info("warm worker exited cleanly (returncode=%s)", proc.returncode)
            return
        except TimeoutError:
            LOG.warning("warm worker ignored stdin close; killing")
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_S)
        except (TimeoutError, ProcessLookupError) as exc:
            LOG.warning("warm worker reaping issue: %s", exc)

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
        """Relay worker diagnostics so the pipe never fills and blocks the worker."""
        if proc.stderr is None:  # pragma: no cover - defensive
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                LOG.debug("[warm-worker] %s", line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash the caller
            LOG.debug("stderr relay stopped: %s", exc)


__all__ = [
    "WarmPoolBackend",
    "WarmWorkerError",
    "WarmWorkerGoneError",
    "WarmWorkerTimeoutError",
]
