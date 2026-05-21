"""Tests that MCP tool descriptions guide open-weight models correctly.

Open-weight models lean on the inputSchema descriptions far more than
frontier models do. These tests guard the contract that the resolved
default slug is embedded in every ``project`` field so callers never
have to guess.
"""

from __future__ import annotations


def test_every_tool_project_field_mentions_resolved_default() -> None:
    from code_memory import mcp_server

    for tool in mcp_server._TOOLS:
        props = tool.inputSchema.get("properties", {})
        if "project" not in props:
            continue
        desc = props["project"].get("description", "")
        assert "required" in desc.lower(), (
            f"tool {tool.name!r} project description must call out REQUIRED"
        )
        # the actual resolved slug should be embedded (backticked)
        assert f"`{mcp_server._DEFAULT_SLUG}`" in desc, (
            f"tool {tool.name!r} project description must embed the slug"
        )


def test_project_schema_helper_uses_module_default() -> None:
    from code_memory import mcp_server

    schema = mcp_server._project_schema()
    assert mcp_server._DEFAULT_SLUG in schema["description"]
    assert schema["type"] == "string"


def test_every_tool_lists_project_as_required() -> None:
    from code_memory import mcp_server

    for tool in mcp_server._TOOLS:
        required = tool.inputSchema.get("required", [])
        assert "project" in required, (
            f"tool {tool.name!r} must mark `project` as required"
        )
