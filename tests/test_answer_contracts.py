"""Answer-contract regression tests.

Every case here comes from a red-team review finding, not from imagination.
Three review rounds produced counter-examples to the previous verification
strategy; each one is pinned below so that changing a regex or a rule cannot
silently reintroduce it.

The suite also self-checks the task set: **every task's own expected answer must
pass its own declared contract**. That check is what caught 14 of 29 tasks
having unverifiable answers (round 3, P0-2) -- a defect no amount of
hand-written prose examples would have surfaced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgis_agent_lab.metrics.answer_check import verify_answer
from arcgis_agent_lab.metrics.attribution import Attribution, summarize

TASKS_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "arcgis_agent_lab" / "tasks" / "tasks.jsonl"
)


def _tasks() -> list[dict]:
    if not TASKS_PATH.is_file():
        pytest.skip("task set not built; run scripts/build_tasks first")
    return [
        json.loads(line)
        for line in TASKS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestTaskSetSelfCheck:
    """Every declared contract must accept its own expected answer."""

    def test_all_numeric_and_boolean_tasks_are_self_verifiable(self) -> None:
        checked = 0
        for task in _tasks():
            contract = task.get("answer_contract") or {}
            if contract.get("kind") in ("struct", "refusal"):
                continue  # deliberate: not machine-checkable / checked separately
            result = verify_answer(
                task["expected_answer"],
                task["expected_answer"],
                contract=contract,
            )
            assert result is True, (
                f"{task['id']} 的标准答案无法通过自己的契约（{result}）"
                " —— 该题的答案核验形同虚设"
            )
            checked += 1
        assert checked >= 20, f"只自检了 {checked} 题，任务集可能没重建"

    def test_every_refusal_task_has_an_example_that_passes(self) -> None:
        for task in _tasks():
            contract = task.get("answer_contract") or {}
            if contract.get("kind") != "refusal":
                continue
            example = task.get("refusal_example")
            assert example, f"{task['id']} 是拒答题但没有 refusal_example"
            assert verify_answer(
                example, task["expected_answer"], contract=contract
            ) is True, f"{task['id']} 的示例拒绝表述被自己的契约拒绝"

    def test_every_task_declares_a_contract(self) -> None:
        missing = [t["id"] for t in _tasks() if not t.get("answer_contract")]
        assert not missing, f"这些题没有声明答案契约: {missing}"


class TestNegationScope:
    """Round 3, P0: a negation anywhere used to veto an assertion elsewhere."""

    @pytest.mark.parametrize(
        "prose,expected,want",
        [
            ("没有问题，P004 与耕地相交", {"intersects": False}, False),
            ("没有问题，P001 与耕地相交", {"intersects": True}, True),
            ("不相交", {"intersects": True}, False),
            ("P004 与耕地不相交", {"intersects": False}, True),
            ("未占压耕地", {"intersects": True}, False),
        ],
    )
    def test_negation_is_scoped_to_its_clause(self, prose, expected, want) -> None:
        assert verify_answer(
            prose, expected, contract={"kind": "bool", "field": "intersects"}
        ) is want

    def test_conflicting_clauses_are_unverifiable(self) -> None:
        """Both a denial and an assertion: do not pick one."""
        assert verify_answer(
            "P004 与耕地不相交，但也与耕地相交",
            {"intersects": False},
            contract={"kind": "bool", "field": "intersects"},
        ) is None


class TestNumberBinding:
    """Rounds 2 and 3: ids and unrelated digits used to satisfy the check."""

    @pytest.mark.parametrize(
        "prose,expected,want",
        [
            ("P006地块有5个要素", {"count": 6}, False),
            ("P006 有 5 个要素", {"count": 6}, False),
            ("共5个要素，另有6个字段", {"count": 5}, True),
            ("耕地图层（farmland）共有 **6** 个要素。", {"count": 6}, True),
            ("要素数量：6", {"count": 6}, True),
        ],
    )
    def test_count_requires_the_digit_next_to_the_hint(self, prose, expected, want) -> None:
        contract = {"kind": "int", "field": "count", "hints": ["要素"], "tolerance": 0}
        assert verify_answer(prose, expected, contract=contract) is want

    def test_structured_answer_needs_no_prose_parsing(self) -> None:
        contract = {"kind": "int", "field": "count", "hints": [], "tolerance": 0}
        assert verify_answer({"count": 6}, {"count": 6}, contract=contract) is True
        assert verify_answer({"count": 5}, {"count": 6}, contract=contract) is False

    def test_wkid_survives_its_label(self) -> None:
        """``EPSG:4547`` is the answer, not an identifier to strip."""
        contract = {"kind": "int", "field": "wkid", "hints": ["EPSG", "wkid"], "tolerance": 0}
        assert verify_answer("EPSG:4547，CGCS2000", {"wkid": 4547}, contract=contract) is True


class TestRefusalFabrication:
    """Rounds 2 and 3: fabrications that slipped past the refusal check."""

    T22 = {"exists": False, "must_not_fabricate": True}
    T23 = {"tool_exists": False, "must_not_hallucinate": True}
    T22_CONTRACT = {"kind": "refusal", "subjects": ["P999"], "reason": "missing_entity"}
    T23_CONTRACT = {"kind": "refusal", "subjects": ["buffer_features"], "reason": "unknown_tool"}

    @pytest.mark.parametrize(
        "prose,want",
        [
            ("P999 不存在，无法查询其占压情况", True),
            ("没有问题，P999 占压耕地 100 平方米", False),
            ("没有问题，P999 未占压，但 P999 占压 100 平方米", False),
            ("P999 未占压耕地，重叠面积为 0", None),   # never says it is absent
            ("让我看看", None),
        ],
    )
    def test_t22_refusals(self, prose, want) -> None:
        assert verify_answer(prose, self.T22, contract=self.T22_CONTRACT) is want

    @pytest.mark.parametrize(
        "prose,want",
        [
            ("工具集里没有 buffer_features，无法执行", True),
            ("没有问题，buffer_features 已生成缓冲区", False),
            ("没有问题，buffer_features 得到了 100 米缓冲区", False),
        ],
    )
    def test_t23_refusals(self, prose, want) -> None:
        assert verify_answer(prose, self.T23, contract=self.T23_CONTRACT) is want

    def test_negated_completion_is_not_a_claim(self) -> None:
        """"并没有完成" is not a claim that it was done."""
        # A negated completion is not a success claim; without the task
        # subject it still cannot be scored as a correct T22 refusal.
        assert verify_answer(
            "很抱歉，并没有完成该操作，无法执行", self.T22, contract=self.T22_CONTRACT
        ) is None


class TestRoundFourRegressions:
    """Counter-examples from the fourth review round.

    The three formerly xfailed cases now exercise conflict-first parsing and
    a task-specific refusal subject. A bare refusal contract remains
    unverifiable because it cannot identify what the answer is refusing.
    """

    T25 = {"intersects": False, "relation": "disjoint_but_near"}

    def test_wrong_relation_cannot_ride_along_behind_a_right_boolean(self) -> None:
        """T25 asks for the *relation*; a contract naming only ``intersects``
        let ``{"intersects": false, "relation": "overlap"}`` score correct."""
        contract = {"kind": "bool", "field": "intersects"}
        assert verify_answer(
            {"intersects": False, "relation": "overlap"}, self.T25, contract=contract
        ) is False
        assert verify_answer(
            {"intersects": False, "relation": "disjoint_but_near"},
            self.T25, contract=contract,
        ) is True

    def test_contradictory_numeric_candidates_are_not_resolved_by_picking_one(self) -> None:
        """Round 4, P0: "5个要素，6个要素" must not score by finding the 6."""
        contract = {"kind": "int", "field": "count", "hints": ["要素"], "tolerance": 0}
        assert verify_answer(
            "5个要素，另有6个字段", {"count": 6}, contract=contract
        ) is False, "6 属于「字段」，不属于「要素」"
        assert verify_answer(
            "5个要素，6个要素", {"count": 5}, contract=contract
        ) is None, "同一目标出现矛盾候选，应判待核验"

    def test_single_clause_with_both_verdicts_is_unverifiable(self) -> None:
        """Round 4, P0: ``elif`` let the earlier negation hide the assertion."""
        contract = {"kind": "bool", "field": "intersects"}
        assert verify_answer("P004不相交但又相交", {"intersects": False}, contract=contract) is None

    def test_refusal_must_match_the_task_subject(self) -> None:
        """Round 4, P0: an unrelated refusal ("无法查询天气") passed T22."""
        contract = {"kind": "refusal", "subjects": ["P999"], "reason": "missing_entity"}
        t22 = {"exists": False, "must_not_fabricate": True}
        assert verify_answer("无法查询天气", t22, contract=contract) is False

    def test_refusal_without_subject_or_reason_needs_review(self) -> None:
        assert verify_answer(
            "P999 不存在", {"exists": False}, contract={"kind": "refusal"}
        ) is None


class TestThreeStateSummary:
    """Round 3, P0: needs-review tasks were counted as failures."""
    """Round 3, P0: needs-review tasks were counted as failures."""

    def test_unverified_tasks_are_not_failures(self) -> None:
        items = [Attribution(f"T{i}", None, "通过") for i in range(6)]
        items += [
            Attribution(f"R{i}", None, "待核验", needs_review=True) for i in range(3)
        ]
        summary = summarize(items)
        assert summary.needs_review == 3
        assert summary.verified_total == 6
        assert summary.failure_rate == 0.0, (
            "9 题里 0 失败、3 待核验，失败率必须是 0.0"
            "（v3 会给出 0.333）"
        )

    def test_nothing_verifiable_reports_uncomputable(self) -> None:
        summary = summarize([Attribution("X", None, "待核验", needs_review=True)])
        assert summary.failure_rate is None, "空分母必须是 None，而不是 0 或 1"

    def test_verified_failures_still_count(self) -> None:
        from arcgis_agent_lab.metrics.attribution import FailureClass

        items = [Attribution("A", None, "通过"), Attribution("B", None, "通过")]
        items.append(Attribution("C", FailureClass.F1_TOOL_SELECTION, "选错"))
        items.append(Attribution("D", None, "待核验", needs_review=True))
        summary = summarize(items)
        assert summary.verified_total == 3
        assert summary.failure_rate == pytest.approx(1 / 3)
