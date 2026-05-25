from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Sentinel values for ``CODE_MEMORY_PROJECT`` that mean "infer from cwd"
# rather than "use a project literally named this". Recognising these
# avoids the silent footgun of indexing into a namespace called ``auto``.
_AUTO_SENTINELS = frozenset({"", "auto", "default"})


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s or "default"


def _git_toplevel(start: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    top = out.stdout.strip()
    return Path(top) if top else None


def detect_project_slug(root: str | Path | None = None) -> str:
    """Resolve project slug.

    Priority:
      1. explicit `root` (path) -> git toplevel basename, else dir name
      2. CODE_MEMORY_PROJECT env var
      3. cwd -> git toplevel basename, else cwd name
    """
    if root is not None:
        p = Path(root).resolve()
        top = _git_toplevel(p if p.is_dir() else p.parent)
        return slugify((top or p).name)

    env = os.environ.get("CODE_MEMORY_PROJECT", "").strip()
    if env and env.lower() not in _AUTO_SENTINELS:
        return slugify(env)

    cwd = Path.cwd()
    top = _git_toplevel(cwd)
    return slugify((top or cwd).name)


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
    claims_db: Path = Path(_env("CLAIMS_DB", "./data/claims.db"))
    data_dir: Path = Path(_env("DATA_DIR", "./data"))

    # Claim extraction (Graphiti-style user-prompt facts).
    # Disabled by default — opt in once the Ollama model is pulled.
    claims_enabled: bool = _env("CLAIMS_EXTRACTION", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    claims_llm_model: str = _env("CLAIMS_LLM_MODEL", "gemma2:9b")
    claims_llm_timeout: float = float(_env("CLAIMS_LLM_TIMEOUT", "30"))
    claims_min_confidence: float = float(_env("CLAIMS_MIN_CONFIDENCE", "0.6"))

    def for_project(self, slug: str) -> Config:
        slug = slugify(slug)
        return replace(
            self,
            qdrant_code=f"{self.qdrant_code}__{slug}",
            qdrant_episodes=f"{self.qdrant_episodes}__{slug}",
            falkor_graph=f"{self.falkor_graph}__{slug}",
            episodic_db=self.data_dir / slug / "episodic.db",
            claims_db=self.data_dir / slug / "claims.db",
        )


CONFIG = Config()
