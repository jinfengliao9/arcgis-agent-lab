"""Metrics and attribution tests.

Everything here runs against **constructed** trajectories. That is not a
shortcut around testing: the recorded runs available today are reference
trajectories (all correct), so no real failures exist to classify yet. Testing
the classifier against constructed cases is also the only way to separate "the
classifier is wrong" from "the model made a mistake".

Two assertions carry most of the weight:

* ``test_reference_trajectory_is_clean`` -- the reference behaviour must score
  full marks. If the scripted agent (which is correct by construction) fails the
  metric, the metric is wrong, not the agent.
* ``test_environment_noise_leaves_denominator`` -- ``F6`` must not merely be
  labelled, it must leave the denominator. Otherwise infrastructure trouble
  silently becomes a model failure.
"""

from __future__ import annotations

import pytest

from arcgis_agent_lab.harness.recorder import HARNESS_REFUSAL_KIND, ToolCallEvent
from arcgis_agent_lab.harness.toolset import BRIDGE_TOOL_UNIVERSE
from arcgis_agent_lab.metrics import (
    FailureClass,
    TaskObservation,
    attribute_task,
    score_trajectory,
    summarize,
)

KNOWN = BRIDGE_TOOL_UNIVERSE


def ev(
    tool: str,
    *,
    ok: bool = True,
    kind: str | None = None,
    msg: str | None = None,
    turn: int = 1,
) -> ToolCallEvent:
    """Build a recorded call event."""
    return ToolCallEvent(
        run_id="test-run",
        task_id="T",
        turn=turn,
        tool_name=tool,
        arguments={},
        ok=ok,
        error_kind=kind,
        error_message=msg,
        duration_ms=1.0,
        timestamp="2026-01-01T00:00:00+00:00",
    )


def obs(
    expected: tuple[str, ...],
    events: tuple[ToolCallEvent, ...],
    *,
    answer_correct: bool | None = None,
) -> TaskObservation:
    return TaskObservation(
        task_id="T",
        expected_tools=expected,
        events=events,
        known_tools=KNOWN,
        answer_correct=answer_correct,
    )


# --------------------------------------------------------------------------- #
# Trajectory metrics
# --------------------------------------------------------------------------- #


class TestTrajectoryMetrics:
    def test_exact_match_scores_all_three(self) -> None:
        score = score_trajectory("T", ["a", "b"], ["a", "b"])
        assert score.tao and score.tio and score.tem
        assert score.step_ratio == 1.0
        assert not score.missing and not score.extra

    def test_right_tools_wrong_order_is_tao_only(self) -> None:
        score = score_trajectory("T", ["a", "b"], ["b", "a"])
        assert score.tao, "collection is covered"
        assert not score.tio, "relative order is not preserved"
        assert not score.tem

    def test_missing_tool_fails_tao(self) -> None:
        score = score_trajectory("T", ["a", "b"], ["a"])
        assert not score.tao
        assert score.missing == ("b",)

    def test_repeated_calls_are_extra_and_raise_step_ratio(self) -> None:
        score = score_trajectory("T", ["a"], ["a", "a", "x"])
        assert score.tao, "the expected tool did appear"
        assert not score.tem
        assert score.extra == ("x",)
        assert score.step_ratio == 3.0

    def test_empty_reference_requires_empty_actual(self) -> None:
        assert score_trajectory("T", [], []).tem
        wandering = score_trajectory("T", [], ["a"])
        assert not wandering.tem
        assert wandering.step_ratio == float("inf")


# --------------------------------------------------------------------------- #
# Attribution -- one constructed case per class
# --------------------------------------------------------------------------- #


class TestAttributionClasses:
    def test_reference_trajectory_is_clean(self) -> None:
        """The whole point: correct-by-construction behaviour must pass."""
        result = attribute_task(
            obs(("select_by_attribute", "intersect_features"),
                (ev("select_by_attribute"), ev("intersect_features", turn=2)),
                answer_correct=True)
        )
        assert result.passed, result.detail

    def test_f1_missing_required_tool(self) -> None:
        result = attribute_task(
            obs(("select_by_attribute", "intersect_features"), (ev("select_by_attribute"),))
        )
        assert result.failure is FailureClass.F1_TOOL_SELECTION
        assert "intersect_features" in result.detail
        assert not result.excluded_from_score

    def test_f1_withheld_tool_is_surface_probing_not_hallucination(self) -> None:
        """A real tool this evaluation hides must not be scored as invented."""
        result = attribute_task(
            obs(("a",), (ev("execute_spatial_tool", ok=False, kind=HARNESS_REFUSAL_KIND),))
        )
        assert result.failure is FailureClass.F1_TOOL_SELECTION
        assert result.failure is not FailureClass.F8_HALLUCINATED_TOOL

    def test_f2_right_tools_wrong_order(self) -> None:
        result = attribute_task(
            obs(("intersect_features", "statistics_analysis"),
                (ev("statistics_analysis"), ev("intersect_features", turn=2)))
        )
        assert result.failure is FailureClass.F2_TOOL_ORDER
        assert "顺序" in result.detail

    def test_f3_right_sequence_wrong_answer(self) -> None:
        result = attribute_task(
            obs(("near_analysis",), (ev("near_analysis"),), answer_correct=False)
        )
        assert result.failure is FailureClass.F3_PARAMETER_OR_CRS

    def test_f4_contract_violation(self) -> None:
        result = attribute_task(
            obs(("delete_dataset",),
                (ev("delete_dataset", ok=False, kind="validation", msg="confirm required"),))
        )
        assert result.failure is FailureClass.F4_CONTRACT_VIOLATION

    def test_f5_sandbox_escape(self) -> None:
        result = attribute_task(
            obs(("get_feature_count",),
                (ev("get_feature_count", ok=False, kind="security", msg="outside root"),))
        )
        assert result.failure is FailureClass.F5_SANDBOX_ESCAPE

    def test_f6_license_is_environment_noise(self) -> None:
        result = attribute_task(
            obs(("a",), (ev("a", ok=False, kind="license", msg="no seat"),))
        )
        assert result.failure is FailureClass.F6_ENVIRONMENT_NOISE
        assert result.excluded_from_score is True

    def test_geoprocessing_with_a_parameter_code_is_a_model_fault(self) -> None:
        """Regression guard: 58 real failures were mis-charged to the runtime.

        The bridge collapses every ``arcpy.ExecuteError`` into one worker kind,
        so the ArcPy code embedded in the message is the only thing separating
        "the model passed a bad parameter" from "the engine broke". Before this
        table existed, every one of them was excluded from the denominator.
        """
        result = attribute_task(
            obs(
                ("calculate_geometry",),
                (
                    ev(
                        "calculate_geometry",
                        ok=False,
                        kind="geoprocessing",
                        msg="ERROR 000800: 该值不是 AREA_GEODESIC | PERIMETER_LENGTH_GEODESIC",
                    ),
                ),
            )
        )
        assert result.failure is FailureClass.F3_PARAMETER_OR_CRS
        assert result.excluded_from_score is False

    def test_output_already_exists_is_a_contract_violation(self) -> None:
        result = attribute_task(
            obs(
                ("select_by_attribute",),
                (
                    ev(
                        "select_by_attribute",
                        ok=False,
                        kind="geoprocessing",
                        msg="ERROR 000725: 输出要素类: 数据集 ... 已存在。",
                    ),
                ),
            )
        )
        assert result.failure is FailureClass.F4_CONTRACT_VIOLATION

    def test_missing_input_dataset_is_state_propagation(self) -> None:
        """F7's first real trigger: an artefact an earlier step never created."""
        result = attribute_task(
            obs(
                ("select_by_attribute",),
                (
                    ev(
                        "select_by_attribute",
                        ok=False,
                        kind="geoprocessing",
                        msg="ERROR 000732: 输入要素: 数据集 ... 不存在或不受支持",
                    ),
                ),
            )
        )
        assert result.failure is FailureClass.F7_STATE_PROPAGATION
        assert result.excluded_from_score is False

    def test_cannot_create_output_is_a_parameter_fault(self) -> None:
        result = attribute_task(
            obs(
                ("copy_features",),
                (ev("copy_features", ok=False, kind="geoprocessing",
                    msg="ERROR 000210: 无法创建输出 ..."),),
            )
        )
        assert result.failure is FailureClass.F3_PARAMETER_OR_CRS

    def test_unrecognised_geoprocessing_stays_environment_noise(self) -> None:
        """An unexplained engine error must not be charged to the model."""
        result = attribute_task(
            obs(
                ("some_tool",),
                (ev("some_tool", ok=False, kind="geoprocessing",
                    msg="ERROR 999999: 异常错误导致工具失败"),),
            )
        )
        assert result.failure is FailureClass.F6_ENVIRONMENT_NOISE
        assert result.excluded_from_score is True

    def test_f8_hallucinated_tool(self) -> None:
        result = attribute_task(obs(("a",), (ev("buffer_features"),)))
        assert result.failure is FailureClass.F8_HALLUCINATED_TOOL
        assert not result.excluded_from_score

    def test_environment_noise_outranks_everything_else(self) -> None:
        """F6 is checked first: a runtime failure explains the rest of the mess."""
        result = attribute_task(
            obs(
                ("a", "b"),
                (
                    ev("a", ok=False, kind="license", msg="no seat"),
                    ev("nonsense_tool", turn=2),  # would otherwise be F8
                ),
            )
        )
        assert result.failure is FailureClass.F6_ENVIRONMENT_NOISE

    def test_expected_refusal_is_a_pass_not_a_failure(self) -> None:
        """A task whose correct outcome is a refusal must not be scored as F5.

        Regression guard: before this rule existed, deliberately triggering a
        PathGuard rejection (the correct behaviour for an out-of-bounds request)
        was attributed as a sandbox-escape failure, and the reference agent's own
        run reported a 20% failure rate against itself.

        ``answer_correct=True`` is required since round 4: producing the refusal
        is not enough on its own, the answer must also be verified as
        non-contradictory.
        """
        observation = TaskObservation(
            task_id="T",
            expected_tools=(),
            events=(ev("get_feature_count", ok=False, kind="security", msg="outside root"),),
            known_tools=KNOWN,
            expected_error_kind="security",
            answer_correct=True,
        )
        result = attribute_task(observation)
        assert result.passed, result.detail
        assert result.failure is not FailureClass.F5_SANDBOX_ESCAPE
        assert "按预期被拒" in result.detail

    def test_unverified_answer_after_expected_refusal_needs_review(self) -> None:
        """Round 4, P0: the refusal branch accepted a None (unparseable) answer.

        A model that triggers the refusal and then says something evasive
        ("操作结束") had been scored as demonstrating boundary awareness.
        """
        observation = TaskObservation(
            task_id="T",
            expected_tools=(),
            events=(ev("get_feature_count", ok=False, kind="security"),),
            known_tools=KNOWN,
            expected_error_kind="security",
            answer_correct=None,
        )
        result = attribute_task(observation)
        assert result.needs_review
        assert not result.passed

    def test_missing_expected_refusal_is_a_failure(self) -> None:
        """Not being refused when refusal is required is itself the failure."""
        observation = TaskObservation(
            task_id="T",
            expected_tools=(),
            events=(ev("get_feature_count", ok=True),),
            known_tools=KNOWN,
            expected_error_kind="security",
        )
        result = attribute_task(observation)
        assert result.failure is FailureClass.F1_TOOL_SELECTION
        assert "预期收到 security 拒绝" in result.detail

    def test_environment_noise_still_outranks_expected_refusal(self) -> None:
        """A real runtime failure must stay visible even on a refusal task."""
        observation = TaskObservation(
            task_id="T",
            expected_tools=(),
            events=(ev("get_feature_count", ok=False, kind="geoprocessing", msg="engine"),),
            known_tools=KNOWN,
            expected_error_kind="security",
        )
        result = attribute_task(observation)
        assert result.failure is FailureClass.F6_ENVIRONMENT_NOISE
        assert result.excluded_from_score is True

    def test_f7_now_has_a_trigger(self) -> None:
        """F7 was declared with no rule until a real run supplied one.

        ``ERROR 000732`` (input dataset not found) is the signature of an
        artefact an earlier step failed to create -- mid-chain state
        propagation, not a bad parameter. Folding it into F6 would have
        overstated how much of the failure budget is "not the model's fault".
        """
        result = attribute_task(
            obs(
                ("select_by_attribute",),
                (ev("select_by_attribute", ok=False, kind="geoprocessing",
                    msg="ERROR 000732: 数据集 ... 不存在或不受支持"),),
            )
        )
        assert result.failure is FailureClass.F7_STATE_PROPAGATION

    def test_every_class_has_a_remediation(self) -> None:
        from arcgis_agent_lab.metrics import REMEDIATION

        missing = [c for c in FailureClass if c not in REMEDIATION]
        assert not missing, f"classes without a named fix: {missing}"


# --------------------------------------------------------------------------- #
# Summary aggregation
# --------------------------------------------------------------------------- #


class TestSummary:
    def test_environment_noise_leaves_denominator(self) -> None:
        """The F6 design decision, asserted rather than described."""
        attributions = [
            attribute_task(
                obs(("get_feature_count",), (ev("get_feature_count"),), answer_correct=True)
            ),
            attribute_task(
                obs(("get_feature_count",), (ev("get_feature_count", ok=False, kind="license"),))
            ),
        ]
        summary = summarize(attributions)
        assert summary.total == 2
        assert summary.excluded == 1
        assert summary.scored_total == 1, "environment noise must not be scored"
        assert summary.passed == 1
        assert summary.failure_rate == 0.0
        assert "F6_environment_noise" not in summary.distribution

    def test_distribution_carries_remediations(self) -> None:
        attributions = [
            # F1: a required tool never got called.
            attribute_task(
                obs(("select_by_attribute", "intersect_features"), (ev("select_by_attribute"),))
            ),
            # F3: sequence matches the reference, the answer does not.
            attribute_task(
                obs(("near_analysis",), (ev("near_analysis"),), answer_correct=False)
            ),
        ]
        summary = summarize(attributions)
        assert summary.distribution == {"F1_tool_selection": 1, "F3_parameter_or_crs": 1}
        assert set(summary.remediations) == set(summary.distribution)
        payload = summary.to_dict()
        assert payload["scored_total"] == 2
        assert payload["failure_rate"] == 1.0

    def test_invented_tool_names_are_hallucinations_not_classified_away(self) -> None:
        """A guard against a subtle test bug: placeholder names are NOT tools.

        An earlier version of these tests used names like ``"a"``, which the
        classifier correctly flagged as F8. Using real tool names in the
        aggregation tests is what makes them test aggregation rather than the
        hallucination rule.
        """
        assert "a" not in KNOWN
        assert attribute_task(obs(("a",), (ev("a"),))).failure is (
            FailureClass.F8_HALLUCINATED_TOOL
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
