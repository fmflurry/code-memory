"""Backend factory + Ollama HybridVec compatibility tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import httpx
import pytest

import code_memory.embed as embed_pkg
from code_memory.embed import (
    Embedder,
    HybridVec,
    OllamaEmbedder,
    get_embedder,
    set_embedder_for_tests,
)
from code_memory.embed.ollama import OllamaEmbedder as _OllamaEmbedderCls


class _FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        from code_memory.embed import SparseVec

        return [HybridVec(dense=[0.1] * 8, sparse=SparseVec([], [])) for _ in texts]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    set_embedder_for_tests(None)
    yield
    set_embedder_for_tests(None)


def test_resolve_backend_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(embed_pkg.ENV_BACKEND, raising=False)
    assert embed_pkg._resolve_backend() == "ollama"


@pytest.mark.parametrize("value", ["flagembed", "flag", "m3", "fastembed", "FlagEmbed"])
def test_resolve_backend_flagembed_aliases(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(embed_pkg.ENV_BACKEND, value)
    assert embed_pkg._resolve_backend() == "flagembed"


@pytest.mark.parametrize("value", ["ollama", "OLLAMA", "anything-else", ""])
def test_resolve_backend_falls_back_to_ollama(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(embed_pkg.ENV_BACKEND, value)
    assert embed_pkg._resolve_backend() == "ollama"


def test_get_embedder_returns_ollama_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(embed_pkg.ENV_BACKEND, raising=False)
    e = get_embedder()
    assert isinstance(e, _OllamaEmbedderCls)


def test_set_embedder_for_tests_overrides_factory() -> None:
    fake = _FakeEmbedder()
    set_embedder_for_tests(cast(Embedder, fake))
    assert get_embedder() is fake


def test_ollama_embed_returns_hybridvec_with_empty_sparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama backend returns HybridVec to match the M3 shape; sparse is empty."""

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        def post(self, url: str, json: dict) -> _Resp:  # noqa: A002 - mirrors httpx API
            return _Resp({"embeddings": [[0.1, 0.2, 0.3]] * len(json["input"])})

        def close(self) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", lambda **_: _FakeClient())
    e = OllamaEmbedder()
    out = e.embed(["a", "b"])
    assert len(out) == 2
    for hv in out:
        assert isinstance(hv, HybridVec)
        assert hv.dense == [0.1, 0.2, 0.3]
        assert hv.sparse.indices == []
        assert hv.sparse.values == []
