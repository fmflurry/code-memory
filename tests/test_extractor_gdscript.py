"""Godot 4 GDScript coverage for the tree-sitter extractor.

GDScript's grammar is close to Python but adds Godot-specific declarations
that the generic walker does not know about, so these tests pin the four
primitives the rest of the pipeline depends on:

- symbols: top-level ``class_name``, inner ``class`` definitions,
  ``func`` (incl. ``static func``) + parameter arity, ``_init``
  constructors (the grammar exposes *no* ``name`` field on
  ``constructor_definition`` — the ``_init`` token is the node's literal
  text), ``signal`` (arity), ``const``, ``var`` / ``@export var``
  properties, and ``enum`` declarations.
- imports: ``preload("res://...")`` / ``load("res://...")`` are parsed as
  plain ``call`` nodes; their string argument is surfaced as an import
  edge and the call itself is suppressed (it is a file dependency, not a
  callable).
- calls: ``call`` (``foo(args)``) and ``attribute_call``
  (``obj.method(args)``), each with call-site arity.
- references: ``extends`` inheritance (``extends Node2D`` /
  ``class_name Foo extends RefCounted``) plus parameter / return / const /
  variable type annotations — minus Godot's primitive scalars.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_memory.extractor.treesitter import Extractor, extract_file, lang_for

GDSCRIPT_SRC = """\
extends Node2D

class_name Foo

signal hit(amount: int)
signal ready_ref(target: Target)

enum State { IDLE, RUN }

const MAX_HP := 100
const DEFAULT_COLOR: Color = Color.WHITE

@export var speed: float = 10.0
var untyped = 3
var health: Health
var repo: Repo

class Inner:
    var value: int = 1

    func compute(a: int, b: int) -> Result:
        return make(a, b)

static func bar(x: int) -> int:
    return x

func _init() -> void:
    pass

func helper(who: Who) -> Signal:
    bar(1)
    GameState.reset()
    load("res://scenes/main.tscn")

const Pre = preload("res://foo.gd")
"""


def _extract(tmp_path: Path, name: str, body: str):
    f = tmp_path / name
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    ex = extract_file(f)
    assert ex is not None
    return ex


def test_extension_maps_to_gdscript() -> None:
    assert lang_for("Main.gd") == "gdscript"
    assert lang_for("scenes/player.GD") == "gdscript"
    assert lang_for("project.godot") is None


def test_extracts_symbols_with_arity(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "main.gd", GDSCRIPT_SRC)
    assert ex.lang == "gdscript"
    by_name = {s.name: s for s in ex.symbols}

    # top-level + inner class declarations
    assert "Foo" in by_name
    assert "Inner" in by_name

    # signals / constants / variables
    assert "hit" in by_name
    assert "ready_ref" in by_name
    assert "MAX_HP" in by_name
    assert "DEFAULT_COLOR" in by_name
    assert "speed" in by_name
    assert "untyped" in by_name
    assert "health" in by_name
    assert "repo" in by_name

    # functions / static functions / constructor
    assert {"bar", "helper", "compute", "_init"} <= set(by_name)

    # signal parameter arity
    assert by_name["hit"].param_count == 1
    assert by_name["ready_ref"].param_count == 1

    # function parameter arity
    assert by_name["bar"].param_count == 1
    assert by_name["compute"].param_count == 2
    assert by_name["helper"].param_count == 1

    # the constructor carries the implicit ``_init`` name and zero params
    ctor = by_name["_init"]
    assert ctor.kind == "constructor_definition"
    assert ctor.param_count == 0

    # enum surfaces under its declared name
    assert by_name["State"].kind == "enum_definition"


def test_extracts_preload_and_load_as_imports(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "main.gd", GDSCRIPT_SRC)
    assert "res://foo.gd" in ex.imports
    assert "res://scenes/main.tscn" in ex.imports


def test_preload_load_are_not_recorded_as_calls(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "main.gd", GDSCRIPT_SRC)
    calls = {c.name for c in ex.calls}
    assert "preload" not in calls
    assert "load" not in calls


def test_extracts_calls_with_arity(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "main.gd", GDSCRIPT_SRC)
    calls = {c.name for c in ex.calls}
    # plain call + attribute (method) call
    assert {"bar", "make", "reset"} <= calls
    by_call = {c.name: c.arity for c in ex.calls}
    assert by_call["bar"] == 1
    assert by_call["make"] == 2
    assert by_call["reset"] == 0


def test_extracts_type_references(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "main.gd", GDSCRIPT_SRC)
    refs = set(ex.references)
    # inheritance
    assert "Node2D" in refs
    # signal parameter / return / const / variable annotations
    assert {"Target", "Color", "Health", "Repo", "Result", "Who", "Signal"} <= refs
    # Godot primitive scalars never surface as references
    assert "int" not in refs
    assert "float" not in refs
    assert "void" not in refs


def test_inheritance_via_class_name_extends(tmp_path: Path) -> None:
    src = "class_name Foo extends RefCounted\n"
    ex = _extract(tmp_path, "foo.gd", src)
    assert "RefCounted" in set(ex.references)


def test_extractor_walk_yields_gdscript_file(tmp_path: Path) -> None:
    (tmp_path / "player.gd").write_text(GDSCRIPT_SRC, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not code", encoding="utf-8")
    # ``ignore_dirs=()`` avoids the pre-existing ``tmp`` entry in
    # ``DEFAULT_IGNORE_DIRS`` which matches every pytest temp path under
    # ``/tmp`` (unrelated to GDScript; tracked separately).
    results = list(Extractor(ignore_dirs=(), respect_gitignore=False).walk(tmp_path))
    langs = {ex.lang for ex in results}
    assert "gdscript" in langs
    paths = {Path(ex.path).name for ex in results}
    assert "player.gd" in paths
    assert "notes.txt" not in paths


SCOPE_SRC = """\
signal ping

const TOP_CONST: int = 1
var top_var: int = 2

@export var exported: int = 3

class Inner:
    const CLASS_CONST := 4
    var class_var: int = 5

    func method() -> void:
        var local: int = 6
        const LOCAL_CONST := 7
        class_var = local + LOCAL_CONST


func free_func() -> void:
    var free_local: int = 8
    const FREE_CONST := 9
"""


def test_function_locals_are_not_symbols(tmp_path: Path) -> None:
    ex = _extract(tmp_path, "scope.gd", SCOPE_SRC)
    names = {s.name for s in ex.symbols}

    # file-scope + class-scope properties / constants ARE symbols
    assert {"TOP_CONST", "top_var", "exported", "CLASS_CONST", "class_var"} <= names
    # the class method and the free function are symbols
    assert {"method", "free_func", "ping"} <= names

    # function-local ``var`` / ``const`` must NOT leak in as symbols
    assert "local" not in names
    assert "LOCAL_CONST" not in names
    assert "free_local" not in names
    assert "FREE_CONST" not in names


def test_engine_builtin_types_are_emitted_as_references(tmp_path: Path) -> None:
    """v1 limitation: only scalar primitives are filtered.

    Godot engine types (Vector2, Color, Signal, ...) are deliberately
    emitted as references so ``who references X`` still finds them.
    """
    src = "var pos: Vector2\nvar tint: Color\nvar bus: Signal\n"
    ex = _extract(tmp_path, "engine.gd", src)
    refs = set(ex.references)
    assert {"Vector2", "Color", "Signal"} <= refs


def test_load_callee_shadowing_is_documented_limitation(tmp_path: Path) -> None:
    """v1 caveat: ``preload`` / ``load`` are matched by bare text.

    A user-defined ``func load(p)`` is indistinguishable from the builtin at
    extract time, so the call is suppressed and an import edge is emitted.
    This pins the accepted behaviour so a future scope-aware fix flips it
    deliberately.
    """
    src = 'func load(p: String) -> String:\n    return p\n\n\nfunc f() -> void:\n    load("x")\n'
    ex = _extract(tmp_path, "shadow.gd", src)
    assert "x" in ex.imports
    assert "load" not in {c.name for c in ex.calls}
