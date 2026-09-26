# ArcGIS 工具编排评测（arcgis-agent-lab）

> 给 LLM 一套真实的 ArcGIS 地理处理工具链，**量化它在哪一步失败、为什么失败、该改什么**。
>
> 一句话概括增量：**基座证明「LLM 能调用 ArcGIS 工具」；本项目证明「它在哪一步失败」，并且让这个证明不需要 ArcGIS 许可就能被复现。**

---

## 为什么值得做这件事

「把 GIS 封装成 MCP 工具」这件事已经被做烂了 —— GitHub 上 30+ 个仓库，最高 88★。
学术界也已经有了成熟的 GIS agent 基准：**GeoAgentBench**（53 任务 / 117 工具）、
**GISAgentBench**（349 任务）、**GeoAnalystBench**（50 任务）。

**但它们全都在开源栈上跑。** GeoAgentBench 明确排除了依赖专有格式的任务 ——
因为 ArcGIS 这条路**天然不可复现**：商业许可、Windows 独占、桌面引擎状态。

于是出现了一个真实的空白：

| 对谁 | 它们在做什么 | 本项目做什么 |
|---|---|---|
| 30+ 个 ArcGIS MCP 仓库 | 证明「能调通」 | 量化「**何时失败、为何失败**」 |
| GABench / GISAgentBench | 在**开源栈**上评工具调用与代码生成 | 在**商业桌面 GIS 的 MCP 工具面**上评，且提供**免许可复现模式** |
| 多数同类项目 | 报一个准确率 | 四层序列指标 + **F1–F9 失败归因**（每类都指向修复方向）；未回答与待核验单列，不混入通过 |

**本项目要回答的不是「多好」，而是「该改什么」。**

---

## 关键数字

| 指标 | 值 | 说明 |
|---|---|---|
| arcpy 单次调用开销 | **234 s → 1.6 s** | 自研常驻热池后端，约 150 倍 |
| 6 次图层导入总耗时 | 23.4 min → **4 min 11 s** | 上游默认后端 vs 本项目 |
| 12 次调用（含预热） | **54 s** | 脚本化参考 agent 的一轮完整运行 |
| 基座单元测试 | **86/86 全绿** | 在 ArcGIS Pro **3.6** + Python **3.13.7** 上（上游兼容表只到 3.4） |
| 本项目测试 | **60 passed** | 指标、归因、统计、帧读取、LLM agent 解析 |
| 金标准任务集 | **29 题** | single_step 12 / multi_step 6 / crs_trap 5 / contract 6 |
| 被测工具面 | **100 个命名工具** | 刻意排除任意代码执行入口（见下） |
| 上游回馈 | **3 个 issue** | 见文末，含一个静默失败缺陷 |

---

## 三层增量

### 1. WarmPoolBackend —— 让评测跑得起来

基座默认每次调用起一个新 worker，因此**每次**都要付 `import arcpy` 的税。
上游文档写的是 10–30 秒；本机实测最坏 **239 秒**，导致上游自带的冒烟基准里
8 个 case 有 2 个直接撞上客户端 240 秒超时。

本项目实现基座**自己预留但未实现**的热池后端（`ExecutionBackend` Protocol 的另一个实现）：

- `warm_worker.py` **复用上游的 `process_frame`**（它已被拆成纯函数），只把「读一帧就退出」
  改成**循环读帧**，并在循环前主动预热 arcpy 一次。**不改任何语义**，错误分类映射原封不动。
- `warm_pool.py` 实现常驻池：懒启动、崩溃自动重启、超时即回收。

**同时保留了权衡**：热池牺牲上游的 *zero state leakage between jobs* 保证
（`arcpy.env`、GP 历史、检出的扩展许可会跨帧残留），所以它是 **opt-in**，
默认仍是 `SubprocessBackend`。**知道自己在放弃什么，比宣称拿到什么更重要。**

### 2. Harness —— 只执行与记录，不做判断

```
harness/toolset.py   导出工具面为 function-calling schema（含逃生舱纪律）
harness/session.py   执行一次调用：契约校验 → PathGuard → backend → 记录
harness/recorder.py  每次调用一行 JSONL（含耗时与 error_kind）
harness/runner.py    遍历任务集，不评分
harness/agents.py    脚本化参考 agent
```

**评分与执行分离**：一份轨迹可以在不同指标定义下重新打分，而不用重跑那些昂贵的工具调用。

### 3. Metrics —— 回答「该改什么」

**四层序列指标**（定义参考 GABench，实现为本项目版本）：

| 指标 | 回答的问题 |
|---|---|
| `TAO` | 该用的工具都出现了吗（不计顺序） |
| `TIO` | 顺序对吗 |
| `TEM` | 完全一致吗（不多不少） |
| `step_ratio` | 绕路了吗 |

**F1–F9 失败归因**，每一类都绑定一个修复方向：

| 代码 | 失败类型 | 该改什么 |
|---|---|---|
| `F1` | 工具选择错误 | 改工具描述（消歧不足） |
| `F2` | 工具顺序错误 | 改规划提示词 |
| `F3` | 参数 / 坐标系错误 | 在工具描述里补单位与 CRS 约束 |
| `F4` | 契约违反 | 把 confirm / overwrite 等语义暴露给模型 |
| `F5` | 沙箱越界 | **先检查 ALLOWED_ROOTS 配置 —— 这可能不是模型的错** |
| `F6` | 引擎 / 许可噪声 | **从模型分数中剔除** |
| `F7` | 多步状态污染 | 在中间产物后加校验点 |
| `F8` | 幻觉工具 | 补工具发现机制 |
| `F9` | 未回答 / 中断 | 检查 agent 稳定性与轮次预算 |

其中 **`F6` 不只是被标记，它必须离开分母** —— 否则环境故障会被静默算成模型能力。
这一点在代码里是结果自带的 `excluded_from_score` 属性，而不是靠调用方记得过滤。

**`F6` 的边界经过一次修正**：基座把所有 `arcpy.ExecuteError` 压成同一个
`geoprocessing` 种类，而初版归因器照单全收当成环境噪声 —— 结果把模型的错误
洗成了环境的错，还踢出了分母。真实运行里 **58 次这样的失败全部是模型的问题**：
参数值非法（`ERROR 000800`）、没带 `overwrite`（`000725`）、引用了没建成的数据集
（`000732`）。现在按 **ArcPy 错误码显式映射**，未识别的才留作 F6。
修正后计入评分从 12 条变成 28 条，而且 **`F7` 第一次有了真实判据**。

---

## 三个方法论决定（都写进了代码注释）

**① 逃生舱不提供给模型。** 基座暴露 `execute_spatial_tool`，接受任意工具名与参数。
允许它会**绕开 100 个具名工具去现场写代码** —— 那正是已有基准测过的东西，
而「工具编排能力」就测不到了。见 `harness/toolset.py`。

**② harness 不走 MCP 传输层，直接驱动等价的三件套。** 它绕过传输层调用 registry + 契约校验 + PathGuard + backend —— 传输层只搬字节，工具面与执行语义都在下游三件套里，harness 原样复用。**等价性是论证出来的，不是性质测试证明的** —— 这是本项目的诚实边界之一（Codex 红队指出 bundle 里并无 parity 测试，措辞已从"验证等价性"改为"直接驱动等价三件套"）。

**③ 参考轨迹必须拿满分。** 脚本化 agent 按构造做正确的事，所以它在有脚本的任务上**必须零失败**。
**如果它没拿到满分，那是指标错了，不是 agent 错了。**

> 这条断言不是装饰。本项目用它抓出了**三个基准自身的缺陷**：
> 「未尝试」被当成「失败」（TAO 假跌到 0.345）、「预期拒绝」被当成「失败」、
> 任务集的参考工具链不完整。没有这条断言，这些都会以"看起来很正常的低分"被记录下来，
> 而整份基准的可信度就建立在沙滩上。

---

## 快速开始

**第 0 步是必须的**：本项目是 `arcgis-mcp-bridge` 的**增量**，它作为依赖使用。
上游代码**不在本仓库里**（见「依赖的上游」一节的许可证说明），需要自己取。

```bash
git clone https://github.com/<you>/arcgis-agent-lab.git
cd arcgis-agent-lab

# 1) 取上游依赖（本项目 import 它，但不 vendor 它）
git clone --depth 1 https://github.com/muend/arcgis-mcp-bridge.git upstream/arcgis-mcp-bridge

# 2) 建环境并安装（numpy / pyproj / shapely 是场景生成的直接依赖）
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"        # Windows
# source .venv/bin/activate && pip install -e ".[dev]" # Linux/macOS
```

`upstream/` 目录被 `.gitignore` 排除，所以脚本按**仓库内相对路径**找它。
如果你把它放在别处，用 `ARCGIS_MCP_UPSTREAM_ROOT` 指向即可
（所有需要它的脚本都会读这个变量）。

```bash
PY=".venv/Scripts/python"        # 需要 3.11+

# 3) 生成合成场景（确定性，固定 seed；不需要 GDAL、不需要许可）
"$PY" -m arcgis_agent_lab.data.generate --out bench/synthetic

# 4) 从真值生成任务集（期望值自动派生，不手填）
"$PY" -m arcgis_agent_lab.tasks.build_tasks --scenario bench/synthetic

# 5) 跑测试（这一步不需要 ArcGIS）
"$PY" -m pytest tests/ -q

# 6) 需要 ArcGIS Pro 的部分：导入场景 → 跑参考 agent → 出报告
export ARCPY_PYTHON_PATH="C:/Program Files/ArcGIS/Pro/bin/Python/envs/arcgispro-py3/python.exe"
export ARCGIS_MCP_ALLOWED_ROOTS="$(pwd)/bench"
export ARCGIS_MCP_SCRATCH_GDB="$(pwd)/bench/scratch.gdb"
"$PY" scripts/prepare_scenario.py
"$PY" scripts/run_suite.py --agent scripted
"$PY" scripts/make_report.py
```

**没有 ArcGIS 许可也能验证一部分**：`scripts/verify_harness.py` 检查工具面纪律、
错误分类与轨迹记录（14 项断言），不需要 arcpy。`pytest` 的 94 个测试同样如此 ——
这是本项目"可复现"目标的一部分。

---

## 诚实的边界

- **规模小**：29 题，而 GABench 53 题、GISAgentBench 349 题。小是刻意取舍 ——
  这个领域以前没人评测，首先因为商业桌面 GIS 不可复现。本项目先解决「可复现」，规模是第二步。
- **合成数据**：真实宗地/耕地数据**没有 ground truth**（无法验证「这块地到底有没有占压」），
  所以评测用自带真值的合成场景。演示可接真实数据，评测不行。
- **单机单许可**：串行执行（`ARCGIS_MCP_MAX_WORKERS=1`），不反映并发表现。
- **PEA（参数级指标）未实现**：需要每次调用的参考参数，当前任务集只带工具名链。
  **已声明未实现，而不是用近似值顶替。**
- **F7 有一条错误码触发规则（ArcPy `000732`），但没有产物级校验**：多步链的中间产物
  是否真的支持下游引用，目前不从产物验证 —— 这是与参数级指标同源的待办。
- **部分任务的可验证性经过了一轮外部红队审核并重构**：布尔/数值题的期望答案必须能被
  其指定工具链的**产物**验证（相切求交产物为零要素的题已重写）；contract 类的文字拒绝题
  改为按答案语义评分，不再强制要求产生特定工具错误。

---

## 上游回馈

在基座上工作时发现的每个问题都已反馈给 `muend/arcgis-mcp-bridge`，
草稿留档在 `docs/contributions/`：

| # | 内容 | 性质 |
|---|---|---|
| [#23](https://github.com/muend/arcgis-mcp-bridge/issues/23) | ArcPy 原生层写 fd 1 会让常驻 worker 的帧流永久错位 | 技术缺陷 + 两处修法 |
| [#24](https://github.com/muend/arcgis-mcp-bridge/issues/24) | ArcGIS Pro 3.6 / Python 3.13.7 验证 + 冷启动耗时警告 | 兼容性确认 |
| [#25](https://github.com/muend/arcgis-mcp-bridge/issues/25) | `import_from_geojson` 对 `.geojson` 只解析面要素 | **静默失败** + 最小复现 |

**第 25 条是本项目踩得最深的一个坑，也最能说明这个项目在做什么。**

现象：点图层和线图层导入后是**空的**，且**全程不报错**。
后果：下游任务失败（`ERROR 000732 数据集不存在`），或者模型如实报告"这个图层是空的"——
**而错误被归到了模型头上**。

我们花了一整轮评测把失败归因到模型身上，才反过来发现根因是文件扩展名：
`arcpy.conversion.JSONToFeatures` 对 `.geojson` 只认面要素，
把同样的字节改名成 `.json` 就三种几何类型全部正常。

**这恰好是本项目的核心命题本身**：一个不报错的错误比一个会报错的错误危险得多，
因为它把责任推给了链条上的下一环。

---

## 依赖的上游

基座是 [`muend/arcgis-mcp-bridge`](https://github.com/muend/arcgis-mcp-bridge)（**Apache-2.0**），
提供 100 个 arcpy 工具的 MCP 封装与双进程隔离架构。**本项目在其之上做增量，不改动其语义。**

指标定义参考 [GeoAgentBench (GABench, arXiv 2604.13888)](https://arxiv.org/abs/2604.13888)，
它同时提出了本项目复用的 PEA 指标。**借鉴是刻意标注的** —— 本项目的贡献不是发明新 schema，
而是把已有的评测纪律应用到一个现有基准主动排除的技术栈上。

---

## 目录结构

```
src/arcgis_agent_lab/
├── backends/   warm_worker.py  warm_pool.py     常驻热池（234s → 1.6s）
├── data/       generate.py                       确定性合成场景 + 真值
├── tasks/      build_tasks.py  tasks.jsonl       29 条金标准任务（真值自动派生）
├── harness/    toolset/session/recorder/runner/agents
├── metrics/    trajectory.py  attribution.py     四层指标 + F1–F8 归因
├── stats/      bootstrap.py  paired.py           置信区间 + 配对检验（零依赖）
└── report/     build.py                          报告（含指纹冻结与局限声明）
scripts/        verify_harness / prepare_scenario / run_suite / make_report
docs/           basement-walkthrough.md           基座技术拆解
tests/          39 个测试
```
