from __future__ import annotations

import pytest
from tree_sitter import Parser

from code_memory.extractor import treesitter


def test_parser_for_falls_back_to_language_pack_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    treesitter._parser_for.cache_clear()
    fallback_parser = treesitter.get_parser("python")

    def fake_get_language(lang: str) -> object:
        assert lang == "python"
        return object()

    def fake_get_parser(lang: str) -> Parser:
        assert lang == "python"
        return fallback_parser

    monkeypatch.setattr(treesitter, "get_language", fake_get_language)
    monkeypatch.setattr(treesitter, "get_parser", fake_get_parser)

    try:
        assert treesitter._parser_for("python") is fallback_parser
    finally:
        treesitter._parser_for.cache_clear()
