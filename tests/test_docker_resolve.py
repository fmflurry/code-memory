"""Tests for the docker-command resolution helper (``code_memory._docker``).

All subprocess activity is mocked — no docker, WSL, or network needed.
Guards the Desktop-free contract:

1. A working native ``docker`` wins on every OS (Docker Desktop stays
   first-class).
2. On Windows, a dead/missing native CLI falls back to ``wsl -e docker``;
   POSIX never probes wsl.
3. Failures (missing binary, non-zero exit, timeout) degrade to
   ``daemon-down`` / ``none`` — never an exception.
4. Resolution is cached per process; ``_reset_cache`` re-probes.
5. Path translation: identity when native, ``wslpath -a`` when wsl, with a
   pure-Python ``/mnt/c/...`` fallback.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from code_memory import _docker


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch: pytest.MonkeyPatch):
    _docker._reset_cache()
    monkeypatch.delenv("CODEMEMORY_WSL_DISTRO", raising=False)
    yield
    _docker._reset_cache()


class FakeProc:
    """Route shutil.which / subprocess.run through canned behaviors."""

    def __init__(
        self,
        *,
        on_path: set[str],
        ok_cmds: set[tuple[str, ...]],
        timeout_cmds: set[tuple[str, ...]] = frozenset(),
        stdout: dict[tuple[str, ...], str] | None = None,
    ):
        self.on_path = on_path
        self.ok_cmds = ok_cmds
        self.timeout_cmds = timeout_cmds
        self.stdout = stdout or {}
        self.run_calls: list[tuple[str, ...]] = []

    def which(self, name: str) -> str | None:
        return f"/fake/{name}" if name in self.on_path else None

    def run(self, argv, **kwargs):
        # argv[0] is the which()-resolved path; normalize back to the name.
        key = (argv[0].rsplit("/", 1)[-1], *argv[1:])
        self.run_calls.append(key)
        if key in self.timeout_cmds:
            raise subprocess.TimeoutExpired(cmd=list(key), timeout=kwargs.get("timeout", 0))
        rc = 0 if key in self.ok_cmds else 1
        return SimpleNamespace(returncode=rc, stdout=self.stdout.get(key, ""), stderr="")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_docker.shutil, "which", self.which)
        monkeypatch.setattr(_docker.subprocess, "run", self.run)


def _set_windows(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(_docker, "_is_windows", lambda: value)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_native_docker_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(on_path={"docker"}, ok_cmds={("docker", "info")})
    fake.install(monkeypatch)
    _set_windows(monkeypatch, True)

    res = _docker.resolve_docker()
    assert res.argv == ("docker",)
    assert res.kind == "native"
    assert _docker.docker_argv() == ["docker"]


def test_wsl_fallback_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(
        on_path={"docker", "wsl"},
        ok_cmds={("wsl", "-e", "docker", "info")},
    )
    fake.install(monkeypatch)
    _set_windows(monkeypatch, True)

    res = _docker.resolve_docker()
    assert res.argv == ("wsl", "-e", "docker")
    assert res.kind == "wsl"


def test_wsl_fallback_when_no_native_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(on_path={"wsl"}, ok_cmds={("wsl", "-e", "docker", "info")})
    fake.install(monkeypatch)
    _set_windows(monkeypatch, True)

    res = _docker.resolve_docker()
    assert res.argv == ("wsl", "-e", "docker")
    assert res.kind == "wsl"


def test_no_wsl_probe_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(
        on_path={"docker", "wsl"},
        ok_cmds={("wsl", "-e", "docker", "info")},  # would succeed if probed
    )
    fake.install(monkeypatch)
    _set_windows(monkeypatch, False)

    res = _docker.resolve_docker()
    assert res.kind == "daemon-down"
    assert res.argv is None
    assert not any(call[0] == "wsl" for call in fake.run_calls)


def test_daemon_down_detail_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(on_path={"docker"}, ok_cmds=set())
    fake.install(monkeypatch)
    _set_windows(monkeypatch, False)

    res = _docker.resolve_docker()
    assert res.kind == "daemon-down"
    assert "colima start" in res.detail


def test_nothing_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(on_path=set(), ok_cmds=set())
    fake.install(monkeypatch)
    _set_windows(monkeypatch, True)

    res = _docker.resolve_docker()
    assert res.kind == "none"
    assert res.argv is None
    assert _docker.docker_argv() is None


def test_timeout_is_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(
        on_path={"docker"},
        ok_cmds=set(),
        timeout_cmds={("docker", "info")},
    )
    fake.install(monkeypatch)
    _set_windows(monkeypatch, False)

    res = _docker.resolve_docker()
    assert res.kind == "daemon-down"


def test_distro_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEMEMORY_WSL_DISTRO", "Ubuntu-24.04")
    fake = FakeProc(
        on_path={"wsl"},
        ok_cmds={("wsl", "-d", "Ubuntu-24.04", "-e", "docker", "info")},
    )
    fake.install(monkeypatch)
    _set_windows(monkeypatch, True)

    res = _docker.resolve_docker()
    assert res.argv == ("wsl", "-d", "Ubuntu-24.04", "-e", "docker")


def test_resolution_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(on_path={"docker"}, ok_cmds={("docker", "info")})
    fake.install(monkeypatch)
    _set_windows(monkeypatch, False)

    _docker.resolve_docker()
    calls_after_first = len(fake.run_calls)
    _docker.resolve_docker()
    _docker.docker_argv()
    assert len(fake.run_calls) == calls_after_first

    _docker._reset_cache()
    _docker.resolve_docker()
    assert len(fake.run_calls) > calls_after_first


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------


def _resolve_as_wsl(monkeypatch: pytest.MonkeyPatch, fake: FakeProc) -> None:
    fake.ok_cmds.add(("wsl", "-e", "docker", "info"))
    fake.on_path.add("wsl")
    fake.install(monkeypatch)
    _set_windows(monkeypatch, True)
    assert _docker.resolve_docker().kind == "wsl"


def test_to_docker_path_identity_when_native(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(on_path={"docker"}, ok_cmds={("docker", "info")})
    fake.install(monkeypatch)
    _set_windows(monkeypatch, True)

    assert _docker.to_docker_path(r"C:\Users\x\.code-memory") == r"C:\Users\x\.code-memory"


def test_to_docker_path_uses_wslpath(monkeypatch: pytest.MonkeyPatch) -> None:
    win = r"C:\Users\x\.code-memory\docker\docker-compose.yml"
    wslpath_cmd = ("wsl", "-e", "wslpath", "-a", win)
    fake = FakeProc(
        on_path=set(),
        ok_cmds={wslpath_cmd},
        stdout={wslpath_cmd: "/mnt/c/Users/x/.code-memory/docker/docker-compose.yml\n"},
    )
    _resolve_as_wsl(monkeypatch, fake)

    assert _docker.to_docker_path(win) == "/mnt/c/Users/x/.code-memory/docker/docker-compose.yml"
    # Second lookup served from the cache — no extra wslpath spawn.
    n = len(fake.run_calls)
    _docker.to_docker_path(win)
    assert len(fake.run_calls) == n


def test_to_docker_path_fallback_when_wslpath_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(on_path=set(), ok_cmds=set())
    _resolve_as_wsl(monkeypatch, fake)

    assert _docker.to_docker_path(r"C:\Users\x\.code-memory") == "/mnt/c/Users/x/.code-memory"


def test_docker_path_exists_via_wsl_test(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon_side = "/mnt/c/Users/x/.code-memory/docker/docker-compose.yml"
    fake = FakeProc(on_path=set(), ok_cmds={("wsl", "-e", "test", "-e", daemon_side)})
    _resolve_as_wsl(monkeypatch, fake)

    assert _docker.docker_path_exists(daemon_side) is True
    assert _docker.docker_path_exists("/mnt/c/absent") is False


def test_docker_path_exists_native(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProc(on_path={"docker"}, ok_cmds={("docker", "info")})
    fake.install(monkeypatch)
    _set_windows(monkeypatch, False)

    real = tmp_path / "compose.yml"
    real.write_text("services: {}\n")
    assert _docker.docker_path_exists(real) is True
    assert _docker.docker_path_exists(tmp_path / "absent.yml") is False
