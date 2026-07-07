"""Docker command resolution — no Docker Desktop required.

The updater shells out to ``docker`` (compose pull/up, inspect, rm). That
works with any engine whose CLI is on PATH — Docker Desktop, docker-ce,
Colima — but on Windows the recommended Desktop-free setup is docker-ce
running *inside* WSL2, where no ``docker.exe`` exists on the Windows side.
There the CLI is still one hop away: ``wsl -e docker ...``.

This module resolves, once per process, how to reach a working daemon:

1. ``docker`` on PATH with a live daemon (``docker info``) — native. Docker
   Desktop and friends stay first-class.
2. Windows only: ``wsl -e docker info`` — docker-ce inside the default WSL2
   distro (or ``CODEMEMORY_WSL_DISTRO`` to pin one).

When commands run through WSL, Windows paths passed as arguments (compose
``-f`` / ``--project-directory``) must be translated to ``/mnt/c/...`` —
:func:`to_docker_path` does that via ``wslpath`` with a pure-Python
fallback. Paths *read back from docker* (compose labels) are already
daemon-side; validate those with :func:`docker_path_exists`, never with
``Path.exists()`` on the Windows side.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal["native", "wsl", "daemon-down", "none"]

# `docker info` answers in well under a second against a live daemon; the
# generous ceilings cover Docker Desktop mid-start (native) and a cold-boot
# of the WSL2 VM (wsl) without hanging the updater forever.
_NATIVE_TIMEOUT = 15.0
_WSL_TIMEOUT = 20.0
_AUX_TIMEOUT = 10.0

_DETAIL_DAEMON_DOWN = (
    "docker CLI found but no daemon reachable — start it "
    "(WSL2: any `wsl` command boots the VM; macOS: `colima start`; "
    "or start Docker Desktop if that's what you use)"
)
_DETAIL_NONE = (
    "no docker found (native or WSL) — "
    "see README § Docker without Docker Desktop"
)


@dataclass(frozen=True)
class DockerResolution:
    argv: tuple[str, ...] | None
    kind: Kind
    detail: str


_CACHED: DockerResolution | None = None
_PATH_CACHE: dict[str, str] = {}


def _is_windows() -> bool:
    return sys.platform == "win32"


def _wsl_base() -> tuple[str, ...]:
    distro = os.environ.get("CODEMEMORY_WSL_DISTRO", "").strip()
    return ("wsl", "-d", distro) if distro else ("wsl",)


def _run_quiet(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Run a probe command; None on missing binary, timeout, or spawn error.

    ``WSL_UTF8=1`` keeps wsl.exe's own diagnostics decodable (they are
    UTF-16LE by default); harmless for non-wsl commands.
    """
    resolved = shutil.which(cmd[0])
    if resolved is None:
        return None
    try:
        return subprocess.run(
            [resolved, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "WSL_UTF8": "1"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _ok(cmd: list[str], *, timeout: float) -> bool:
    p = _run_quiet(cmd, timeout=timeout)
    return p is not None and p.returncode == 0


def _resolve() -> DockerResolution:
    native_cli = shutil.which("docker") is not None
    if native_cli and _ok(["docker", "info"], timeout=_NATIVE_TIMEOUT):
        return DockerResolution(("docker",), "native", "docker (native daemon)")

    if _is_windows():
        prefix = (*_wsl_base(), "-e", "docker")
        if shutil.which("wsl") and _ok([*prefix, "info"], timeout=_WSL_TIMEOUT):
            return DockerResolution(prefix, "wsl", f"docker via WSL (`{' '.join(prefix)}`)")

    if native_cli:
        return DockerResolution(None, "daemon-down", _DETAIL_DAEMON_DOWN)
    return DockerResolution(None, "none", _DETAIL_NONE)


def resolve_docker(*, refresh: bool = False) -> DockerResolution:
    """How to reach a working docker daemon, cached per process."""
    global _CACHED
    if _CACHED is None or refresh:
        _CACHED = _resolve()
    return _CACHED


def docker_argv() -> list[str] | None:
    """Argv prefix for docker commands (``[..., "compose", ...]``), or None."""
    res = resolve_docker()
    return list(res.argv) if res.argv else None


def to_docker_path(path: str | Path) -> str:
    """Translate a Windows path for the resolved docker; identity when native.

    Only for paths that originate on this machine's filesystem. Paths read
    back from docker (compose labels) are already daemon-side — pass those
    verbatim and check them with :func:`docker_path_exists`.
    """
    raw = str(path)
    if resolve_docker().kind != "wsl":
        return raw
    hit = _PATH_CACHE.get(raw)
    if hit is not None:
        return hit
    p = _run_quiet([*_wsl_base(), "-e", "wslpath", "-a", raw], timeout=_AUX_TIMEOUT)
    if p is not None and p.returncode == 0 and p.stdout.strip():
        out = p.stdout.strip()
    else:
        out = _mnt_fallback(raw)
    _PATH_CACHE[raw] = out
    return out


def _mnt_fallback(raw: str) -> str:
    # C:\Users\x -> /mnt/c/Users/x — enough for the default automount root.
    if len(raw) >= 2 and raw[1] == ":":
        return "/mnt/" + raw[0].lower() + raw[2:].replace("\\", "/")
    return raw.replace("\\", "/")


def docker_path_exists(path: str | Path) -> bool:
    """Existence check on the daemon's side of the filesystem boundary."""
    if resolve_docker().kind != "wsl":
        return Path(path).exists()
    return _ok([*_wsl_base(), "-e", "test", "-e", str(path)], timeout=_AUX_TIMEOUT)


def _reset_cache() -> None:
    global _CACHED
    _CACHED = None
    _PATH_CACHE.clear()
