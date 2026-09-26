# 基座拆解：arcgis-mcp-bridge 是怎么做的

> 这份文档的目的：**让你在被追问到任何一层时都答得上来。**
>
> 它拆的是基座（`muend/arcgis-mcp-bridge` v0.6.6，Apache-2.0）。关于归属怎么表述，
> 先看第零节 —— 那里的建议不是道德说教，是**对你最有利的策略判断**。

---

## 零、先把归属问题说清楚（先读这节）

### 事实

- 基座是**开源项目**（Apache-2.0），作者 `muend`，103 个工具、86 个测试、已上 PyPI 和 MCP Registry。
- 我们的项目是**在它之上做增量**：热池后端、编排层、评测层。

### 推荐的口径（诚实，而且比"我全做的"更强）

> "基座是一个开源项目（Apache-2.0，作者 muend），把 ArcGIS 的 100 个地理处理工具封装成了 MCP 服务。
> 我在它上面做三件事：**发现并解决了它的性能瓶颈**（它自己在注释里预留了热池后端但没实现，我实现并量化了）、
> **搭了编排层**、**做了一套它没有的评测**。"

### 为什么这个口径更强（这点很重要）

| 说法 | 面试官的反应 |
|---|---|
| "我从零实现了 100 个 arcpy 工具的 MCP 封装" | 应届生做这个量级？可疑。而且一旦追问"你怎么处理 GDB 内部路径的边界检查"，答不上就崩 |
| "我调研了 30+ 个同类项目，选了最合适的一个基座，找到它预留但没实现的瓶颈并解决" | **判断力 + 工程能力**的组合。这是资深工程师的思维方式 |

**关键认知：简历上真正稀缺的不是"我会写代码"，而是"我知道该复用而不是重造轮子，并且能找到真正值得改的地方"。**
一个应届生说出后者，比说出前者可信得多，也值钱得多。

### 底线（不主动说，但被问必须承认）

- 被问"这 100 个工具是你写的吗？" → **如实说不是**，说清你做的是哪一部分。
- 不要声称基座的架构（双进程、PathGuard、注册表）是你设计的。
- **可以说的是**：你能把它们的**设计理由**讲清楚——这本身就是掌握，而不是背稿。

> 一个技巧：**主动说边界反而加分**。你可以主动讲："工具封装是基座的，我的增量在编排和评测。"
> 面试官会记住你"分得清自己做了什么"——这是很多候选人做不到的。

---

## 一、整体架构：两个进程，一条管道

```
┌─────────────────────────────┐         ┌──────────────────────────────┐
│  Layer A  ·  server.py      │         │  Layer B  ·  worker.py       │
│  （MCP stdio 端点）          │         │  （唯一 import arcpy 的地方）  │
│                             │         │                              │
│  · FastMCP，走 stdio JSON-RPC│  NDJSON │  · 读一行 WorkerJob          │
│  · 注册 103 个工具            │ ──────► │  · 执行 arcpy 地理处理        │
│  · 校验输入、做路径预检       │ ◄────── │  · 写一行 WorkerResult        │
│  · 绝不 import arcpy         │  管道    │  · 绝不碰 stdout 之外的东西    │
└─────────────────────────────┘         └──────────────────────────────┘
```

**核心设计**：**Layer A 永远不 import arcpy。**

**为什么这么设计（三个理由，都能被追问）**：

1. **可测试性** —— server 可以在没装 ArcGIS 的机器上 import、跑单测。
   上游的类注释原话：*"unit-test suites and static tooling can import it on machines with no ArcGIS installation"*。
2. **崩溃隔离** —— arcpy 是闭源 C++/COM 封装，可能原生崩溃（native crash）。
   崩在子进程里，JSON-RPC 通道还活着，父进程把崩溃转成一条结构化错误帧。
3. **许可与依赖** —— arcpy 只能在装了 ArcGIS Pro 的 Windows 上跑（几百 MB 原生依赖 + 许可检出）。
   把它隔离在一个可替换的子进程里，服务端才有部署自由度。

**数据流**（以一次工具调用为例）：

```
MCP host 提问
   │
   ▼
Layer A: 校验输入（Pydantic）→ 路径预检（PathGuard）→ 组装 WorkerJob
   │  NDJSON 一行写进 worker 的 stdin
   ▼
Layer B: 重新校验 → 再跑一次 PathGuard → 调 arcpy → 组装 WorkerResult
   │  NDJSON 一行写回 stdout
   ▼
Layer A: 解帧 → 成功则返回结果，失败则抛出带 GP 消息栈的可读错误
```

---

## 二、五个核心机制（每一条都可能被追问）

### 2.1 声明式工具注册表 —— 100 个工具怎么不写 100 个 wrapper

**问题**：100 个工具如果每个都写一个 MCP endpoint + 一个 worker handler，就是 200 个函数。

**上游的解法**：每个工具只声明一个 `ToolSpec`：

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: Category          # 10 个垂直领域之一
    description: str            # 给 Claude 看的工具说明
    input_model: type[ToolInput]  # Pydantic 输入模型
    worker_fn: WorkerFn         # (arcpy, 校验后的输入) -> dict
    destructive: bool = False   # True => 输入模型必须带 confirm 字段
```

然后：
- **server 侧**：一个通用的 `_make_catalog_proxy(spec)` 工厂，为每个 spec 生成 MCP endpoint。
  它把 `spec.input_model` 塞进函数的类型注解，**FastMCP 自动从 Pydantic 模型推导出 JSON Schema** ——
  也就是说，**schema 和校验来自同一个类，不可能不一致**。
- **worker 侧**：一个通用的 `_handle_run_tool` 分发器，按 `spec.input_model` 校验后调 `spec.worker_fn`。

**结果**：加一个新工具只需要动两个地方 —— 输入模型（`contracts/`）+ spec 和 worker 函数（`tools/`）。
**`server.py` 一行都不用改。**

> **被追问**："加一个工具要改几个文件？"
> 答：两个。registry 的注释原话就是 "Adding a tool therefore touches exactly two places ... Open/Closed at package scale"。

**另外两个细节（能主动说出来很加分）**：

- `register()` 里有一条**注册期校验**：如果 `destructive=True` 但输入模型没有 `confirm` 字段，
  **直接拒绝注册**（`raise ValueError`）。也就是说危险工具不可能忘记加确认字段——**这是把安全约束放进了注册机制，而不是靠人记得**。
- 可执行的地理处理工具是一个**闭集枚举** `SpatialToolName`（只有 Buffer/Clip），
  注释明确写着 *"there is no dynamic `getattr(arcpy, ...)` anywhere in the codebase"* ——
  **不允许动态反射调用 arcpy**，这是防注入的设计。

### 2.2 契约层：一份模型，两个执行点

`contracts/base.py` 是所有序列化形状的唯一来源，两个家族：

1. **工具契约**（Claude 通过 `tools/list` 看到的 schema）
2. **IPC 信封**（`WorkerJob` / `WorkerResult`，两个进程之间的 NDJSON 帧）

**三个设计细节**：

- 全部 Pydantic v2，`frozen=True`（值对象，校验后不可变）+ `extra="forbid"`（**未知字段直接报错，不是忽略**）。
  注释原话：*"unknown keys are a contract violation, not a shrug"*。
- `WorkerResult` 有一个跨字段校验器 `_ok_xor_error`：`ok=True` 不许带 error，`ok=False` 必须带 error。
  **把不变量写进模型，而不是靠调用方自律。**
- `WorkerError.kind` 是一个**闭集**，只有 5 个值：

| kind | 含义 | 谁来修 |
|---|---|---|
| `validation` | 载荷在 worker 侧没过 Pydantic 校验 | 服务端 / 调用方 |
| `security` | PathGuard 拒绝 | 配置或调用方 |
| `geoprocessing` | **arcpy.ExecuteError —— 工具跑了但失败了** | 业务/数据问题 |
| `license` | 许可检不出 | 环境问题 |
| `internal` | 其它；细节留在 stderr | 未知 |

> **这个分类是评测失败归因的天然基础** —— 我们的 F1–F8 分类直接复用了它作为判据之一。

### 2.3 PathGuard：威胁模型先写清楚

`security.py` 的模块 docstring 第一句就是威胁模型：

> *"Tool arguments arrive from an LLM host and may be influenced by prompt injection
> (e.g., a malicious string embedded in a document Claude read)."*

**它防的是什么**：LLM 传来的路径参数可能被提示注入污染。
一个精心构造的 `in_features` 如 `C:\data\..\..\Windows\System32` 或指向攻击者共享盘的 UNC 路径，
**绝不允许到达 arcpy**。

**具体规则**：

- **先解析再比对** —— 符号链接、`..`、相对段全部展开后才做边界检查，
  **绝不比较未解析的字符串**（注释强调）。
- 解析后的路径必须等于或位于某个 allowed root 之下。
- **支持 GDB 内部引用**（`...\data.gdb\roads`）：`data.gdb` 是真实目录，`roads` 是逻辑尾巴 ——
  前半段按文件系统解析做边界检查，尾巴单独校验成"合法数据集名"（不能含分隔符或 `..`）。
- **写比读更严**：输出数据集名必须符合 ArcGIS 规则（字母数字下划线，不能数字开头）；覆盖已有数据必须显式 `overwrite=true`。
- 拒绝：UNC 路径、相对路径、NUL 字节、超长路径、Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）。

> **被追问**："路径穿越怎么防的？"
> 答："先 resolve 再比对，不比较未解析字符串；而且 GDB 内部的逻辑尾巴单独校验成合法数据集名 ——
> 因为 GDB 那个路径只有 arcpy 认识，文件系统解析不了，所以不能整条拿去做 resolve。"

**同一份 guard 代码在两个进程各跑一次**（Layer A 预检、Layer B 复验），DRY ——
注释原话：*"one implementation, two enforcement points"*。

### 2.4 执行后端：Protocol 而非继承

```python
@runtime_checkable
class ExecutionBackend(Protocol):
    async def run_job(self, job: WorkerJob, *, timeout_s: int) -> WorkerResult: ...
```

**为什么用 Protocol（结构化类型）而不是抽象基类**？上游注释给了答案：

> *"The server layer should depend on a capability ("something that can run a WorkerJob"),
> not on an inheritance tree (Dependency Inversion)."*

**而且它明确预留了我们的活**：

> *"A future `WarmPoolBackend` (persistent pre-imported arcpy worker) ... satisfies the Protocol
> simply by having the right method shape — no registration, no base-class import."*

**失败哲学**（很值得讲）：`SubprocessBackend` 的模块 docstring 说 ——

> *"This module NEVER lets a worker failure propagate as an unhandled exception into the stdio event loop.
> Every failure mode — timeout, crash, garbage output, non-zero exit — is converted into a structured
> `WorkerResult` with `ok=False`."*

即：**任何 worker 侧的问题都不许以异常形式逃逸**。JSON-RPC 通道无论如何都活着。

### 2.5 免许可测试：用 MagicMock 顶掉 arcpy

`tests/conftest.py` 只有一个 fixture，但它是整个工程化水平的关键：

```python
@pytest.fixture(scope="session", autouse=True)
def mock_arcpy_environment() -> None:
    mock_arcpy = MagicMock()
    mock_arcpy.__name__ = "arcpy"
    mock_sa = MagicMock()
    mock_arcpy.sa = mock_sa
    mock_arcpy.CheckExtension.return_value = "Available"
    sys.modules["arcpy"] = mock_arcpy          # ← 直接注入 sys.modules
    sys.modules["arcpy.sa"] = mock_sa
    sys.modules["sa"] = mock_sa
```

**效果**：86 个测试在 4.6 秒内跑完，**不需要 ArcGIS、不需要许可**。

**这也是我们能做"免许可评测"的技术前提** —— 阶段 6 的 `replay` 模式直接照抄这个模式。

> （有意思的细节：这个文件的注释是**土耳其语**写的，说明基座作者非英语母语。）

---

## 三、被追问时的答法预演

| 可能的问题 | 答法要点 | 出处（你可以说"在 `xxx.py` 里"） |
|---|---|---|
| 为什么分两个进程？ | ① 可测试（没 ArcGIS 也能 import）② arcpy 可能原生崩溃，隔离在子进程 ③ 许可与依赖隔离 | `server.py` 模块 docstring |
| server 会 import arcpy 吗？ | **绝不会**，这是 Layer B 的专属。基座甚至有"注册表守卫"测试在守着这条 | `server.py` §"What this module must NEVER do" |
| 100 个工具怎么管理的？ | 声明式 `ToolSpec` + 一个通用代理工厂 + 一个通用分发器。加工具只动两处 | `registry.py` docstring |
| schema 和校验会不会不一致？ | 不会 —— 代理函数的类型注解就是那个 Pydantic 模型，schema 由它推导 | `_make_catalog_proxy()` |
| 危险操作怎么防误用？ | `destructive=True` 的工具必须带 `confirm` 字段，否则**注册期就报错**；不安全工具根本注册不进来 | `registry.register()` |
| 路径穿越怎么防？ | 先 resolve 再比对；GDB 逻辑尾巴单独校验；拒绝 UNC/相对/NUL/保留设备名 | `security.py` `_resolve()` |
| 为什么用 Protocol 不用 ABC？ | 依赖"能力"而非"继承树"（依赖倒置）；测试和未来的热池后端都能直接满足结构 | `execution.py` 类 docstring |
| worker 崩了会怎样？ | 崩不影响 JSON-RPC 通道。非零退出码被转成结构化 `internal` 错误帧 | `execution.py` `_run_one()` |
| 怎么在没 ArcGIS 的机器上跑测试？ | session 级 fixture 往 `sys.modules` 注入 `MagicMock` 顶掉 arcpy | `tests/conftest.py` |
| 错误分几类？ | 5 类：validation / security / geoprocessing / license / internal | `contracts/base.py` `WorkerError` |
| 超时语义是什么？ | 超时**只算地理处理时间**，排队等信号量的时间不计入 | `execution.py` `run_job()` docstring |

---

## 四、我们的增量（这部分是你的）

### 4.1 WarmPoolBackend —— 补上基座预留的那块

**问题（实测，不是推测）**：基座默认每次调用起一个新 worker，因此每次都要付 `import arcpy` 的税。
上游文档写的是 10–30 秒；**我们环境上实测最坏 239 秒**，导致 8 个冒烟 case 里有 2 个直接撞上客户端的 240 秒超时。

**做法**：
- `warm_worker.py` —— **复用上游的 `process_frame`（它已经被拆成纯函数）**，只把"读一帧就退出"改成
  **循环读帧**，并在循环前主动预热 arcpy 一次。**不改任何语义**，错误分类映射原封不动。
- `warm_pool.py` —— 实现上游预留的 `ExecutionBackend` Protocol。

**实测效果**：

| | 每次调用的 arcpy 启动开销 |
|---|---|
| `SubprocessBackend`（上游默认） | 30 s ~ 239 s（**每次**） |
| `WarmPoolBackend`（本项目） | **一次性预热，之后 0.00 s** |

**而且我们明确保留了这个权衡**：热池会牺牲上游的 "zero state leakage between jobs" 保证
（`arcpy.env`、GP 历史、检出的扩展许可都会跨帧残留），所以这个后端是 **opt-in**、默认仍是
`SubprocessBackend`，并且把权衡写进了代码注释和文档。

> **面试时这一点比"我做了个优化"值钱**：它证明你知道自己在放弃什么。

**过程中真实踩到的坑（都能讲）**：
1. `asyncio.Lock` **不可重入** —— `aclose()` 持锁后又调了一个内部会再取同一把锁的方法，
   导致**永久死锁**：程序看起来跑完了但进程永不退出。这个 bug 只在"最后一行日志之后再无输出"里暴露出来。
2. 客户端的 `call_tool(timeout=240)` 是**硬编码**的，服务端把超时调到 900 秒也没用 ——
   所以**瓶颈不在配置，在架构**。这个认知是整个增量立项的依据。
3. 上游的 registry **必须先 import `arcgis_mcp.tools` 才会被填充**（各分类模块在 import 时注册）。

### 4.2 编排层与评测层

（待实现 —— 见 `PLAN.md` 阶段 2–5）

---

## 五、一句话总结这个基座的设计水平

**它值得学的地方不在"封装了 100 个工具"，而在四件事：**

1. **依赖倒置做到位** —— 执行后端是 Protocol，所以我们的热池能零改动接入
2. **契约单一来源** —— schema 和校验来自同一个 Pydantic 模型，不可能漂移
3. **安全约束机制化** —— 危险工具缺确认字段就注册不进来，靠机制而不是靠人记得
4. **失败边界彻底** —— 任何 worker 故障都转成结构化帧，协议通道永不受影响

**能把这四条讲清楚，比"我做过一个 MCP 项目"有说服力得多。**
