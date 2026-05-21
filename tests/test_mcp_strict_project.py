"""Tests for the strict `project` parameter contract at the MCP boundary.

Open-weight models routinely omit `project` or invent values like
``"auto"``. The MCP server must reject those calls with a helpful error
that tells the model exactly which slug to pass on retry — never silently
fall back to cwd detection (which hides namespace bugs).
"""

from __future__ import annotations

import pytest

from code_memory.mcp_server import (
    MissingProjectError,
    _DEFAULT_SLUG,
    _require_project,
)


def test_missing_project_raises() -> None:
    with pytest.raises(MissingProjectError):
        _require_project({})


def test_blank_string_project_raises() -> None:
    with pytest.raises(MissingProjectError):
        _require_project({"project": ""})


def test_whitespace_only_project_raises() -> None:
    with pytest.raises(MissingProjectError):
        _require_project({"project": "   "})


@pytest.mark.parametrize("sentinel", ["auto", "AUTO", "Auto", "default", "DEFAULT"])
def test_sentinel_project_values_raise(sentinel: str) -> None:
    with pytest.raises(MissingProjectError):
        _require_project({"project": sentinel})


def test_non_string_project_raises() -> None:
    with pytest.raises(MissingProjectError):
        _require_project({"project": 42})
    with pytest.raises(MissingProjectError):
        _require_project({"project": None})


def test_valid_project_is_returned_verbatim() -> None:
    assert _require_project({"project": "gc-webapp"}) == "gc-webapp"


def test_valid_project_is_trimmed() -> None:
    assert _require_project({"project": "  gc-webapp  "}) == "gc-webapp"


def test_error_message_includes_default_slug() -> None:
    try:
        _require_project({})
    except MissingProjectError as e:
        # The error must tell the agent which slug to pass next time.
        assert _DEFAULT_SLUG in str(e)
        # And mention the discovery escape hatch.
        assert "code-memory projects" in str(e)
    else:
        pytest.fail("expected MissingProjectError")
