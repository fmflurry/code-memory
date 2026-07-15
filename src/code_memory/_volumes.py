"""Cross-engine docker volume migration (Docker Desktop → docker-ce in WSL2).

When a Windows machine switches engines, the FalkorDB/Qdrant volumes stay
behind on the Docker Desktop engine — the index does not follow. Re-ingesting
rebuilds it, but on large repos that costs hours; copying the volumes takes
minutes.

The trick that makes this automatable: on Windows both engines can run at
the same time — Docker Desktop over its named pipe (``docker``), docker-ce
over ``wsl -e docker``. Each volume is streamed container-to-container
(``tar cz`` on the source engine piped into ``tar xz`` on the target), so no
intermediate file and no engine start/stop juggling beyond bringing Desktop
up for the read.

Safety rails:
* engine identity check (``docker info --format {{.ID}}``) — Docker
  Desktop's WSL integration can make both CLIs reach the *same* engine, in
  which case there is nothing to migrate and wiping would destroy the data;
* explicit confirmation before target volumes are overwritten;
* containers stopped on both engines during the copy.

``offer_volume_migration`` is the ``code-memory update`` hook: it detects
the switch, asks migrate / re-ingest / skip, and remembers the decision in
``~/.code-memory/.volume-migration-resolved`` so update stops nagging.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ._docker import _is_windows, _wsl_base, resolve_docker

NATIVE_PREFIX = ("docker",)
VOLUME_SUFFIXES = ("falkor_data", "qdrant_data")
TARGET_PROJECT = "code-memory"
CONTAINERS = ("cm-falkordb", "cm-qdrant", "cm-tei")
MARKER_NAME = ".volume-migration-resolved"

# Copying multi-GB volumes through a pipe has no useful upper bound; only
# the control commands get one.
_CTL_TIMEOUT = 120.0


def _home() -> Path:
    return Path(os.environ.get("CODEMEMORY_HOME", str(Path.home() / ".code-memory")))


def _wsl_docker() -> tuple[str, ...]:
    return (*_wsl_base(), "-e", "docker")


def _run(cmd: list[str], *, timeout: float = _CTL_TIMEOUT) -> subprocess.CompletedProcess[str] | None:
    resolved = shutil.which(cmd[0])
    if resolved is None:
        return None
    try:
        return subprocess.run(
            [resolved, *cmd[1:]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "WSL_UTF8": "1"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _engine_id(prefix: tuple[str, ...]) -> str | None:
    p = _run([*prefix, "info", "--format", "{{.ID}}"], timeout=30)
    if p is None or p.returncode != 0:
        return None
    return p.stdout.strip() or None


def _list_volumes(prefix: tuple[str, ...]) -> list[str]:
    p = _run([*prefix, "volume", "ls", "--format", "{{.Name}}"])
    if p is None or p.returncode != 0:
        return []
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]


def match_source_volumes(names: list[str]) -> dict[str, str]:
    """Map volume-name suffix → source volume, first match wins.

    Desktop-side volumes are ``<project>_falkor_data`` etc. where
    ``<project>`` depends on whoever ran compose first — match by suffix
    rather than assuming a project name.
    """
    out: dict[str, str] = {}
    for name in names:
        for suffix in VOLUME_SUFFIXES:
            if name.endswith(suffix) and suffix not in out:
                out[suffix] = name
    return out


def _try_start_desktop() -> None:
    """Best-effort Docker Desktop launch; the caller polls for the engine."""
    # Recent Desktop ships a `docker desktop start` CLI subcommand.
    p = _run(["docker", "desktop", "start"], timeout=60)
    if p is not None and p.returncode == 0:
        return
    for root in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if not root:
            continue
        for rel in ("Docker\\Docker\\Docker Desktop.exe", "Programs\\DockerDesktop\\Docker Desktop.exe"):
            exe = Path(root) / rel
            if exe.exists():
                try:
                    subprocess.Popen([str(exe)])
                except OSError:
                    continue
                return


def _wait_for_engine(prefix: tuple[str, ...], *, attempts: int = 24) -> str | None:
    import time

    for _ in range(attempts):
        eid = _engine_id(prefix)
        if eid:
            return eid
        time.sleep(5)
    return None


def _stream_volume(
    src_prefix: tuple[str, ...],
    dst_prefix: tuple[str, ...],
    src_vol: str,
    dst_vol: str,
) -> tuple[bool, str]:
    """Pipe the volume contents source-engine → target-engine, no temp file."""
    src_exe = shutil.which(src_prefix[0])
    dst_exe = shutil.which(dst_prefix[0])
    if src_exe is None or dst_exe is None:
        return False, "docker/wsl not on PATH"
    src_cmd = [src_exe, *src_prefix[1:], "run", "--rm", "-v", f"{src_vol}:/from:ro",
               "alpine", "tar", "cz", "-C", "/from", "."]
    dst_cmd = [dst_exe, *dst_prefix[1:], "run", "--rm", "-i", "-v", f"{dst_vol}:/to",
               "alpine", "sh", "-c", "find /to -mindepth 1 -delete && tar xz -C /to"]
    env = {**os.environ, "WSL_UTF8": "1"}
    try:
        src = subprocess.Popen(src_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        dst = subprocess.Popen(dst_cmd, stdin=src.stdout, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, env=env)
    except OSError as exc:
        return False, f"spawn failed: {exc}"
    assert src.stdout is not None
    src.stdout.close()  # let the source see EPIPE if the target dies
    _, dst_err = dst.communicate()
    src_err = src.stderr.read() if src.stderr else b""
    src.wait()
    if src.returncode != 0:
        return False, f"source tar failed: {src_err.decode('utf-8', 'replace').strip()}"
    if dst.returncode != 0:
        return False, f"target untar failed: {dst_err.decode('utf-8', 'replace').strip()}"
    return True, "ok"


def migrate_volumes(*, assume_yes: bool = False) -> tuple[bool, str]:
    """Copy FalkorDB + Qdrant volumes from the Docker Desktop engine to WSL."""
    wsl = _wsl_docker()

    dst_id = _engine_id(wsl)
    if dst_id is None:
        return False, "WSL docker engine not reachable (`wsl -e docker info` fails)"

    src_id = _engine_id(NATIVE_PREFIX)
    if src_id is None:
        print("  starting Docker Desktop (needed to read the old volumes — this can take a minute)...")
        _try_start_desktop()
        src_id = _wait_for_engine(NATIVE_PREFIX)
        if src_id is None:
            return False, "Docker Desktop engine not reachable — start it manually, then re-run `code-memory update --migrate-volumes`"

    if src_id == dst_id:
        return False, (
            "both CLIs reach the SAME engine (Docker Desktop WSL integration is likely "
            "routing `wsl -e docker` to Desktop) — nothing to migrate; disable the "
            "integration for the distro or stop Desktop and retry"
        )

    sources = match_source_volumes(_list_volumes(NATIVE_PREFIX))
    if not sources:
        return False, "no *falkor_data / *qdrant_data volumes on the Docker Desktop engine"

    plan = {suffix: (src, f"{TARGET_PROJECT}_{suffix}") for suffix, src in sources.items()}
    print("  migration plan (Docker Desktop → WSL engine):")
    for src, dst in plan.values():
        print(f"    {src}  →  {dst}  (target contents will be REPLACED)")
    if not assume_yes:
        try:
            answer = input("  Proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            return False, "cancelled"

    # Quiesce both sides so the copy is consistent.
    _run([*NATIVE_PREFIX, "stop", *CONTAINERS])
    _run([*wsl, "stop", *CONTAINERS])

    for suffix, (src, dst) in plan.items():
        _run([*wsl, "volume", "create", dst])
        ok, msg = _stream_volume(NATIVE_PREFIX, wsl, src, dst)
        if not ok:
            _run([*wsl, "start", "cm-falkordb", "cm-qdrant"])
            return False, f"{src} → {dst}: {msg}"
        print(f"  copied {src} → {dst}")

    _run([*wsl, "start", "cm-falkordb", "cm-qdrant"])
    return True, (
        f"{len(plan)} volume(s) migrated; containers restarted on the WSL engine "
        "(Docker Desktop can be stopped/uninstalled now)"
    )


def offer_volume_migration(*, force: bool = False) -> int:
    """``code-memory update`` hook: detect an engine switch and ask once.

    Returns a non-zero exit contribution only when an attempted migration
    fails. Windows-only; a no-op everywhere else.
    """
    if not _is_windows():
        return 0
    if resolve_docker().kind != "wsl":
        return 0  # not running on the WSL engine — no switch to handle
    if shutil.which("docker") is None:
        return 0  # no Docker Desktop CLI left — nothing to migrate from

    marker = _home() / MARKER_NAME
    if marker.exists() and not force:
        return 0

    try:
        is_tty = sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        is_tty = False
    if not is_tty:
        print("  Volumes: your previous Docker Desktop volumes may still hold the index —")
        print("           run `code-memory update --migrate-volumes` in a terminal to copy them, or re-ingest.")
        return 0

    print()
    print("  Engine switch detected: docker now runs via WSL and Docker Desktop is still installed.")
    print("  The code index lives in docker volumes and did NOT follow the engine change.")
    print("    [m] migrate    — copy the FalkorDB + Qdrant volumes over (starts Docker Desktop temporarily)")
    print("    [r] re-ingest  — skip the copy; rebuild with `code-memory ingest <repo>` (episodes SQLite is unaffected)")
    print("    [s] skip       — decide later (asked again on next update)")
    try:
        answer = input("  Choice [m/r/S]: ").strip().lower()
    except EOFError:
        answer = ""

    if answer in ("m", "migrate"):
        ok, msg = migrate_volumes()
        print(f"  Volumes: {'ok' if ok else 'FAILED'} — {msg}")
        if ok:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("migrated\n", encoding="utf-8")
            return 0
        return 1
    if answer in ("r", "reingest", "re-ingest"):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("reingest\n", encoding="utf-8")
        print("  Re-ingest chosen — run `code-memory ingest <repo>` for each repo you index.")
        return 0
    print("  Skipped — you'll be asked again on the next `code-memory update`.")
    return 0
