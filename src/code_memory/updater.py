"""Smart updater for code-memory.

Detects which components are already installed locally and refreshes
only those — never asks the user to re-confirm pieces they already opted
into during the initial one-liner install.

Components inspected:

* CLI install method (``uv tool`` / ``pipx`` / ``pip``) + version vs PyPI
* Docker stack (FalkorDB + Qdrant)
* Ollama models present locally (``bge-m3``, optionally ``gemma2:9b``)
* Optional Python extras (``hybrid`` via ``FlagEmbedding``, ``dotnet`` via ``dnfile``)
* Claude Code plugin + MCP server registration
* OpenCode global npm package

The update flow is intentionally idempotent and noisy-on-change only:
already-current items print one line each, anything actually upgraded
prints its old/new state.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from . import __version__ as _LOCAL_VERSION

PYPI_PACKAGE = "flurryx-code-memory"
DEFAULT_REPO_URL = os.environ.get(
    "CODEMEMORY_REPO_URL", "https://github.com/fmflurry/code-memory"
)
CODEMEMORY_HOME = Path(os.environ.get("CODEMEMORY_HOME", str(Path.home() / ".code-memory")))

InstallMethod = Literal["uv-tool", "pipx", "pip", "editable", "unknown"]


@dataclass
class ComponentState:
    name: str
    present: bool
    detail: str = ""
    current: str | None = None
    latest: str | None = None


@dataclass
class UpdatePlan:
    install_method: InstallMethod
    components: list[ComponentState] = field(default_factory=list)
    cli_current: str = ""
    cli_latest: str = ""


# ---------- detection ----------


def _run(cmd: list[str], *, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, check=check, capture_output=capture, text=True, env={**os.environ}
    )


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def current_cli_version() -> str:
    return _LOCAL_VERSION


def latest_pypi_version(pkg: str = PYPI_PACKAGE, *, timeout: float = 5.0) -> str | None:
    try:
        r = httpx.get(f"https://pypi.org/pypi/{pkg}/json", timeout=timeout)
        r.raise_for_status()
        return r.json()["info"]["version"]
    except Exception:  # noqa: BLE001
        return None


def detect_install_method() -> InstallMethod:
    """Best-effort: where is the running interpreter living?

    Editable installs are detected via PEP 610 ``direct_url.json``;
    uv tool / pipx are detected from ``sys.prefix`` (the venv root),
    not ``sys.executable`` which on macOS often resolves through a
    symlink into Homebrew/conda.
    """
    if _is_editable_install():
        return "editable"

    prefix = str(Path(sys.prefix).resolve()).lower()
    if "/uv/tools/" in prefix or "\\uv\\tools\\" in prefix:
        return "uv-tool"
    if "/pipx/venvs/" in prefix or "\\pipx\\venvs\\" in prefix:
        return "pipx"
    if _have("pip") and _pip_shows():
        return "pip"
    return "unknown"


def _is_editable_install() -> bool:
    try:
        from importlib.metadata import distribution

        d = distribution(PYPI_PACKAGE)
        raw = d.read_text("direct_url.json") or ""
        if not raw:
            return False
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:  # noqa: BLE001
        return False


def _pip_shows() -> bool:
    p = _run([sys.executable, "-m", "pip", "show", PYPI_PACKAGE])
    return p.returncode == 0


def _ollama_models() -> list[str]:
    if not _have("ollama"):
        return []
    p = _run(["ollama", "list"])
    if p.returncode != 0:
        return []
    names: list[str] = []
    for line in p.stdout.splitlines()[1:]:
        first = line.split()
        if first:
            names.append(first[0])
    return names


def _docker_compose_present() -> bool:
    return (CODEMEMORY_HOME / "docker" / "docker-compose.yml").exists()


def _docker_running(service: str) -> bool:
    if not _have("docker"):
        return False
    p = _run(["docker", "ps", "--format", "{{.Names}}"])
    if p.returncode != 0:
        return False
    return any(service in name for name in p.stdout.splitlines())


def _claude_plugin_present() -> bool:
    if not _have("claude"):
        return False
    p = _run(["claude", "plugin", "list"])
    return p.returncode == 0 and "code-memory" in p.stdout


def _claude_mcp_present() -> bool:
    if not _have("claude"):
        return False
    p = _run(["claude", "mcp", "list"])
    return p.returncode == 0 and "code-memory" in p.stdout


def _npm_pkg_present(pkg: str = "code-memory-opencode") -> bool:
    if not _have("npm"):
        return False
    p = _run(["npm", "ls", "-g", "--depth=0", "--json"])
    if p.returncode != 0:
        return False
    try:
        deps = json.loads(p.stdout).get("dependencies", {})
        return pkg in deps
    except Exception:  # noqa: BLE001
        return False


def _python_module_present(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def build_plan() -> UpdatePlan:
    plan = UpdatePlan(install_method=detect_install_method())
    plan.cli_current = current_cli_version()
    plan.cli_latest = latest_pypi_version() or "?"

    falkor_running = _docker_running("falkor")
    qdrant_running = _docker_running("qdrant")
    compose_here = _docker_compose_present()

    plan.components = [
        ComponentState(
            name="CLI (flurryx-code-memory)",
            present=True,
            detail=f"via {plan.install_method}",
            current=plan.cli_current,
            latest=plan.cli_latest,
        ),
        ComponentState(
            name="Docker: FalkorDB",
            present=compose_here or falkor_running,
            detail="running" if falkor_running else ("compose present" if compose_here else "stopped"),
        ),
        ComponentState(
            name="Docker: Qdrant",
            present=compose_here or qdrant_running,
            detail="running" if qdrant_running else ("compose present" if compose_here else "stopped"),
        ),
    ]

    models = _ollama_models()
    for m in ("bge-m3", "gemma2:9b"):
        plan.components.append(
            ComponentState(
                name=f"Ollama: {m}",
                present=any(name.startswith(m) for name in models),
            )
        )

    plan.components.append(
        ComponentState(
            name="Extra: hybrid (FlagEmbedding)",
            present=_python_module_present("FlagEmbedding"),
        )
    )
    plan.components.append(
        ComponentState(
            name="Extra: dotnet (dnfile)",
            present=_python_module_present("dnfile"),
        )
    )

    plan.components.append(
        ComponentState(name="Claude Code plugin", present=_claude_plugin_present())
    )
    plan.components.append(
        ComponentState(name="Claude Code MCP", present=_claude_mcp_present())
    )
    plan.components.append(
        ComponentState(name="OpenCode plugin (npm)", present=_npm_pkg_present())
    )

    return plan


# ---------- upgrade actions ----------


def upgrade_cli(method: InstallMethod, *, bleeding: bool = False) -> tuple[bool, str]:
    """Upgrade the CLI in-place via the same channel it was installed from."""
    source = f"git+{DEFAULT_REPO_URL}" if bleeding else PYPI_PACKAGE
    if method == "uv-tool":
        if bleeding:
            cmd = ["uv", "tool", "install", "--force", "--from", source, "code-memory"]
        else:
            cmd = ["uv", "tool", "upgrade", PYPI_PACKAGE]
    elif method == "pipx":
        cmd = ["pipx", "upgrade", PYPI_PACKAGE] if not bleeding else [
            "pipx", "install", "--force", source
        ]
    elif method == "pip":
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", source]
    elif method == "editable":
        return False, "editable install — `git pull` in the repo to update"
    else:
        return False, "unknown install method — re-run the one-liner installer"
    p = _run(cmd, capture=False)
    return p.returncode == 0, " ".join(cmd)


def upgrade_docker_images() -> tuple[bool, str]:
    if not _docker_compose_present() or not _have("docker"):
        return False, "skipped (no compose / docker)"
    compose = CODEMEMORY_HOME / "docker" / "docker-compose.yml"
    pull = _run(
        ["docker", "compose", "-f", str(compose), "--project-directory", str(CODEMEMORY_HOME), "pull"],
        capture=False,
    )
    if pull.returncode != 0:
        return False, "docker compose pull failed"
    up = _run(
        ["docker", "compose", "-f", str(compose), "--project-directory", str(CODEMEMORY_HOME), "up", "-d"],
        capture=False,
    )
    return up.returncode == 0, "compose pulled + up"


def upgrade_ollama_model(model: str) -> tuple[bool, str]:
    if not _have("ollama"):
        return False, "ollama not on PATH"
    p = _run(["ollama", "pull", model], capture=False)
    return p.returncode == 0, f"pulled {model}"


def upgrade_claude_plugin() -> tuple[bool, str]:
    if not _have("claude"):
        return False, "claude CLI not on PATH"
    # `claude plugin install` is idempotent — re-pin to latest from marketplace
    p = _run(
        ["claude", "plugin", "install", "code-memory@code-memory", "--scope", "user", "--force"],
        capture=False,
    )
    return p.returncode == 0, "claude plugin refreshed"


def upgrade_npm_pkg(pkg: str = "code-memory-opencode") -> tuple[bool, str]:
    if not _have("npm"):
        return False, "npm not on PATH"
    p = _run(["npm", "i", "-g", pkg], capture=False)
    return p.returncode == 0, f"npm i -g {pkg}"


# ---------- orchestrator ----------


def run_update(*, check_only: bool, full: bool, bleeding: bool) -> int:
    """Top-level entry point used by the CLI.

    ``check_only`` prints the plan and exits 0/1 based on whether anything
    is behind. ``full`` re-runs the one-liner installer (curl … | bash).
    Default is the smart path: upgrade-in-place only what is already present.
    """
    plan = build_plan()
    _print_plan(plan)

    behind_cli = plan.cli_latest not in ("?", plan.cli_current)

    if check_only:
        return 1 if behind_cli else 0

    if full:
        return _run_full_installer()

    rc = 0
    if behind_cli:
        ok, detail = upgrade_cli(plan.install_method, bleeding=bleeding)
        print(f"  CLI upgrade: {'ok' if ok else 'FAILED'} — {detail}")
        rc |= 0 if ok else 1
    else:
        print(f"  CLI: already {plan.cli_current}")

    for comp in plan.components:
        if not comp.present:
            continue
        if comp.name == "Docker: FalkorDB" or comp.name == "Docker: Qdrant":
            # one pass for both
            continue
        if comp.name.startswith("Ollama: "):
            model = comp.name.split(": ", 1)[1]
            ok, detail = upgrade_ollama_model(model)
            print(f"  {comp.name}: {'ok' if ok else 'skip'} — {detail}")
        elif comp.name == "Claude Code plugin":
            ok, detail = upgrade_claude_plugin()
            print(f"  {comp.name}: {'ok' if ok else 'skip'} — {detail}")
        elif comp.name == "OpenCode plugin (npm)":
            ok, detail = upgrade_npm_pkg()
            print(f"  {comp.name}: {'ok' if ok else 'skip'} — {detail}")

    if any(c.present for c in plan.components if c.name.startswith("Docker:")):
        ok, detail = upgrade_docker_images()
        print(f"  Docker stack: {'ok' if ok else 'skip'} — {detail}")

    return rc


def _print_plan(plan: UpdatePlan) -> None:
    print(f"code-memory updater  (install: {plan.install_method})")
    print(f"  CLI: {plan.cli_current}  →  latest: {plan.cli_latest}")
    print("  Components detected locally:")
    for c in plan.components[1:]:  # skip CLI row, already shown
        mark = "•" if c.present else "·"
        suffix = f"  ({c.detail})" if c.detail else ""
        state = "" if c.present else "  [not installed — skip]"
        print(f"    {mark} {c.name}{suffix}{state}")


def _run_full_installer() -> int:
    """Pipe the one-liner installer through bash (or PowerShell on Windows)."""
    raw = os.environ.get(
        "CODEMEMORY_RAW_URL", "https://raw.githubusercontent.com/fmflurry/code-memory/main"
    )
    if sys.platform == "win32":
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"irm {raw}/install.ps1 | iex",
        ]
    else:
        cmd = ["bash", "-c", f"curl -fsSL {raw}/install.sh | bash"]
    p = _run(cmd, capture=False)
    return p.returncode
