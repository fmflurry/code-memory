"""Tests for the Desktop→WSL volume migration (``code_memory._volumes``).

Everything mocked — no docker, no WSL, no TTY. Guards:

1. ``offer_volume_migration`` triggers only on Windows + wsl resolution +
   Desktop CLI present, and honors the decision marker.
2. Non-interactive runs never prompt — they print the hint and leave the
   marker unset so an interactive run can ask later.
3. The engine-identity guard refuses to "migrate" a volume onto itself
   (Docker Desktop WSL integration routing both CLIs to one engine).
4. Suffix-based source-volume matching (project prefix unknown).
5. The copy pipeline quiesces containers, streams every planned volume,
   and restarts the WSL side.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from code_memory import _volumes
from code_memory._docker import DockerResolution

WSL_RES = DockerResolution(("wsl", "-e", "docker"), "wsl", "docker via WSL")
NATIVE_RES = DockerResolution(("docker",), "native", "docker (native daemon)")


@pytest.fixture()
def windows_wsl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(_volumes, "_is_windows", lambda: True)
    monkeypatch.setattr(_volumes, "resolve_docker", lambda **kw: WSL_RES)
    monkeypatch.setattr(_volumes.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(_volumes, "_home", lambda: tmp_path)
    return tmp_path


def _tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(_volumes.sys, "stdin", SimpleNamespace(isatty=lambda: value))


# ---------------------------------------------------------------------------
# offer_volume_migration gating
# ---------------------------------------------------------------------------


def test_offer_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_volumes, "_is_windows", lambda: False)
    assert _volumes.offer_volume_migration() == 0


def test_offer_noop_when_native_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_volumes, "_is_windows", lambda: True)
    monkeypatch.setattr(_volumes, "resolve_docker", lambda **kw: NATIVE_RES)
    assert _volumes.offer_volume_migration() == 0


def test_offer_noop_when_marker_present(windows_wsl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (windows_wsl / _volumes.MARKER_NAME).write_text("reingest\n", encoding="utf-8")
    _tty(monkeypatch, True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("must not prompt when marker exists")
    )
    assert _volumes.offer_volume_migration() == 0


def test_offer_non_tty_prints_hint_only(
    windows_wsl: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tty(monkeypatch, False)
    assert _volumes.offer_volume_migration() == 0
    out = capsys.readouterr().out
    assert "--migrate-volumes" in out
    assert not (windows_wsl / _volumes.MARKER_NAME).exists()  # ask again interactively


def test_offer_reingest_writes_marker(windows_wsl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a: "r")
    assert _volumes.offer_volume_migration() == 0
    assert (windows_wsl / _volumes.MARKER_NAME).read_text(encoding="utf-8") == "reingest\n"


def test_offer_skip_leaves_no_marker(windows_wsl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert _volumes.offer_volume_migration() == 0
    assert not (windows_wsl / _volumes.MARKER_NAME).exists()


def test_offer_migrate_success_writes_marker(
    windows_wsl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a: "m")
    monkeypatch.setattr(_volumes, "migrate_volumes", lambda **kw: (True, "2 volume(s) migrated"))
    assert _volumes.offer_volume_migration() == 0
    assert (windows_wsl / _volumes.MARKER_NAME).read_text(encoding="utf-8") == "migrated\n"


def test_offer_migrate_failure_keeps_asking(
    windows_wsl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a: "m")
    monkeypatch.setattr(_volumes, "migrate_volumes", lambda **kw: (False, "boom"))
    assert _volumes.offer_volume_migration() == 1
    assert not (windows_wsl / _volumes.MARKER_NAME).exists()


# ---------------------------------------------------------------------------
# migrate_volumes
# ---------------------------------------------------------------------------


def test_migrate_refuses_same_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_volumes, "_engine_id", lambda prefix: "SAME-ID")
    ok, msg = _volumes.migrate_volumes(assume_yes=True)
    assert ok is False
    assert "SAME engine" in msg


def test_migrate_no_source_volumes(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = {("docker",): "SRC", ("wsl", "-e", "docker"): "DST"}
    monkeypatch.setattr(_volumes, "_engine_id", lambda prefix: ids.get(tuple(prefix)))
    monkeypatch.setattr(_volumes, "_list_volumes", lambda prefix: ["random_thing"])
    ok, msg = _volumes.migrate_volumes(assume_yes=True)
    assert ok is False
    assert "no *falkor_data" in msg


def test_migrate_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = {("docker",): "SRC", ("wsl", "-e", "docker"): "DST"}
    monkeypatch.setattr(_volumes, "_engine_id", lambda prefix: ids.get(tuple(prefix)))
    monkeypatch.setattr(
        _volumes, "_list_volumes",
        lambda prefix: ["oldproj_falkor_data", "oldproj_qdrant_data", "unrelated"],
    )
    runs: list[list[str]] = []
    monkeypatch.setattr(
        _volumes, "_run",
        lambda cmd, **kw: runs.append(list(cmd)) or SimpleNamespace(returncode=0, stdout=""),
    )
    streams: list[tuple[str, str]] = []

    def fake_stream(src_prefix, dst_prefix, src, dst):
        streams.append((src, dst))
        return True, "ok"

    monkeypatch.setattr(_volumes, "_stream_volume", fake_stream)

    ok, msg = _volumes.migrate_volumes(assume_yes=True)

    assert ok, msg
    assert ("oldproj_falkor_data", "code-memory_falkor_data") in streams
    assert ("oldproj_qdrant_data", "code-memory_qdrant_data") in streams
    stops = [c for c in runs if "stop" in c]
    assert len(stops) == 2  # both engines quiesced
    assert any(c[:2] == ["wsl", "-e"] and "start" in c for c in runs)  # WSL side restarted


def test_migrate_stream_failure_restarts_target(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = {("docker",): "SRC", ("wsl", "-e", "docker"): "DST"}
    monkeypatch.setattr(_volumes, "_engine_id", lambda prefix: ids.get(tuple(prefix)))
    monkeypatch.setattr(_volumes, "_list_volumes", lambda prefix: ["p_falkor_data"])
    runs: list[list[str]] = []
    monkeypatch.setattr(
        _volumes, "_run",
        lambda cmd, **kw: runs.append(list(cmd)) or SimpleNamespace(returncode=0, stdout=""),
    )
    monkeypatch.setattr(_volumes, "_stream_volume", lambda *a: (False, "pipe broke"))

    ok, msg = _volumes.migrate_volumes(assume_yes=True)

    assert ok is False
    assert "pipe broke" in msg
    assert any(c[:2] == ["wsl", "-e"] and "start" in c for c in runs)


def test_migrate_cancelled_before_any_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = {("docker",): "SRC", ("wsl", "-e", "docker"): "DST"}
    monkeypatch.setattr(_volumes, "_engine_id", lambda prefix: ids.get(tuple(prefix)))
    monkeypatch.setattr(_volumes, "_list_volumes", lambda prefix: ["p_qdrant_data"])
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    monkeypatch.setattr(
        _volumes, "_run", lambda cmd, **kw: pytest.fail("must not touch containers when cancelled")
    )

    ok, msg = _volumes.migrate_volumes()

    assert ok is False
    assert msg == "cancelled"


# ---------------------------------------------------------------------------
# suffix matching
# ---------------------------------------------------------------------------


def test_match_source_volumes_by_suffix() -> None:
    got = _volumes.match_source_volumes(
        ["myproj_falkor_data", "other_qdrant_data", "dup_falkor_data", "noise"]
    )
    assert got == {"falkor_data": "myproj_falkor_data", "qdrant_data": "other_qdrant_data"}
