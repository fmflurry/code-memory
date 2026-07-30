"""Pins FIX C's wiring: the digest from ``OllamaEmbedder.model_digest()``
(see test_embed_ollama_digest.py) must actually land in the embed-cache
key, and must do so *lazily*.

Critical constraint driving the "lazy" design: ``Pipeline.__init__``
calls ``get_embedder()`` unconditionally, even when the caller passed
``--no-vectors`` / ``skip_vectors=True`` (orchestrator/pipeline.py:319
runs before the ``skip_vectors`` check at :326). If resolving the cache
key eagerly required Ollama to be reachable, a plain graph-only ingest
with Ollama down would break for no reason -- today it works fine
because the embedder is built but never actually asked to ``.embed()``
anything. So: ``_build_inner_embedder("ollama")`` must return the
model-id as a zero-arg callable, resolved (and memoized) by
``CachedEmbedder`` only on the first real ``embed()``/``embed_one()``
call -- exactly the same moment Ollama must be reachable anyway for
embedding to work at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

import code_memory.embed as embed_pkg
from code_memory.embed import CachedEmbedder, EmbedCache, hash_chunk
from code_memory.embed.m3 import HybridVec, SparseVec


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeTagsClient:
    def __init__(
        self,
        payload: dict | None = None,
        raise_on_get: BaseException | None = None,
    ) -> None:
        self.payload = payload or {}
        self.raise_on_get = raise_on_get
        self.get_calls = 0

    def get(self, url: str, *args: object, **kwargs: object) -> _FakeResponse:
        self.get_calls += 1
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return _FakeResponse(self.payload)

    def close(self) -> None:
        return None


class _RecordingInner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        self.calls.append(list(texts))
        return [HybridVec(dense=[0.1], sparse=SparseVec([], [])) for _ in texts]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    embed_pkg.set_embedder_for_tests(None)
    yield
    embed_pkg.set_embedder_for_tests(None)


# ---------------------------------------------------------------- laziness


def test_build_inner_embedder_ollama_does_not_touch_network_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeTagsClient(raise_on_get=RuntimeError("network must not be touched here"))
    monkeypatch.setattr(httpx, "Client", lambda **_: client)

    emb, model_id_source = embed_pkg._build_inner_embedder("ollama")

    assert client.get_calls == 0
    assert callable(model_id_source), (
        "ollama's model_id must be a zero-arg callable resolved lazily, "
        "not a pre-computed string -- so callers that never call "
        ".embed() (e.g. `ingest --no-vectors`) never need Ollama "
        "reachable just to build the embedder"
    )


def test_ollama_model_id_includes_digest_once_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeTagsClient(
        {"models": [{"name": "bge-m3:latest", "digest": "sha256:deadbeef"}]}
    )
    monkeypatch.setattr(httpx, "Client", lambda **_: client)

    _, model_id_source = embed_pkg._build_inner_embedder("ollama")
    resolved = model_id_source()

    assert resolved == "model:bge-m3@sha256:deadbeef"


def test_cached_embedder_resolves_model_id_lazily_on_first_embed_and_memoizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeTagsClient(
        {"models": [{"name": "bge-m3:latest", "digest": "sha256:cafef00d"}]}
    )
    monkeypatch.setattr(httpx, "Client", lambda **_: client)

    _, model_id_source = embed_pkg._build_inner_embedder("ollama")
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=_RecordingInner(), cache=cache, model_id=model_id_source)

    assert client.get_calls == 0, "constructing CachedEmbedder must not resolve the digest"

    ce.embed(["hello"])
    assert client.get_calls == 1
    assert ce.model_id == "model:bge-m3@sha256:cafef00d"

    ce.embed(["world"])
    assert client.get_calls == 1, "digest must be memoized, not re-fetched per embed() call"


def test_cached_embedder_propagates_digest_failure_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The critical design call: unreachable Ollama / model missing from
    /api/tags must hard-fail the embed() call, never silently degrade to
    a digest-less key -- that would reintroduce the exact "silently
    wrong vectors forever" bug this fix closes."""
    client = _FakeTagsClient(raise_on_get=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(httpx, "Client", lambda **_: client)

    _, model_id_source = embed_pkg._build_inner_embedder("ollama")
    cache = EmbedCache(tmp_path / "c.db")
    ce = CachedEmbedder(inner=_RecordingInner(), cache=cache, model_id=model_id_source)

    with pytest.raises(RuntimeError):
        ce.embed(["hello"])

    # Nothing must have been written under ANY key -- not a digest-less
    # fallback, not a partial write.
    row_count = cache.conn.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]
    assert row_count == 0


# ---------------------------------------------------------- other backends


def test_tei_model_id_remains_a_plain_immediate_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEI has no Ollama-style /api/tags digest concept; its key must
    stay a plain, stable string produced immediately (not a lazy
    callable) -- the digest-pinning fix is Ollama-specific."""
    _, tei_key = embed_pkg._build_inner_embedder("tei")
    assert isinstance(tei_key, str)
    assert tei_key.startswith("model:")


def test_flagembed_model_id_remains_a_plain_immediate_string() -> None:
    _, key = embed_pkg._build_inner_embedder("flagembed")
    assert isinstance(key, str)
    assert key.startswith("flagembed:")


# ---------------------------------------------------- old rows are ignored


def test_old_digest_less_rows_are_not_matched_by_new_digest_bearing_key(
    tmp_path: Path,
) -> None:
    """Existing rows keyed `model:bge-m3` (digest-less) must never be
    misread as valid under the new `model:bge-m3@<digest>` key -- they
    must simply miss, forcing a one-time re-embed (accepted cost), never
    silently returning stale vectors under a mismatched key."""
    cache = EmbedCache(tmp_path / "c.db")
    old_key = "model:bge-m3"
    new_key = "model:bge-m3@sha256:deadbeef"
    h = hash_chunk("some chunk text")
    cache.put_many(
        [(h, HybridVec(dense=[0.1], sparse=SparseVec([], [])))], model=old_key
    )

    assert cache.get_many([h], new_key) == {}
    assert h in cache.get_many([h], old_key)
