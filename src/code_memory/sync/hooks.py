"""Git hooks installer: post-checkout / post-merge / post-rewrite / post-commit.

Each hook fires ``code-memory sync`` in the background. Hooks are
idempotent — the installer detects an existing code-memory block and
overwrites it without disturbing the rest of the file. Plain ``rm`` of
the marker block removes our integration cleanly.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

HOOKS = ("post-checkout", "post-merge", "post-rewrite", "post-commit", "post-applypatch")

MARKER_START = "# >>> code-memory sync >>>"
MARKER_END = "# <<< code-memory sync <<<"


@dataclass(frozen=True)
class HookInstallResult:
    installed: list[str]
    skipped: list[str]
    hooks_dir: str


def install_hooks(repo: Path, *, trigger_cmd: str | None = None) -> HookInstallResult:
    """Install code-memory hooks into ``repo``.

    Returns lists of hook names that were (re)written vs skipped.
    """
    repo = Path(repo).resolve()
    hooks_dir = _hooks_dir(repo)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    trigger = trigger_cmd or _default_trigger()
    installed: list[str] = []
    skipped: list[str] = []
    for hook in HOOKS:
        path = hooks_dir / hook
        block = _block_for(hook, trigger)
        if path.exists():
            current = path.read_text()
            if MARKER_START in current and MARKER_END in current:
                new = _replace_block(current, block)
                if new == current:
                    skipped.append(hook)
                    continue
            else:
                new = current.rstrip() + "\n\n" + block + "\n"
        else:
            new = "#!/usr/bin/env bash\nset -euo pipefail\n\n" + block + "\n"
        path.write_text(new)
        _chmod_exec(path)
        installed.append(hook)
    return HookInstallResult(
        installed=installed,
        skipped=skipped,
        hooks_dir=str(hooks_dir),
    )


def uninstall_hooks(repo: Path) -> HookInstallResult:
    repo = Path(repo).resolve()
    hooks_dir = _hooks_dir(repo)
    removed: list[str] = []
    skipped: list[str] = []
    for hook in HOOKS:
        path = hooks_dir / hook
        if not path.exists():
            skipped.append(hook)
            continue
        content = path.read_text()
        if MARKER_START not in content:
            skipped.append(hook)
            continue
        stripped = _strip_block(content)
        if stripped.strip() in ("", "#!/usr/bin/env bash", "#!/usr/bin/env bash\nset -euo pipefail"):
            path.unlink()
        else:
            path.write_text(stripped)
            _chmod_exec(path)
        removed.append(hook)
    return HookInstallResult(
        installed=removed,
        skipped=skipped,
        hooks_dir=str(hooks_dir),
    )


def hook_status(repo: Path) -> dict[str, bool]:
    hooks_dir = _hooks_dir(Path(repo).resolve())
    out: dict[str, bool] = {}
    for hook in HOOKS:
        path = hooks_dir / hook
        out[hook] = path.is_file() and MARKER_START in path.read_text(errors="ignore")
    return out


# ---------------------------------------------------------------------------


def _hooks_dir(repo: Path) -> Path:
    """Return the hooks directory honouring ``core.hooksPath`` when set."""
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    custom = out.stdout.strip()
    if custom:
        p = Path(custom)
        if not p.is_absolute():
            p = repo / p
        return p
    return repo / ".git" / "hooks"


def _default_trigger() -> str:
    """The command embedded in each hook.

    Background + disown so git never blocks on the sync. Stderr -> log
    file so failures are observable.
    """
    log_path = "$HOME/.cache/codememory/hook.log"
    return (
        f"mkdir -p \"$(dirname {log_path})\" && "
        f"( code-memory sync . --trigger \"$HOOK\" "
        f">> {log_path} 2>&1 & ) "
        "; disown 2>/dev/null || true"
    )


def _block_for(hook: str, trigger: str) -> str:
    return (
        f"{MARKER_START}\n"
        f"HOOK={hook}\n"
        f"command -v code-memory >/dev/null 2>&1 && {{ {trigger}; }}\n"
        f"{MARKER_END}"
    )


def _replace_block(content: str, block: str) -> str:
    start = content.index(MARKER_START)
    end = content.index(MARKER_END, start) + len(MARKER_END)
    return content[:start] + block + content[end:]


def _strip_block(content: str) -> str:
    start = content.index(MARKER_START)
    end = content.index(MARKER_END, start) + len(MARKER_END)
    return (content[:start].rstrip() + "\n" + content[end:].lstrip()).strip() + "\n"


def _chmod_exec(path: Path) -> None:
    if os.name == "nt":
        return  # Git for Windows uses bash; chmod n/a
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
