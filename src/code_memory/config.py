from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path


# Config file name (project-local and global). KEY=VALUE per line, '#'
# starts a comment. Real shell env always wins; project file beats
# global file. Layering exists so users can pin defaults once
# (~/.config/code-memory/config) and override per repo
# (./.code-memoryrc) without polluting the shell rc.
_RC_BASENAME = ".code-memoryrc"
_GLOBAL_RC = (
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    / "code-memory"
    / "config"
)


def _parse_rc(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        # Strip matching surrounding quotes if any.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def _project_rc() -> Path | None:
    """Locate project rc: cwd, then walk up to git toplevel."""
    cwd = Path.cwd()
    candidate = cwd / _RC_BASENAME
    if candidate.is_file():
        return candidate
    top = _git_toplevel(cwd)
    if top is not None:
        candidate = top / _RC_BASENAME
        if candidate.is_file():
            return candidate
    return None


def _load_rc_into_environ() -> None:
    """Populate os.environ with rc-file values without overriding the
    real shell. Project rc beats global rc.

    Precedence (highest → lowest):
        real shell env > ./.code-memoryrc > ~/.config/code-memory/config
    """
    # Apply global first so the project pass can shadow it. Neither
    # pass overrides anything already in the shell environment.
    for source in (_GLOBAL_RC, _project_rc()):
        if source is None:
            continue
        for k, v in _parse_rc(source).items():
            if k not in os.environ:
                os.environ[k] = v


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


# Populate os.environ from rc files *before* the ``Config`` dataclass
# defaults are evaluated (those are computed at module import via
# ``_env(...)`` calls in field defaults). Real shell env still wins.
_load_rc_into_environ()


# Vector dimensionality of the embedding models we ship recipes for.
# Used to default ``EMBED_DIM`` when the operator only sets
# ``EMBED_MODEL``. Saves the silent-mismatch footgun where the model
# emits 768-d vectors but the Qdrant collection was created for 1024.
# Keys are matched case-insensitively against the leading model name
# (anything before ``:``), so ``bge-m3:latest``, ``bge-m3:567m-fp16``,
# and ``BAAI/bge-m3`` all resolve to the same dim.
_KNOWN_MODEL_DIMS: dict[str, int] = {
    # bge family
    "bge-m3": 1024,
    "baai/bge-m3": 1024,
    "bge-large-en": 1024,
    "bge-base-en": 768,
    "bge-small-en": 384,
    # nomic — fast Mac default for code search
    "nomic-embed-text": 768,
    "nomic-embed-text-v1": 768,
    "nomic-embed-text-v1.5": 768,
    # mixedbread
    "mxbai-embed-large": 1024,
    # snowflake
    "snowflake-arctic-embed:s": 384,
    "snowflake-arctic-embed:m": 768,
    "snowflake-arctic-embed:l": 1024,
}


def resolve_embed_dim(model_name: str, override: int = 0) -> int:
    """Return the vector dim for ``model_name``, honouring ``override``.

    ``override > 0`` wins (operators with a custom model still in
    control). Otherwise look up the model's base name in the known
    table. Falls back to ``1024`` (bge-m3 default) with a print to
    stderr so the operator notices we're guessing.
    """
    if override > 0:
        return override
    lower = model_name.strip().lower()
    # Try the full name (so ``snowflake-arctic-embed:s`` matches its
    # own dim, not the parent family's). Fall back to the bare base
    # name (so ``bge-m3:latest`` still resolves via ``bge-m3``).
    if lower in _KNOWN_MODEL_DIMS:
        return _KNOWN_MODEL_DIMS[lower]
    base = lower.split(":", 1)[0]
    if base in _KNOWN_MODEL_DIMS:
        return _KNOWN_MODEL_DIMS[base]
    # Unknown model — fall back to the bge-m3 default but warn so the
    # operator notices a mismatch before it produces broken vectors.
    import sys as _sys
    _sys.stderr.write(
        f"[code-memory] WARNING: embed model {model_name!r} not in "
        f"known-dim table; defaulting to 1024. Set EMBED_DIM=<n> to silence.\n"
    )
    return 1024


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
    # TEI (text-embeddings-inference) server URL. Used only when
    # ``EMBED_BACKEND=tei``. The enterprise-deploy story: stand TEI up
    # on a GPU host (Linux + CUDA), point ``TEI_URL`` at it, get a
    # 5-10× cold-ingest speedup over Ollama with the same bge-m3
    # weights. On Mac there's no GPU advantage and Ollama's Metal path
    # is faster — leave on the default backend there.
    tei_url: str = _env("TEI_URL", "http://localhost:8080")
    embed_model: str = _env("EMBED_MODEL", "bge-m3")
    # ``embed_dim`` defaults to the dimension of the configured model
    # so users don't have to keep two env vars in sync. Override with
    # ``EMBED_DIM`` when running a model not in the known-dim table.
    embed_dim: int = int(_env("EMBED_DIM", "0"))

    qdrant_url: str = _env("QDRANT_URL", "http://localhost:6333")
    qdrant_code: str = _env("QDRANT_COLLECTION_CODE", "code_chunks")
    qdrant_episodes: str = _env("QDRANT_COLLECTION_EPISODES", "episodes")
    qdrant_claim_entities: str = _env(
        "QDRANT_COLLECTION_CLAIM_ENTITIES", "claim_entities"
    )

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
    # Cosine similarity at or above which a freshly embedded
    # subject/object reuses an existing entity instead of creating a new
    # one. 0.85 is a conservative default — false-merges hurt more than
    # extra entities (they propagate to every downstream claim).
    claims_entity_threshold: float = float(
        _env("CLAIMS_ENTITY_THRESHOLD", "0.85")
    )

    def for_project(self, slug: str) -> Config:
        slug = slugify(slug)
        return replace(
            self,
            qdrant_code=f"{self.qdrant_code}__{slug}",
            qdrant_episodes=f"{self.qdrant_episodes}__{slug}",
            qdrant_claim_entities=f"{self.qdrant_claim_entities}__{slug}",
            falkor_graph=f"{self.falkor_graph}__{slug}",
            episodic_db=self.data_dir / slug / "episodic.db",
            claims_db=self.data_dir / slug / "claims.db",
        )


CONFIG = Config()
