"""Dart / Flutter coverage for the tree-sitter extractor.

Dart's grammar differs structurally from the other supported languages,
so these tests pin the four primitives the rest of the pipeline depends on:

- symbols: classes, mixins, enums, extensions, typedefs, top-level
  functions, methods, getters/setters, constructors (+ parameter arity).
  Dart function names sit *after* the return type inside
  ``function_signature``; the generic name fallback would grab the return
  type, so this guards the dedicated first-``identifier`` lookup.
- imports: ``import 'package:...';`` / ``export 'src/...';`` — the URI is
  buried under ``configurable_uri > uri`` and needs digging out.
- calls: Dart has no single call-expression node; a call is an
  ``identifier`` / ``selector`` followed by a ``selector`` wrapping an
  ``argument_part``. Callee recovery walks back to the preceding sibling.
- references: inheritance (``extends`` / ``implements`` / ``with``),
  field / parameter / return types, generic arguments — minus primitives.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import extract_file, lang_for

DART_SRC = """\
import 'dart:async';
import 'package:flutter/material.dart';
import 'src/foo.dart' as foo;
export 'src/bar.dart';

class Greeter extends Base implements Greetable with LoggerMixin {
  final Repo repo;
  String name;
  Greeter(this.repo, this.name);

  Future<User?> hello(String who) async {
    final svc = Service();
    repo.save(who);
    return greet(who);
  }

  String get displayName => name;
  set displayName(String v) => name = v;
}

abstract class Greetable {
  String greet(String who);
}

mixin LoggerMixin {
  void log(String m) { print(m); }
}

enum Color { red, green, blue }

extension StringX on String {
  String shout() => toUpperCase();
}

typedef IntCallback = void Function(int);

int topLevel(int a, int b) => a + b;

void main() {
  final g = Greeter(Repo(), "x");
  g.hello("world");
  topLevel(1, 2);
}
"""


def _extract(tmp_path: Path, name: str, body: str):
    f = tmp_path / name
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    ex = extract_file(f)
    assert ex is not None
    return ex


def test_extension_maps_to_dart() -> None:
    assert lang_for("main.dart") == "dart"
    assert lang_for("lib/widgets/button.DART") == "dart"
    assert lang_for("pubspec.yaml") is None


def test_extracts_symbols_with_arity(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "greeter.dart", DART_SRC)
    assert ex.lang == "dart"
    by_name = {s.name: s for s in ex.symbols}

    # classes / mixin / enum / extension / typedef
    assert {
        "Greeter",
        "Greetable",
        "LoggerMixin",
        "Color",
        "StringX",
        "IntCallback",
    } <= set(by_name)

    # top-level + member functions resolve their NAME, not the return type
    assert {"hello", "greet", "log", "shout", "topLevel", "main"} <= set(by_name)

    # getters / setters keyed by member name
    assert "displayName" in by_name

    # parameter arity on callables
    assert by_name["topLevel"].param_count == 2
    assert by_name["hello"].param_count == 1
    assert by_name["main"].param_count == 0

    # the constructor is captured with its parameter count
    ctors = [s for s in ex.symbols if s.kind == "constructor_signature"]
    assert any(c.name == "Greeter" and c.param_count == 2 for c in ctors)


def test_extracts_import_and_export_uris(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "greeter.dart", DART_SRC)
    assert {
        "dart:async",
        "package:flutter/material.dart",
        "src/foo.dart",
        "src/bar.dart",
    } <= set(ex.imports)


def test_extracts_calls_and_filters_print(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "greeter.dart", DART_SRC)
    calls = {c.name for c in ex.calls}
    # plain calls, constructor calls, and method calls
    assert {"Service", "save", "greet", "Greeter", "Repo", "hello", "topLevel"} <= calls
    # ``print`` is stoplisted as noise
    assert "print" not in calls
    # arity is recorded at the call site
    by_call = {c.name: c.arity for c in ex.calls}
    assert by_call["topLevel"] == 2
    assert by_call["save"] == 1


def test_extracts_type_references(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "greeter.dart", DART_SRC)
    refs = set(ex.references)
    # inheritance: extends / implements / with
    assert {"Base", "Greetable", "LoggerMixin"} <= refs
    # field type + generic return-type arguments
    assert {"Repo", "Future", "User"} <= refs
    # core scalars are treated as primitives, never emitted as references
    assert "String" not in refs
    assert "int" not in refs
