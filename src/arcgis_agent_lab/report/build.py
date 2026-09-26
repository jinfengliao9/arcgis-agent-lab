"""Build the evaluation report from a recorded run.

What a report must contain to be trustworthy
--------------------------------------------
1. **A frozen fingerprint** -- scenario SHA-256, bridge commit, tool-catalogue
   hash, interpreter and ArcGIS Pro versions, agent identity. Without it a
   number is unattributable: nobody can tell whether a difference came from the
   model or from the data underneath it. The bridge's own benchmark makes the
   same point in its README: *"Do not pool results across ArcGIS Pro, bridge,
   FastMCP, Python, or hardware versions."*
2. **Intervals, not point estimates** -- a score over 29 tasks has a resolution
   limit; quoting it bare invites over-reading.
3. **The failure distribution with remedies attached** -- this is the number
   that answers "what do we change next".
4. **Limitations, written by the author, before anyone asks.** A report that
   lists only strengths is marketing.

Rendering is deliberately separated from measurement: the report can be
regenerated from the trajectory and summary files alone, with no ArcGIS, no
licence and no model.
"""

from __future__ import annotations

import hashlib
import platform
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from ..harness.runner import SuiteResult
from ..harness.toolset import export_tool_schemas
from ..metrics.attribution import Attribution, AttributionSummary, summarize
from ..metrics.trajectory import TrajectoryScore, aggregate
from ..stats import bootstrap_ci


def tool_catalogue_fingerprint() -> tuple[int, str]:
    """Size and SHA-256 of the offered tool surface.

    Hashed over name + description, sorted: two runs whose surfaces differ only
    in tool *ordering* are the same surface, and should fingerprint the same.
    Description text is included because the model sees it -- rewording a tool
    description can change behaviour as much as renaming the tool.
    """
    payload = sorted(
        f"{entry['function']['name']}\t{entry['function']['description']}"
        for entry in export_tool_schemas()
    )
    digest = hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()
    return len(payload), digest


@dataclass(frozen=True)
class Fingerprint:
    """Everything needed to reproduce and attribute one run."""

    scenario_sha256: str
    bridge_commit: str
    bridge_version: str
    tool_catalogue_size: int
    tool_catalogue_sha256: str
    host_python: str
    arcgis_pro_version: str
    agent: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        rows = [
            ("场景指纹 (scenario sha256)", self.scenario_sha256 or "—"),
            ("基座 commit", self.bridge_commit or "—"),
            ("基座版本", self.bridge_version or "—"),
            ("被测工具面", f"{self.tool_catalogue_size} tools  sha256={self.tool_catalogue_sha256[:16]}…"),
            ("宿主 Python", self.host_python),
            ("ArcGIS Pro", self.arcgis_pro_version or "—"),
            ("agent", self.agent),
        ]
        lines = ["| 项 | 值 |", "|---|---|"]
        lines += [f"| {key} | `{value}` |" for key, value in rows]
        return "\n".join(lines)


def capture_fingerprint(
    *,
    suite: SuiteResult,
    bridge_commit: str = "",
    bridge_version: str = "",
    arcgis_pro_version: str = "",
) -> Fingerprint:
    """Assemble the fingerprint for a suite run."""
    size, digest = tool_catalogue_fingerprint()
    return Fingerprint(
        scenario_sha256=suite.scenario_fingerprint,
        bridge_commit=bridge_commit,
        bridge_version=bridge_version,
        tool_catalogue_size=size,
        tool_catalogue_sha256=digest,
        host_python=platform.python_version(),
        arcgis_pro_version=arcgis_pro_version,
        agent=suite.agent,
    )


@dataclass
class ReportInputs:
    """Everything the renderer needs -- no live system required."""

    suite: SuiteResult
    fingerprint: Fingerprint
    trajectory_scores: Sequence[TrajectoryScore] = field(default_factory=tuple)
    attributions: Sequence[Attribution] = field(default_factory=tuple)
    limitations: Sequence[str] = field(default_factory=tuple)
    notes: Sequence[str] = field(default_factory=tuple)


def _render_metrics(inputs: ReportInputs) -> str:
    scores = list(inputs.trajectory_scores)
    if not scores:
        return "_本次运行没有可评分的轨迹。_\n"

    suite_metrics = aggregate(scores)
    lines = [
        "| 指标 | 值 | 95% CI | 说明 |",
        "|---|---|---|---|",
    ]

    tao = [1.0 if s.tao else 0.0 for s in scores]
    tio = [1.0 if s.tio else 0.0 for s in scores]
    tem = [1.0 if s.tem else 0.0 for s in scores]

    for label, series, note in (
        ("TAO (工具集合覆盖)", tao, "该用的工具都出现了吗（不计顺序）"),
        ("TIO (相对顺序保持)", tio, "顺序对吗"),
        ("TEM (严格序列匹配)", tem, "完全一致吗（不多不少）"),
    ):
        interval = bootstrap_ci(series)
        lines.append(
            f"| {label} | {interval.point:.3f} | "
            f"[{interval.low:.3f}, {interval.high:.3f}] | {note} |"
        )

    lines.append(
        f"| 平均步数比 | {suite_metrics.mean_step_ratio:.3f} | — | "
        ">1.5 通常意味着绕路 |"
    )
    return "\n".join(lines) + "\n"


def _render_attribution(summary: AttributionSummary) -> str:
    if summary.total == 0:
        return "_没有归因结果。_\n"

    # Three states are rendered as three states. A task whose answer could not
    # be verified is not a failure, and the failure rate uses only verified
    # outcomes as its denominator; with nothing verified it is reported as
    # uncomputable rather than as 0 % or 100 % (Codex red-team round 3).
    if summary.failure_rate is None:
        rate_line = "- 失败率：**无法计算**（没有可核验的通过/失败样本）\n"
    else:
        rate_line = f"- 失败率：**{summary.failure_rate:.3f}**（分母 = 已核验 {summary.verified_total} 题）\n"

    head = (
        f"- 任务总数：**{summary.total}**\n"
        f"- 通过：**{summary.passed}**\n"
        f"- 待核验：**{summary.needs_review}**（答案无法自动判定；**不计入分子或分母**）\n"
        f"- 计入评分：**{summary.scored_total}**"
        f"（剔除环境噪声 {summary.excluded} 条）\n"
        f"{rate_line}"
    )
    if not summary.distribution:
        return head + "\n_没有失败样本。_\n"

    lines = [head, "| 失败类别 | 数量 | 该改什么 |", "|---|---|---|"]
    for code, count in sorted(summary.distribution.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{code}` | {count} | {summary.remediations.get(code, '—')} |")
    return "\n".join(lines) + "\n"


def render_report(inputs: ReportInputs) -> str:
    """Render the full Markdown report."""
    summary = summarize(list(inputs.attributions))
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    parts: list[str] = [
        "# ArcGIS 工具编排评测报告",
        "",
        f"> 生成时间：{generated}　·　agent：`{inputs.suite.agent}`"
        f"　·　run id：`{inputs.suite.run_id}`",
        "",
        "## 1. 环境指纹（结论只在指纹相同的前提下可比）",
        "",
        inputs.fingerprint.to_markdown(),
        "",
        "## 2. 运行摘要",
        "",
        f"- 任务数：{len(inputs.suite.results)}",
        f"- 已回答：{inputs.suite.answered}",
        f"- 工具调用总数：{inputs.suite.total_turns}",
        f"- 失败调用数：{inputs.suite.total_failures}",
        f"- 开始 / 结束：{inputs.suite.started_at} → {inputs.suite.finished_at}",
        "",
        "## 3. 序列指标（带 95% 置信区间）",
        "",
        _render_metrics(inputs),
        "## 4. 失败归因分布",
        "",
        _render_attribution(summary),
    ]

    if inputs.notes:
        parts += ["## 5. 备注", ""]
        parts += [f"- {note}" for note in inputs.notes]
        parts.append("")

    parts += ["## 6. 局限（作者主动声明）", ""]
    if inputs.limitations:
        parts += [f"- {item}" for item in inputs.limitations]
    else:
        parts.append("- _未声明局限。_")
    parts.append("")

    return "\n".join(parts)


def write_report(text: str, path: Path) -> Path:
    """Persist a rendered report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


__all__ = [
    "Fingerprint",
    "ReportInputs",
    "capture_fingerprint",
    "render_report",
    "tool_catalogue_fingerprint",
    "write_report",
]
