"""Execution backends for the ArcGIS MCP worker.

Upstream ships exactly one backend: ``arcgis_mcp.execution.SubprocessBackend``
(spawn-per-call, maximum isolation). Its own class docstring reserves room for
the alternative implemented here::

    The warm-pool variant will live behind the same Protocol when latency
    matters.   -- arcgis_mcp/execution.py

This package provides that variant. Both satisfy the same structural
``ExecutionBackend`` Protocol, so they are interchangeable at the single
construction site in ``arcgis_mcp.server.build_runtime``.
"""

from .warm_pool import WarmPoolBackend

__all__ = ["WarmPoolBackend"]
