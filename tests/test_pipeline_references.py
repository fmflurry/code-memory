"""Pipeline emits REFERENCES edges for type-position name refs.

Regression guard: type references collected by the extractor must reach
the graph as REFERENCES edges (File → Symbol{name::X}, unresolved). The
resolver later rewrites the placeholder to the real defined symbol.

Without this plumbing, ``code-memory callers`` on a C# interface
returns zero results because nothing in the graph connects the
interface to its implementers / parameter-type users.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from code_memory.extractor.treesitter import Call, ExtractedFile
from code_memory.orchestrator import pipeline as pipeline_mod


class _CapturingGraph:
    """FalkorStore stand-in that captures every upserted node + edge."""

    def __init__(self) -> None:
        self.nodes: list[Any] = []
        self.edges: list[Any] = []

    def ensure_indexes(self) -> None:
        return None

    def upsert_nodes(self, nodes: Any, **_kw: Any) -> None:
        self.nodes.extend(nodes)

    def upsert_edges(self, edges: Any, **_kw: Any) -> None:
        self.edges.extend(edges)

    def query(self, q: str, params: dict[str, Any] | None = None) -> Any:
        class _R:
            result_set: list[Any] = []

        return _R()


@pytest.fixture
def pipeline_with_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[pipeline_mod.Pipeline, _CapturingGraph]:
    monkeypatch.setattr(
        pipeline_mod.Pipeline, "__init__", lambda self, **kw: None
    )
    pipe = pipeline_mod.Pipeline()
    pipe.graph = _CapturingGraph()  # type: ignore[assignment]
    pipe.vector = type(
        "V",
        (),
        {
            "upsert": lambda self, *a, **kw: None,
            "delete_by_path": lambda self, *a, **kw: None,
        },
    )()  # type: ignore[assignment]
    pipe.cfg = type(
        "C", (), {"qdrant_code": "code", "qdrant_episodes": "eps"}
    )()  # type: ignore[assignment]
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


def _ex_with_refs(tmp_path: Path, refs: list[str]) -> ExtractedFile:
    src_path = tmp_path / "x.cs"
    src_path.write_text("public class C {}", encoding="utf-8")
    return ExtractedFile(
        path=str(src_path),
        lang="csharp",
        symbols=[],
        imports=[],
        calls=[],
        injects=[],
        references=refs,
        source="public class C {}",
        generated=False,
    )


def test_pipeline_emits_references_edges(
    pipeline_with_capture: tuple[pipeline_mod.Pipeline, _CapturingGraph],
    tmp_path: Path,
) -> None:
    pipe, graph = pipeline_with_capture
    ex = _ex_with_refs(tmp_path, ["IFooService", "BusinessResult"])
    pipe.ingest_file(ex)

    ref_edges = [e for e in graph.edges if e.type == "REFERENCES"]
    targets = {e.dst_key for e in ref_edges}
    assert targets == {
        "name::IFooService",
        "name::BusinessResult",
    }
    # Each REFERENCES edge must point at an unresolved placeholder so
    # the resolver can rewrite it later.
    assert all(e.props.get("unresolved") for e in ref_edges)
    # The matching placeholder Symbol nodes must be emitted too.
    placeholder_keys = {
        n.key for n in graph.nodes
        if n.label == "Symbol" and n.key.startswith("name::")
    }
    assert {"name::IFooService", "name::BusinessResult"} <= placeholder_keys


def test_pipeline_dedupes_repeated_references(
    pipeline_with_capture: tuple[pipeline_mod.Pipeline, _CapturingGraph],
    tmp_path: Path,
) -> None:
    """If a file references the same type 10 times, emit one edge, not 10."""
    pipe, graph = pipeline_with_capture
    ex = _ex_with_refs(tmp_path, ["IFoo"] * 10)
    pipe.ingest_file(ex)
    ref_edges = [e for e in graph.edges if e.type == "REFERENCES"]
    assert len(ref_edges) == 1


def test_pipeline_keeps_references_independent_from_calls(
    pipeline_with_capture: tuple[pipeline_mod.Pipeline, _CapturingGraph],
    tmp_path: Path,
) -> None:
    """REFERENCES and CALLS use distinct edge types — they never merge."""
    pipe, graph = pipeline_with_capture
    src_path = tmp_path / "x.cs"
    src_path.write_text("public class C {}", encoding="utf-8")
    ex = ExtractedFile(
        path=str(src_path),
        lang="csharp",
        symbols=[],
        imports=[],
        calls=[Call(name="Run", arity=0)],
        injects=[],
        references=["IFoo"],
        source="public class C {}",
        generated=False,
    )
    pipe.ingest_file(ex)
    edge_types = {e.type for e in graph.edges}
    assert "CALLS" in edge_types
    assert "REFERENCES" in edge_types
