from __future__ import annotations

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
}

SYMBOL_NODE_TYPES = {
    "function_declaration",
    "function_definition",
    "method_definition",
    "class_declaration",
    "class_definition",
    "arrow_function",
    "export_statement",
}

CALL_NODE_TYPES = {"call_expression", "call"}

IMPORT_NODE_TYPES = {"import_statement", "import_from_statement"}


@dataclass
class Symbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    snippet: str


@dataclass
class ExtractedFile:
    path: str
    lang: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    source: str = ""


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
    source = p.read_text(encoding="utf-8", errors="replace")
    if looks_minified(source[:MINIFIED_SNIFF_BYTES]):
        return None  # minified / bundled / generated
    parser = _parser_for(lang)
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node
    ex = ExtractedFile(path=str(p.resolve()), lang=lang, source=source)
    _walk(root, source, ex)
    return ex


def _walk(node: Node, source: str, ex: ExtractedFile) -> None:
    t = node.type
    if t in SYMBOL_NODE_TYPES:
        name = _symbol_name(node, source)
        if name:
            ex.symbols.append(
                Symbol(
                    name=name,
                    kind=t,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    snippet=_slice(source, node),
                )
            )
    elif t in IMPORT_NODE_TYPES:
        mod = _import_module(node, source)
        if mod:
            ex.imports.append(mod)
    elif t in CALL_NODE_TYPES:
        callee = _callee_name(node, source)
        if callee:
            ex.calls.append(callee)
    for child in node.children:
        _walk(child, source, ex)


def _slice(source: str, node: Node) -> str:
    return source[node.start_byte : node.end_byte]


def _symbol_name(node: Node, source: str) -> str | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return _slice(source, name)
    for child in node.children:
        if child.type in {"identifier", "type_identifier"}:
            return _slice(source, child)
    return None


def _import_module(node: Node, source: str) -> str | None:
    for child in node.children:
        if child.type in {"string", "string_fragment", "dotted_name", "module_name"}:
            return _slice(source, child).strip("'\"")
    return None


def _callee_name(node: Node, source: str) -> str | None:
    fn = node.child_by_field_name("function") or node.child_by_field_name("callee")
    if fn is None and node.children:
        fn = node.children[0]
    if fn is None:
        return None
    return _slice(source, fn).split("(")[0].strip()


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
