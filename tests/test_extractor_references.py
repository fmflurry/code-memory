"""Type-position reference extraction for C# (base lists, parameter/field
types, generics, type constraints, cast/is/as/typeof targets).

Regression guard: before the type-refs pass, ``code-memory callers`` on a
plain C# interface returned 0 because the graph only modelled call
expressions. These tests pin the extractor's REFERENCES output so the
graph keeps surfacing interface implementers, parameter-type usages,
generic-arg appearances, and constraint clauses.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from code_memory.extractor.treesitter import extract_file


def _refs(source: str) -> set[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".cs", delete=False) as f:
        f.write(source)
        path = f.name
    ex = extract_file(path)
    Path(path).unlink(missing_ok=True)
    assert ex is not None
    return set(ex.references)


def test_base_list_inheritance_captured() -> None:
    refs = _refs(
        """
        namespace N;
        public interface IFoo {}
        public class Bar : IFoo {}
        """
    )
    assert "IFoo" in refs


def test_multiple_base_types_captured() -> None:
    refs = _refs(
        """
        namespace N;
        public class Foo : BaseA, IB, IC {}
        """
    )
    assert {"BaseA", "IB", "IC"} <= refs


def test_parameter_type_captured() -> None:
    refs = _refs(
        """
        namespace N;
        public class Svc
        {
            public void Do(IFoo input, BarSvc svc) {}
        }
        """
    )
    assert {"IFoo", "BarSvc"} <= refs


def test_field_and_property_types_captured() -> None:
    refs = _refs(
        """
        namespace N;
        public class Svc
        {
            private IFoo _foo;
            public BarSvc Bar { get; set; }
        }
        """
    )
    assert {"IFoo", "BarSvc"} <= refs


def test_method_return_type_captured() -> None:
    refs = _refs(
        """
        namespace N;
        public class Svc
        {
            public BusinessResult Do() => null;
        }
        """
    )
    assert "BusinessResult" in refs


def test_generic_args_captured() -> None:
    refs = _refs(
        """
        namespace N;
        public class Svc
        {
            public List<ParameterDuplication> Items;
            public Task<UserDto> Fetch() => null;
        }
        """
    )
    assert {"List", "ParameterDuplication", "Task", "UserDto"} <= refs


def test_type_constraints_captured() -> None:
    refs = _refs(
        """
        namespace N;
        public class Svc<T> where T : IAggregate, new() {}
        """
    )
    assert "IAggregate" in refs


def test_cast_is_as_typeof_captured() -> None:
    refs = _refs(
        """
        namespace N;
        public class Svc
        {
            public void Do(object o)
            {
                var x = (UserDto)o;
                if (o is BarSvc b) {}
                var y = o as IFoo;
                var t = typeof(BusinessResult);
            }
        }
        """
    )
    assert {"UserDto", "BarSvc", "IFoo", "BusinessResult"} <= refs


def test_primitives_skipped() -> None:
    refs = _refs(
        """
        namespace N;
        public class Svc
        {
            public int Do(string s, bool b) => 0;
        }
        """
    )
    assert refs.isdisjoint({"int", "string", "bool", "void"})


def test_qualified_name_emits_last_segment_only() -> None:
    refs = _refs(
        """
        namespace N;
        public class Svc
        {
            public System.Collections.Generic.List<App.Bar.UserDto> Items;
        }
        """
    )
    # We want the type names, not the namespace tokens.
    assert "List" in refs
    assert "UserDto" in refs
    assert "System" not in refs and "Collections" not in refs
