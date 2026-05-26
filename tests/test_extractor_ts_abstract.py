"""TypeScript ``abstract class`` extraction (Angular ports / DI tokens)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import extract_file


def _write(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f


def test_abstract_class_is_a_symbol(tmp_path: Path) -> None:
    """Angular clean-arch uses ``export abstract class Port`` as the
    DI token. Without registering it as a Symbol, the resolver can
    never bind ``inject(Port)`` to a definition."""
    f = _write(
        tmp_path,
        "create-draft.port.ts",
        """\
        import { Observable } from 'rxjs';

        export abstract class CreatePurchaseOrderDraftPort {
          abstract with(draft: unknown): Observable<unknown>;
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    names = [s.name for s in ex.symbols]
    assert "CreatePurchaseOrderDraftPort" in names


def test_abstract_method_signature_is_a_symbol(tmp_path: Path) -> None:
    """The abstract method on a port is the call target the use case
    invokes. Indexing it lets the resolver bind ``this.port.with()``
    to a single definition."""
    f = _write(
        tmp_path,
        "port.ts",
        """\
        export abstract class P {
          abstract doWork(x: number): void;
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    names = [s.name for s in ex.symbols]
    assert "doWork" in names
