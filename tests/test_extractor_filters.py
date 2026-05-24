"""Tests for gitignore + minified-file filtering in the extractor walker."""

from __future__ import annotations

from pathlib import Path

from code_memory.extractor.gitignore import GitignoreMatcher
from code_memory.extractor.treesitter import (
    DEFAULT_IGNORE_DIRS,
    Extractor,
    extract_file,
    looks_minified,
)

REAL_TS = "export function hello(name: string): string { return name; }\n"


def _seed_repo(root: Path) -> None:
    """Create a minimal repo-like tree used by walker tests."""
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "app" / "auth.ts").write_text(REAL_TS)
    (root / ".angular" / "cache" / "vite" / "deps").mkdir(parents=True)
    (root / ".angular" / "cache" / "vite" / "deps" / "dep.js").write_text(REAL_TS)
    (root / "tmp" / "acme-ng-security-18" / "package").mkdir(parents=True)
    (root / "tmp" / "acme-ng-security-18" / "package" / "x.ts").write_text(REAL_TS)
    (root / "dist").mkdir()
    (root / "dist" / "bundle.js").write_text(REAL_TS)


# ---------------------------------------------------------------- minified


def test_looks_minified_detects_long_avg_line() -> None:
    sample = ("a" * 500 + "\n") * 4  # avg line len 500 > threshold
    assert looks_minified(sample)


def test_looks_minified_detects_no_newline() -> None:
    assert looks_minified("a" * 3000)


def test_looks_minified_passes_normal_source() -> None:
    assert not looks_minified(REAL_TS * 10)


def test_extract_file_skips_minified(tmp_path: Path) -> None:
    f = tmp_path / "min.js"
    f.write_text("var a=1;" * 800)  # one giant line of JS
    assert extract_file(f) is None


def test_extract_file_keeps_real_source(tmp_path: Path) -> None:
    f = tmp_path / "ok.ts"
    f.write_text(REAL_TS)
    assert extract_file(f) is not None


# ---------------------------------------------------------------- gitignore


def test_gitignore_matcher_skips_listed_dirs(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".angular/\ntmp/\ndist/\n")
    _seed_repo(tmp_path)
    matcher = GitignoreMatcher.from_root(tmp_path)
    assert matcher.match(tmp_path / ".angular" / "cache" / "vite" / "deps" / "dep.js", is_dir=False)
    assert matcher.match(tmp_path / "tmp" / "acme-ng-security-18" / "package" / "x.ts", is_dir=False)
    assert matcher.match(tmp_path / "dist" / "bundle.js", is_dir=False)
    assert not matcher.match(tmp_path / "src" / "app" / "auth.ts", is_dir=False)


def test_gitignore_matcher_honors_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("logs/\n!logs/keep.log\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "drop.log").write_text("x")
    (tmp_path / "logs" / "keep.log").write_text("x")
    matcher = GitignoreMatcher.from_root(tmp_path)
    assert matcher.match(tmp_path / "logs" / "drop.log", is_dir=False)
    assert not matcher.match(tmp_path / "logs" / "keep.log", is_dir=False)


def test_gitignore_matcher_always_skips_dot_git(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    matcher = GitignoreMatcher.from_root(tmp_path)
    assert matcher.match(tmp_path / ".git" / "config", is_dir=False)


# ---------------------------------------------------------------- walker


def test_walker_skips_angular_cache_and_tmp_by_default(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    paths = {Path(ex.path) for ex in Extractor().walk(tmp_path)}
    src_file = (tmp_path / "src" / "app" / "auth.ts").resolve()
    assert src_file in paths
    for ex_path in paths:
        parts = set(ex_path.parts)
        assert ".angular" not in parts
        assert "tmp" not in parts
        assert "dist" not in parts


def test_walker_respects_gitignore_for_unlisted_dirs(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    # add a dir not in DEFAULT_IGNORE_DIRS and gitignore it
    custom = tmp_path / "weird_artifacts"
    custom.mkdir()
    (custom / "x.ts").write_text(REAL_TS)
    assert "weird_artifacts" not in DEFAULT_IGNORE_DIRS
    (tmp_path / ".gitignore").write_text("weird_artifacts/\n")
    paths = {Path(ex.path) for ex in Extractor().walk(tmp_path)}
    assert (custom / "x.ts").resolve() not in paths


def test_walker_can_disable_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("src/\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.ts").write_text(REAL_TS)
    paths = {Path(ex.path) for ex in Extractor(respect_gitignore=False).walk(tmp_path)}
    assert (tmp_path / "src" / "auth.ts").resolve() in paths
