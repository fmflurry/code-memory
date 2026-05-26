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
