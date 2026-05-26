from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from tree_sitter import Language, Node, Parser
from tree_sitter_language_pack import get_language

LANG_BY_EXT: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".py": "python",
    # .NET ecosystem
    ".cs": "csharp",
    ".cshtml": "razor",
    ".razor": "razor",
    ".vb": "vb",
    ".fs": "fsharp",
    ".fsi": "fsharp",
    ".fsx": "fsharp",
}

SYMBOL_NODE_TYPES = {
    "function_declaration",
    "function_definition",
    "method_definition",
    "class_declaration",
    "class_definition",
    # TypeScript ``abstract class`` parses as its own node type; missing
    # it makes Angular clean-arch ports invisible to the graph, which
    # in turn leaves every ``inject(Port)`` edge unresolved.
    "abstract_class_declaration",
    "abstract_method_signature",
    "arrow_function",
    "export_statement",
    # C# / Razor (Razor embeds C#)
    "method_declaration",
    "interface_declaration",
    "struct_declaration",
    "record_declaration",
    "enum_declaration",
    "constructor_declaration",
    "delegate_declaration",
    "property_declaration",
    # VB.NET
    "class_block",
    "module_block",
    "namespace_block",
    # F#
    "function_or_value_defn",
    "type_definition",
    "method_or_prop_defn",
    "named_module",
}

CALL_NODE_TYPES = {
    "call_expression",
    "call",
    "invocation_expression",
    "invocation",  # VB
    # C# / VB / Razor: ``new Foo()`` parses as ``object_creation_expression``
    # rather than ``invocation_expression``. Without this, calls to
    # constructors (factories, DI registrations, ``new Builder().X()``) never
    # become CALLS edges, which is the #1 reason the call graph looks empty
    # on real .NET codebases.
    "object_creation_expression",
}

# Nodes that carry a type expression via a field named "type" (or "returns").
# When walking, look up these fields and harvest every identifier inside the
# type subtree. Covers C# parameter/field/property/variable/cast/typeof/is/as
# plus TypeScript/JS type annotations.
TYPE_FIELD_NODE_TYPES = {
    # C# declarations
    "parameter", "variable_declaration", "property_declaration",
    "field_declaration", "event_declaration", "indexer_declaration",
    "delegate_declaration", "method_declaration",
    # C# expressions referencing a type
    "cast_expression", "as_expression", "is_expression",
    "typeof_expression", "sizeof_expression", "default_expression",
    "array_creation_expression", "stack_alloc_array_creation_expression",
    # TS / JS
    "type_annotation", "type_alias_declaration",
    "as_expression",  # TS overlaps name
    "satisfies_expression",
}

# Nodes whose direct (non-punctuation) children ARE type expressions —
# walk every child as a type tree. ``base_list`` (`class X : Foo, IBar`),
# generic arguments, and constraint clauses fall here.
TYPE_CHILDREN_NODE_TYPES = {
    "base_list",                          # C#
    "type_argument_list",                 # C# generics
    "type_arguments",                     # TS/JS generics
    "type_parameter_constraints_clause",  # C# `where T : Foo`
    "implements_clause",                  # TS `implements Foo, Bar`
    "extends_clause",                     # TS `extends Foo`
    "extends_type_clause",                # TS interface extends
    "heritage_clause",                    # TS class heritage
    "tuple_type",                         # C# `(int, Foo)` — walk for Foo
    "tuple_element",
}

# Primitive / language-built-in type tokens — never emit as a reference.
# These usually appear as `predefined_type` nodes (skipped structurally) but
# some grammars emit them as bare identifiers in odd positions.
_PRIMITIVE_TYPE_NAMES: frozenset[str] = frozenset({
    # C#
    "void", "bool", "byte", "sbyte", "short", "ushort", "int", "uint",
    "long", "ulong", "float", "double", "decimal", "char", "string",
    "object", "dynamic", "var", "nint", "nuint",
    # TS/JS
    "any", "unknown", "never", "number", "boolean", "undefined", "null",
    "this", "symbol", "bigint",
})

IMPORT_NODE_TYPES = {
    "import_statement",
    "import_from_statement",
    "using_directive",  # C#
    "razor_using_directive",  # Razor
    "imports_statement",  # VB
    "import_decl",  # F#
}

# Razor / Blazor ``@inject TypeName Member`` directives. Each one is
# a DI dependency declaration that we want as a graph edge from the
# file to the injected type.
INJECT_NODE_TYPES = {
    "razor_inject_directive",
}


@dataclass
class Symbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    snippet: str
    namespace: str | None = None
    partial: bool = False
    # Parameter count for callable kinds (method_declaration,
    # function_declaration, ...). ``None`` when the kind is not
    # callable (class_declaration, etc.) or when the parser couldn't
    # locate a parameter_list child.
    param_count: int | None = None


@dataclass(frozen=True)
class Call:
    """One call site: ``name(args)`` with arity captured.

    Arity feeds the resolver's overload-disambiguation tier: when
    multiple definitions share the same name (classic C# / Java
    overload pattern), prefer the one whose parameter count matches.

    ``receiver_type`` is the inferred type of the call's receiver, set
    for TS ``this.<field>.<method>()`` patterns where the field's type
    can be read off a member initializer or annotation. The resolver
    uses it to narrow ``<method>`` to the methods defined on that type
    — without it, every Angular use case's call to its port collapses
    to an ambiguous bare identifier.
    """

    name: str
    arity: int
    receiver_type: str | None = None


@dataclass
class ExtractedFile:
    path: str
    lang: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    # DI declarations: list of injected type names (Razor ``@inject TypeName Member``).
    # Populated for ``.razor`` / ``.cshtml`` files; empty for other languages.
    injects: list[str] = field(default_factory=list)
    # Type-position name references: base lists (`class X : IFoo`), parameter
    # types, field/property types, generic args, type constraints, cast/is/as/
    # typeof targets, etc. Powers "who touches type X" queries (callers + refs).
    references: list[str] = field(default_factory=list)
    source: str = ""
    generated: bool = False


@lru_cache(maxsize=16)
def _parser_for(lang: str) -> Parser:
    language: Language = get_language(lang)
    return Parser(language)


def lang_for(path: str | Path) -> str | None:
    return LANG_BY_EXT.get(Path(path).suffix.lower())


MAX_FILE_BYTES = 500_000  # skip files larger than ~500KB (bundles, minified)
MAX_LINE_LEN = 2000  # likely minified if any line is this long
MINIFIED_SNIFF_BYTES = 4096  # bytes to inspect for minified-file heuristic
MINIFIED_AVG_LINE = 200  # avg line length above this in sniff window => minified

# Substrings that, when present in the first ~2KB of a file, mark it as
# auto-generated. These are case-insensitive contains checks.
GENERATED_HEADER_MARKERS = (
    "@generated",
    "auto-generated",
    "autogenerated",
    "code generated by",
    "do not edit",
    "this file was generated",
    "generated by openapi",
    "generated by swagger",
    "generated by ng-openapi-gen",
    "generated by openapi-generator",
)

# Path segments / suffixes that indicate generated output.
_GENERATED_PATH_PARTS = ("generated", "__generated__", "openapi-gen", "swagger-gen")
_GENERATED_PATH_SUFFIXES = (".generated.ts", ".generated.js", ".g.ts", ".g.dart")


def _has_generated_header(sample: str) -> bool:
    lower = sample[:2048].lower()
    return any(m in lower for m in GENERATED_HEADER_MARKERS)


def _has_generated_path(path: Path) -> bool:
    parts_lower = {part.lower() for part in path.parts}
    if any(p in parts_lower for p in _GENERATED_PATH_PARTS):
        return True
    name_lower = path.name.lower()
    return any(name_lower.endswith(suf) for suf in _GENERATED_PATH_SUFFIXES)


def looks_generated(path: str | Path, sample: str) -> bool:
    """Detect auto-generated code by path heuristics or header markers."""
    p = Path(path)
    return _has_generated_path(p) or _has_generated_header(sample)


def looks_minified(sample: str) -> bool:
    """Detect minified / pre-bundled JS without parsing.

    Triggers when:
    - the sniffed window has no newline (one giant line), or
    - the average line length within the sniffed window exceeds
      ``MINIFIED_AVG_LINE``, or
    - any line in the sniffed window exceeds ``MAX_LINE_LEN``.

    Vite/webpack dep caches and minified bundles all match at least one.
    """
    if not sample:
        return False
    if "\n" not in sample:
        return True
    lines = sample.splitlines()
    if any(len(line) > MAX_LINE_LEN for line in lines):
        return True
    avg = len(sample) / max(len(lines), 1)
    return avg > MINIFIED_AVG_LINE


def extract_file(path: str | Path) -> ExtractedFile | None:
    p = Path(path)
    lang = lang_for(p)
    if lang is None:
        return None
    try:
        size = p.stat().st_size
    except OSError:
        return None
    if size > MAX_FILE_BYTES:
        return None
    raw = p.read_bytes()
    # Strip a UTF-8 BOM if present so tree-sitter's byte offsets line up
    # with our slicing buffer. Some Windows-authored C# files ship one.
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    source = raw.decode("utf-8", errors="replace")
    if looks_minified(source[:MINIFIED_SNIFF_BYTES]):
        return None  # minified / bundled
    parser = _parser_for(lang)
    tree = parser.parse(raw)
    root = tree.root_node
    ex = ExtractedFile(
        path=str(p.resolve()),
        lang=lang,
        source=source,
        generated=looks_generated(p, source),
    )
    _walk(root, raw, ex, ns_stack=[], class_stack=[])
    return ex


# C# block-scoped ``namespace Foo { ... }``. Pushed while walking the
# block's children and popped on exit.
_BLOCK_NAMESPACE_NODE_TYPES = {"namespace_declaration"}

# C# 10 ``namespace Foo;`` (file-scoped). One per file by spec; applies
# to *everything after it*. We push without popping.
_FILE_SCOPED_NAMESPACE_NODE_TYPES = {"file_scoped_namespace_declaration"}

# Symbol kinds that can carry a ``partial`` modifier in C#. Partial
# classes / structs / interfaces / records get merged into a single
# logical entity in the graph; non-partial symbols stay file-scoped.
_PARTIAL_CAPABLE_KINDS = {
    "class_declaration",
    "struct_declaration",
    "interface_declaration",
    "record_declaration",
}

# Symbol kinds that take parameters — we record their arity for the
# resolver's overload disambiguation tier. Non-callable kinds
# (classes, modules, enums) skip the count.
_CALLABLE_KINDS = {
    "function_declaration",
    "function_definition",
    "method_definition",
    "method_declaration",
    "constructor_declaration",
    "delegate_declaration",
    "arrow_function",
    "function_or_value_defn",  # F#
}


def _is_partial_modifier(node: Node, source: bytes) -> bool:
    """``True`` when this is a ``modifier`` node carrying ``partial``."""
    if node.type != "modifier":
        return False
    text = _slice(source, node).strip()
    return text == "partial"


def _has_partial_modifier(node: Node, source: bytes) -> bool:
    return any(_is_partial_modifier(c, source) for c in node.children)


def _namespace_name(node: Node, source: bytes) -> str | None:
    """Return the dotted name of a C# namespace declaration."""
    for child in node.children:
        if child.type in {"qualified_name", "identifier"}:
            return _slice(source, child)
    return None


_CLASS_DECL_NODE_TYPES = frozenset(
    {"class_declaration", "abstract_class_declaration", "class"}
)


def _walk(
    node: Node,
    source: bytes,
    ex: ExtractedFile,
    ns_stack: list[str],
    class_stack: list[dict[str, str]],
) -> None:
    t = node.type
    pushed_ns = False
    pushed_class = False
    if t in _CLASS_DECL_NODE_TYPES:
        body = None
        for child in node.children:
            if child.type == "class_body":
                body = child
                break
        if body is not None:
            class_stack.append(_ts_class_field_types(body, source))
            pushed_class = True
    if t in _BLOCK_NAMESPACE_NODE_TYPES:
        ns = _namespace_name(node, source)
        if ns:
            ns_stack.append(ns)
            pushed_ns = True
    elif t in _FILE_SCOPED_NAMESPACE_NODE_TYPES:
        # C# 10 file-scoped namespace scopes the rest of the file.
        # Push and never pop within this walk — there is at most one.
        ns = _namespace_name(node, source)
        if ns:
            ns_stack.append(ns)

    if t in SYMBOL_NODE_TYPES:
        name = _symbol_name(node, source)
        if name:
            partial = (
                t in _PARTIAL_CAPABLE_KINDS and _has_partial_modifier(node, source)
            )
            param_count = _param_count(node) if t in _CALLABLE_KINDS else None
            ex.symbols.append(
                Symbol(
                    name=name,
                    kind=t,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    snippet=_slice(source, node),
                    namespace=".".join(ns_stack) if ns_stack else None,
                    partial=partial,
                    param_count=param_count,
                )
            )
    elif t in IMPORT_NODE_TYPES:
        mod = _import_module(node, source)
        if mod:
            ex.imports.append(mod)
    elif t in INJECT_NODE_TYPES:
        injected = _inject_type(node, source)
        if injected:
            ex.injects.append(injected)
    elif t in CALL_NODE_TYPES:
        # Angular DI: ``inject(Token)`` becomes an INJECTS edge instead
        # of a (stoplisted) CALL. Without this, the entire DI graph for
        # Angular 14+ codebases is invisible.
        token = _angular_inject_token(node, source)
        if token:
            ex.injects.append(token)
        else:
            callee = _callee_name(node, source)
            if callee:
                receiver_type: str | None = None
                if class_stack:
                    field = _this_field_receiver(node, source)
                    if field:
                        receiver_type = class_stack[-1].get(field)
                ex.calls.append(
                    Call(
                        name=callee,
                        arity=_call_arity(node),
                        receiver_type=receiver_type,
                    )
                )

    if t in TYPE_FIELD_NODE_TYPES:
        # ``method_declaration`` exposes the return type via ``returns``
        # in some grammars; everything else uses ``type``.
        type_node = node.child_by_field_name("type") or node.child_by_field_name(
            "returns"
        )
        if type_node is not None:
            _collect_type_refs(type_node, source, ex.references)
    if t in TYPE_CHILDREN_NODE_TYPES:
        for child in node.children:
            if child.type in {",", ":", "(", ")", "<", ">", "where", "extends", "implements"}:
                continue
            _collect_type_refs(child, source, ex.references)
    # C# pattern / cast / typeof: tree-sitter doesn't expose a `type`
    # field on these, so collect the type child positionally.
    if t == "cast_expression":
        # `(Type)expr` — type is the single child between `(` and `)`.
        between = []
        opened = False
        for child in node.children:
            if child.type == "(":
                opened = True
                continue
            if child.type == ")":
                break
            if opened:
                between.append(child)
        for c in between:
            _collect_type_refs(c, source, ex.references)
    elif t in {"as_expression", "is_expression"}:
        # `value as Type` / `value is Type` — type follows the keyword.
        keyword = "as" if t == "as_expression" else "is"
        seen_kw = False
        for child in node.children:
            if not seen_kw:
                if child.type == keyword:
                    seen_kw = True
                continue
            _collect_type_refs(child, source, ex.references)
    elif t == "is_pattern_expression":
        # `value is Pattern` — find declaration_pattern / type_pattern
        # children and pick their type identifier(s).
        for child in node.children:
            if child.type in {"declaration_pattern", "type_pattern", "recursive_pattern"}:
                # First identifier-bearing sub is the type name.
                for sub in child.children:
                    if sub.type in {"identifier", "type_identifier", "qualified_name", "generic_name"}:
                        _collect_type_refs(sub, source, ex.references)
                        break
    elif t in {"typeof_expression", "sizeof_expression", "default_expression"}:
        # `typeof(Type)` — type between the parens.
        opened = False
        for child in node.children:
            if child.type == "(":
                opened = True
                continue
            if child.type == ")":
                break
            if opened and child.type not in {","}:
                _collect_type_refs(child, source, ex.references)

    for child in node.children:
        _walk(child, source, ex, ns_stack, class_stack)

    if pushed_ns:
        ns_stack.pop()
    if pushed_class:
        class_stack.pop()


def _slice(source: bytes, node: Node) -> str:
    """Return UTF-8 text at the node's byte range.

    Tree-sitter reports byte offsets into the parsed buffer, not
    character offsets. Slicing a Python ``str`` with those offsets
    silently chops identifiers on files that contain any non-ASCII
    bytes (e.g. French C# with accents). Slicing ``bytes`` then
    decoding fixes the off-by-many-bytes drift.
    """
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


_FSHARP_DEEP_NAME_NODES = {
    "function_or_value_defn",
    "type_definition",
}


def _first_identifier_deep(node: Node, source: bytes) -> str | None:
    """BFS for the first identifier-bearing token inside ``node``."""
    queue: list[Node] = [node]
    while queue:
        current = queue.pop(0)
        for child in current.children:
            if child.type in {"identifier", "type_identifier"}:
                return _slice(source, child)
            queue.append(child)
    return None


def _symbol_name(node: Node, source: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return _slice(source, name)
    if node.type in _FSHARP_DEEP_NAME_NODES:
        return _first_identifier_deep(node, source)
    for child in node.children:
        if child.type in {"identifier", "type_identifier", "property_identifier"}:
            return _slice(source, child)
    return None


def _import_module(node: Node, source: bytes) -> str | None:
    # Python ``from X import Y`` and ``from .X import Y`` expose the
    # module via a ``module_name`` field. Without this branch the first
    # ``dotted_name`` child wins — which for ``from ..pkg.mod import Sym``
    # is ``Sym`` (the imported name), not the module. Result: the graph
    # files the import under the wrong key and ``importers <module>``
    # misses every relative caller.
    module_name_field = node.child_by_field_name("module_name") or node.child_by_field_name("name")
    if module_name_field is not None:
        return _slice(source, module_name_field).strip("'\"")
    for child in node.children:
        if child.type in {
            "string",
            "string_fragment",
            "dotted_name",
            "module_name",
            "relative_import",  # Python ``..pkg.mod``
            "qualified_name",
            "namespace_name",  # VB
            "long_identifier",  # F#
            "identifier",
        }:
            return _slice(source, child).strip("'\"")
    return None


_PARAMETER_LIST_TYPES = {
    "parameter_list",
    "formal_parameters",
    "parameters",  # F# / Python
}

_PARAMETER_NODE_TYPES = {
    "parameter",
    "required_parameter",
    "optional_parameter",
    "rest_parameter",
    "typed_parameter",
    "typed_default_parameter",
    "default_parameter",
    "identifier",  # F# value bindings expose bare identifiers
}


def _param_count(node: Node) -> int | None:
    """Count parameters of a callable declaration.

    Looks for a ``parameter_list`` (or grammar-specific equivalent)
    child and counts its parameter children, ignoring punctuation
    tokens like ``(``, ``)``, ``,``. Returns ``None`` when no
    parameter list child is found — that signals the caller to leave
    ``param_count`` unset rather than write a misleading 0.
    """
    for child in node.children:
        if child.type in _PARAMETER_LIST_TYPES:
            count = 0
            for sub in child.children:
                if sub.type in _PARAMETER_NODE_TYPES:
                    count += 1
            return count
    return None


def _call_arity(node: Node) -> int:
    """Count arguments at a call site.

    Returns the number of argument children in the call's argument
    list. Falls back to ``0`` when we can't find one — that matches
    what tree-sitter reports for property/field references parsed as
    invocation_expression (rare, but happens in C# generated code).
    """
    for child in node.children:
        if child.type in {"argument_list", "arguments"}:
            count = 0
            for sub in child.children:
                if sub.type in {"argument", "spread_element"}:
                    count += 1
                elif sub.type not in {"(", ")", ",", "{", "}"}:
                    # Some grammars (Python) emit expression children
                    # directly without an ``argument`` wrapper.
                    count += 1
            return count
    return 0


def _collect_type_refs(node: Node, source: bytes, out: list[str]) -> None:
    """Walk a type expression subtree, appending each referenced type name.

    Handles:
    - ``identifier`` / ``type_identifier`` → emit text
    - ``qualified_name`` / ``member_access_expression`` → emit right-most segment
    - ``generic_name`` → emit the generic's name + recurse into type_arguments
    - ``nullable_type`` / ``array_type`` / ``pointer_type`` → recurse into element
    - ``predefined_type`` / primitive identifiers → skip (no graph value)
    - ``tuple_type`` / ``tuple_element`` → recurse for inner names
    """
    t = node.type
    if t in {"predefined_type", "implicit_type", "this_type"}:
        return
    if t in {"identifier", "type_identifier"}:
        name = _slice(source, node).strip()
        if name and name not in _PRIMITIVE_TYPE_NAMES:
            out.append(name)
        return
    if t == "qualified_name":
        # ``Foo.Bar.Baz`` — recurse into the right-most type-bearing
        # child. Left segments are usually namespaces, not types. The
        # right-most can be a plain identifier (``Foo.Bar``), a
        # ``generic_name`` (``Foo.Bar.List<T>``), or another nested
        # qualified_name when grammars produce a left-leaning tree.
        last = None
        for child in node.children:
            if child.type in {
                "identifier",
                "type_identifier",
                "generic_name",
                "qualified_name",
            }:
                last = child
        if last is not None:
            _collect_type_refs(last, source, out)
        return
    if t == "generic_name":
        # ``List<int, Foo>`` — emit `List`, then recurse into the type args.
        for child in node.children:
            if child.type in {"identifier", "type_identifier"}:
                name = _slice(source, child).strip()
                if name and name not in _PRIMITIVE_TYPE_NAMES:
                    out.append(name)
                break
        for child in node.children:
            if child.type in {"type_argument_list", "type_arguments"}:
                for sub in child.children:
                    if sub.type in {"<", ">", ","}:
                        continue
                    _collect_type_refs(sub, source, out)
        return
    # Wrapper / composite type nodes — recurse to find inner type names.
    for child in node.children:
        _collect_type_refs(child, source, out)


_CLASS_BODY_NODE_TYPES = frozenset({"class_body", "object_type"})
_TS_FIELD_DECL_TYPES = frozenset(
    {
        "public_field_definition",
        "property_definition",
        "property_signature",
        "abstract_method_signature",
    }
)


def _ts_class_field_types(body: Node, source: bytes) -> dict[str, str]:
    """Map of ``field_name → type_name`` for a TS class body.

    Reads two sources per field:

    1. A type annotation (``private foo: Bar``) — the most reliable
       signal.
    2. An initializer of the form ``inject(Token)`` — Angular 14+ DI;
       lets a use case's injected port surface its type even when no
       explicit annotation is written.

    Also handles constructor parameter properties
    (``constructor(private foo: Bar) {}``), which TypeScript treats as
    fields. Without the constructor scan, Angular services that stick
    to the older ``constructor(private repo: Repo)`` style stay
    invisible to receiver-type resolution.
    """
    out: dict[str, str] = {}
    for child in body.children:
        if child.type in _TS_FIELD_DECL_TYPES:
            name_node = child.child_by_field_name("name")
            field_name: str | None = None
            for sub in child.children:
                if sub.type == "property_identifier":
                    field_name = _slice(source, sub)
                    break
            if name_node is not None:
                field_name = _slice(source, name_node)
            if not field_name:
                continue
            type_name = _ts_field_type_from_annotation(child, source)
            if type_name is None:
                type_name = _ts_field_type_from_inject_init(child, source)
            if type_name:
                out[field_name] = type_name
        elif child.type == "method_definition":
            # Constructor parameter properties live on the formal_parameters.
            name_node = child.child_by_field_name("name")
            method_name = _slice(source, name_node) if name_node else None
            if method_name != "constructor":
                continue
            params = child.child_by_field_name("parameters")
            if params is None:
                for sub in child.children:
                    if sub.type == "formal_parameters":
                        params = sub
                        break
            if params is None:
                continue
            for param in params.children:
                if param.type not in {"required_parameter", "optional_parameter"}:
                    continue
                # Only treat as a field when there is an accessibility modifier
                # (private/public/protected) — that's TS's "parameter property"
                # syntax. Plain ctor params live in local scope.
                has_modifier = any(
                    sub.type == "accessibility_modifier" for sub in param.children
                )
                if not has_modifier:
                    continue
                pname = None
                for sub in param.children:
                    if sub.type == "identifier":
                        pname = _slice(source, sub)
                        break
                if not pname:
                    continue
                type_name = _ts_field_type_from_annotation(param, source)
                if type_name:
                    out[pname] = type_name
    return out


def _ts_field_type_from_annotation(node: Node, source: bytes) -> str | None:
    """Read ``: <Type>`` annotation off a field / param node."""
    for child in node.children:
        if child.type == "type_annotation":
            for sub in child.children:
                if sub.type in {"type_identifier", "identifier"}:
                    return _slice(source, sub)
                if sub.type == "generic_type":
                    for inner in sub.children:
                        if inner.type in {"type_identifier", "identifier"}:
                            return _slice(source, inner)
                    return None
    return None


def _ts_field_type_from_inject_init(node: Node, source: bytes) -> str | None:
    """Read ``= inject(Token)`` initializer off a field node."""
    for child in node.children:
        if child.type == "call_expression":
            return _angular_inject_token(child, source)
    return None


def _this_field_receiver(node: Node, source: bytes) -> str | None:
    """For a callee ``this.<field>.<method>``, return ``<field>``.

    Other receiver shapes (chained calls, computed members, bare
    identifiers) return ``None`` — too ambiguous for the receiver-type
    table to help.
    """
    fn = node.child_by_field_name("function") or node.child_by_field_name("callee")
    if fn is None or fn.type != "member_expression":
        return None
    obj = fn.child_by_field_name("object")
    if obj is None or obj.type != "member_expression":
        return None
    inner_obj = obj.child_by_field_name("object")
    inner_prop = obj.child_by_field_name("property")
    if inner_obj is None or inner_obj.type != "this":
        return None
    if inner_prop is None or inner_prop.type != "property_identifier":
        return None
    return _slice(source, inner_prop)


def _angular_inject_token(node: Node, source: bytes) -> str | None:
    """Pull the DI token out of an Angular ``inject(Token)`` call.

    Angular 14+ replaced constructor-DI with the ``inject()`` primitive.
    Without this hook the call gets filtered by ``CALLEE_STOPLIST`` and
    the DI graph for any Angular codebase disappears entirely. We only
    accept call sites whose function is literally ``inject`` to avoid
    capturing user-defined functions of the same name in module scope.
    """
    fn = node.child_by_field_name("function") or node.child_by_field_name("callee")
    if fn is None:
        return None
    fn_text = _slice(source, fn).strip()
    # Drop generic args: ``inject<Token>`` parses as the bare identifier
    # in the function field; defensive split keeps qualified forms out.
    if fn_text.split("<", 1)[0] != "inject":
        return None
    args = None
    for child in node.children:
        if child.type in {"arguments", "argument_list"}:
            args = child
            break
    if args is None:
        return None
    for sub in args.children:
        if sub.type in {"(", ")", ",", "argument"}:
            if sub.type == "argument":
                # Some grammars wrap each arg in `argument`; descend.
                for inner in sub.children:
                    name = _last_identifier(_slice(source, inner).strip())
                    if name:
                        return name
            continue
        raw = _slice(source, sub).strip()
        name = _last_identifier(raw)
        if name:
            return name
    return None


def _inject_type(node: Node, source: bytes) -> str | None:
    """Pull the injected type name out of a Razor ``@inject`` directive.

    Grammar: ``@inject <Type> <Member>``. Tree-sitter wraps the
    `<Type> <Member>` pair in a ``variable_declaration``; the type is
    the first ``identifier`` / ``qualified_name`` / ``generic_name``
    child. We capture the **type name only** — for ``ILogger<Foo>``
    that's ``ILogger`` (the resolver matches by bare identifier;
    generic parameters live at the call site, not in the graph).
    """
    for child in node.children:
        if child.type == "variable_declaration":
            for sub in child.children:
                if sub.type in {"identifier", "qualified_name", "type_identifier"}:
                    return _slice(source, sub)
                if sub.type == "generic_name":
                    # Drop the ``<T, ...>`` tail by finding the first
                    # plain identifier under it.
                    for inner in sub.children:
                        if inner.type in {"identifier", "type_identifier"}:
                            return _slice(source, inner)
            break
    return None


# Callees that are stdlib / framework / RxJS / Angular DI built-ins.
# Filtered at extract time so they never enter the graph as CALLS edges;
# they pollute "who calls X" queries with high-frequency noise.
CALLEE_STOPLIST: frozenset[str] = frozenset(
    {
        # JS builtins
        "console", "JSON", "Math", "Object", "Array", "Promise", "Number",
        "String", "Boolean", "Date", "RegExp", "Symbol", "Map", "Set",
        "parseInt", "parseFloat", "isNaN", "isFinite",
        "setTimeout", "setInterval", "clearTimeout", "clearInterval",
        "fetch", "structuredClone", "queueMicrotask",
        # Angular DI / lifecycle
        "inject", "Injectable", "Component", "Directive", "Pipe", "NgModule",
        "Input", "Output", "ViewChild", "ContentChild", "HostListener",
        "HostBinding",
        # RxJS operators commonly chained via .pipe()
        "pipe", "subscribe", "map", "filter", "tap", "switchMap", "mergeMap",
        "concatMap", "exhaustMap", "catchError", "take", "takeUntil", "first",
        "of", "from", "EMPTY", "throwError", "combineLatest", "forkJoin",
        "BehaviorSubject", "Subject", "ReplaySubject",
        # Generic test helpers
        "describe", "it", "test", "expect", "beforeEach", "afterEach",
        "beforeAll", "afterAll", "jest", "vi", "spyOn",
    }
)


def _callee_name(node: Node, source: bytes) -> str | None:
    """Return the last identifier of a call expression's callee.

    For ``foo()`` → ``foo``. For ``this.svc.method()`` → ``method``.
    For ``a.b.c()`` → ``c``. Computed (``a[b]()``) and chained
    (``f()()``) callees collapse to ``None`` — too ambiguous to resolve.

    Returns ``None`` for callees in :data:`CALLEE_STOPLIST` so they don't
    enter the graph as noise.
    """
    # ``new Foo()`` exposes the constructor target under the ``type`` field;
    # plain calls use ``function`` / ``callee``. Without the ``type`` branch
    # the first-child fallback would land on the ``new`` keyword and miss
    # every constructor invocation.
    fn = (
        node.child_by_field_name("type")
        or node.child_by_field_name("function")
        or node.child_by_field_name("callee")
    )
    if fn is None and node.children:
        fn = node.children[0]
    if fn is None:
        return None
    raw = _slice(source, fn).split("(")[0].strip()
    name = _last_identifier(raw)
    if name is None or name in CALLEE_STOPLIST:
        return None
    return name


_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _last_identifier(expr: str) -> str | None:
    """Extract the trailing identifier from a (possibly chained) expression.

    ``this.foo.bar``     → ``bar``
    ``MyClass.staticFn`` → ``staticFn``
    ``foo``              → ``foo``
    ``arr[i]``           → ``None`` (computed)
    ``f()``              → ``None`` (chained call; shouldn't normally hit)
    """
    # Reject anything with brackets or calls in the trailing position.
    if expr.endswith("]") or expr.endswith(")"):
        return None
    parts = expr.split(".")
    last = parts[-1].strip()
    if not last:
        return None
    m = _IDENT_RE.fullmatch(last)
    return m.group(0) if m else None


DEFAULT_IGNORE_DIRS: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "out",
    "coverage",
    ".turbo",
    ".cache",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    # Angular / Vite / Nx / Yarn / Parcel / SvelteKit caches and tarballs
    ".angular",
    ".nx",
    ".yarn",
    ".parcel-cache",
    ".svelte-kit",
    "bower_components",
    "vendor",
    "tmp",
    # .NET build output / IDE caches
    "bin",
    "obj",
    "packages",
    "TestResults",
    ".vs",
    "artifacts",
)


class Extractor:
    """Convenience wrapper to walk a directory."""

    def __init__(
        self,
        ignore_dirs: tuple[str, ...] = DEFAULT_IGNORE_DIRS,
        *,
        respect_gitignore: bool = True,
    ) -> None:
        self.ignore_dirs = ignore_dirs
        self.respect_gitignore = respect_gitignore

    def walk(self, root: str | Path):
        from .gitignore import GitignoreMatcher

        root_path = Path(root).resolve()
        matcher = (
            GitignoreMatcher.from_root(root_path) if self.respect_gitignore else None
        )
        ignore_set = set(self.ignore_dirs)
        for p in root_path.rglob("*"):
            if not p.is_file():
                continue
            if any(part in ignore_set for part in p.parts):
                continue
            if matcher is not None and matcher.match(p, is_dir=False):
                continue
            ex = extract_file(p)
            if ex is not None:
                yield ex
