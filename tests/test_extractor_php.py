"""PHP language coverage for the tree-sitter extractor.

Covers the four primitives the rest of the pipeline depends on:

- symbols (classes, interfaces, traits, enums, functions, methods + arity)
- imports (``use Foo\\Bar;``)
- calls (function, ``$obj->method``, ``Foo::method``, ``new Foo()``)
- namespaces (file-scoped ``namespace X;`` *and* block ``namespace X { }``)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import extract_file


def _write(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f


def test_extracts_symbols_imports_calls(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "user_service.php",
        """\
        <?php
        namespace App\\Service;

        use App\\Repo\\UserRepo;
        use App\\Model\\User;

        class UserService {
          public function __construct(private UserRepo $repo) {}
          public function find(int $id): ?User { return $this->repo->byId($id); }
          public function all(): array { return UserRepo::list(); }
          public function make(): User { return new User(); }
        }

        interface IFoo {}
        trait TFoo {}
        enum Status { case Active; }
        function bare($x) { return helper($x); }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    assert ex.lang == "php"

    by_name = {s.name: s for s in ex.symbols}
    # core kinds present
    assert by_name["UserService"].kind == "class_declaration"
    assert by_name["IFoo"].kind == "interface_declaration"
    assert by_name["TFoo"].kind == "trait_declaration"
    assert by_name["Status"].kind == "enum_declaration"
    assert by_name["bare"].kind == "function_definition"
    assert by_name["find"].kind == "method_declaration"

    # file-scoped namespace propagates to every top-level symbol
    assert by_name["UserService"].namespace == "App\\Service"
    assert by_name["bare"].namespace == "App\\Service"

    # arity for callable kinds, ``None`` for non-callable
    assert by_name["__construct"].param_count == 1
    assert by_name["find"].param_count == 1
    assert by_name["all"].param_count == 0
    assert by_name["bare"].param_count == 1
    assert by_name["UserService"].param_count is None

    # ``use`` clauses surface as imports
    assert "App\\Repo\\UserRepo" in ex.imports
    assert "App\\Model\\User" in ex.imports

    call_names = {c.name for c in ex.calls}
    # instance method, static method, ctor target, free function
    assert {"byId", "list", "User", "helper"}.issubset(call_names)
    # ``new`` keyword must not leak as a callee
    assert "new" not in call_names


def test_block_namespace_scopes_only_its_body(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "multi_ns.php",
        """\
        <?php
        namespace A {
          class InA {}
        }
        namespace B {
          class InB {}
        }
        class GlobalCls {}
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    ns = {s.name: s.namespace for s in ex.symbols}
    assert ns == {"InA": "A", "InB": "B", "GlobalCls": None}


def test_qualified_ctor_callee_resolves_to_class_name(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "qualified_new.php",
        """\
        <?php
        class X {
          function m() { return new App\\Other\\Thing(); }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    # backslash-qualified ``new App\\Other\\Thing()`` collapses to the
    # bare trailing identifier so the graph can resolve it the same way
    # as an unqualified ``new Thing()``.
    assert any(c.name == "Thing" for c in ex.calls)
    assert not any(c.name == "new" for c in ex.calls)


def test_php_extension_is_recognized(tmp_path: Path) -> None:
    f = _write(tmp_path, "tiny.php", "<?php function noop() {}\n")
    ex = extract_file(f)
    assert ex is not None
    assert ex.lang == "php"
    assert [s.name for s in ex.symbols] == ["noop"]


def test_phtml_extension_is_recognized(tmp_path: Path) -> None:
    # Laravel/Zend/WordPress view templates use ``.phtml`` with the same
    # PHP grammar — the extractor must treat them identically.
    f = _write(tmp_path, "view.phtml", "<?php class V {}\n")
    ex = extract_file(f)
    assert ex is not None
    assert ex.lang == "php"
    assert [s.name for s in ex.symbols] == ["V"]


def test_multi_clause_use_emits_every_fqcn(tmp_path: Path) -> None:
    # ``use A, B\\C;`` is one statement, two imports. Aliased ``use D as
    # X;`` is one import keyed by the FQCN, not the alias. Without the
    # multi-clause walk, only the first FQCN survives and ``importers
    # B\\C`` misses every caller.
    f = _write(
        tmp_path,
        "uses.php",
        """\
        <?php
        use X, Y\\Z;
        use A\\B as Alias;
        use Plain;
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    assert ex.imports == ["X", "Y\\Z", "A\\B", "Plain"]


def test_type_refs_extends_implements(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "heritage.php",
        """\
        <?php
        interface I1 {}
        interface I2 {}
        class Foo extends Bar implements I1, I2 {}
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    refs = set(ex.references)
    assert {"Bar", "I1", "I2"}.issubset(refs)


def test_type_refs_properties_and_params_and_returns(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "types.php",
        """\
        <?php
        class Svc {
          private UserRepo $repo;
          protected ?Logger $log;
          public array $tags;
          public function __construct(
            private DateTimeImmutable $when,
            Logger|Stream $out,
            ?Cache $c = null,
          ) {}
          public function find(int $id): ?User { return null; }
          public function multi(): Logger|Stream {}
          public function intersect(): Countable&ArrayAccess {}
        }
        function freefn(Foo $a): Bar {}
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    refs = set(ex.references)
    # property + param + return positions
    assert {"UserRepo", "Logger", "DateTimeImmutable", "Stream",
            "Cache", "User", "Countable", "ArrayAccess",
            "Foo", "Bar"}.issubset(refs), refs
    # primitives stay out of the graph
    assert "int" not in refs
    assert "array" not in refs
    assert "null" not in refs


def test_qualified_type_refs_collapse_to_trailing_segment(tmp_path: Path) -> None:
    # ``App\\Other\\Thing`` in a type position references the leaf
    # ``Thing`` — the graph resolves by bare identifier, so the
    # namespace prefix is shed (same convention as C# qualified names).
    f = _write(
        tmp_path,
        "qualified_types.php",
        """\
        <?php
        function nsd(App\\Other\\Thing $x): App\\Other\\Other {}
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    refs = set(ex.references)
    assert {"Thing", "Other"}.issubset(refs)
