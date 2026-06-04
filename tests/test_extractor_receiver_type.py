"""Receiver-type inference for TypeScript ``this.<field>.<method>()``.

The Angular clean-arch use case pattern is:

    class CreateDraftUseCase {
      private readonly port = inject(MyPort);
      execute() { return this.port.with(...); }
    }

Without receiver-type inference the call site emits a bare ``with``
which the resolver can never disambiguate against the dozens of other
``.with(...)`` chains in the codebase. We tag the call with the
inferred receiver type so the resolver can narrow to the methods
defined on that type.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import extract_file


def _write(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f


def test_field_initialized_with_inject_gets_receiver_type(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "use-case.ts",
        """\
        import { inject } from '@angular/core';
        class UseCase {
          private readonly port = inject(MyPort);
          execute(x) { return this.port.with(x); }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    matches = [c for c in ex.calls if c.name == "with"]
    assert matches, "expected `with` call to be extracted"
    assert matches[0].receiver_type == "MyPort"


def test_field_with_type_annotation_gets_receiver_type(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "svc.ts",
        """\
        class Svc {
          private repo!: UserRepository;
          load(id) { return this.repo.findById(id); }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    matches = [c for c in ex.calls if c.name == "findById"]
    assert matches
    assert matches[0].receiver_type == "UserRepository"


def test_constructor_param_property_gets_receiver_type(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "ctor.ts",
        """\
        class Svc {
          constructor(private readonly repo: UserRepository) {}
          load(id) { return this.repo.findById(id); }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    matches = [c for c in ex.calls if c.name == "findById"]
    assert matches
    assert matches[0].receiver_type == "UserRepository"


def test_unrelated_call_has_no_receiver_type(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "plain.ts",
        """\
        function helper(x) { return doSomething(x); }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    matches = [c for c in ex.calls if c.name == "doSomething"]
    assert matches
    assert matches[0].receiver_type is None


def _recv(ex, name: str) -> str | None:
    matches = [c for c in ex.calls if c.name == name]
    assert matches, f"expected `{name}` call to be extracted"
    return matches[0].receiver_type


# --- C# -------------------------------------------------------------------


def test_csharp_field_and_property_and_primary_ctor(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "Svc.cs",
        """\
        namespace App;
        class Svc {
          private readonly IFooRepo _repo;
          public IBarRepo Bar { get; }
          public Svc(IFooRepo repo, IBarRepo bar) { _repo = repo; Bar = bar; }
          public void Run(int id) {
            _repo.GetById(id);
            this.Bar.Save(id);
            Console.WriteLine(id);
          }
        }
        class Primary(IBazRepo baz) {
          public void Go() { baz.Fetch(); }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None and ex.lang == "csharp"
    assert _recv(ex, "GetById") == "IFooRepo"  # bare field access
    assert _recv(ex, "Save") == "IBarRepo"  # this.Property access
    assert _recv(ex, "Fetch") == "IBazRepo"  # primary-ctor parameter
    # a static call on a non-field identifier stays unnarrowed
    assert _recv(ex, "WriteLine") is None


# --- PHP ------------------------------------------------------------------


def test_php_property_and_constructor_promotion(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "Svc.php",
        """\
        <?php
        namespace App;
        class Svc {
          private UserRepo $repo;
          public function __construct(private OrderRepo $orders) {}
          public function run(int $id) {
            $this->repo->byId($id);
            $this->orders->find($id);
            helper($id);
          }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None and ex.lang == "php"
    assert _recv(ex, "byId") == "UserRepo"  # explicit property
    assert _recv(ex, "find") == "OrderRepo"  # constructor promotion
    assert _recv(ex, "helper") is None  # free function, no receiver


# --- Dart -----------------------------------------------------------------


def test_dart_field_receiver(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "svc.dart",
        """\
        class Svc {
          final UserRepo repo;
          OrderRepo orders;
          Svc(this.repo, this.orders);
          void run(int id) {
            repo.byId(id);
            this.orders.find(id);
            helper(id);
          }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None and ex.lang == "dart"
    assert _recv(ex, "byId") == "UserRepo"  # bare field access
    assert _recv(ex, "find") == "OrderRepo"  # this.field access
    assert _recv(ex, "helper") is None  # top-level function, no receiver
