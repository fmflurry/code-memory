"""Sync decision tree: pure logic, no live storage backends.

These tests exercise the branching in :func:`sync_repo` by monkey-patching
the heavy strategies (full ingest, incremental, snapshot apply) with
recording stubs so we observe which branch was taken without touching
Qdrant or FalkorDB.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_memory.sync import sync as sync_mod


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not on PATH")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD")


@dataclass
class Calls:
    full: int = 0
    incremental: list[str] = None
    pull: list[str] = None
    dirty_only: int = 0
    dirty_deleted_seen: list[list[str]] = None

    def __post_init__(self) -> None:
        self.incremental = []
        self.pull = []
        self.dirty_deleted_seen = []


@pytest.fixture(autouse=True)
def _stub_strategies(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Replace heavy strategies with recording stubs.

    Each strategy returns a SyncResult so the decision tree can keep
    threading data through; the body is a no-op so no live infra is
    needed.
    """
    calls = Calls()

    def fake_full(root, slug, head, branch, store, *, publish):
        calls.full += 1
        return sync_mod.SyncResult(action="full_ingest", head_sha=head)

    def fake_incremental(root, slug, head, branch, *, base, store, publish):
        calls.incremental.append(base)
        return sync_mod.SyncResult(
            action="incremental", head_sha=head, base_sha=base
        )

    def fake_pull(root, slug, head, branch, store, *, publish):
        calls.pull.append(head)
        return sync_mod.SyncResult(
            action="pull_snapshot", head_sha=head, snapshot_sha=head
        )

    def fake_dirty(root, slug, head, dirty, dirty_deleted=()):  # noqa: ANN001
        calls.dirty_only += 1
        calls.dirty_deleted_seen.append([p.name for p in dirty_deleted])
        return sync_mod.SyncResult(action="dirty_only", head_sha=head)

    monkeypatch.setattr(sync_mod, "_run_full_ingest", fake_full)
    monkeypatch.setattr(sync_mod, "_run_incremental", fake_incremental)
    monkeypatch.setattr(sync_mod, "_pull_and_apply", fake_pull)
    monkeypatch.setattr(sync_mod, "_run_dirty_only", fake_dirty)
    return calls


def test_no_prior_no_snapshot_runs_full_ingest(tmp_path: Path, _stub_strategies: Calls) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    result = sync_mod.sync_repo(repo, project="demo", fetch=False)
    assert result.action == "full_ingest"
    assert _stub_strategies.full == 1


def test_clean_head_matches_state_is_noop(
    tmp_path: Path, _stub_strategies: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    head = _init(repo)

    # Inject matching state via the in-memory IngestStateStore
    from code_memory.config import CONFIG
    from code_memory.orchestrator.ingest_state import IngestStateStore

    cfg = CONFIG.for_project("demo")
    state = IngestStateStore(cfg.episodic_db)
    state.set(repo, sha=head, branch="main")
    state.close()

    result = sync_mod.sync_repo(repo, project="demo", fetch=False)
    assert result.action == "noop"
    assert _stub_strategies.full == 0
    assert _stub_strategies.incremental == []


def test_stale_state_triggers_incremental(
    tmp_path: Path, _stub_strategies: Calls
) -> None:
    repo = tmp_path / "repo"
    base = _init(repo)
    # advance HEAD
    (repo / "b.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")

    from code_memory.config import CONFIG
    from code_memory.orchestrator.ingest_state import IngestStateStore

    cfg = CONFIG.for_project("demo2")
    state = IngestStateStore(cfg.episodic_db)
    state.set(repo, sha=base, branch="main")
    state.close()

    result = sync_mod.sync_repo(repo, project="demo2", fetch=False)
    assert result.action == "incremental"
    assert _stub_strategies.incremental == [base]


def test_uncommitted_delete_routes_to_dirty_only(
    tmp_path: Path, _stub_strategies: Calls
) -> None:
    """A plain ``rm`` against a tracked file with no other dirty content
    must still escape the noop branch and reach ``_run_dirty_only`` so
    the file can be torn down from graph + vector. Previously this
    short-circuited as ``noop`` because ``dirty_files`` ignored deletes.
    """
    repo = tmp_path / "repo"
    head = _init(repo)

    # Mark state as up-to-date so we land in the "HEAD matches state" branch.
    from code_memory.config import CONFIG
    from code_memory.orchestrator.ingest_state import IngestStateStore

    cfg = CONFIG.for_project("demo3")
    state = IngestStateStore(cfg.episodic_db)
    state.set(repo, sha=head, branch="main")
    state.close()

    (repo / "a.py").unlink()  # worktree-only delete

    result = sync_mod.sync_repo(repo, project="demo3", fetch=False)
    assert result.action == "dirty_only"
    assert _stub_strategies.dirty_only == 1
    assert _stub_strategies.dirty_deleted_seen == [["a.py"]]
