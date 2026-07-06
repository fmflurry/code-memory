"""Tests for the codememory_assert_claim MCP handler.

The handler lets the agent author a claim directly without invoking
the local LLM extractor. We exercise:

* Required-field validation surfaces a ValueError payload.
* Type / range checks on ``polarity`` and ``confidence``.
* A successful insert persists to the project-scoped claims.db with
  the expected fields and predicate canonicalization.
* Single-valued predicates still trigger contradiction handling via
  ClaimsStore.upsert.
* Project validation reuses the strict ``_require_project`` contract.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from code_memory import mcp_server
from code_memory.claims import ClaimsStore
from code_memory.config import CONFIG


def _payload(content: list) -> dict:
    """Decode the JSON payload returned in the first TextContent block."""
    assert content, "handler returned no content"
    return json.loads(content[0].text)


@pytest.fixture()
def isolated_config(monkeypatch, tmp_path: Path):
    """Point the server's CONFIG at a tmp data_dir so claims.db is isolated.

    The dataclass is frozen so we use :func:`dataclasses.replace`.
    """
    isolated = replace(CONFIG, data_dir=tmp_path)
    monkeypatch.setattr(mcp_server, "CONFIG", isolated)
    monkeypatch.setenv("CODE_MEMORY_NO_GUARD", "1")
    return isolated


def _base_args(project: str = "test-proj") -> dict:
    return {
        "project": project,
        "subject": "project",
        "predicate": "uses",
        "object": "Postgres",
    }


# ------------------------------------------------------------- validation


def test_missing_subject_returns_error(isolated_config) -> None:
    args = _base_args()
    args.pop("subject")
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["error"] == "ValueError"
    assert "subject" in payload["message"]


def test_blank_predicate_returns_error(isolated_config) -> None:
    args = _base_args()
    args["predicate"] = "   "
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["error"] == "ValueError"
    assert "predicate" in payload["message"]


def test_missing_object_returns_error(isolated_config) -> None:
    args = _base_args()
    args.pop("object")
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["error"] == "ValueError"
    assert "object" in payload["message"]


def test_polarity_must_be_bool(isolated_config) -> None:
    args = _base_args()
    args["polarity"] = "true"  # string, not bool
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["error"] == "ValueError"
    assert "polarity" in payload["message"]


def test_confidence_out_of_range_returns_error(isolated_config) -> None:
    args = _base_args()
    args["confidence"] = 1.5
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["error"] == "ValueError"
    assert "confidence" in payload["message"]


def test_confidence_non_numeric_returns_error(isolated_config) -> None:
    args = _base_args()
    args["confidence"] = "high"
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["error"] == "ValueError"


def test_missing_project_raises(isolated_config) -> None:
    args = _base_args()
    args.pop("project")
    with pytest.raises(mcp_server.MissingProjectError):
        mcp_server._assert_claim(args)


# ------------------------------------------------------------- insertion


def test_successful_insert_persists_to_claims_db(isolated_config) -> None:
    # Act
    payload = _payload(mcp_server._assert_claim(_base_args()))

    # Assert — response surface
    assert payload["project"] == "test-proj"
    assert payload["subject"] == "project"
    assert payload["predicate"] == "uses"
    assert payload["object"] == "Postgres"
    assert payload["polarity"] is True
    assert payload["confidence"] == pytest.approx(0.95)
    assert "claim_id" in payload

    # Assert — row reachable via the underlying store
    cfg = isolated_config.for_project("test-proj")
    store = ClaimsStore(path=cfg.claims_db)
    try:
        rows = store.current()
    finally:
        store.close()
    assert len(rows) == 1
    assert rows[0].id == payload["claim_id"]


def test_predicate_is_canonicalized_to_kebab(isolated_config) -> None:
    args = _base_args()
    args["predicate"] = "Is Located At"
    args["subject"] = "billing service"
    args["object"] = "apps/api/billing"
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["predicate"] == "is-located-at"


def test_evidence_span_is_stored_when_provided(isolated_config) -> None:
    args = _base_args()
    args["evidence_span"] = "we use Postgres in prod"
    payload = _payload(mcp_server._assert_claim(args))

    cfg = isolated_config.for_project("test-proj")
    store = ClaimsStore(path=cfg.claims_db)
    try:
        rec = store.by_id(payload["claim_id"])
    finally:
        store.close()
    assert rec is not None
    assert rec.evidence_span == "we use Postgres in prod"


def test_evidence_span_missing_is_allowed(isolated_config) -> None:
    """Agent-authored claims often have no verbatim quote — allow it."""
    payload = _payload(mcp_server._assert_claim(_base_args()))
    cfg = isolated_config.for_project("test-proj")
    store = ClaimsStore(path=cfg.claims_db)
    try:
        rec = store.by_id(payload["claim_id"])
    finally:
        store.close()
    assert rec is not None
    assert rec.evidence_span == ""


def test_valid_at_override_is_respected(isolated_config) -> None:
    args = _base_args()
    args["valid_at"] = 123.456
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["valid_at"] == pytest.approx(123.456)


def test_polarity_false_persists_as_negation(isolated_config) -> None:
    args = _base_args()
    args["polarity"] = False
    args["object"] = "Redis"
    payload = _payload(mcp_server._assert_claim(args))
    assert payload["polarity"] is False


# -------------------------------------------------- contradiction handling


def test_single_valued_predicate_closes_prior(isolated_config) -> None:
    """A second `(project, uses, X)` must close the first."""
    first = _base_args()
    first["valid_at"] = 100.0
    mcp_server._assert_claim(first)

    second = _base_args()
    second["object"] = "MySQL"
    second["valid_at"] = 200.0
    mcp_server._assert_claim(second)

    cfg = isolated_config.for_project("test-proj")
    store = ClaimsStore(path=cfg.claims_db)
    try:
        current = store.current()
    finally:
        store.close()
    assert len(current) == 1
    assert current[0].object == "MySQL"


# ----------------------------------------------------------- registration


def test_handler_is_registered() -> None:
    assert "codememory_assert_claim" in mcp_server._HANDLERS
    assert mcp_server._HANDLERS["codememory_assert_claim"] is mcp_server._assert_claim


def test_tool_is_listed_in_TOOLS() -> None:
    names = {t.name for t in mcp_server._TOOLS}
    assert "codememory_assert_claim" in names
