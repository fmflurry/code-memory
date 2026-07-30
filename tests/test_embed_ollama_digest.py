"""Pins FIX C's primitive: ``OllamaEmbedder.model_digest()``.

Root cause: the cache key is ``f"model:{emb.model}"`` = ``model:bge-m3``
(embed/__init__.py:177), and Ollama silently resolves the unpinned tag
to ``bge-m3:latest``. If an operator re-pulls the model, ``:latest``
can point at different weights while the tag string -- and therefore
the cache key -- is unchanged: old and new vectors mix silently in one
collection with no error. Verified live: ``bge-m3:latest`` resolved to
digest ``7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab``.

Design decision pinned here -- the most important call in this round:
when Ollama is unreachable, ``/api/tags`` errors, or the configured
model is absent from the response, ``model_digest()`` **hard-fails**
(raises) rather than falling back to a digest-less key. Falling back
would silently reintroduce the exact bug this fix exists to close.
This costs nothing beyond what already existed: embedding via Ollama
already requires Ollama to be reachable (there is no offline mode for
this backend), and operators who need offline/no-daemon embedding
already have ``EMBED_BACKEND=flagembed`` as an escape hatch.

Failures are NOT memoized -- only a successful digest sticks (pinned by
``test_model_digest_retries_after_a_transient_failure``), so transient
Ollama downtime at process start doesn't permanently poison the
singleton for the rest of a long-lived MCP server process.
"""

from __future__ import annotations

import httpx
import pytest

from code_memory.embed.ollama import OllamaEmbedder


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeTagsClient:
    """Fake httpx.Client exposing only GET (Ollama's /api/tags)."""

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


def _embedder(monkeypatch: pytest.MonkeyPatch, client: _FakeTagsClient, model: str = "bge-m3") -> OllamaEmbedder:
    monkeypatch.setattr(httpx, "Client", lambda **_: client)
    return OllamaEmbedder(model=model)


# ---------------------------------------------------------------- happy path


def test_model_digest_matches_tag_suffixed_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama resolves the unpinned `bge-m3` to a tagged `bge-m3:latest`
    entry in /api/tags; matching must strip the `:tag` suffix."""
    client = _FakeTagsClient(
        {"models": [{"name": "bge-m3:latest", "digest": "sha256:aaa111"}]}
    )
    emb = _embedder(monkeypatch, client)
    assert emb.model_digest() == "sha256:aaa111"


def test_model_digest_matches_exact_tag_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the operator pins EMBED_MODEL to an explicit tag (e.g.
    `bge-m3:v1`) that matches /api/tags verbatim, no suffix-stripping is
    needed -- exact match must also work."""
    client = _FakeTagsClient(
        {"models": [{"name": "bge-m3:v1", "digest": "sha256:pinned"}]}
    )
    emb = _embedder(monkeypatch, client, model="bge-m3:v1")
    assert emb.model_digest() == "sha256:pinned"


def test_model_digest_is_fetched_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeTagsClient(
        {"models": [{"name": "bge-m3:latest", "digest": "sha256:aaa111"}]}
    )
    emb = _embedder(monkeypatch, client)

    emb.model_digest()
    emb.model_digest()
    emb.model_digest()

    assert client.get_calls == 1, "digest must be memoized, not fetched per call"


def test_different_digests_for_same_model_name_produce_different_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of this fix: a re-pull that changes the digest
    behind an unchanged model name must be distinguishable."""
    client_a = _FakeTagsClient(
        {"models": [{"name": "bge-m3:latest", "digest": "sha256:AAA"}]}
    )
    emb_a = _embedder(monkeypatch, client_a)
    digest_a = emb_a.model_digest()

    client_b = _FakeTagsClient(
        {"models": [{"name": "bge-m3:latest", "digest": "sha256:BBB"}]}
    )
    emb_b = _embedder(monkeypatch, client_b)
    digest_b = emb_b.model_digest()

    assert digest_a != digest_b


def test_model_digest_is_stable_regardless_of_tags_list_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards against relying on list/dict iteration order: the target
    model's position in /api/tags' `models` list must not affect the
    resolved digest."""
    payload_first = {
        "models": [
            {"name": "bge-m3:latest", "digest": "sha256:target"},
            {"name": "llama3:latest", "digest": "sha256:decoy"},
        ]
    }
    payload_last = {
        "models": [
            {"name": "llama3:latest", "digest": "sha256:decoy"},
            {"name": "bge-m3:latest", "digest": "sha256:target"},
        ]
    }
    emb1 = _embedder(monkeypatch, _FakeTagsClient(payload_first))
    emb2 = _embedder(monkeypatch, _FakeTagsClient(payload_last))

    assert emb1.model_digest() == emb2.model_digest() == "sha256:target"


# ---------------------------------------------------------------- failure paths


def test_model_digest_raises_when_ollama_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeTagsClient(raise_on_get=httpx.ConnectError("connection refused"))
    emb = _embedder(monkeypatch, client)
    with pytest.raises(RuntimeError):
        emb.model_digest()


def test_model_digest_raises_when_model_absent_from_tags_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model not pulled -- different root cause from "Ollama unreachable"
    but the same required outcome: hard-fail, never a silent digest-less
    fallback."""
    client = _FakeTagsClient(
        {"models": [{"name": "llama3:latest", "digest": "sha256:decoy"}]}
    )
    emb = _embedder(monkeypatch, client)
    with pytest.raises(RuntimeError):
        emb.model_digest()


def test_model_digest_retries_after_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure must NOT be memoized -- only success sticks. Ollama
    coming back up (or the model being pulled) after an earlier failure
    must succeed on the next call rather than staying poisoned for the
    rest of the process."""
    client = _FakeTagsClient(raise_on_get=httpx.ConnectError("down"))
    emb = _embedder(monkeypatch, client)

    with pytest.raises(RuntimeError):
        emb.model_digest()

    client.raise_on_get = None
    client.payload = {
        "models": [{"name": "bge-m3:latest", "digest": "sha256:recovered"}]
    }
    assert emb.model_digest() == "sha256:recovered"
