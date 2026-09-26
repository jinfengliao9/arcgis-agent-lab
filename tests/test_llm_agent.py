"""Tests for the model-backed agent.

Scope is deliberately narrow: parsing and prompt construction. The agent's
*behaviour* is measured by the evaluation itself, not asserted here -- a unit
test cannot say whether a model picked the right tool, only that a malformed
response does not crash the harness and that the prompt says what it must.
"""

from __future__ import annotations

import json

import pytest

from arcgis_agent_lab.harness.llm_agent import (
    LAYER_CATALOGUE,
    _parse_arguments,
    build_system_prompt,
    outcome_as_tool_content,
)
from arcgis_agent_lab.harness.session import ToolOutcome


class TestArgumentParsing:
    def test_parses_a_json_string(self) -> None:
        assert _parse_arguments('{"wkid": 4547}') == {"wkid": 4547}

    def test_tolerates_malformed_json(self) -> None:
        """A model emitting broken JSON must cause a validation failure, not a crash.

        Returning an empty argument set lets the contract layer reject the call
        and record it -- which is exactly the F4 signal the benchmark wants.
        Raising here instead would abort the task and lose the evidence.
        """
        assert _parse_arguments('{"wkid":') == {}

    def test_tolerates_none_and_blank(self) -> None:
        assert _parse_arguments(None) == {}
        assert _parse_arguments("") == {}
        assert _parse_arguments("   ") == {}

    def test_passes_through_a_dict(self) -> None:
        assert _parse_arguments({"wkid": 4326}) == {"wkid": 4326}

    def test_rejects_non_object_json(self) -> None:
        assert _parse_arguments("[1, 2, 3]") == {}
        assert _parse_arguments('"just a string"') == {}


class TestToolContent:
    def test_success_is_machine_readable(self) -> None:
        payload = outcome_as_tool_content(ToolOutcome(True, {"count": 6}, None, None))
        assert json.loads(payload) == {"ok": True, "result": {"count": 6}}

    def test_failure_surfaces_the_error_kind(self) -> None:
        """The model must see *why* a call was refused, or it cannot adapt.

        Hiding the error kind would blind the F4/F5 attribution classes, which
        exist precisely to observe whether the model reads a refusal correctly.
        """
        payload = outcome_as_tool_content(
            ToolOutcome(False, None, "security", "outside every allowed root")
        )
        body = json.loads(payload)
        assert body["ok"] is False
        assert body["error_kind"] == "security"
        assert "outside every allowed root" in body["error"]


class TestSystemPrompt:
    def test_includes_the_workspace_path(self) -> None:
        prompt = build_system_prompt(r"E:\ws\scratch.gdb")
        assert r"E:\ws\scratch.gdb" in prompt

    def test_lists_every_dataset(self) -> None:
        prompt = build_system_prompt(r"E:\ws\scratch.gdb")
        for name, _desc in LAYER_CATALOGUE:
            assert name in prompt, f"dataset {name} missing from the prompt"

    def test_states_that_paths_are_confined(self) -> None:
        """Without this the model wastes its budget probing C:\\ and D:\\.

        Observed in the first real run: 122 tool calls, every one rejected.
        """
        prompt = build_system_prompt(r"E:\ws\scratch.gdb")
        assert "absolute path" in prompt
        assert "rejected" in prompt

    def test_forbids_inventing_numbers(self) -> None:
        """The instruction that makes fabricated values attributable as F3."""
        prompt = build_system_prompt(r"E:\ws\scratch.gdb")
        assert "never invent" in prompt


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
