"""Tests for the ingest health check and _count_ingestable_files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_memory.orchestrator.ingest_state import IngestState
from code_memory.orchestrator import pipeline as pipeline_mod


class _FakeGraph:
    """Stand-in for FalkorStore that returns a configurable symbol count."""

    def __init__(self, symbol_count: int = 0) -> None:
        self._symbol_count = symbol_count

    def count_symbols(self) -> int:
        return self._symbol_count

    def ensure_indexes(self) -> None:
        pass

    def query(self, q: str, params: dict[str, Any] | None = None) -> Any:
        class _R:
            result_set: list[Any] = []
        return _R()

    @property
    def graph(self) -> _FakeGraph:
        return self


def _make_pipe(cfg_override: object | None = None) -> pipeline_mod.Pipeline:
    """Minimal Pipeline with faked deps and a config override."""
    pipe = pipeline_mod.Pipeline.__new__(pipeline_mod.Pipeline)
    pipe.graph = _FakeGraph()
    pipe.slug = "test"
    pipe._health_check_reason = None
    if cfg_override is not None:
        pipe.cfg = cfg_override  # type: ignore[assignment]
    return pipe


# -- _count_ingestable_files ------------------------------------------------


def test_count_ingestable_files_empty_dir(tmp_path: Path) -> None:
    n = pipeline_mod._count_ingestable_files(tmp_path)
    assert n == 0


def test_count_ingestable_files_mixed_extensions(tmp_path: Path) -> None:
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.cs").write_text("")
    (tmp_path / "d.txt").write_text("")       # not in LANG_BY_EXT
    (tmp_path / "e.md").write_text("")        # not in LANG_BY_EXT
    n = pipeline_mod._count_ingestable_files(tmp_path)
    assert n == 3  # .ts, .py, .cs


def test_count_ingestable_files_ignores_dot_git(tmp_path: Path) -> None:
    (tmp_path / "a.ts").write_text("")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "objects").mkdir()
    n = pipeline_mod._count_ingestable_files(tmp_path)
    assert n == 1


def test_count_ingestable_files_ignores_default_ignore_dirs(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.ts").write_text("")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "dep.ts").write_text("")
    n = pipeline_mod._count_ingestable_files(tmp_path)
    assert n == 1


def test_count_ingestable_files_respects_gitignore(tmp_path: Path) -> None:
    # Pattern ``*.gen.*`` matches ``anything.gen.anything`` via fnmatch.
    (tmp_path / ".gitignore").write_text("*.gen.*\n")
    (tmp_path / "main.ts").write_text("")
    (tmp_path / "util.gen.ts").write_text("")
    n = pipeline_mod._count_ingestable_files(tmp_path)
    assert n == 1  # only main.ts
    assert (tmp_path / "util.gen.ts").is_file()  # file still on disk


def test_count_ingestable_files_nested_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("")
    (tmp_path / "src" / "util.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("")
    n = pipeline_mod._count_ingestable_files(tmp_path)
    assert n == 3


# -- _health_check_ok -------------------------------------------------------


def test_health_check_skipped_when_disabled(tmp_path: Path) -> None:
    cfg = type("C", (), {
        "ingest_health_check_enabled": False,
        "ingest_health_check_min_ratio": 0.5,
    })()
    pipe = _make_pipe(cfg)
    prior = IngestState(
        repo_root=str(tmp_path), last_sha="abc", last_ts=1.0,
        file_count=10, symbol_count=5,
    )
    assert pipe._health_check_ok(tmp_path, prior) is True
    assert pipe._health_check_reason is None


def test_health_check_skipped_when_counts_missing(tmp_path: Path) -> None:
    cfg = type("C", (), {
        "ingest_health_check_enabled": True,
        "ingest_health_check_min_ratio": 0.5,
    })()
    pipe = _make_pipe(cfg)
    # Legacy state — no file_count / symbol_count
    prior = IngestState(
        repo_root=str(tmp_path), last_sha="abc", last_ts=1.0,
    )
    assert pipe._health_check_ok(tmp_path, prior) is True


def test_health_check_ok_when_file_count_stable(tmp_path: Path) -> None:
    cfg = type("C", (), {
        "ingest_health_check_enabled": True,
        "ingest_health_check_min_ratio": 0.5,
    })()
    pipe = _make_pipe(cfg)
    # Put a few files so current count ≈ prior count
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "b.ts").write_text("")
    prior = IngestState(
        repo_root=str(tmp_path), last_sha="abc", last_ts=1.0,
        file_count=2, symbol_count=4,
    )
    assert pipe._health_check_ok(tmp_path, prior) is True
    assert pipe._health_check_reason is None


def test_health_check_ok_when_file_count_grows_but_symbol_ratio_healthy(
    tmp_path: Path,
) -> None:
    """File count grew >20 % but graph has enough symbols — still OK."""
    cfg = type("C", (), {
        "ingest_health_check_enabled": True,
        "ingest_health_check_min_ratio": 0.5,
    })()
    graph = _FakeGraph(symbol_count=50)
    pipe = _make_pipe(cfg)
    pipe.graph = graph
    # 100 files in repo but prior had only 10
    for i in range(100):
        (tmp_path / f"file_{i}.ts").write_text("")
    prior = IngestState(
        repo_root=str(tmp_path), last_sha="abc", last_ts=1.0,
        file_count=10, symbol_count=40,
    )
    assert pipe._health_check_ok(tmp_path, prior) is True
    assert pipe._health_check_reason is None


def test_health_check_fails_when_symbol_count_too_low(
    tmp_path: Path,
) -> None:
    """File count grew >20 % and graph has too few symbols — force re-ingest."""
    cfg = type("C", (), {
        "ingest_health_check_enabled": True,
        "ingest_health_check_min_ratio": 0.5,
    })()
    graph = _FakeGraph(symbol_count=3)
    pipe = _make_pipe(cfg)
    pipe.graph = graph
    # 100 files in repo but prior had only 10
    for i in range(100):
        (tmp_path / f"file_{i}.ts").write_text("")
    prior = IngestState(
        repo_root=str(tmp_path), last_sha="abc", last_ts=1.0,
        file_count=10, symbol_count=40,
    )
    assert pipe._health_check_ok(tmp_path, prior) is False
    assert pipe._health_check_reason is not None
    assert "health-check" in pipe._health_check_reason
