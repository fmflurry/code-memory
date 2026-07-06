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
    """Return the main repo root for *start*, resolving linked worktrees.

    Inside a **linked git worktree** ``git rev-parse --show-toplevel``
    returns the worktree's own directory, not the main repo root.  That
    causes a slug mismatch: the worktree mints its own Qdrant / Falkor
    namespace instead of sharing the main repo's.

    Resolution: after obtaining the ``--show-toplevel`` baseline we run a
    second call with ``--path-format=absolute --git-common-dir``.

    * In a linked worktree: returns the absolute path to the MAIN repo's
      ``.git`` dir → ``Path(result).parent`` is the main repo root.
    * In the main worktree: returns ``<root>/.git`` → ``.parent`` equals
      the same root ``--show-toplevel`` already gave (no regression).

    Any failure in the second call (old git, missing binary, bad output,
    non-absolute path that can't be resolved, non-existent directory) is
    silently ignored and the ``--show-toplevel`` result is returned as-is.
    """
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
    if not top:
        return None
    baseline = Path(top)

    # Second call: resolve linked worktree → main repo root.
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                str(start),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return baseline

    if common.returncode != 0:
        return baseline

    git_common_raw = common.stdout.strip()
    if not git_common_raw:
        return baseline

    git_common = Path(git_common_raw)
    # Guard against older git ignoring --path-format and returning a
    # relative path; resolve it relative to start before taking .parent.
    if not git_common.is_absolute():
        git_common = (start / git_common).resolve()

    main_root = git_common.parent
    # Only adopt the common-dir-derived root when it actually exists.
    if not main_root.is_dir():
        return baseline

    return main_root


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


def is_linked_git_worktree(path: Path | None = None) -> bool:
    """Return ``True`` when *path* is inside a **linked** git worktree.

    A linked worktree shares the main repo's object store but has its own
    ``HEAD``.  ``git rev-parse --git-dir`` returns a worktree-private path
    (``<main>/.git/worktrees/<name>``), while ``git rev-parse --git-common-dir``
    returns the shared ``<main>/.git``.  The two paths differ in a linked
    worktree; they are equal in the main worktree.

    Contract:
    * Pure boolean — never raises; missing git binary, non-git path, or any
      subprocess error returns ``False``.
    * Default *path* is :func:`Path.cwd`.  If the resolved path is a file,
      its parent directory is used.
    * Main worktree and non-git dirs both return ``False``.
    """
    start: Path = path if path is not None else Path.cwd()
    if not start.is_dir():
        start = start.parent

    def _run_git(args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(start)] + args,
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        if result is None or result.returncode != 0:
            return None
        out = result.stdout.strip()
        return out if out else None

    raw_git_dir = _run_git(["rev-parse", "--git-dir"])
    if raw_git_dir is None:
        return False

    raw_common_dir = _run_git(["rev-parse", "--git-common-dir"])
    if raw_common_dir is None:
        return False

    def _resolve_git_path(raw: str) -> Path:
        p = Path(raw)
        if p.is_absolute():
            return p.resolve()
        return (start / p).resolve()

    git_dir = _resolve_git_path(raw_git_dir)
    common_dir = _resolve_git_path(raw_common_dir)
    return git_dir != common_dir


def is_inside_git_worktree(path: Path | None = None) -> bool:
    """Return ``True`` when *path* (default: cwd) is inside a git worktree.

    This is a lightweight predicate used by callers that need to decide
    whether to skip reingest of files that landed outside any known git
    repository.  A ``False`` result means there is no ``.git`` directory
    in the ancestry chain — the path is not part of a tracked project and
    slug-based collection routing would produce an arbitrary name derived
    from the raw directory name.

    Contract:
    * Pure boolean — never raises; unknown/missing git binary returns ``False``.
    * Does **not** require the path to be a git toplevel; any subdirectory
      inside a worktree returns ``True``.
    * Suitable as a fast guard before calling :func:`detect_project_slug`.
    """
    check = path or Path.cwd()
    return _git_toplevel(check if check.is_dir() else check.parent) is not None


def detect_project_slug(root: str | Path | None = None) -> str:
    """Resolve project slug.

    Priority:
      1. explicit `root` (path) -> git toplevel basename, else dir name
      2. CODE_MEMORY_PROJECT env var
      3. cwd -> git toplevel basename, else cwd name

    The returned slug is always a non-empty string — it never signals
    "not a git repo" by itself.  Callers that want to *skip* processing
    for paths outside any git worktree should call
    :func:`is_inside_git_worktree` first and branch on that result.
    The name-based fallback (dir basename) is intentionally preserved so
    that legitimate non-git projects (archives, temp checkout trees) can
    still be indexed under a stable namespace.
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
    # Default to 127.0.0.1 rather than ``localhost`` to avoid Windows
    # DNS resolving ``localhost`` → ``::1`` (IPv6) first when Ollama /
    # Qdrant / FalkorDB bind IPv4-only, which causes httpx to hang for
    # up to 300 s waiting for a connection that will never succeed.
    # Operators who explicitly export OLLAMA_URL / QDRANT_URL / etc.
    # retain full control — these are pure default-value changes.
    ollama_url: str = _env("OLLAMA_URL", "http://127.0.0.1:11434")
    # TEI (text-embeddings-inference) server URL. Used only when
    # ``EMBED_BACKEND=tei``. The enterprise-deploy story: stand TEI up
    # on a GPU host (Linux + CUDA), point ``TEI_URL`` at it, get a
    # 5-10× cold-ingest speedup over Ollama with the same bge-m3
    # weights. On Mac there's no GPU advantage and Ollama's Metal path
    # is faster — leave on the default backend there.
    tei_url: str = _env("TEI_URL", "http://127.0.0.1:8080")
    embed_model: str = _env("EMBED_MODEL", "bge-m3")
    # ``embed_dim`` defaults to the dimension of the configured model
    # so users don't have to keep two env vars in sync. Override with
    # ``EMBED_DIM`` when running a model not in the known-dim table.
    embed_dim: int = int(_env("EMBED_DIM", "0"))

    qdrant_url: str = _env("QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_code: str = _env("QDRANT_COLLECTION_CODE", "code_chunks")
    qdrant_episodes: str = _env("QDRANT_COLLECTION_EPISODES", "episodes")
    qdrant_claim_entities: str = _env(
        "QDRANT_COLLECTION_CLAIM_ENTITIES", "claim_entities"
    )
    # Semantic index over user-claim triples (subject + predicate + object
    # + evidence_span). Distinct from ``qdrant_claim_entities`` — that
    # one stores canonical entity vectors for resolver dedup; this one
    # stores per-claim vectors so retrieve can return semantically
    # matching claims alongside code + episodes. SQLite (``claims.db``)
    # remains source of truth; this collection is rebuildable.
    qdrant_claims: str = _env("QDRANT_COLLECTION_CLAIMS", "claims")

    falkor_host: str = _env("FALKOR_HOST", "127.0.0.1")
    falkor_port: int = int(_env("FALKOR_PORT", "6379"))
    falkor_graph: str = _env("FALKOR_GRAPH", "code_graph")

    # Resolved once at import time. Late-binding against `Path.cwd()` would
    # diverge whenever a long-lived process (MCP server) shares storage
    # with shell invocations launched from a different cwd, silently
    # routing writes and reads to different files.
    episodic_db: Path = Path(_env("EPISODIC_DB", "./data/episodic.db")).resolve()
    claims_db: Path = Path(_env("CLAIMS_DB", "./data/claims.db")).resolve()
    data_dir: Path = Path(_env("DATA_DIR", "./data")).resolve()

    # Claim extraction (removed). The LLM-based extraction path was removed
    # in favor of agent-authored claims via ``codememory_assert_claim``.
    # Cosine similarity at or above which a freshly embedded
    # subject/object reuses an existing entity instead of creating a new
    # one. 0.85 is a conservative default — false-merges hurt more than
    # extra entities (they propagate to every downstream claim).
    # Ingest health check: when ``file_count`` is stored from a prior full
    # ingest, the incremental path compares the repo's current ingestable
    # file count against the stored value.  If files grew by >20 % the
    # symbol count is cross-checked against the graph; a low ratio forces
    # a full re-ingest.  Disable by setting ``INGEST_HEALTH_CHECK=false``.
    ingest_health_check_enabled: bool = (
        _env("INGEST_HEALTH_CHECK", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    # Minimum expected symbols-per-file ratio.  Repos with large generated
    # or declaration-heavy code may average > 5 symbols/file; a value
    # below 0.5 suggests the graph is incomplete.
    ingest_health_check_min_ratio: float = float(
        _env("INGEST_HEALTH_CHECK_MIN_RATIO", "0.5")
    )

    claims_entity_threshold: float = float(
        _env("CLAIMS_ENTITY_THRESHOLD", "0.85")
    )
    # Cosine similarity at or above which a freshly extracted claim
    # collapses into the closest existing open claim instead of being
    # inserted as a new row. Catches paraphrastic near-duplicates
    # ("project uses flurryx" / "project depends-on flurryx" /
    # "user wants-to add a skipifcached using flurryx") that escape the
    # exact (subject, predicate, object) dedupe in ClaimsStore. Threshold
    # is conservative — false-merges across genuinely-distinct claims
    # hurt more than letting an occasional duplicate slip through.
    claims_semantic_dedup_threshold: float = float(
        _env("CLAIMS_SEMANTIC_DEDUP_THRESHOLD", "0.90")
    )

    def for_project(self, slug: str) -> Config:
        slug = slugify(slug)
        return replace(
            self,
            qdrant_code=f"{self.qdrant_code}__{slug}",
            qdrant_episodes=f"{self.qdrant_episodes}__{slug}",
            qdrant_claim_entities=f"{self.qdrant_claim_entities}__{slug}",
            qdrant_claims=f"{self.qdrant_claims}__{slug}",
            falkor_graph=f"{self.falkor_graph}__{slug}",
            episodic_db=self.data_dir / slug / "episodic.db",
            claims_db=self.data_dir / slug / "claims.db",
        )


CONFIG = Config()
