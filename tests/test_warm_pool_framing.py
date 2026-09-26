"""Frame-reading tests for the warm pool backend.

These cover the fix for the stdout-contamination failure mode: ArcPy's native
layer writes warnings straight to file descriptor 1, bypassing Python's
``sys.stdout``, so a long-lived worker's stdout stream is **not** guaranteed to
contain only result frames. A single stray line used to shift every later read
by one, and the session never recovered.

The tests live here rather than in a scratch script because this logic is the
whole reason the warm pool is usable in a long session -- and because it is
exactly the kind of code that gets "simplified" back into a naive
``readline()`` during a later refactor. The first test reproduces the literal
line observed in production.
"""

from __future__ import annotations

import asyncio

import pytest

from arcgis_agent_lab.backends.warm_pool import (
    WarmPoolBackend,
    WarmWorkerGoneError,
    WarmWorkerTimeoutError,
)
from arcgis_mcp.contracts import WorkerResult


def _frame(job_id: str) -> str:
    """A well-formed result frame, as the worker writes it."""
    return WorkerResult(job_id=job_id, ok=True, result={"pong": True}).model_dump_json()


def _reader(*lines: str, eof: bool = True) -> asyncio.StreamReader:
    """A stream pre-loaded with lines, optionally followed by EOF."""
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data((line + "\n").encode("utf-8"))
    if eof:
        reader.feed_eof()
    return reader


class _FakeProc:
    """Minimal stand-in: only ``stdout`` is read by the code under test."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.stdout = reader


def _backend() -> WarmPoolBackend:
    """A backend whose teardown is neutralised -- these tests exercise reads only."""
    backend = object.__new__(WarmPoolBackend)

    async def _noop() -> None:
        return None

    backend._discard_worker = _noop  # type: ignore[method-assign]
    return backend


class TestFrameReading:
    def test_reads_a_well_formed_frame(self) -> None:
        async def scenario() -> WorkerResult:
            proc = _FakeProc(_reader(_frame("j1")))
            return await _backend()._read_frame(proc, "j1", timeout_s=5)

        result = asyncio.run(scenario())
        assert result.job_id == "j1"
        assert result.ok

    def test_skips_the_arcpy_warning_line_observed_in_production(self) -> None:
        """Regression guard: this exact line broke a full evaluation run.

        ArcPy's native layer emitted it on fd 1 between frames. The naive
        "read the next line" implementation treated it as the response and every
        subsequent read was off by one.
        """
        warning = "WARNING 000635: 输入为空或 NULL 作为空。"

        async def scenario() -> WorkerResult:
            proc = _FakeProc(_reader(warning, _frame("j1")))
            return await _backend()._read_frame(proc, "j1", timeout_s=5)

        assert asyncio.run(scenario()).job_id == "j1"

    def test_skips_several_stray_lines(self) -> None:
        async def scenario() -> WorkerResult:
            proc = _FakeProc(_reader("noise 1", "noise 2", _frame("j1")))
            return await _backend()._read_frame(proc, "j1", timeout_s=5)

        assert asyncio.run(scenario()).job_id == "j1"

    def test_skips_a_frame_belonging_to_another_job(self) -> None:
        """Matching on job_id recovers even when frames arrive out of order."""

        async def scenario() -> WorkerResult:
            proc = _FakeProc(_reader(_frame("some-other-job"), _frame("j1")))
            return await _backend()._read_frame(proc, "j1", timeout_s=5)

        assert asyncio.run(scenario()).job_id == "j1"

    def test_eof_without_a_frame_is_an_error(self) -> None:
        async def scenario() -> None:
            proc = _FakeProc(_reader("only noise, then the pipe closes"))
            await _backend()._read_frame(proc, "j1", timeout_s=5)

        with pytest.raises(WarmWorkerGoneError):
            asyncio.run(scenario())

    def test_silence_times_out(self) -> None:
        """No data and no EOF: the read must not hang forever."""

        async def scenario() -> None:
            proc = _FakeProc(_reader(eof=False))
            await _backend()._read_frame(proc, "j1", timeout_s=0.05)

        with pytest.raises(WarmWorkerTimeoutError):
            asyncio.run(scenario())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
