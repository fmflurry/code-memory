"""Tests for OllamaEmbedder split connect/read timeout configuration.

Guards the fix for Windows IPv6-fallback hangs:
- The httpx client must be configured with ``httpx.Timeout`` (not a flat float).
- The connect timeout must be short (~5 s).
- The read timeout must remain long (300 s) for cold model loads.
- Custom connect_timeout / read_timeout constructor args are respected.
- Existing functional behaviour (embed, embed_one, retry, empty input) is
  tested here as a regression guard — not exhaustive; see test_embed_tei.py
  for the shape of those checks.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from code_memory.embed.ollama import OllamaEmbedder
from code_memory.embed.m3 import HybridVec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self.payload = payload if payload is not None else {"embeddings": []}
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any]) -> _Resp:
        self.calls.append({"url": url, "json": json})
        return _Resp(self.payload, self.status)

    def close(self) -> None:
        pass


def _make(
    payload: Any = None,
    status: int = 200,
) -> tuple[OllamaEmbedder, _FakeClient]:
    emb = OllamaEmbedder(url="http://127.0.0.1:11434")
    fake = _FakeClient(payload=payload, status=status)
    emb._client = fake  # type: ignore[assignment]
    return emb, fake


# ---------------------------------------------------------------------------
# Timeout configuration tests
# ---------------------------------------------------------------------------


def test_default_timeout_is_httpx_timeout_instance() -> None:
    """The client must use httpx.Timeout, not a plain float."""
    emb = OllamaEmbedder(url="http://127.0.0.1:11434")
    assert isinstance(emb._client.timeout, httpx.Timeout)
    emb.close()


def test_default_connect_timeout_is_short() -> None:
    """Connect timeout must be <= 5 s to fail fast on wrong IP stack."""
    emb = OllamaEmbedder(url="http://127.0.0.1:11434")
    assert emb._client.timeout.connect is not None
    assert emb._client.timeout.connect <= 5.0
    emb.close()


def test_default_read_timeout_is_long() -> None:
    """Read timeout must stay >= 300 s for cold Ollama model loads."""
    emb = OllamaEmbedder(url="http://127.0.0.1:11434")
    assert emb._client.timeout.read is not None
    assert emb._client.timeout.read >= 300.0
    emb.close()


def test_custom_connect_timeout_is_respected() -> None:
    emb = OllamaEmbedder(url="http://127.0.0.1:11434", connect_timeout=2.5)
    assert emb._client.timeout.connect == pytest.approx(2.5)
    emb.close()


def test_custom_read_timeout_is_respected() -> None:
    emb = OllamaEmbedder(url="http://127.0.0.1:11434", read_timeout=600.0)
    assert emb._client.timeout.read == pytest.approx(600.0)
    emb.close()


def test_connect_timeout_shorter_than_read_timeout() -> None:
    """Invariant: connect < read so we fail fast on bad routes."""
    emb = OllamaEmbedder(url="http://127.0.0.1:11434")
    assert emb._client.timeout.connect < emb._client.timeout.read  # type: ignore[operator]
    emb.close()


# ---------------------------------------------------------------------------
# Functional regression (embed still works after the refactor)
# ---------------------------------------------------------------------------


def test_embed_empty_returns_empty() -> None:
    emb, fake = _make()
    assert emb.embed([]) == []
    assert fake.calls == []


def test_embed_posts_to_api_embed_endpoint() -> None:
    emb, fake = _make(payload={"embeddings": [[0.1, 0.2]]})
    emb.embed(["hello"])
    assert "/api/embed" in fake.calls[0]["url"]


def test_embed_uses_input_key() -> None:
    emb, fake = _make(payload={"embeddings": [[0.0, 0.0]]})
    emb.embed(["x"])
    body = fake.calls[0]["json"]
    assert "input" in body
    assert body["input"] == ["x"]


def test_embed_returns_hybridvec_with_empty_sparse() -> None:
    emb, _ = _make(payload={"embeddings": [[0.1, 0.2, 0.3]]})
    result = emb.embed(["hi"])
    assert len(result) == 1
    assert isinstance(result[0], HybridVec)
    assert result[0].dense == pytest.approx([0.1, 0.2, 0.3])
    assert result[0].sparse.indices == []
    assert result[0].sparse.values == []


def test_embed_one_returns_single_vec() -> None:
    emb, _ = _make(payload={"embeddings": [[1.0, 2.0]]})
    vec = emb.embed_one("test")
    assert isinstance(vec, HybridVec)
    assert vec.dense == pytest.approx([1.0, 2.0])
