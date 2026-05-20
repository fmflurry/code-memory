from __future__ import annotations

from collections.abc import Sequence

import httpx

from ..config import CONFIG


class OllamaEmbedder:
    """Thin sync wrapper over Ollama /api/embed."""

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.url = (url or CONFIG.ollama_url).rstrip("/")
        self.model = model or CONFIG.embed_model
        self._client = httpx.Client(timeout=timeout)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        res = self._client.post(
            f"{self.url}/api/embed",
            json={"model": self.model, "input": list(texts)},
        )
        res.raise_for_status()
        data = res.json()
        embeddings = data.get("embeddings")
        if embeddings is None:
            raise RuntimeError(f"Ollama returned no embeddings: {data}")
        return embeddings

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OllamaEmbedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
