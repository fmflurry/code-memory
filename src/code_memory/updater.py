"""Smart updater for code-memory.

Detects which components are already installed locally and refreshes
only those — never asks the user to re-confirm pieces they already opted
into during the initial one-liner install.

Components inspected:

* CLI install method (``uv tool`` / ``pipx`` / ``pip``) + version vs PyPI
* Docker stack (FalkorDB + Qdrant)
* Ollama models present locally (``bge-m3``)
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
LEGACY_PACKAGE = "code-memory"  # historical dist name still in older uv-tool venvs
UV_TOOL_NAME = "code-memory"  # entry-point/tool-name; what `uv tool list` shows
DEFAULT_REPO_URL = os.environ.get(
    "CODEMEMORY_REPO_URL", "https://github.com/fmflurry/code-memory"
)
CODEMEMORY_HOME = Path(os.environ.get("CODEMEMORY_HOME", str(Path.home() / ".code-memory")))

# Stable compose project name. The compose file pins fixed container_names
# (``cm-falkordb`` etc.), so the containers are global singletons: only the
# compose project that originally created them may recreate them. If a later
# ``compose up`` runs under a *different* project name, Docker refuses with
# "container name already in use". We therefore always pin ``-p`` to one
# constant and, when containers already exist, to whatever project actually
# owns them — so update never collides with install.
COMPOSE_PROJECT = "code-memory"
COMPOSE_CONTAINERS = ("cm-falkordb", "cm-qdrant", "cm-tei")

InstallMethod = Literal["uv-tool", "pipx", "pip", "editable", "unknown"]


# Optional extras advertised in pyproject.toml. The picker below uses this
# registry to decide what to show, how to detect "is it installed", and
# which package name to inject for ``pipx``. Keep in sync with
# ``[project.optional-dependencies]`` — there is no programmatic discovery
# for installed (wheel-only) builds.
EXTRAS: dict[str, dict[str, str]] = {
    "dotnet": {
        "module": "dnfile",
        "desc": ".NET assembly metadata indexing (pure-Python PE reader, light).",
    },
    "hybrid": {
        "module": "FlagEmbedding",
        "desc": "In-process BGE-M3 dense+sparse via FlagEmbedding. Heavy — pulls torch (~2 GB).",
    },
}


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
    # On Windows the CLIs we shell out to (npm, claude) ship as .cmd batch
    # files. CreateProcess only resolves .exe images by the bare name, so
    # ``["npm", ...]`` raises WinError 2 even though it is on PATH. Resolve the
    # real path via PATHEXT-aware which(), and run batch files through cmd.exe.
    resolved = shutil.which(cmd[0]) or cmd[0]
    args = [resolved, *cmd[1:]]
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        args = [comspec, "/c", *args]
    return subprocess.run(
        args, check=check, capture_output=capture, text=True, env={**os.environ}
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
    from importlib.metadata import distribution

    for name in (PYPI_PACKAGE, LEGACY_PACKAGE):
        try:
            d = distribution(name)
        except Exception:  # noqa: BLE001
            continue
        try:
            raw = d.read_text("direct_url.json") or ""
        except Exception:  # noqa: BLE001
            raw = ""
        if not raw:
            continue
        if bool(json.loads(raw).get("dir_info", {}).get("editable")):
            return True
    return False


def _pip_shows() -> bool:
    for name in (PYPI_PACKAGE, LEGACY_PACKAGE):
        if _run([sys.executable, "-m", "pip", "show", name]).returncode == 0:
            return True
    return False


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


def _running_compose_file() -> Path | None:
    """Probe live containers for the compose file that owns them.

    Handles dev installs whose compose file lives in the repo, not under
    ``~/.code-memory/docker/``. Returns the first compose path found
    among ``cm-falkordb`` / ``cm-qdrant`` containers, or None.
    """
    if not _have("docker"):
        return None
    for name in ("cm-falkordb", "cm-qdrant"):
        p = _run([
            "docker",
            "inspect",
            "-f",
            "{{ index .Config.Labels \"com.docker.compose.project.config_files\" }}",
            name,
        ])
        if p.returncode == 0:
            path = p.stdout.strip()
            if path and Path(path).exists():
                return Path(path)
    return None


def _owning_compose_project() -> str | None:
    """Compose project name that already owns the cm-* containers, if any.

    Reading the live ``com.docker.compose.project`` label lets the updater
    recreate the existing containers under their original project instead of
    guessing a name from a directory basename (which is what caused the
    "container name already in use" conflict).
    """
    if not _have("docker"):
        return None
    for name in ("cm-falkordb", "cm-qdrant"):
        p = _run([
            "docker",
            "inspect",
            "-f",
            "{{ index .Config.Labels \"com.docker.compose.project\" }}",
            name,
        ])
        if p.returncode == 0:
            proj = p.stdout.strip()
            if proj:
                return proj
    return None


def _remove_conflicting_containers() -> None:
    """Force-remove the fixed-name cm-* containers.

    Last-resort recovery when ``compose up`` still hits a name conflict (e.g.
    the existing containers carry no compose project label, or an unmanaged
    container squatted the name). Named volumes (falkor_data, qdrant_data,
    tei_data) survive ``rm``, so indexed data is preserved.
    """
    if not _have("docker"):
        return
    _run(["docker", "rm", "-f", *COMPOSE_CONTAINERS])


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
    for m in ("bge-m3",):
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
    """Upgrade the CLI in-place via the same channel it was installed from.

    For uv-tool we always do a ``--reinstall --from <source>`` so legacy
    installs whose dist is named ``code-memory`` (pre-rename) get cleanly
    migrated to ``flurryx-code-memory`` without the user noticing.
    """
    source = f"git+{DEFAULT_REPO_URL}" if bleeding else PYPI_PACKAGE
    if method == "uv-tool":
        cmd = [
            "uv",
            "tool",
            "install",
            "--reinstall",
            "--force",
            "--from",
            source,
            UV_TOOL_NAME,
        ]
    elif method == "pipx":
        # pipx-installed users likely registered under either name; force
        # a reinstall from <source> so the package name converges to current.
        cmd = ["pipx", "install", "--force", source]
    elif method == "pip":
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", source]
    elif method == "editable":
        return False, "editable install — `git pull` in the repo to update"
    else:
        return False, "unknown install method — re-run the one-liner installer"
    p = _run(cmd, capture=False)
    return p.returncode == 0, " ".join(cmd)


def upgrade_docker_images() -> tuple[bool, str]:
    if not _have("docker"):
        return False, "docker not on PATH"
    compose = CODEMEMORY_HOME / "docker" / "docker-compose.yml"
    if not compose.exists():
        live = _running_compose_file()
        if live is None:
            return False, "no compose file at ~/.code-memory/ and no running cm-* containers"
        compose = live
    project_dir = compose.parent

    # Pin the project name so naming never depends on the directory basename.
    # Prefer the project the running containers already belong to — that is the
    # only project allowed to recreate the fixed-name cm-* containers, so using
    # it sidesteps the "container name already in use" conflict.
    project = _owning_compose_project() or COMPOSE_PROJECT

    base = [
        "docker", "compose",
        "-f", str(compose),
        "--project-directory", str(project_dir),
        "-p", project,
    ]

    pull = _run([*base, "pull"], capture=False)
    if pull.returncode != 0:
        return False, "docker compose pull failed"

    up = _run([*base, "up", "-d", "--remove-orphans"], capture=False)
    if up.returncode == 0:
        return True, f"compose pulled + up (project={project}, {compose})"

    # Recovery: a stale or unmanaged container is squatting the fixed name.
    # Drop the cm-* containers (named volumes persist) and recreate cleanly.
    print("  Docker: name conflict — removing stale cm-* containers and retrying")
    _remove_conflicting_containers()
    up_retry = _run([*base, "up", "-d", "--remove-orphans"], capture=False)
    return up_retry.returncode == 0, f"compose recreated after conflict (project={project}, {compose})"


def upgrade_ollama_model(model: str) -> tuple[bool, str]:
    if not _have("ollama"):
        return False, "ollama not on PATH"
    p = _run(["ollama", "pull", model], capture=False)
    return p.returncode == 0, f"pulled {model}"


def upgrade_claude_plugin() -> tuple[bool, str]:
    if not _have("claude"):
        return False, "claude CLI not on PATH"
    # `claude plugin update <name>` is the canonical refresh path.
    # `--force` was a previous-version flag; current CLI rejects it.
    p = _run(["claude", "plugin", "update", "code-memory@code-memory"], capture=False)
    if p.returncode == 0:
        return True, "claude plugin updated"
    # Fall back to install — handles the never-installed-after-marketplace-add edge.
    p2 = _run(
        ["claude", "plugin", "install", "code-memory@code-memory", "--scope", "user"],
        capture=False,
    )
    return p2.returncode == 0, "claude plugin re-installed"


def upgrade_npm_pkg(pkg: str = "code-memory-opencode") -> tuple[bool, str]:
    if not _have("npm"):
        return False, "npm not on PATH"
    p = _run(["npm", "i", "-g", pkg], capture=False)
    return p.returncode == 0, f"npm i -g {pkg}"


# ---------- orchestrator ----------


def run_update(
    *,
    check_only: bool,
    full: bool,
    bleeding: bool,
    extras_override: str | None = None,
) -> int:
    """Top-level entry point used by the CLI.

    ``check_only`` prints the plan and exits 0/1 based on whether anything
    is behind. ``full`` re-runs the one-liner installer (curl … | bash).
    Default is the smart path: upgrade-in-place only what is already present.

    ``extras_override`` is the value of the ``--extras`` CLI flag (highest
    priority over the ``CODEMEMORY_EXTRAS`` env var).
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

    rc |= offer_missing_extras(plan.install_method, extras_override=extras_override)
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


def resolve_extras_selection(
    *,
    env_value: str | None,
    is_tty: bool,
    missing: list[str],
) -> tuple[list[str], str]:
    """Pure helper — no I/O — that decides which extras to install.

    Returns ``(selected_names, mode)`` where ``mode`` is one of
    ``"env"``, ``"interactive"``, or ``"skip"``.

    Priority:
    1. ``env_value`` not None → parse comma list; ``"none"`` or empty → nothing.
    2. ``is_tty`` True → caller must run the interactive loop (mode=``"interactive"``).
    3. Otherwise → skip (non-interactive, no env override).
    """
    if env_value is not None:
        stripped = env_value.strip()
        if stripped.lower() == "none" or stripped == "":
            return [], "env"
        names = [n.strip() for n in stripped.split(",") if n.strip()]
        valid = [n for n in names if n in EXTRAS and n in missing]
        return valid, "env"
    if is_tty:
        return [], "interactive"
    return [], "skip"


def _install_selected_extras(names: list[str], method: InstallMethod) -> int:
    """Install a list of extras; returns OR of per-extra return codes.

    A single failed extra does not abort the rest — each failure sets the
    nonzero bit in ``rc`` but installation continues.
    """
    rc = 0
    for name in names:
        print()
        print(f"Installing extra: {name}")
        ok, detail = install_extra(name, method)
        marker = "ok" if ok else "FAILED"
        print(f"  {marker} — {detail}")
        rc |= 0 if ok else 1
    return rc


def offer_missing_extras(
    method: InstallMethod,
    *,
    extras_override: str | None = None,
) -> int:
    """Offer to install extras that are not yet present.

    Called automatically at the end of the smart ``run_update`` path.
    Never blocks ``--check`` / ``--full`` (those return before reaching here).

    Priority: ``extras_override`` (CLI ``--extras`` flag) >
    ``CODEMEMORY_EXTRAS`` env var > interactive prompt > skip silently.
    """
    missing = [n for n in EXTRAS if not _python_module_present(EXTRAS[n]["module"])]
    if not missing:
        return 0

    if method in ("unknown",):
        print(
            f"\033[2m  hint: optional extras not yet installed: {', '.join(missing)}.\n"
            "  Run `code-memory extras` or set CODEMEMORY_EXTRAS=dotnet,hybrid.\033[0m"
        )
        return 0

    # For editable installs, we need a valid pyproject.toml to install extras.
    if method == "editable":
        repo_root = Path(__file__).resolve().parents[2]
        if not (repo_root / "pyproject.toml").exists():
            print(
                f"\033[2m  hint: editable install without pyproject.toml at {repo_root};\n"
                "  cannot install optional extras automatically.\033[0m"
            )
            return 0

    # Determine TTY status safely.
    try:
        is_tty = sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        is_tty = False

    # CLI flag takes precedence over env var.
    effective_env = extras_override if extras_override is not None else os.environ.get("CODEMEMORY_EXTRAS")

    selected, mode = resolve_extras_selection(
        env_value=effective_env,
        is_tty=is_tty,
        missing=missing,
    )

    if mode == "env":
        if effective_env is not None:
            # Warn about any names that were not in EXTRAS or already installed.
            raw_names = [n.strip() for n in effective_env.split(",") if n.strip() and n.strip().lower() != "none"]
            unknown = [n for n in raw_names if n not in EXTRAS]
            already = [n for n in raw_names if n in EXTRAS and n not in missing]
            for n in unknown:
                print(f"\033[33m  [warn]\033[0m unknown extra '{n}' (known: {', '.join(EXTRAS)})")
            for n in already:
                print(f"  · {n} already installed — skipped")
        if not selected:
            return 0
        return _install_selected_extras(selected, method)

    if mode == "interactive":
        todo: list[str] = []
        print()
        print("Optional extras available:")
        for name in missing:
            info = EXTRAS[name]
            print(f"  · {name}  — not installed")
            print(f"      {info['desc']}")
            try:
                answer = input(f"      Install `{name}`? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer in ("y", "yes"):
                todo.append(name)
        if not todo:
            print("Nothing to install.")
            return 0
        return _install_selected_extras(todo, method)

    # mode == "skip"
    print(
        f"\033[2m  hint: optional extras not yet installed: {', '.join(missing)}.\n"
        "  Re-run with CODEMEMORY_EXTRAS=dotnet,hybrid or `code-memory extras`.\033[0m"
    )
    return 0


def install_extra(name: str, method: InstallMethod) -> tuple[bool, str]:
    """Install a single optional extra using whichever channel owns the CLI.

    Returns ``(ok, detail)`` where detail is the command actually run, or a
    reason string when the channel cannot install extras (e.g. unknown).
    """
    if name not in EXTRAS:
        return False, f"unknown extra: {name}"

    source_with_extra = f"{PYPI_PACKAGE}[{name}]"

    if method == "editable":
        repo_root = Path(__file__).resolve().parents[2]
        if not (repo_root / "pyproject.toml").exists():
            return False, f"editable install but pyproject.toml not at {repo_root}"
        cmd = [sys.executable, "-m", "pip", "install", "-e", f".[{name}]"]
        p = subprocess.run(cmd, cwd=str(repo_root), env={**os.environ}, text=True)
    elif method == "uv-tool":
        cmd = [
            "uv", "tool", "install",
            "--reinstall", "--force",
            "--from", source_with_extra,
            UV_TOOL_NAME,
        ]
        p = _run(cmd, capture=False)
    elif method == "pipx":
        # pipx inject adds the runtime module into the existing tool venv
        # without uninstalling/reinstalling code-memory itself.
        cmd = ["pipx", "inject", PYPI_PACKAGE, EXTRAS[name]["module"]]
        p = _run(cmd, capture=False)
    elif method == "pip":
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", source_with_extra]
        p = _run(cmd, capture=False)
    else:
        return False, "unknown install method — re-run the one-liner installer first"

    return p.returncode == 0, " ".join(cmd)


def run_extras_wizard() -> int:
    """Interactive picker to enable optional Python extras post-install.

    Lists every extra defined in :data:`EXTRAS`, shows current state, and
    asks per-extra whether to install. Already-present extras are skipped
    silently (use the package manager directly to uninstall).
    """
    method = detect_install_method()
    print(f"code-memory extras  (install: {method})")
    if method == "unknown":
        print("  cannot determine install method — aborting.")
        print("  re-run the one-liner installer or use `pip install` manually.")
        return 1

    todo: list[str] = []
    for name, info in EXTRAS.items():
        present = _python_module_present(info["module"])
        if present:
            print(f"  ✓ {name}  — installed ({info['module']})")
            continue
        print(f"  · {name}  — not installed")
        print(f"      {info['desc']}")
        answer = input(f"      Install `{name}`? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            todo.append(name)

    if not todo:
        print("Nothing to install.")
        return 0

    rc = 0
    for name in todo:
        print()
        print(f"Installing extra: {name}")
        ok, detail = install_extra(name, method)
        marker = "ok" if ok else "FAILED"
        print(f"  {marker} — {detail}")
        rc |= 0 if ok else 1
    return rc


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
