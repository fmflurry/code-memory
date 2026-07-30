"""Wiring tests for the ``codememory_layer_violations`` MCP tool.

New symbols under test (do not exist yet — RED until implemented):

  - ``code_memory.mcp_server._layer_violations(args) -> list[TextContent]``
  - Registered in ``mcp_server._HANDLERS["codememory_layer_violations"]``
  - Listed in ``mcp_server._TOOLS`` (so ``list_tools()`` advertises it)

Mirrors the existing registration-test pattern for
``codememory_injectors`` / ``codememory_assert_claim``
(test_mcp_assert_claim.py: test_handler_is_registered /
test_tool_is_listed_in_TOOLS) — a light assertion, no heavy MCP
transport harness needed.
"""

from __future__ import annotations

import json

import pytest

from code_memory import mcp_server


def _payload(content: list) -> dict:
    assert content, "handler returned no content"
    return json.loads(content[0].text)


def test_layer_violations_handler_is_registered() -> None:
    assert "codememory_layer_violations" in mcp_server._HANDLERS
    assert (
        mcp_server._HANDLERS["codememory_layer_violations"]
        is mcp_server._layer_violations
    )


def test_layer_violations_tool_is_listed_in_TOOLS() -> None:
    names = {t.name for t in mcp_server._TOOLS}
    assert "codememory_layer_violations" in names


def test_layer_violations_tool_requires_project_in_schema() -> None:
    tool = next(t for t in mcp_server._TOOLS if t.name == "codememory_layer_violations")
    assert "project" in tool.inputSchema.get("required", [])


def test_layer_violations_handler_dispatches_to_store_and_returns_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeStore:
        def layer_violations(self) -> list[dict]:
            return [
                {
                    "file": "/repo/domain/UseCase.cs",
                    "layer": "domain",
                    "depends_on_file": "/repo/infrastructure/Impl.cs",
                    "depends_on_layer": "infrastructure",
                    "symbol": "DoWork",
                }
            ]

    monkeypatch.setattr(
        mcp_server, "_graph_for", lambda project: (_FakeStore(), project)
    )

    result = mcp_server._layer_violations({"project": "test-proj"})
    body = _payload(result)

    assert body["project"] == "test-proj"
    assert body["violations"] == [
        {
            "file": "/repo/domain/UseCase.cs",
            "layer": "domain",
            "depends_on_file": "/repo/infrastructure/Impl.cs",
            "depends_on_layer": "infrastructure",
            "symbol": "DoWork",
        }
    ]


def test_layer_violations_handler_requires_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(mcp_server.MissingProjectError):
        mcp_server._layer_violations({})
