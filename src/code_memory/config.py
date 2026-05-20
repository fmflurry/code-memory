from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Config:
    ollama_url: str = _env("OLLAMA_URL", "http://localhost:11434")
    embed_model: str = _env("EMBED_MODEL", "bge-m3")
    embed_dim: int = int(_env("EMBED_DIM", "1024"))

    qdrant_url: str = _env("QDRANT_URL", "http://localhost:6333")
    qdrant_code: str = _env("QDRANT_COLLECTION_CODE", "code_chunks")
    qdrant_episodes: str = _env("QDRANT_COLLECTION_EPISODES", "episodes")

    falkor_host: str = _env("FALKOR_HOST", "localhost")
    falkor_port: int = int(_env("FALKOR_PORT", "6379"))
    falkor_graph: str = _env("FALKOR_GRAPH", "code_graph")

    episodic_db: Path = Path(_env("EPISODIC_DB", "./data/episodic.db"))


CONFIG = Config()
