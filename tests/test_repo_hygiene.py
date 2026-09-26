"""Repository hygiene tests.

Two checks that would have caught defects that shipped:

1. **U+FFFD replacement characters.** A file containing ``\ufffd`` is not
   corrupt in the sense a byte check detects -- it is *valid UTF-8*. It means
   characters were destroyed earlier and replaced by the substitution character.
   The task set shipped with 150 of them across 22 of 29 task prompts, and the
   generator that produced it was damaged the same way, so regenerating did not
   help.

   Root cause, recorded because it is easy to repeat: an earlier fix read the
   file with ``errors="replace"`` and wrote it back. That converted the illegal
   byte sequence ``EF BF 3F`` into the *legal* sequence ``EF BF BD 3F`` --
   destroying recoverable information and making the damage invisible to a
   byte-level encoding check. Damage that cannot announce itself.

2. **Contradictory numbers in the README.** The same file claimed 39, 60 and 94
   tests at different points, and 25 vs 29 tasks. Numbers drift; a test that
   checks them against reality does not.

Both are cheap to run and neither needs ArcGIS.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: ``runs/`` holds run artefacts (trajectories, snapshots) and is gitignored --
#: it never enters the repository, so its contents are not a publication
#: concern. Historical snapshots legitimately contain an old copy of the task
#: definitions from before the corruption was repaired.
SKIP_PARTS = {
    ".venv", "__pycache__", ".git", "upstream", ".tmp", ".pytest-tmp",
    "node_modules", "runs", "reports",
}
SCANNED_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".toml", ".txt", ".yml", ".yaml"}


def _repo_text_files() -> list[Path]:
    out = []
    me = Path(__file__).resolve()
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in SCANNED_SUFFIXES:
            continue
        if SKIP_PARTS & set(p.parts):
            continue
        # The review bundle is a generated concatenation of files already
        # checked individually; excluding it avoids double-reporting.
        if p.name == "codex-review-bundle.md":
            continue
        # This file necessarily contains the offending character as a literal.
        if p.resolve() == me:
            continue
        out.append(p)
    return out


class TestNoReplacementCharacters:
    def test_no_ufffd_anywhere(self) -> None:
        """U+FFFD is a tombstone, not a character. There should be none."""
        offenders = {}
        for p in _repo_text_files():
            text = p.read_text(encoding="utf-8", errors="replace")
            if "\ufffd" in text:
                offenders[str(p.relative_to(REPO_ROOT))] = text.count("\ufffd")
        assert not offenders, (
            "以下文件含 U+FFFD 替换字符（说明有字符被销毁，且因为是合法 UTF-8，"
            "按字节做的编码体检查不出来）：\n"
            + "\n".join(f"  {f:<58}{n} 处" for f, n in sorted(offenders.items()))
            + "\n\n修法：定位损坏点，按上下文恢复原字符。**不要**用 errors='replace' "
            "读回写 —— 那会把可恢复的非法字节固化成合法替换字符。"
        )

    def test_task_prompts_are_readable(self) -> None:
        """Every task prompt should look like a sentence a person wrote."""
        tasks_path = REPO_ROOT / "src" / "arcgis_agent_lab" / "tasks" / "tasks.jsonl"
        if not tasks_path.is_file():
            pytest.skip("task set not built")
        for line in tasks_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            task = json.loads(line)
            q = task["question"]
            assert "\ufffd" not in q, f"{task['id']} 题面含替换字符"
            assert len(q) >= 8, f"{task['id']} 题面过短: {q!r}"
            assert q.rstrip()[-1] in "。？：", f"{task['id']} 题面没有正常结尾: {q!r}"


def _actual_test_count() -> int | None:
    """Count collected tests by listing them.

    Uses ``sys.executable``, not a bare ``python``: the bare name resolves to
    whatever is first on PATH (here, a system interpreter without pytest), and
    the resulting failure is easy to misread as "few tests exist".

    Parsing the "N tests collected" summary is brittle across pytest versions
    and -q levels; counting the node-id lines is not.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    stdout = proc.stdout or ""
    node_ids = [ln for ln in stdout.splitlines() if "::" in ln and ln.strip()]
    if node_ids:
        return len(node_ids)
    found = re.search(r"(\d+)\s+tests?\s+collected", stdout + (proc.stderr or ""))
    return int(found.group(1)) if found else None


class TestReadmeNumbersMatchReality:
    """The README quoted three different test counts at one point."""

    def _readme(self) -> str:
        return (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_test_count_matches_pytest(self) -> None:
        text = self._readme()
        claims = {int(m) for m in re.findall(r"\*\*(\d+)\s*passed", text)}
        if not claims:
            pytest.skip("README 未给出测试数")
        actual = _actual_test_count()
        if actual is None:
            pytest.skip("无法取得实际测试数")
        assert claims == {actual}, (
            f"README 声称的测试数 {sorted(claims)} 与实际 {actual} 不一致"
            " —— 同一份 README 里出现多个数字会直接损害可信度"
        )

    def test_task_count_matches_task_file(self) -> None:
        tasks_path = REPO_ROOT / "src" / "arcgis_agent_lab" / "tasks" / "tasks.jsonl"
        if not tasks_path.is_file():
            pytest.skip("task set not built")
        actual = sum(1 for line in tasks_path.read_text(encoding="utf-8").splitlines() if line.strip())
        text = self._readme()
        claims = {int(m) for m in re.findall(r"\*\*(\d+)\s*题\*\*", text)}
        if not claims:
            pytest.skip("README 未给出题数")
        assert claims == {actual}, f"README 声称的题数 {sorted(claims)} 与实际 {actual} 不一致"

    def test_no_placeholder_paths(self) -> None:
        text = self._readme()
        for bad in ("<you>", "<your-name>", "path/to/python"):
            assert bad not in text, f"README 里还有占位符 {bad!r} —— 读者会照抄失败"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
