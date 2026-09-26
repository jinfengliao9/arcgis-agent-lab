## Summary

While building a persistent (warm-pool) execution backend against
`arcgis_mcp.execution.ExecutionBackend`, I hit a failure mode that
spawn-per-call is structurally immune to. Recording it here because it
constrains what any warm-pool variant can look like.

**ArcPy's native layer writes to file descriptor 1 directly, bypassing Python's
`sys.stdout`.** `worker.main()` rebinds `sys.stdout` to `sys.stderr`, which
stops *Python-level* prints from reaching the protocol stream — but not the
native layer.

## Why this matters for a persistent worker, and not (yet) upstream

With `SubprocessBackend` the worker lives for exactly one frame and the parent
reads stdout once. A stray line is absorbed by `_parse_response()`, which takes
the **last** non-empty line.

In a persistent worker the same stray line is fatal: the parent's `readline()`
consumes it as if it were the response, and **every subsequent read is off by
one for the rest of the session**.

## Observed

```
unparseable frame: Invalid JSON: expected value at line 1 column 1
  input_value='WARNING 000635: 输入为空或 NULL 作为空。'
```

ArcGIS Pro 3.6 / Python 3.13.7, in-process FastMCP client, one warm worker
serving ~40 sequential `run_tool` frames. ArcPy emits the warning during a
geoprocessing call, i.e. *between* frames.

## Two fixes, both needed

**1. fd-level redirection in the worker** — keeps the protocol stream clean at
the source:

```python
real_stdout_fd = os.dup(1)
os.dup2(2, 1)            # anything writing to fd 1 now lands on stderr
sys.stdout = sys.stderr  # upstream's Python-level guard, kept
...
os.write(real_stdout_fd, (frame + "\n").encode("utf-8"))
```

This is strictly safer than `sys.stdout = sys.stderr` alone and costs nothing
in the one-shot case, so it could live in `worker.main()` unconditionally.

**2. Correlation on the reader side** — recovers even if a stray line slips
through: skip anything that is not a well-formed frame for the expected
`job_id`, instead of taking the next line.

Both are needed. (1) keeps the common case clean; (2) means a single leak does
not poison a long session.

## Suggested change

(1) belongs in `worker.main()`. (2) is a backend-side concern and sits
naturally in whatever module implements the warm pool.

Happy to send a PR for (1) if useful.

## Environment

- arcgis-mcp-bridge 0.6.6 (commit `2019d7b0`)
- ArcGIS Pro 3.6, Python 3.13.7, Windows 11 x64
- `ARCGIS_MCP_MAX_WORKERS=1`, `ARCGIS_MCP_TOOL_TIMEOUT=900`
