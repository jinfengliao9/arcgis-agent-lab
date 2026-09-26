## Compatibility confirmation

The README's compatibility table lists up to 3.4. I ran the full suite on a
newer combination and it passes cleanly, so it may be worth adding.

```
ArcGIS Pro 3.6.0  ·  Python 3.13.7  ·  Windows 11 x64
python -m pytest -q   ->   86 passed in 4.61s
```

I also ran the runtime benchmark on the same machine:

```
python -m benchmarks.arcgis_pro_smoke \
  --arcpy-python "...\arcgispro-py3\python.exe" \
  --allowed-root <root> --scratch-gdb <root>\scratch.gdb \
  --arcgis-pro-version "3.6" --expected-tool-count 103 --write-check
```

Result: **the tool surface and safety cases behave exactly as documented** —
103 tools discovered, PathGuard rejection of an out-of-root path, and the
destructive `confirm=true` gate all correct.

## One caveat worth knowing: cold `import arcpy` is far slower than ~10-30 s

Measured across three independent processes on this machine:

| run | `import arcpy` |
|---|---|
| 1 | 239.0 s |
| 2 | 230.3 s |
| 3 | 223.6 s |

`MsMpEng.exe` (Windows Defender) was sitting at ~465 MB of RSS while these ran,
which points at real-time scanning of ArcGIS's native DLLs as the cause.

Practical consequence for the benchmark harness: it pins
`ARCGIS_MCP_TOOL_TIMEOUT=180`, so the **first** arcpy-backed case
(`arcpy-spatial-reference`) times out at 180 s, while every subsequent one
passes in **26–37 s** once the DLLs are cached. Not a bug in the bridge — but
180 s is too tight for a cold start on a machine with real-time AV, and the
`SubprocessBackend` docstring's "~10-30 s" assumption does not hold here.

(Raising `ARCGIS_MCP_TOOL_TIMEOUT` externally does override the harness
default — `setdefault` in `benchmarks/arcgis_pro_smoke.py` is a nice touch.)

## Small pytest note that cost me an hour

`pytest` failed **62 of 86** tests here with `PermissionError: [WinError 5]`
— not a code problem. pytest's default `tmp_path` root
(`%LOCALAPPDATA%\Temp\pytest-of-<user>`) was access-denied on this machine, so
every fixture that requested `tmp_path` errored at setup.

Passing `--basetemp=<project>/.pytest-tmp` makes the suite pass **86/86**.
Might be worth a line in CONTRIBUTING.md for Windows users — the error message
looks like a code failure and sent me looking in the wrong place first.

## Environment

- arcgis-mcp-bridge 0.6.6 (commit `2019d7b0`)
- ArcGIS Pro 3.6.0, Python 3.13.7, Windows 11 x64
- Licence level: ArcInfo, all extensions available
