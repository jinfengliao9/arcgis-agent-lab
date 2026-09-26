# 怎么用：把 ArcGIS 接到 AI 客户端上

## 这个项目是什么形态

**不是插件、不是网页、不是软件，而是两样东西：**

| 形态 | 是什么 | 入口 |
|---|---|---|
| **MCP 服务端** | 一个后台服务，向 AI 客户端提供 **103 个 ArcGIS 工具** | `python -m arcgis_mcp.server` |
| **评测工具** | 命令行脚本，跑基准、出报告 | `scripts/` 下的脚本 |

要做"实际场景使用"，用的是**第一种**：把它启动起来接到 AI 客户端上，
然后用自然语言让 AI 帮你做 GIS 分析。

---

## 已经配置好了

配置写在 `%USERPROFILE%\.workbuddy\mcp.json`：

```json
{
  "mcpServers": {
    "arcgis-pro": {
      "type": "stdio",
      "command": "E:/arcgis-agent-lab/.venv/Scripts/python.exe",
      "args": ["-m", "arcgis_mcp.server"],
      "env": {
        "ARCPY_PYTHON_PATH": "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe",
        "ARCGIS_MCP_ALLOWED_ROOTS": "E:\\arcgis-agent-lab\\bench",
        "ARCGIS_MCP_SCRATCH_GDB": "E:\\arcgis-agent-lab\\bench\\scratch.gdb",
        "ARCGIS_MCP_MAX_WORKERS": "1",
        "ARCGIS_MCP_TOOL_TIMEOUT": "900",
        "ARCGIS_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### 三个环境变量的含义

| 变量 | 作用 | 改动时机 |
|---|---|---|
| `ARCPY_PYTHON_PATH` | 有许可的 ArcPython 解释器（跑 arcpy 的那层） | 换机器时改 |
| `ARCGIS_MCP_ALLOWED_ROOTS` | **数据边界**：只有这个目录下的数据能读写，`;` 分隔多个 | **换成你自己的数据目录** |
| `ARCGIS_MCP_SCRATCH_GDB` | 默认输出位置（必须是**已存在**的 File GDB） | 同上 |

> **`ARCGIS_MCP_ALLOWED_ROOTS` 是硬边界。** 越界路径会被拒绝，不是警告 ——
> 这是有意的，它保证 AI 不会误删你其他位置的文件。

> **`ARCGIS_MCP_TOOL_TIMEOUT` 为什么要调到 900 秒**：本机冷启动 `import arcpy` 实测
> **223–239 秒**（上游文档假设的是 10–30 秒）。上游默认是 **600 秒**，其实已经够用 ——
> **调到 900 秒是留余量，不是必需。**
>
> **更正**：本文档早先版本写成「上游默认 180 秒会让第一次调用必然失败」，那是错的。
> 上游 `config.py` 的 `_DEFAULT_TOOL_TIMEOUT_S` 是 **600**（已核源码）。
> 记这一笔，是因为这个项目的主题就是「不报错的数字最危险」—— 文档里的数字同样要核。

---

## 激活步骤

配置写好后**不会自动生效**，需要手动信任一次：

1. 打开 WorkBuddy 的**连接器管理**页面
2. 右上角找到**自定义连接器**入口
3. 在列表里找到 `arcgis-pro`，点 **信任 / Trust**
4. 之后新开对话即可使用

---

## 怎么验证是否通了

在对话里直接说：

```
调用 health_check，看我这里 ArcGIS 桥通不通
```

`health_check` 是三个核心工具之一，它**不导入 arcpy**，所以毫秒级返回 ——
先用它确认 IPC 通路，再跑真正的空间分析。

通了之后，试一个只读工具：

```
列出 E:\arcgis-agent-lab\bench\scratch.gdb 里有哪些图层，各有多少个要素
```

---

## 可以试的任务（当前测试数据）

数据在 `E:\arcgis-agent-lab\bench\scratch.gdb`：

| 图层 | 内容 | 坐标系 |
|---|---|---|
| `parcels` | 30 宗地，字段 `parcel_id` / `district_id` / `area_m2` | EPSG:4547（米） |
| `farmland` | 6 块耕地 | EPSG:4547 |
| `buildings` | 40 个建筑 | EPSG:4547 |
| `roads` | 6 条道路 | EPSG:4547 |
| `facilities` | 10 个设施点 | EPSG:4547 |
| `parcels_geographic` | 同一批宗地，**经纬度坐标** | EPSG:4326（度） |

**建议按难度递增试：**

```
1. 耕地图层一共有多少个要素？

2. 宗地 P001 是否占压耕地？占压多少平方米？

3. 宗地 P002 是否占压耕地？占压多少平方米？
   （★ P002 与耕地是"相切"关系：intersects 为真但面积恰为 0。
     两个问题分开问，看它能不能分清"是否相交"和"压了多少"）

4. 哪些宗地与耕地相交？请列出宗地编号。

5. 按村组（district_id）统计宗地的数量和总面积。

6. parcels_geographic 用的是经纬度。请计算宗地 P001 与 P002 之间的距离，单位是米。
   （★ 直接在地理坐标系上算，返回的单位是"度"，比米小约 5 个数量级，且不报错）

7. 宗地图层现在实际使用的是什么坐标系？请查证后说明。
```

---

## 换成你自己的数据

**步骤**：

1. 把 `ARCGIS_MCP_ALLOWED_ROOTS` 改成你的数据目录，例如
   `"D:\\GIS\\项目A;E:\\arcgis-agent-lab\\bench"`（可多个，`;` 分隔）
2. `ARCGIS_MCP_SCRATCH_GDB` 指向你的项目里**已存在**的 File GDB
3. 重启 WorkBuddy，重新信任

**注意事项**：

- 路径分隔符在 JSON 里要写 `\\`（转义）
- GDB 必须**已经存在** —— 桥不会替你创建它
- 只读操作可以直接做；`delete_dataset` / `repair_geometry` / `near_analysis` /
  `define_projection` 这些**破坏性工具**必须显式传 `confirm=true`，否则会被拒绝。
  这是安全闸，不是 bug。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 首次调用卡约 4 分钟 | `import arcpy` 冷启动（本机实测 223–239 秒） | 正常，第二次起约 2 秒 |
| 所有 ArcGIS 工具都失败，报 `Worker process exited with code 1` | worker 解释器不对 | 检查 `ARCPY_PYTHON_PATH` 指向的 python 能否 `import arcpy` |
| 路径被拒：`Boundary rejection` | 数据不在 `ALLOWED_ROOTS` 内 | 改环境变量，或用允许范围内的路径 |
| `ERROR 000517: 没有为输入数据集定义坐标系` | 数据缺 CRS 声明 | 用 `define_projection` 补声明（**不是** `project_features`） |
| 导入 GeoJSON 后图层为空 | 用了 `.geojson` 扩展名 | 改名为 `.json` —— 见 issue #25 |
| 工具报 `数据集已存在` | 上次运行的残留 | 换个输出名，或先删除 |

---

## 一个重要的使用习惯

**让 AI 报出它调用的工具和参数，而不是只给结论。**

这个项目的核心发现之一是：**不报错的错误最危险**。坐标系搞错、距离单位是度、
几何相切但面积为零 —— 这些都不会抛异常，只会静静地给你一个错答案。

所以拿到结果时值得多问一句：

```
你用了哪些工具？关键参数是什么？这个距离是在什么坐标系下算的？
```

能答清楚这些的 AI 才是可信的；答不上来的，它的结论也就没有依据。
