"""Regression test: extractor must slice by bytes, not by str chars.

Tree-sitter reports byte offsets. Slicing a Python ``str`` with those
offsets silently truncates identifiers on any file with non-ASCII
content above the symbol — common in French/German/Spanish codebases.
This file makes sure C#, Python and TypeScript all survive a UTF-8
preamble.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import extract_file


def test_csharp_with_french_accents(tmp_path: Path) -> None:
    f = tmp_path / "CommandeRules.cs"
    f.write_text(
        textwrap.dedent(
            """\
            // Règles métier pour la gestion des commandes — accents éàùç
            using System;
            using Acme.Sample.Common;

            namespace Acme.Sample.Documents
            {
                public class CommandeRules
                {
                    private readonly string _référence;

                    public CommandeRules(string référence)
                    {
                        _référence = référence;
                    }

                    public void InitialiserDocument()
                    {
                        Console.WriteLine(_référence);
                        DonnerDocumentRules();
                    }

                    private void DonnerDocumentRules() { }
                }
            }
            """
        ),
        encoding="utf-8",
    )
    ex = extract_file(f)
    assert ex is not None
    names = {s.name for s in ex.symbols}
    assert "CommandeRules" in names, f"got: {names}"
    assert "InitialiserDocument" in names, f"got: {names}"
    assert "DonnerDocumentRules" in names, f"got: {names}"
    assert "System" in ex.imports
    assert "Acme.Sample.Common" in ex.imports
    # Callee resolution should also produce un-truncated names.
    assert "InitialiserDocument" in ex.calls or "DonnerDocumentRules" in ex.calls


def test_csharp_with_utf8_bom(tmp_path: Path) -> None:
    """Some Windows-authored C# files ship a UTF-8 BOM. Extractor must strip it."""
    f = tmp_path / "WithBom.cs"
    body = (
        "namespace Demo\n"
        "{\n"
        "    public class WithBom\n"
        "    {\n"
        "        public void Faire() { }\n"
        "    }\n"
        "}\n"
    )
    f.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    ex = extract_file(f)
    assert ex is not None
    names = {s.name for s in ex.symbols}
    assert "WithBom" in names
    assert "Faire" in names


def test_python_with_unicode_docstring(tmp_path: Path) -> None:
    f = tmp_path / "u.py"
    f.write_text(
        textwrap.dedent(
            '''\
            """Module docstring with é à ç that pushes byte offsets."""

            def faire_quelque_chose():
                return "fini"
            '''
        ),
        encoding="utf-8",
    )
    ex = extract_file(f)
    assert ex is not None
    assert "faire_quelque_chose" in {s.name for s in ex.symbols}


def test_typescript_with_unicode_comments(tmp_path: Path) -> None:
    f = tmp_path / "u.ts"
    f.write_text(
        textwrap.dedent(
            """\
            // Composant gérant les commandes — éàçù
            import { Injectable } from '@angular/core';

            @Injectable({ providedIn: 'root' })
            export class CommandeService {
              public charger(): void {
                console.log('chargé');
              }
            }
            """
        ),
        encoding="utf-8",
    )
    ex = extract_file(f)
    assert ex is not None
    names = {s.name for s in ex.symbols}
    assert "CommandeService" in names
    assert "charger" in names
