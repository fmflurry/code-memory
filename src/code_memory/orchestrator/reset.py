"""Project reset utilities.

Two scopes:

- **code index** — Qdrant code collection + FalkorDB graph + SQLite
  ``ingest_state`` table. Wiping this leaves prior agent conversations
  intact and is the common case (e.g. after broadening ignore rules).
- **episodes** — Qdrant episode collection + episodic SQLite DB. Wiping
  this is destructive: it forgets every recorded task/episode for the
  project. Opt-in via ``include_episodes=True``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from falkordb import FalkorDB

from ..config import CONFIG, slugify
from ..graph.falkor_store import FalkorStore
from ..vector.qdrant_store import QdrantStore


@dataclass
class ResetResult:
    project: str
    vectors_dropped: bool
    graph_cleared: bool
    state_cleared: bool
    episodes_dropped: bool
    episodic_db_removed: bool


def list_projects() -> list[str]:
    """Discover every project slug known to the storage backends.

    Union of:
    - Qdrant collections named ``<qdrant_code>__<slug>``
    - FalkorDB graphs named ``<falkor_graph>__<slug>``
    - subdirectories of ``data_dir`` (one dir per slug)
    """
    slugs: set[str] = set()

    code_prefix = f"{CONFIG.qdrant_code}__"
    eps_prefix = f"{CONFIG.qdrant_episodes}__"
    try:
        client = QdrantStore().client
        for c in client.get_collections().collections:
            for prefix in (code_prefix, eps_prefix):
                if c.name.startswith(prefix):
                    slugs.add(c.name[len(prefix) :])
    except Exception:
        pass

    graph_prefix = f"{CONFIG.falkor_graph}__"
    try:
        db = FalkorDB(host=CONFIG.falkor_host, port=CONFIG.falkor_port)
        for name in db.list_graphs():
            if name.startswith(graph_prefix):
                slugs.add(name[len(graph_prefix) :])
    except Exception:
        pass

    if CONFIG.data_dir.is_dir():
        for sub in CONFIG.data_dir.iterdir():
            if sub.is_dir():
                slugs.add(sub.name)

    return sorted(slugs)


def reset_project(slug: str, *, include_episodes: bool = False) -> ResetResult:
    """Wipe code index (and optionally episodic memory) for one project."""
    slug = slugify(slug)
    cfg = CONFIG.for_project(slug)

    result = ResetResult(
        project=slug,
        vectors_dropped=False,
        graph_cleared=False,
        state_cleared=False,
        episodes_dropped=False,
        episodic_db_removed=False,
    )

    try:
        vector = QdrantStore()
        vector.recreate_collection(cfg.qdrant_code)
        result.vectors_dropped = True
    except Exception:
        pass

    try:
        graph = FalkorStore(graph_name=cfg.falkor_graph)
        graph.clear_graph()
        result.graph_cleared = True
    except Exception:
        pass

    if include_episodes:
        try:
            QdrantStore().recreate_collection(cfg.qdrant_episodes)
            result.episodes_dropped = True
        except Exception:
            pass
        db_path = Path(cfg.episodic_db)
        if db_path.is_file():
            db_path.unlink(missing_ok=True)
            result.episodic_db_removed = True
            result.state_cleared = True
    else:
        # keep episodes; just clear ingest_state rows for this project's DB
        from .ingest_state import IngestStateStore

        db_path = Path(cfg.episodic_db)
        if db_path.is_file():
            store = IngestStateStore(db_path)
            try:
                store.conn.execute("DELETE FROM ingest_state")
                store.conn.commit()
                result.state_cleared = True
            finally:
                store.close()

    return result


def reset_all(*, include_episodes: bool = False) -> list[ResetResult]:
    return [reset_project(s, include_episodes=include_episodes) for s in list_projects()]
