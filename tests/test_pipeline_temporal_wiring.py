"""End-to-end check: Pipeline plumbs head_sha through every upsert.

Catches the regression where ``_upsert_graph`` / ``_index_*`` methods
forget to forward ``head_sha`` / ``head_ord`` and the temporal layer
silently writes nothing. Uses a fake ``FalkorStore`` that records
every call's kwargs so we can assert head_sha was actually threaded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from code_memory.extractor.treesitter import ExtractedFile
from code_memory.orchestrator import pipeline as pipeline_mod


class _RecordingGraph:
    """Stand-in for ``FalkorStore`` that captures every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def ensure_indexes(self) -> None:  # called by Pipeline.__init__
        self.calls.append(("ensure_indexes", {}))

    def clear_graph(self) -> None:
        self.calls.append(("clear_graph", {}))

    def upsert_nodes(
        self,
        nodes: Any,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        # Materialise the iterable so the test can count nodes too.
        node_list = list(nodes)
        self.calls.append(
            ("upsert_nodes", {"head_sha": head_sha, "head_ord": head_ord, "n": len(node_list)})
        )

    def upsert_edges(
        self,
        edges: Any,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        edge_list = list(edges)
        self.calls.append(
            ("upsert_edges", {"head_sha": head_sha, "head_ord": head_ord, "n": len(edge_list)})
        )

    def delete_file(
        self,
        path: str,
        *,
        head_sha: str | None = None,
        head_ord: int | None = None,
    ) -> None:
        self.calls.append(
            ("delete_file", {"path": path, "head_sha": head_sha, "head_ord": head_ord})
        )

    def query(self, q: str, params: dict[str, Any] | None = None) -> Any:
        """Empty result for the file-containment query path."""

        class _R:
            result_set: list[Any] = []

        return _R()

    # Pipeline.__init__ accesses .graph for some Cypher queries; the
    # in-memory containment scan goes through ``self.graph.graph``.
    @property
    def graph(self) -> "_RecordingGraph":
        return self


def _ex(tmp_path: Path) -> ExtractedFile:
    """A tiny ExtractedFile with one symbol so _upsert_graph emits something."""
    src_path = tmp_path / "x.cs"
    src_path.write_text("public class C {}", encoding="utf-8")
    return ExtractedFile(
        path=str(src_path),
        lang="csharp",
        symbols=[],  # zero symbols still produces a File node
        imports=[],
        calls=[],
        injects=[],
        source="public class C {}",
        generated=False,
    )


@pytest.fixture
def pipeline_with_fakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[pipeline_mod.Pipeline, _RecordingGraph]:
    # Bypass Qdrant / Falkor / embedder construction.
    monkeypatch.setattr(
        pipeline_mod.Pipeline, "__init__", lambda self, **kw: None
    )
    pipe = pipeline_mod.Pipeline()
    pipe.graph = _RecordingGraph()  # type: ignore[assignment]
    # Stub out the vector store so _upsert_vectors is a no-op.
    pipe.vector = type(
        "V",
        (),
        {
            "upsert": lambda self, *a, **kw: None,
            "delete_by_path": lambda self, *a, **kw: None,
        },
    )()  # type: ignore[assignment]
    pipe.cfg = type("C", (), {"qdrant_code": "code", "qdrant_episodes": "eps"})()  # type: ignore[assignment]
    from code_memory.embed import HybridVec, SparseVec

    def _embed(self: object, texts: list[str]) -> list[HybridVec]:
        return [
            HybridVec(dense=[0.0] * 8, sparse=SparseVec(indices=[], values=[]))
            for _ in texts
        ]

    pipe.embedder = type(
        "E",
        (),
        {"embed": _embed, "embed_one": lambda self, t: None},
    )()  # type: ignore[assignment]
    return pipe, pipe.graph  # type: ignore[return-value]


def test_ingest_file_threads_head_sha_into_upserts(
    pipeline_with_fakes: tuple[pipeline_mod.Pipeline, _RecordingGraph],
    tmp_path: Path,
) -> None:
    pipe, graph = pipeline_with_fakes
    ex = _ex(tmp_path)
    pipe.ingest_file(ex, head_sha="deadbeef", head_ord=42)
    upserts = [c for c in graph.calls if c[0] in {"upsert_nodes", "upsert_edges"}]
    assert upserts, "expected at least one upsert"
    for _name, kw in upserts:
        assert kw["head_sha"] == "deadbeef", f"head_sha not threaded: {kw}"
        assert kw["head_ord"] == 42, f"head_ord not threaded: {kw}"


def test_ingest_file_without_head_sha_keeps_legacy_path(
    pipeline_with_fakes: tuple[pipeline_mod.Pipeline, _RecordingGraph],
    tmp_path: Path,
) -> None:
    """Backwards-compat: callers that don't pass head_sha still work."""
    pipe, graph = pipeline_with_fakes
    ex = _ex(tmp_path)
    pipe.ingest_file(ex)
    for _name, kw in graph.calls:
        if "head_sha" in kw:
            assert kw["head_sha"] is None
            assert kw["head_ord"] is None


def test_reingest_file_tombstones_with_head_sha(
    pipeline_with_fakes: tuple[pipeline_mod.Pipeline, _RecordingGraph],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe, graph = pipeline_with_fakes
    f = tmp_path / "x.cs"
    f.write_text("public class C {}", encoding="utf-8")
    # Stub out the actual extractor so we don't tree-sitter parse.
    from code_memory.extractor import treesitter

    def fake_extract(_p: object) -> ExtractedFile:
        return _ex(tmp_path)

    monkeypatch.setattr(treesitter, "extract_file", fake_extract)

    pipe.reingest_file(f, head_sha="cafef00d", head_ord=99)
    deletes = [c for c in graph.calls if c[0] == "delete_file"]
    assert deletes, "reingest must call delete_file"
    assert deletes[0][1]["head_sha"] == "cafef00d"
    assert deletes[0][1]["head_ord"] == 99


def test_resolve_head_returns_none_outside_git(tmp_path: Path) -> None:
    """``_resolve_head`` on a non-git dir must not raise; callers rely on
    (None, None) so the storage layer skips stamping cleanly."""
    sha, ord_ = pipeline_mod._resolve_head(tmp_path)
    assert sha is None
    assert ord_ is None


def test_resolve_head_returns_sha_and_ord_in_git_repo(tmp_path: Path) -> None:
    """Real git roundtrip with a fresh repo + one commit."""
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
        )

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    (tmp_path / "a.txt").write_text("hello")
    run("add", "a.txt")
    run("commit", "-qm", "init")

    sha, ord_ = pipeline_mod._resolve_head(tmp_path)
    assert sha is not None
    assert len(sha) == 40  # full SHA
    assert ord_ == 1
