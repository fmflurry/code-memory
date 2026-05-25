"""Tests for the Ollama-backed claim extractor.

The transport is stubbed at the ``httpx.Client`` level so tests are
hermetic — no live LLM, no network. We pin three contracts:

* Well-formed model output parses, validates, and deduplicates.
* Hallucinated ``evidence_span`` (not a substring of the prompt) is
  dropped silently. This is the main hallucination guard.
* Transport-level failures raise :class:`ExtractionError`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from code_memory.claims.extractor import (
    Claim,
    ClaimExtractor,
    ExtractionError,
)


# --------------------------------------------------------------- doubles


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _StubClient:
    def __init__(self, claims: list[dict[str, Any]]) -> None:
        self._claims = claims
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        self.calls.append({"url": url, "json": json})
        return _FakeResponse(
            {"message": {"content": json_dumps({"claims": self._claims})}}
        )

    def close(self) -> None:
        pass


class _FailingClient:
    def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        raise httpx.ConnectError("ollama down")

    def close(self) -> None:
        pass


def json_dumps(payload: Any) -> str:
    return json.dumps(payload)


# ------------------------------------------------------------- fixtures


def _make_extractor(client: Any) -> ClaimExtractor:
    extractor = ClaimExtractor(
        url="http://localhost:11434",
        model="gemma2:9b",
        timeout=1.0,
        min_confidence=0.6,
    )
    extractor._client = client  # type: ignore[assignment]
    return extractor


# ---------------------------------------------------------------- tests


def test_extracts_well_formed_claim() -> None:
    # Arrange
    prompt = "we use Qdrant for vectors and FalkorDB for the graph"
    client = _StubClient(
        [
            {
                "subject": "project",
                "predicate": "uses",
                "object": "Qdrant",
                "polarity": True,
                "confidence": 0.95,
                "evidence_span": "use Qdrant for vectors",
            },
            {
                "subject": "project",
                "predicate": "uses",
                "object": "FalkorDB",
                "polarity": True,
                "confidence": 0.95,
                "evidence_span": "FalkorDB for the graph",
            },
        ]
    )
    extractor = _make_extractor(client)

    # Act
    claims = extractor.extract(prompt)

    # Assert
    assert len(claims) == 2
    assert {c.object for c in claims} == {"Qdrant", "FalkorDB"}
    assert all(isinstance(c, Claim) for c in claims)
    assert all(c.predicate == "uses" for c in claims)


def test_drops_hallucinated_evidence_span() -> None:
    """An evidence_span that doesn't appear in the prompt is hallucinated."""
    # Arrange
    prompt = "we ship on Cloud Run"
    client = _StubClient(
        [
            {
                "subject": "deploy",
                "predicate": "deployed-to",
                "object": "Kubernetes",  # invented — not in the prompt
                "polarity": True,
                "confidence": 0.9,
                "evidence_span": "deploys to Kubernetes clusters",
            },
            {
                "subject": "deploy",
                "predicate": "deployed-to",
                "object": "Cloud Run",
                "polarity": True,
                "confidence": 0.9,
                "evidence_span": "ship on Cloud Run",
            },
        ]
    )
    extractor = _make_extractor(client)

    # Act
    claims = extractor.extract(prompt)

    # Assert
    assert [c.object for c in claims] == ["Cloud Run"]


def test_drops_low_confidence() -> None:
    # Arrange
    prompt = "maybe we should switch to Redis"
    client = _StubClient(
        [
            {
                "subject": "project",
                "predicate": "uses",
                "object": "Redis",
                "polarity": True,
                "confidence": 0.3,
                "evidence_span": "switch to Redis",
            }
        ]
    )
    extractor = _make_extractor(client)

    # Act
    claims = extractor.extract(prompt)

    # Assert
    assert claims == []


def test_deduplicates_case_insensitively() -> None:
    # Arrange
    prompt = "we use postgres for storage. Postgres is our DB."
    client = _StubClient(
        [
            {
                "subject": "project",
                "predicate": "uses",
                "object": "postgres",
                "polarity": True,
                "confidence": 0.9,
                "evidence_span": "use postgres for storage",
            },
            {
                "subject": "Project",
                "predicate": "Uses",
                "object": "Postgres",
                "polarity": True,
                "confidence": 0.9,
                "evidence_span": "Postgres is our DB",
            },
        ]
    )
    extractor = _make_extractor(client)

    # Act
    claims = extractor.extract(prompt)

    # Assert
    assert len(claims) == 1
    # predicate normalized to lower-kebab
    assert claims[0].predicate == "uses"


def test_handles_empty_claims_array() -> None:
    # Arrange
    extractor = _make_extractor(_StubClient([]))

    # Act
    claims = extractor.extract("should I use Redis here?")

    # Assert
    assert claims == []


def test_handles_non_json_response() -> None:
    """A model that emits prose instead of JSON returns [], never raises."""

    # Arrange
    class _NonJsonClient:
        def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
            return _FakeResponse({"message": {"content": "I am a teapot."}})

        def close(self) -> None:
            pass

    extractor = _make_extractor(_NonJsonClient())

    # Act
    claims = extractor.extract("we use Qdrant")

    # Assert
    assert claims == []


def test_handles_empty_prompt() -> None:
    # Arrange
    extractor = _make_extractor(_StubClient([]))

    # Act
    claims = extractor.extract("   \n  ")

    # Assert
    assert claims == []


def test_transport_failure_raises_extraction_error() -> None:
    # Arrange
    extractor = _make_extractor(_FailingClient())

    # Act / Assert
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract("we use Qdrant")
    assert "ollama" in str(excinfo.value).lower()


def test_normalizes_predicate_casing_and_spacing() -> None:
    # Arrange
    prompt = "I prefer the dark theme"
    client = _StubClient(
        [
            {
                "subject": "user",
                "predicate": "  PREFERS  ",
                "object": "dark theme",
                "polarity": True,
                "confidence": 0.95,
                "evidence_span": "prefer the dark theme",
            }
        ]
    )
    extractor = _make_extractor(client)

    # Act
    claims = extractor.extract(prompt)

    # Assert
    assert len(claims) == 1
    assert claims[0].predicate == "prefers"
