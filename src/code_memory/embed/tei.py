"""text-embeddings-inference (TEI) backend.

`HuggingFace TEI <https://github.com/huggingface/text-embeddings-inference>`_
is a purpose-built embedding server. On a Linux + NVIDIA host with the
same ``BAAI/bge-m3`` weights, it serves embeddings at **5-10× the
throughput** of Ollama because:

* Built on ONNX Runtime / candle-rs with native CUDA batching.
* Streams + dynamically batches concurrent requests instead of
  serialising one-at-a-time.
* No HTTP-to-llama.cpp daemon hop per call.

For enterprise CI / staging where the cold ingest of a large monorepo
matters, this is the way to break the ``bge-m3`` throughput floor
without changing models or losing semantic recall.

Trade-off vs ``OllamaEmbedder``:

* Same shape (dense-only ``HybridVec`` with empty sparse) so callers
  swap backends transparently.
* TEI must be running before code-memory ingests; Ollama-style "I
  brought my own daemon" still applies.
* On Mac (no NVIDIA), TEI's CPU path is roughly on par with Ollama's
  Metal path — there's no advantage. Stay on Ollama there.

Activated via ``EMBED_BACKEND=tei``; URL via ``TEI_URL``.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from ..config import CONFIG
from .m3 import HybridVec, SparseVec


class TEIEmbedder:
    """Sync wrapper over TEI's ``/embed`` endpoint.

    Returns :class:`HybridVec` with an empty sparse component so the
    shape matches :class:`OllamaEmbedder` and :class:`M3Embedder`.
    Callers (pipeline, retrieve) need no branching on backend type.

    TEI's request payload differs slightly from Ollama's:

    * Endpoint: ``POST /embed``
    * Body: ``{"inputs": [...]}``
    * Response: ``[[float, ...], [float, ...]]`` (raw vector list, no
      wrapping object).

    A ``truncate=true`` flag is set so over-length chunks are silently
    truncated to the model's max sequence length rather than failing
    the whole batch — the same forgiving semantic Ollama applies.
    """

    def __init__(
        self,
        url: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        # TEI doesn't accept a model id at request time — the daemon
        # is launched with a single ``--model-id`` flag — so we don't
        # carry one through requests. ``self.model`` exists for
        # parity with :class:`OllamaEmbedder` and is sourced from
        # ``EMBED_MODEL`` so the cache key namespace lines up across
        # backends pointing at the same model weights.
        self.url = (url or CONFIG.tei_url).rstrip("/")
        self.model = CONFIG.embed_model
        self._client = httpx.Client(timeout=timeout)

    def embed(self, texts: Sequence[str]) -> list[HybridVec]:
        if not texts:
            return []
        res = self._client.post(
            f"{self.url}/embed",
            json={"inputs": list(texts), "truncate": True},
        )
        res.raise_for_status()
        data = res.json()
        # TEI returns ``[[float, ...], ...]`` — a bare list of
        # vectors, one per input, in the same order. No wrapper key.
        if not isinstance(data, list):
            raise RuntimeError(f"TEI returned unexpected shape: {type(data).__name__}")
        if len(data) != len(texts):
            raise RuntimeError(
                f"TEI returned {len(data)} vectors for {len(texts)} inputs"
            )
        empty = SparseVec(indices=[], values=[])
        return [
            HybridVec(dense=[float(x) for x in vec], sparse=empty)
            for vec in data
        ]

    def embed_one(self, text: str) -> HybridVec:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TEIEmbedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
