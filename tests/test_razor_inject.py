"""Razor ``@inject`` extraction + resolver integration."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import extract_file
from code_memory.orchestrator.resolver import PLACEHOLDER_PREFIX, resolve_graph

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resolver import _FakeGraph, _FakeStore  # noqa: E402


# --------------------------------------------------------------- extractor


def test_extract_single_inject(tmp_path: Path) -> None:
    f = tmp_path / "Users.razor"
    f.write_text(
        textwrap.dedent(
            """\
            @page "/users"
            @inject IUserService UserService

            <h1>Users</h1>
            """
        ),
        encoding="utf-8",
    )
    ex = extract_file(f)
    assert ex is not None
    assert ex.lang == "razor"
    assert ex.injects == ["IUserService"]


def test_extract_multiple_injects(tmp_path: Path) -> None:
    f = tmp_path / "Multi.razor"
    f.write_text(
        textwrap.dedent(
            """\
            @inject IUserService UserService
            @inject NavigationManager Nav
            @inject ILogger<Multi> Logger
            """
        ),
        encoding="utf-8",
    )
    ex = extract_file(f)
    assert ex is not None
    assert "IUserService" in ex.injects
    assert "NavigationManager" in ex.injects
    # Generic types like ``ILogger<Multi>`` collapse to the first
    # identifier; the parameter survives at call sites if needed.
    assert any(name.startswith("ILogger") for name in ex.injects)


def test_extract_no_inject_means_empty_list(tmp_path: Path) -> None:
    f = tmp_path / "Plain.razor"
    f.write_text("@page \"/\"\n<h1>Hello</h1>\n", encoding="utf-8")
    ex = extract_file(f)
    assert ex is not None
    assert ex.injects == []


# --------------------------------------------------------------- resolver


def _store(**kw: object) -> _FakeStore:
    defaults: dict[str, object] = {
        "defines": [],
        "imports": [],
        "placeholders": [],
        "calls": [],
        "file_project": [],
        "project_assemblies": [],
        "type_index": [],
        "inject_edges": [],
    }
    defaults.update(kw)
    return _FakeStore(_FakeGraph(**defaults))  # type: ignore[arg-type]


PH = f"{PLACEHOLDER_PREFIX}IUserService"
ASM = "Acme.Lib, Version=1.0.0.0"
TYPE_KEY = f"{ASM}::Acme.Lib.IUserService"


def test_inject_resolves_via_assembly() -> None:
    store = _store(
        placeholders=[(PH, "IUserService")],
        inject_edges=[("/repo/Acme/Users.razor", PH)],
        file_project=[("/repo/Acme/Users.razor", "/repo/Acme/Acme.csproj")],
        project_assemblies=[("/repo/Acme/Acme.csproj", ASM)],
        type_index=[("IUserService", TYPE_KEY, ASM)],
    )
    stats = resolve_graph(store)
    assert stats.edges_resolved_assembly == 1
    rewrites = [w for w in store.graph.writes if w[0] == "rewrite_inject"]
    assert len(rewrites) == 1
    assert rewrites[0][1]["target"] == TYPE_KEY


def test_inject_resolves_to_in_project_symbol() -> None:
    """When the interface is defined in source, prefer that over assembly."""
    store = _store(
        defines=[
            (
                "/repo/Acme/Services/IUserService.cs",
                "IUserService",
                "/repo/Acme/Services/IUserService.cs::IUserService#5",
            )
        ],
        placeholders=[(PH, "IUserService")],
        inject_edges=[("/repo/Acme/Users.razor", PH)],
    )
    stats = resolve_graph(store)
    assert stats.edges_resolved_unique == 1
    rewrites = [w for w in store.graph.writes if w[0] == "rewrite_inject"]
    assert rewrites[0][1]["target"].endswith("::IUserService#5")


def test_inject_keeps_placeholder_when_only_an_inject_uses_it() -> None:
    """A placeholder targeted only by INJECTS must survive cleanup."""
    store = _store(
        placeholders=[(PH, "IUserService")],
        inject_edges=[("/repo/Acme/Users.razor", PH)],
        # No referenced assembly → fall-through to external; placeholder stays.
    )
    stats = resolve_graph(store)
    assert stats.edges_left_external == 1
    assert stats.placeholders_deleted == 0


def test_inject_total_counted_alongside_calls() -> None:
    """edges_total reports both relations summed."""
    store = _store(
        placeholders=[(PH, "IUserService")],
        calls=[("/repo/Acme/App.cs", PH)],
        inject_edges=[("/repo/Acme/Users.razor", PH)],
    )
    stats = resolve_graph(store)
    assert stats.edges_total == 2
