## Summary

Of the four read-only metadata tools, **three report a missing dataset as an
unclassified worker crash** instead of a diagnosable error:

```
dataset = <scratch.gdb>\this_dataset_never_existed

get_extent          -> kind=internal       "Unexpected worker error (OSError); see server logs."
describe_dataset    -> kind=internal       "Unexpected worker error (OSError); see server logs."
get_field_info      -> kind=internal       "Unexpected worker error (OSError); see server logs."
get_feature_count   -> kind=geoprocessing  "ERROR 000732: 输入行: 数据集 ... 不存在或不受支持"
```

Only `get_feature_count` produces something an agent can act on. The other three
collapse into `internal`, whose message points at the server logs -- which a
model cannot read.

## Why this matters more than it looks

An agent-driven geoprocessing session is a long chain of dependent calls. When a
mid-chain step fails, the agent's next decision depends entirely on *what it is
told*. Two failure modes are very different in cost:

* `ERROR 000732: 数据集不存在` -- the agent learns the artefact is missing and can
  retry, change the output name, or repair the chain.
* `Unexpected worker error (OSError); see server logs` -- the agent learns
  nothing. Typical observed behaviour is to retry the same call, probe
  neighbouring names, or give up on the task.

Measured in a 29-task evaluation (1,085 tool calls): the two `internal` failures
below both came from this path, and both tasks ended without an answer.
`get_extent` was called 37 times that run; the two failures were exactly the two
calls whose dataset was missing.

```
task=T11 turn=692 get_extent {"dataset": "...\\int_area_stats"}    <- never created
task=T15 turn=801 get_extent {"dataset": "...\\geo_p12_near"}      <- never created
```

## Likely cause

`arcpy.Describe` on a non-existent path raises `OSError` (or the tool dereferences
`None`), and the worker's error mapping has no case for it, so it falls through to
the generic `internal` bucket. `get_feature_count` uses a GP tool
(`GetCount`), which raises `arcpy.ExecuteError` and therefore lands in the
`geoprocessing` kind with the ArcPy message intact.

## Suggested change

Two options, both small:

1. **Pre-check existence** in these tools and raise a categorised error, e.g.
   `error_kind="not_found"` with the offending path. That is the most useful
   shape for a caller: it is neither an environment fault nor an unknown crash.
2. **Map `OSError` / `AttributeError` from `arcpy.Describe` to the same
   `geoprocessing` treatment** so the ArcPy message survives.

Whichever is chosen, the key point is that "the dataset does not exist" should
never be reported as an unclassified crash -- it is the single most common
recoverable condition in a chained session.

## Environment

- arcgis-mcp-bridge 0.6.6 (commit `2019d7b0`)
- ArcGIS Pro 3.6, Python 3.13.7, Windows 11 x64
- Reproduced directly through `EvalSession` with `ARCGIS_MCP_MAX_WORKERS=1`
