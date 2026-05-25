"""TEI (text-embeddings-inference) backend tests.

TEI's wire shape differs from Ollama in subtle ways that matter:

* Endpoint: ``/embed`` instead of ``/api/embed``.
* Request body uses ``inputs`` not ``input``.
* Response is a bare list of vectors, not wrapped in ``{"embeddings": ...}``.

These tests pin all three so a TEI version bump that breaks any of
them fails loudly instead of silently producing wrong vectors.
"""

from __future__ import annotations

from typing import Any

import pytest

from code_memory.embed.m3 import HybridVec
from code_memory.embed.tei import TEIEmbedder


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
        self.payload = payload if payload is not None else []
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any]) -> _Resp:
        self.calls.append({"url": url, "json": json})
        return _Resp(self.payload, self.status)

    def close(self) -> None:
        pass


def _make(payload: Any = None, status: int = 200) -> tuple[TEIEmbedder, _FakeClient]:
    emb = TEIEmbedder(url="http://test-tei:8080")
    fake = _FakeClient(payload=payload, status=status)
    emb._client = fake  # type: ignore[assignment]
    return emb, fake


def test_embed_empty_input_returns_empty() -> None:
    emb, fake = _make()
    assert emb.embed([]) == []
    assert fake.calls == []  # no HTTP call on empty input


def test_embed_posts_to_embed_endpoint() -> None:
    emb, fake = _make(payload=[[0.1, 0.2]])
    emb.embed(["hello"])
    assert fake.calls[0]["url"] == "http://test-tei:8080/embed"


def test_embed_uses_inputs_key_not_input() -> None:
    """Ollama-style ``input`` would silently fail against TEI."""
    emb, fake = _make(payload=[[0.0, 0.0]])
    emb.embed(["x"])
    body = fake.calls[0]["json"]
    assert "inputs" in body
    assert "input" not in body
    assert body["inputs"] == ["x"]


def test_embed_sets_truncate_true() -> None:
    emb, fake = _make(payload=[[0.0]])
    emb.embed(["short"])
    assert fake.calls[0]["json"]["truncate"] is True


def test_embed_parses_bare_list_response() -> None:
    emb, _ = _make(payload=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    out = emb.embed(["a", "b"])
    assert len(out) == 2
    assert isinstance(out[0], HybridVec)
    assert out[0].dense == pytest.approx([0.1, 0.2, 0.3])
    assert out[1].dense == pytest.approx([0.4, 0.5, 0.6])


def test_embed_returns_empty_sparse_for_shape_parity() -> None:
    """TEI is dense-only; sparse must be empty so callers don't branch."""
    emb, _ = _make(payload=[[0.1, 0.2]])
    vec = emb.embed(["x"])[0]
    assert vec.sparse.indices == []
    assert vec.sparse.values == []


def test_embed_raises_on_count_mismatch() -> None:
    """A response of N vectors for M != N inputs is a server bug — fail loud."""
    emb, _ = _make(payload=[[0.1, 0.2]])
    with pytest.raises(RuntimeError, match="returned 1 vectors for 2 inputs"):
        emb.embed(["a", "b"])


def test_embed_raises_on_unexpected_shape() -> None:
    """If TEI returns a wrapped object instead of a bare list, fail loud."""
    emb, _ = _make(payload={"embeddings": [[0.1, 0.2]]})
    with pytest.raises(RuntimeError, match="unexpected shape"):
        emb.embed(["x"])


def test_embed_one_returns_single_hybridvec() -> None:
    emb, _ = _make(payload=[[1.0, 2.0, 3.0]])
    vec = emb.embed_one("hello")
    assert isinstance(vec, HybridVec)
    assert vec.dense == pytest.approx([1.0, 2.0, 3.0])


def test_close_idempotent() -> None:
    emb, _ = _make()
    emb.close()  # Should not raise


def test_context_manager_closes() -> None:
    closed: list[bool] = []

    class _ClosingClient(_FakeClient):
        def close(self) -> None:
            closed.append(True)

    with TEIEmbedder(url="http://x") as emb:
        emb._client = _ClosingClient()  # type: ignore[assignment]
    assert closed == [True]
