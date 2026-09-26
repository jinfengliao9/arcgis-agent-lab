# arcgis-agent-lab

> 创建于 2026-09-25 · 项目根目录 `E:\arcgis-agent-lab`

## 一句话

给 LLM 一套 arcpy 工具链，让它用自然语言编排 ArcGIS 的空间分析与出图；
并用一套可复现的评测，量化它**在哪一步会失败、为什么失败、该怎么改**。

## 这个项目要证明什么（三层）

| 层 | 目标 | 判断标准 |
|---|---|---|
| **1. 能编排** | 自然语言需求 → 模型自己选工具、拼参数、多步串联、出错自修正 | 端到端 demo 可跑 |
| **2. 能证明** | 量化能力边界，而不是只报一个准确率 | 四层指标 + F1–F8 失败归因 |
| **3. 可复现** | 别人能验证我的结论 | 决策层评测**不需要 ArcGIS 许可**即可跑 |

## 技术栈

- **基座**：`muend/arcgis-mcp-bridge` v0.6.6（Apache-2.0）
  —— 103 个 arcpy 工具、双进程隔离（MCP 层零 arcpy 导入 + 独立 worker 子进程）
- **底座**：ArcGIS Pro 3.6（本机 `ArcInfo` 许可，3D/Spatial/Network/GeoStats 等全扩展可用）
- **环境隔离方式**：独立 venv 装 MCP 层；arcpy 由 `ARCPY_PYTHON_PATH` 指向**原生**
  `C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe`
  → **不克隆 conda 环境、不污染 arcgispro-py3**
- **评测方法论**：沿用《测绘管理法规 RAG》项目那一套 ——
  金标准 + 逐字证据约束 + bootstrap 95% CI + 配对检验 + 失败归因

## 关键纪律（勿犯）

1. **禁止** pip 直装进 `arcgispro-py3`（只读环境，装坏可能需重装 ArcGIS Pro）
2. 评测必须 **`ARCGIS_MCP_MAX_WORKERS=1`**（单机许可不可并行，并发会导致结果不可复现）
3. 不要改成线程池（GP 工具非线程安全；基座「每作业一子进程」是正确设计）
4. **坐标系统一**：距离计算必须投影，否则单位是"度"；`DefineProjection` ≠ `Project`
5. `scratch.gdb` 必须已存在（基座 0.6.0 起 fail-fast 校验）
6. 所有数值必须来自工具返回，**不许模型自己编**

## 交付物

- `RESUME_DRAFT.md` —— 简历文案（以终为始，关键数字待实测填入）
- `eval/tasks/` —— 金标准任务集
- `eval/metrics/attribution.py` —— F1–F8 失败归因（差异化核心）
- `reports/` —— 带置信区间与指纹冻结的评测报告
- `docs/` —— 方法论、失败归因定义、Pro 3.6 兼容性验证记录

## 定位说明（重要）

**本项目不是给终端用户的产品，是技术验证。** 它回答一个具体问题：

> 让 LLM 编排 GIS 工具链，它在哪一步会失败、为什么失败、怎么改。

明确**不做**：地图渲染、生产级部署、遥感影像识别、通用 GIS 助手。
