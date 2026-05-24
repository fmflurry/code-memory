"""Chunk text format tests — verifies signature-first + tail-trim."""

from __future__ import annotations

from code_memory.extractor.treesitter import Symbol
from code_memory.orchestrator.pipeline import (
    MAX_SNIPPET_CHARS,
    SIGNATURE_LINES,
    _symbol_text,
)


def _sym(snippet: str, *, name: str = "doThing", kind: str = "function") -> Symbol:
    return Symbol(name=name, kind=kind, start_line=10, end_line=20, snippet=snippet)


def test_header_contains_path_kind_name() -> None:
    text = _symbol_text(_sym("body line\n"), "/repo/src/auth.ts")
    assert text.startswith("FILE /repo/src/auth.ts\nKIND function NAME doThing")


def test_signature_block_repeats_first_lines() -> None:
    snippet = "function doThing(input: string): boolean {\n  if (!input) return false;\n  return validate(input);\n}\n"
    text = _symbol_text(_sym(snippet), "/x.ts")
    assert "SIGNATURE" in text
    # signature region should contain the function declaration line
    sig_part = text.split("SIGNATURE\n", 1)[1].split("\n")[0:SIGNATURE_LINES]
    assert any("function doThing" in line for line in sig_part)


def test_body_tail_trimmed() -> None:
    big = "x = 1\n" * 1000  # ~6000 chars
    text = _symbol_text(_sym(big), "/x.py")
    # body capped at MAX_SNIPPET_CHARS (1500) + header/signature overhead
    assert len(text) < MAX_SNIPPET_CHARS + 500


def test_empty_snippet_still_produces_chunk() -> None:
    text = _symbol_text(_sym(""), "/x.ts")
    assert "FILE /x.ts" in text
    assert "KIND function NAME doThing" in text


def test_max_snippet_chars_is_under_2k() -> None:
    # Guard against regression toward the old 4000-char limit which
    # diluted m3 dense quality on long methods.
    assert MAX_SNIPPET_CHARS <= 2000
