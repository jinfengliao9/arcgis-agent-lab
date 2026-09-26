"""Export the bridge's tool registry as LLM-facing function schemas.

Discipline: why the escape hatch is excluded
--------------------------------------------
This evaluation asks exactly one question: **can a model choose and sequence the
right tools?** The bridge also exposes ``execute_spatial_tool``, which takes an
arbitrary tool name plus an arbitrary parameter dictionary -- a general-purpose
escape hatch.

Leaving it in would let a model sidestep the named surface entirely and
degenerate every task into "write some Python", which is exactly what
code-generation benchmarks (GeoAnalystBench and friends) already measure. The
measurement would quietly stop being about tool orchestration while still
looking like it was.

So the exported surface excludes it by default, and the exclusion is a **named
constant** rather than an incidental filter -- it is a methodological decision
that belongs in the evaluation report, not a detail buried in a comprehension.

Accepted cost: tasks that would legitimately need buffering cannot be answered
through the escape hatch either. That is a real restriction, and a better one
than measuring the wrong thing.
"""

from __future__ import annotations

from typing import Any, Final

import arcgis_mcp.tools  # noqa: F401  -- importing populates the registry
from arcgis_mcp.registry import ToolSpec, all_specs

#: Tools withheld from the model under test. See the module docstring: the
#: escape hatch would replace tool *selection* with code *generation*.
#:
#: Why a filter at all, when the registry does not contain the tool: the bridge
#: exposes two kinds of endpoint. Its three *core* tools (``health_check`` /
#: ``list_layers`` / ``execute_spatial_tool``) are written by hand in
#: ``server.py`` and are **not** registry entries; the 100 *catalog* tools are.
#: This module exports the catalog, so the escape hatch is already absent by
#: construction. The filter is kept as a guard: should the escape hatch ever
#: migrate into the catalog, it cannot silently re-enter the evaluated surface.
ESCAPE_HATCH_TOOLS: Final[frozenset[str]] = frozenset({"execute_spatial_tool"})


def export_tool_schemas(*, include_escape_hatches: bool = False) -> list[dict[str, Any]]:
    """Return every registry tool as an OpenAI-style function definition.

    Args:
        include_escape_hatches: Set True only for the parity/ablation check that
            deliberately measures what the escape hatch changes. Never for the
            primary run.
    """
    blocked = set() if include_escape_hatches else ESCAPE_HATCH_TOOLS
    schemas: list[dict[str, Any]] = []
    for spec in all_specs():
        if spec.name in blocked:
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_model.model_json_schema(),
                },
            }
        )
    return schemas


def allowed_tool_names(*, include_escape_hatches: bool = False) -> frozenset[str]:
    """Names the harness will accept for execution.

    Kept separate from :func:`export_tool_schemas` on purpose: the schema list is
    what the model *sees*, this set is what the harness *accepts*. Enforcing both
    means a model that hallucinates a withheld tool gets a clean, recorded
    rejection instead of silent execution.
    """
    blocked = set() if include_escape_hatches else ESCAPE_HATCH_TOOLS
    return frozenset(spec.name for spec in all_specs() if spec.name not in blocked)


def spec_for(name: str) -> ToolSpec | None:
    """Look up one spec by name (None when the name does not exist)."""
    for spec in all_specs():
        if spec.name == name:
            return spec
    return None


#: Every tool the bridge can actually execute: the 100 catalog tools **plus** the
#: three core endpoints hand-written in ``server.py``.
#:
#: This exists to keep two very different failures apart when attributing blame.
#: ``buffer_features`` does not exist at all -- the model invented it. But
#: ``execute_spatial_tool`` *does* exist and this evaluation chooses to withhold
#: it, so calling it is surface-probing, not hallucination. Scoring the second as
#: the first would inflate the hallucination rate with behaviour that is really
#: an attempt to escape the measured surface -- two facts that warrant different
#: conclusions.
BRIDGE_TOOL_UNIVERSE: Final[frozenset[str]] = frozenset(
    {spec.name for spec in all_specs()}
    | {"health_check", "list_layers", "execute_spatial_tool"}
)
