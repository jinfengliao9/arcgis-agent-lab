# Red-Team Code Review Prompt（喂给 Codex 用）

> **历史文档 · 任务已完成。** 这是该项目在某一次审查时使用的任务书，**原样保留**，
> 因为它记录了"如何组织一次对抗性审核"这个方法，而不是因为里面提到的文件现在还在。
>
> * 文中提到的 `codex-review-bundle.md` 是**打包中间产物，不在本仓库中**
>   （它是全部源码的合并副本，体积约 280 KB，且与仓库内容重复）。
>   若你想复现这个流程，自己按同样的方式打包一份即可。
> * 这份任务书当时针对的是**当时那一版**代码；代码从那之后又改过多轮。
> * 想了解"按要求审出来什么、怎么修的"，看同目录的
>   `codex-task-answer-check.md` 与 `contributions/` 下的 issue 草稿。

> 使用方法见文末。把本文件连同 `codex-review-bundle.md`（全部核心代码的合并稿）
> 一起交给 Codex。本文件是给审核者的完整任务书：它包含项目背景、审核优先级、
> **故意的设计决定清单**（防止审核者把已知权衡当 bug 报告）、已知局限、以及输出格式要求。

---

## The prompt (paste this to Codex, together with the code bundle)

```text
You are a hostile senior reviewer. Your job is to find real problems in this
codebase, not to praise it. Assume the author is competent but blind to their
own blind spots -- your value is in finding what they cannot see.

If, after honest review, you find nothing at a given severity level, say so
explicitly. "No P0 findings" is a valid and useful answer. Fabricating
problems to seem thorough is worse than useless.

# PROJECT CONTEXT

`arcgis-agent-lab` is an evaluation harness that measures how well LLM agents
drive real ArcGIS Pro geoprocessing tools exposed through MCP. It is built on
top of `muend/arcgis-mcp-bridge` (Apache-2.0, v0.6.6), which provides 103
tools (100 named geoprocessing tools + 3 core endpoints).

The project adds three layers on top of the upstream bridge:

1. `src/arcgis_agent_lab/backends/` -- WarmPoolBackend: a persistent,
   pre-imported arcpy worker pool. Upstream spawns a fresh process per call
   and pays `import arcpy` (223-239s cold on this machine) every time; the
   warm pool pays it once. Upstream's own `ExecutionBackend` Protocol
   reserves room for this but does not implement it.
2. `src/arcgis_agent_lab/harness/` -- the harness: exports the tool surface
   as function-calling schemas, executes calls (contract validation ->
   PathGuard -> backend -> recording), iterates a 29-task benchmark, and
   drives both a scripted reference agent and a DeepSeek chat-model agent.
3. `src/arcgis_agent_lab/metrics/` + `stats/` -- sequence metrics
   (TAO/TIO/TEM/step-ratio, definitions following GeoAgentBench arXiv
   2604.13888), an F1-F8 failure-attribution classifier where every class
   maps to a remediation, percentile-bootstrap confidence intervals and an
   exact McNemar test -- all hand-rolled on the standard library, no SciPy.

Plus: deterministic synthetic scene generation with a truth table
(`data/generate.py`), a 29-task golden set whose expected answers are
derived from that truth (`tasks/build_tasks.py`), and a report generator
that freezes an environment fingerprint (`report/build.py`).

Environment: Windows 11, ArcGIS Pro 3.6, Python 3.13.7 (host) and
arcgispro-py3 3.13.7 (arcpy worker). The host CANNOT import arcpy; all
arcpy work happens in a spawned worker process.

Status: 60 pytest tests pass, ruff clean, one full 29-task LLM run
(deepseek-flash) completed end to end.

# REVIEW SCOPE, IN PRIORITY ORDER

1. `backends/warm_pool.py` + `backends/warm_worker.py`
   Process lifecycle, NDJSON frame protocol, crash/timeout recovery, and the
   fd-level stdout redirection (the worker dups fd 1, points fd 1 at stderr,
   and keeps one duplicate for sanctioned frame writes -- because ArcPy's
   native layer writes to fd 1 directly, bypassing Python's sys.stdout).
   Questions to answer: Can this deadlock? Leak processes or file
   descriptors? Desynchronize after a stray stdout line that is valid JSON
   but belongs to a different job? What happens if the worker is killed
   between reading a frame and writing its response?

2. `metrics/attribution.py`
   The F1-F8 classifier assigns EXACTLY ONE cause per failed task, in a
   fixed priority order. Verify the order cannot mis-attribute: e.g. can a
   genuine model fault be silently absorbed into F6 (environment noise) and
   excluded from the denominator? Is the ArcPy-error-code mapping
   (`_GEOPROCESSING_CODES`) sound? Is "unrecognised geoprocessing error ->
   treat as environment noise" the right default, or does it create a hole?

3. `stats/bootstrap.py` + `stats/paired.py`
   Hand-rolled statistics. Verify the math: percentile bootstrap interval
   construction (index clamping, rounding), the exact McNemar two-sided
   p-value ("sum of probabilities <= observed" convention), and the paired
   bootstrap (resampling shared task indices for both arms). Would a
   statistician flag any of this?

4. `harness/session.py` + `harness/runner.py` + `harness/recorder.py`
   Contract validation order, PathGuard integration, recording fidelity.
   Can a recorded trajectory be incomplete or misleading in a way that would
   corrupt downstream attribution?

5. `tasks/build_tasks.py` + `data/generate.py`
   Expected answers are DERIVED from the same truth table the synthetic data
   was generated from. Assess the circularity risk honestly: is this
   acceptable for a tool-orchestration benchmark (where the question is
   "did it call the right tools", not "did it get ground truth right"), or
   does it invalidate something the README claims?

6. `harness/llm_agent.py`
   Retry logic (429 vs other 4xx), malformed-JSON argument handling (returns
   empty dict so the contract layer rejects and RECORDS the failure), the
   system prompt with workspace context, and the fact that the DeepSeek key
   is read from outside the repository via `default_key_path()`.

7. `report/build.py` + README.md
   Does the frozen fingerprint (scenario sha256, bridge commit, tool
   catalogue hash, versions) actually capture everything needed to
   reproduce a run? Does the README claim anything the code does not do?

# INTENTIONAL DESIGN DECISIONS -- do not report these as bugs

These are documented trade-offs. If you think one is WRONG, say so under a
separate "design disagreements" heading -- but do not list them as defects.

- The harness does NOT go through the MCP transport layer. It drives the
  bridge's registry + contract layer + PathGuard + backend directly,
  arguing the transport only moves bytes. Equivalence is asserted, not
  property-tested.
- `execute_spatial_tool` (arbitrary tool execution) is deliberately
  withheld from the model's tool surface: allowing it would degrade the
  benchmark into a code-generation test, which existing benchmarks already
  measure.
- F6 (environment noise: licence/engine faults) is EXCLUDED from the
  scoring denominator by design, surfaced as `excluded_from_score` on the
  result object. Geoprocessing errors are NOT automatically F6: they are
  classified by ArcPy error code (`ERROR 000800` -> F3, `000725` -> F4,
  `000732` -> F7, `000210` -> F3), with unrecognised codes defaulting to
  environment noise.
- Refusal tasks exist where the CORRECT outcome is a specific error kind
  (`expected_error_kind`); producing that error is a pass, not a failure.
- Serial execution only (`ARCGIS_MCP_MAX_WORKERS=1`): the desktop ArcGIS
  licence does not permit parallel automation.
- The task set is small (29 tasks) on purpose: reproducibility first,
  scale second. Stated in the README.
- Synthetic data only: real parcels have no ground truth.
- The warm pool sacrifices upstream's "zero state leakage between jobs"
  guarantee (arcpy.env, GP history, checked-out extensions persist across
  frames). It is opt-in; the default remains spawn-per-call.
- The worker rebinds sys.stdout to stderr AND redirects fd 1 to fd 2,
  keeping one duplicate of the original fd for the single sanctioned frame
  write per job. This is the fix for ArcPy's native layer writing to fd 1.
- GeoJSON files are written as `.json`, not `.geojson`, because
  `arcpy.conversion.JSONToFeatures` parses `.geojson` as polygons only
  (points/polylines silently produce empty layers). Filed upstream as
  issue #25.
- Scenario files are written in EPSG:4326 (RFC 7946) and projected to
  EPSG:4547 at prepare time; the truth table is computed in metres.

# KNOWN LIMITATIONS -- verify the statements are accurate, do not
# rediscover them as findings

- PEA (parameter-level accuracy, from GABench) is declared but NOT
  implemented; the task set carries tool-name chains only.
- F7 (mid-chain state propagation) has exactly one trigger
  (ArcPy 000732); no artefact-level checks exist.
- Temperature-0 API calls are not fully deterministic; hence confidence
  intervals rather than point estimates.
- The reference trajectory (scripted agent) must score full marks; this is
  used as a validity check on the metrics themselves. It caught three
  benchmark defects during development.

# OUTPUT FORMAT

For each finding:
  [P0|P1|P2] file:line -- one-line title
  What: the concrete defect
  Why it matters: what wrong conclusion or failure it enables
  Fix: the minimal change that resolves it

P0 = can produce a wrong benchmark conclusion (bad statistics, silent
     data loss, mis-attribution, security bypass of PathGuard)
P1 = reliability (races, leaks, unhandled failure paths, protocol bugs)
P2 = quality/maintainability (anything else worth changing)

Then two short sections:
  "Claims vs reality" -- anything the README or docstrings claim that the
  code does not do (or does but does not claim).
  "Design disagreements" -- where you think an intentional trade-off is
  the wrong call, and why.

Be concrete. Cite line numbers from the bundle. Do not pad.

Respond in Chinese (simplified). Keep code identifiers and error messages
in their original language.
```

---

## 使用方法

1. **生成代码包**：仓库根目录已生成 `codex-review-bundle.md`
   （= 本 prompt + 全部核心源码按依赖顺序拼接，每个文件带路径标题）。
2. **交给 Codex**：ChatGPT Plus 里新建对话，选 Codex（或直接上传文件让它审），
   把 `codex-review-bundle.md` 作为附件上传，然后说一句：
   "Review the code in the attached bundle. Follow the instructions at the top
   of the file. 用中文输出。"
3. **收到的报告怎么用**：P0 必须改完再上传 GitHub；P1 逐条评估；
   P2 自行取舍。把 Codex 的报告拿回来给我，我来逐条核实与修复 ——
   **审核者也会出错，不经核实就照单全收，等于把审核的责任转嫁给了运气。**

## 为什么 prompt 里要放"故意的设计决定"清单

红队审核最大的浪费是把**已知权衡**当**新发现**报告。不告诉审核者哪些是故意的，
它有很大概率把「不走 MCP 传输层」「任务集只有 29 题」「热池牺牲状态隔离」这些
README 里写明的决定当成 P1 报上来 —— 你收到一堆噪音，真正的问题反而被淹没。
先把已知项声明掉，它的火力才会集中在未知的盲区上。
