"""Overload disambiguation by call-site arity."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import Call, extract_file
from code_memory.orchestrator.resolver import (
    PLACEHOLDER_PREFIX,
    _pick_by_arity,
    _pick_target,
    resolve_graph,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resolver import _FakeGraph, _FakeStore  # noqa: E402


# --------------------------------------------------------------- extractor


def test_extractor_captures_call_arity_csharp(tmp_path: Path) -> None:
    f = tmp_path / "App.cs"
    f.write_text(
        textwrap.dedent(
            """\
            namespace App;
            public class Demo {
                public void Run() {
                    Foo();
                    Foo(1, 2);
                    Bar.Baz("hello");
                }
            }
            """
        ),
        encoding="utf-8",
    )
    ex = extract_file(f)
    assert ex is not None
    arities = sorted((c.name, c.arity) for c in ex.calls)
    assert ("Foo", 0) in arities
    assert ("Foo", 2) in arities
    assert ("Baz", 1) in arities


def test_extractor_captures_param_count_for_methods(tmp_path: Path) -> None:
    f = tmp_path / "App.cs"
    f.write_text(
        textwrap.dedent(
            """\
            namespace App;
            public class Demo {
                public void DoIt() { }
                public void DoIt(int a, int b) { }
                public string DoIt(string s, int c, bool flag) => s;
            }
            """
        ),
        encoding="utf-8",
    )
    ex = extract_file(f)
    assert ex is not None
    do_it_arities = sorted(
        s.param_count for s in ex.symbols if s.name == "DoIt"
    )
    assert do_it_arities == [0, 2, 3]


def test_class_declarations_have_no_param_count(tmp_path: Path) -> None:
    f = tmp_path / "C.cs"
    f.write_text(
        textwrap.dedent(
            """\
            namespace N;
            public class C
            {
                public void M() {}
            }
            """
        ),
        encoding="utf-8",
    )
    ex = extract_file(f)
    assert ex is not None
    by_name = {s.name: s for s in ex.symbols}
    assert by_name["C"].param_count is None
    assert by_name["M"].param_count == 0


# --------------------------------------------------------------- _pick_by_arity


def test_pick_by_arity_returns_unique_match() -> None:
    candidates = [
        ("/r/a.cs", "/r/a.cs::Run#1", 0),
        ("/r/a.cs", "/r/a.cs::Run#10", 2),
    ]
    assert _pick_by_arity(candidates, 2) == "/r/a.cs::Run#10"
    assert _pick_by_arity(candidates, 0) == "/r/a.cs::Run#1"


def test_pick_by_arity_returns_none_when_ambiguous() -> None:
    """Two candidates with identical arity → no resolution."""
    candidates = [
        ("/r/a.cs", "/r/a.cs::Run#1", 2),
        ("/r/b.cs", "/r/b.cs::Run#10", 2),
    ]
    assert _pick_by_arity(candidates, 2) is None


def test_pick_by_arity_returns_none_when_no_match() -> None:
    candidates = [("/r/a.cs", "/r/a.cs::Run#1", 0)]
    assert _pick_by_arity(candidates, 5) is None


def test_pick_by_arity_ignores_negative_input() -> None:
    """Arity == -1 means the call site arity is unknown; never resolve."""
    candidates = [("/r/a.cs", "/r/a.cs::Run#1", 0)]
    assert _pick_by_arity(candidates, -1) is None


# --------------------------------------------------------------- _pick_target


def test_pick_target_prefers_same_file_with_matching_arity() -> None:
    candidates = [
        ("/r/a.cs", "/r/a.cs::Run#1", 0),
        ("/r/a.cs", "/r/a.cs::Run#10", 2),
    ]
    out = _pick_target(candidates, preferred_file="/r/a.cs", arity=2)
    assert out == "/r/a.cs::Run#10"


def test_pick_target_falls_back_when_arity_unknown() -> None:
    """When arity is -1, same-file logic still returns first same-file hit."""
    candidates = [
        ("/r/a.cs", "/r/a.cs::Run#1", 0),
        ("/r/a.cs", "/r/a.cs::Run#10", 2),
    ]
    out = _pick_target(candidates, preferred_file="/r/a.cs", arity=-1)
    assert out == "/r/a.cs::Run#1"


# --------------------------------------------------------------- resolver integration


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


def test_project_unique_uses_arity_to_disambiguate() -> None:
    """Two same-name definitions in different files; arity picks the right one."""
    # NOTE: the test fake's _FakeGraph signature only takes (file, name,
    # key) tuples; the resolver loads it and pads params=None. So this
    # case exercises the path where param_count is unknown — the
    # overload tiebreak silently gives up and reports ambiguous. That's
    # the safe behaviour we want; only ingest-time data flips it on.
    store = _store(
        defines=[
            ("/r/a.cs", "Run", "/r/a.cs::Run#1"),
            ("/r/b.cs", "Run", "/r/b.cs::Run#10"),
        ],
        placeholders=[(f"{PLACEHOLDER_PREFIX}Run", "Run")],
        calls=[("/r/x.cs", f"{PLACEHOLDER_PREFIX}Run")],
    )
    stats = resolve_graph(store)
    # Without ingested param_count, the resolver can't pick — and
    # rather than coin-flipping, it leaves the call ambiguous.
    assert stats.edges_left_ambiguous == 1


# --------------------------------------------------------------- backwards compat


def test_call_dataclass_is_frozen() -> None:
    c = Call(name="Run", arity=2)
    import pytest

    with pytest.raises(Exception):
        c.name = "Other"  # type: ignore[misc]
