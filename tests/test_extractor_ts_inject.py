"""Angular ``inject(Token)`` extraction (TypeScript)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import extract_file


def _write(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f


def test_ts_inject_emits_injects_edge(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "use-case.ts",
        """\
        import { inject, Injectable } from '@angular/core';
        import { CreatePurchaseOrderDraftPort } from '../ports';

        @Injectable()
        export class CreateDraftUseCase {
          private readonly createDraft = inject(CreatePurchaseOrderDraftPort);

          execute(draft) {
            return this.createDraft.with(draft);
          }
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    assert "CreatePurchaseOrderDraftPort" in ex.injects


def test_ts_inject_multiple(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "multi.ts",
        """\
        import { inject } from '@angular/core';
        class X {
          private a = inject(TokenA);
          private b = inject(TokenB);
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    assert "TokenA" in ex.injects
    assert "TokenB" in ex.injects


def test_ts_inject_does_not_double_as_call_edge(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "single.ts",
        """\
        import { inject } from '@angular/core';
        class X {
          private a = inject(MyToken);
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    assert "MyToken" in ex.injects
    assert all(c.name != "inject" for c in ex.calls)
    assert all(c.name != "MyToken" for c in ex.calls)


def test_ts_inject_qualified_token(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "qualified.ts",
        """\
        import { inject } from '@angular/core';
        class X {
          private svc = inject(NS.MyToken);
        }
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    assert "MyToken" in ex.injects


def test_non_angular_inject_is_ignored(tmp_path: Path) -> None:
    """An ``inject(...)`` call in plain JS without Angular semantics still
    emits an INJECTS edge — this is conservative, but the resolver can
    leave it unresolved if no Token matches."""
    f = _write(
        tmp_path,
        "plain.js",
        """\
        function inject(x) { return x; }
        const v = inject(Something);
        """,
    )
    ex = extract_file(f)
    assert ex is not None
    # Document current behavior: we always treat top-level inject() as DI.
    assert "Something" in ex.injects
