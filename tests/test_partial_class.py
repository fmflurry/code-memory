"""Partial class merge: identical key across files, separate DEFINES."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import Symbol, extract_file
from code_memory.orchestrator.pipeline import _symbol_key


# --------------------------------------------------------------- extractor


def _write_cs(p: Path, body: str) -> None:
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def test_partial_class_namespace_and_flag(tmp_path: Path) -> None:
    f = tmp_path / "g1.cs"
    _write_cs(
        f,
        """\
        namespace Acme.App
        {
            public partial class Greeter
            {
                public void Hello() { }
            }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    by_name = {s.name: s for s in ex.symbols}
    assert by_name["Greeter"].partial is True
    assert by_name["Greeter"].namespace == "Acme.App"
    # Members of a partial class are not themselves partial.
    assert by_name["Hello"].partial is False
    assert by_name["Hello"].namespace == "Acme.App"


def test_non_partial_class_has_no_partial_flag(tmp_path: Path) -> None:
    f = tmp_path / "g.cs"
    _write_cs(
        f,
        """\
        namespace Acme.App
        {
            public class Greeter
            {
                public void Hello() { }
            }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    assert {s.name: s.partial for s in ex.symbols} == {
        "Greeter": False,
        "Hello": False,
    }


def test_file_scoped_namespace(tmp_path: Path) -> None:
    """C# 10 ``namespace Foo;`` syntax."""
    f = tmp_path / "g.cs"
    _write_cs(
        f,
        """\
        namespace Acme.App;

        public partial class Greeter
        {
            public void Hello() { }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    greeter = next(s for s in ex.symbols if s.name == "Greeter")
    assert greeter.namespace == "Acme.App"
    assert greeter.partial is True


def test_nested_namespace_dot_joined(tmp_path: Path) -> None:
    f = tmp_path / "n.cs"
    _write_cs(
        f,
        """\
        namespace Outer
        {
            namespace Inner
            {
                public partial class Greeter { }
            }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    greeter = next(s for s in ex.symbols if s.name == "Greeter")
    assert greeter.namespace == "Outer.Inner"


# --------------------------------------------------------------- graph key


def _sym(name: str, *, ns: str | None, partial: bool, line: int = 1) -> Symbol:
    return Symbol(
        name=name,
        kind="class_declaration",
        start_line=line,
        end_line=line + 1,
        snippet=f"class {name} {{ }}",
        namespace=ns,
        partial=partial,
    )


def test_partial_class_key_collapses_across_files() -> None:
    sym = _sym("Greeter", ns="Acme.App", partial=True)
    k1 = _symbol_key("/repo/Part1.cs", sym)
    k2 = _symbol_key("/repo/Part2.cs", sym)
    assert k1 == k2 == "partial::Acme.App.Greeter"


def test_non_partial_class_key_stays_file_scoped() -> None:
    sym = _sym("Greeter", ns="Acme.App", partial=False, line=42)
    k = _symbol_key("/repo/G.cs", sym)
    assert k == "/repo/G.cs::Greeter#42"


def test_partial_without_namespace_falls_back_to_file_key() -> None:
    """Avoid collapsing two unrelated global-namespace partials together."""
    sym = _sym("Greeter", ns=None, partial=True, line=10)
    k1 = _symbol_key("/repo/A.cs", sym)
    k2 = _symbol_key("/repo/B.cs", sym)
    assert k1 != k2  # safety: never merge without a namespace anchor
