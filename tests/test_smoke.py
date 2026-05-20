"""Pure smoke tests — no infra required."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import extract_file, lang_for


def test_lang_for() -> None:
    assert lang_for("a.ts") == "typescript"
    assert lang_for("a.tsx") == "tsx"
    assert lang_for("a.py") == "python"
    assert lang_for("a.unknown") is None


def test_extract_ts(tmp_path: Path) -> None:
    f = tmp_path / "sample.ts"
    f.write_text(
        textwrap.dedent(
            """
            import { foo } from 'bar';

            export function hello(name: string): string {
              return greet(name);
            }
            """
        ).strip()
    )
    ex = extract_file(f)
    assert ex is not None
    assert ex.lang == "typescript"
    names = [s.name for s in ex.symbols]
    assert "hello" in names
    assert any("bar" in m for m in ex.imports)
    assert "greet" in ex.calls


def test_extract_py(tmp_path: Path) -> None:
    f = tmp_path / "sample.py"
    f.write_text(
        textwrap.dedent(
            """
            import os

            def add(a, b):
                return os.path.join(str(a), str(b))
            """
        ).strip()
    )
    ex = extract_file(f)
    assert ex is not None
    assert ex.lang == "python"
    assert "add" in [s.name for s in ex.symbols]
    assert "os" in ex.imports
