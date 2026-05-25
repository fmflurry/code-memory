"""Python import extraction covers both absolute and relative forms.

Regression: before this fix, ``from ..graph.falkor_store import X`` lost
the module path entirely — the extractor stored ``X`` (the imported
name) as the module key. The graph then filed the IMPORTS edge under
the wrong target and ``importers code_memory.graph.falkor_store``
missed every file that wrote the relative form.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from code_memory.extractor.treesitter import extract_file


def _imports(source: str) -> list[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        # Pad with a body line so the file isn't classified as minified
        # (the heuristic flags single-line samples).
        f.write(source + "\nx = 1\n")
        path = f.name
    ex = extract_file(path)
    Path(path).unlink(missing_ok=True)
    assert ex is not None
    return ex.imports


def test_absolute_from_import_captured() -> None:
    assert "code_memory.graph.falkor_store" in _imports(
        "from code_memory.graph.falkor_store import FalkorStore"
    )


def test_relative_from_import_keeps_dots() -> None:
    assert "..graph.falkor_store" in _imports(
        "from ..graph.falkor_store import FalkorStore"
    )


def test_single_dot_relative_import_captured() -> None:
    assert ".falkor_store" in _imports("from .falkor_store import FalkorStore")


def test_plain_import_captured() -> None:
    assert "os" in _imports("import os")


def test_imported_name_is_not_module() -> None:
    """The imported symbol must NOT be confused with the module path."""
    imports = _imports("from ..graph.falkor_store import FalkorStore")
    assert "FalkorStore" not in imports
