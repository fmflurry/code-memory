"""Read .NET assembly metadata from PE files.

Implementation: pure-Python via ``dnfile`` (read-only PE/ECMA-335
parser). No .NET runtime required; we ingest binaries even on hosts
that have never had `dotnet` installed.

Scope of this module is deliberately narrow:

* one ``AssemblyInfo`` per DLL — identity (name + version) + flat list
  of public ``TypeRef`` entries.
* private / internal / nested-non-public types are dropped at parse
  time. Indexing implementation types would balloon the graph without
  buying the agent anything; only the public surface is reachable
  from other assemblies anyway.
* no member-level data (methods, properties, fields). The schema
  decision in this PR is "Assembly + public Type only"; members can
  be added later as a separate layer.

The reader is best-effort. Corrupt PE files, native DLLs that happen
to have a `.dll` extension, and assemblies without a CLR header are
all skipped quietly so a single bad file in `bin/` doesn't kill an
ingest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TypeRef:
    """One public type exposed by an assembly."""

    namespace: str
    name: str
    kind: str  # "class" | "interface" | "struct" | "enum" | "delegate"
    sealed: bool = False


@dataclass(frozen=True)
class MemberRef:
    """One public member of a Type — methods only at this layer.

    Kept narrow on purpose: properties + events + fields can be added
    when an agent needs them, but methods are what call-site
    resolution will actually disambiguate against. Listing every
    private field of every NuGet type would balloon the graph for
    no return.
    """

    name: str
    kind: str  # "method" | "constructor"
    static: bool
    params: int  # parameter count (without ``this``)


@dataclass
class AssemblyInfo:
    """Top-level result of parsing one DLL."""

    path: str
    name: str
    version: str
    public_key_token: str | None = None
    types: list[TypeRef] = field(default_factory=list)

    @property
    def identity(self) -> str:
        """Canonical key for the graph: ``Name, Version=X.Y.Z.W``.

        Distinct versions of the same assembly are distinct nodes so a
        repo using both `Foo 1.0` and `Foo 2.0` (via separate
        ProjectReferences) doesn't accidentally collapse them.
        """
        return f"{self.name}, Version={self.version}"


def parse_assembly(dll_path: str | Path) -> AssemblyInfo | None:
    """Parse one DLL into :class:`AssemblyInfo`. Returns ``None`` on failure.

    Failures we silence:

    * Native (non-CLR) DLLs — common in `bin/` for projects pulling in
      C++ helpers. ``dnfile`` raises when the CLR header is missing.
    * Corrupted / truncated PE files.
    * Permission denied.

    Failures we propagate: nothing — DLL parsing must not abort an
    ingest. The caller (Pipeline) treats ``None`` as "skip silently".
    """
    p = Path(dll_path).resolve()
    try:
        # Import lazily so the rest of the package stays importable
        # without the optional ``[dotnet]`` extra installed.
        import dnfile
    except ImportError:
        log.warning(
            "dll: dnfile not installed; install code-memory[dotnet] "
            "to index .NET assemblies"
        )
        return None

    try:
        pe = dnfile.dnPE(str(p), fast_load=True)
        pe.parse_data_directories()
    except Exception as e:  # noqa: BLE001 — dnfile raises many subclasses
        log.debug("dll: failed to parse %s — %s", p, e)
        return None

    if pe.net is None or pe.net.mdtables is None:
        return None  # not a managed assembly

    asm_table = pe.net.mdtables.Assembly
    if asm_table is None or asm_table.num_rows == 0:
        # `.dll` that's a netmodule, not a standalone assembly. Skip.
        return None
    asm_row = asm_table.rows[0]
    name = _row_text(asm_row, "Name")
    if not name:
        return None
    version = (
        f"{asm_row.MajorVersion}.{asm_row.MinorVersion}."
        f"{asm_row.BuildNumber}.{asm_row.RevisionNumber}"
    )

    info = AssemblyInfo(
        path=str(p),
        name=name,
        version=version,
        public_key_token=_pub_key_token(asm_row),
    )

    td_table = pe.net.mdtables.TypeDef
    if td_table is not None:
        for row in td_table.rows:
            tref = _typedef_to_ref(row)
            if tref is not None:
                info.types.append(tref)
    return info


# --------------------------------------------------------------- internals


def _row_text(row: object, attr: str) -> str | None:
    """Pull a string field off an mdtable row; dnfile returns plain strs already."""
    value = getattr(row, attr, None)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _pub_key_token(asm_row: object) -> str | None:
    """Return the public-key-token (lowercase hex) if the assembly has one.

    The token is the last 8 bytes of the public key's SHA-1, byte-
    reversed (the .NET convention). Returns ``None`` for unsigned
    assemblies and any extraction failure — token is metadata, not
    structural data, so silence is fine.
    """
    pk = getattr(asm_row, "PublicKey", None)
    if pk is None:
        return None
    # dnfile wraps blobs in ``HeapItemBinary``. ``value`` is the
    # straight bytes attribute; ``value_bytes`` is a method on newer
    # releases. Try in order, treating callables as method-getters.
    blob = b""
    for attr in ("value", "value_bytes", "raw_data"):
        v = getattr(pk, attr, None)
        if v is None:
            continue
        if callable(v):
            try:
                v = v()
            except Exception:  # noqa: BLE001
                continue
        if isinstance(v, (bytes, bytearray)) and v:
            blob = bytes(v)
            break
    if isinstance(pk, (bytes, bytearray)) and not blob:
        blob = bytes(pk)
    if not blob:
        return None
    import hashlib

    return hashlib.sha1(blob).digest()[-8:][::-1].hex()


def _typedef_to_ref(row: object) -> TypeRef | None:
    """Translate one TypeDef row into a public :class:`TypeRef`, or None.

    Filters out:
    * compiler-synthesised ``<Module>`` pseudo-type
    * non-public / non-nested-public types
    * nested types whose enclosing visibility is private — they're
      noise for cross-assembly use even if their own flag is public.
      We approximate via the TypeNamespace check: nested types live
      under their enclosing type via the NestedClass table, which we
      don't walk here; for the public-surface use case, top-level
      types are the right cut.
    """
    namespace = _row_text(row, "TypeNamespace") or ""
    name = _row_text(row, "TypeName") or ""
    if not name or name == "<Module>":
        return None

    flags = getattr(row, "Flags", None)
    if flags is None:
        return None
    # Visibility: keep public top-level (tdPublic) and public nested
    # (tdNestedPublic). Drop everything else.
    if not (
        getattr(flags, "tdPublic", False) or getattr(flags, "tdNestedPublic", False)
    ):
        return None

    kind = _classify_type(flags)
    sealed = bool(getattr(flags, "tdSealed", False))

    return TypeRef(namespace=namespace, name=name, kind=kind, sealed=sealed)


def _classify_type(flags: object) -> str:
    """Derive a coarse kind from TypeDef flags + parent (best-effort).

    Real-precise kind classification needs the BaseType pointer
    (e.g. inherits ``System.Enum`` ⇒ enum, ``System.Delegate`` ⇒
    delegate). We don't walk that here — coarse ``class`` /
    ``interface`` / ``struct`` is enough for "what types does this
    assembly expose" answers. ``enum`` and ``delegate`` get folded
    into ``class`` and ``struct`` respectively.
    """
    if getattr(flags, "tdInterface", False):
        return "interface"
    # Layout flags hint at value types. tdSequentialLayout /
    # tdExplicitLayout typically mean a struct.
    if getattr(flags, "tdSequentialLayout", False) or getattr(
        flags, "tdExplicitLayout", False
    ):
        return "struct"
    return "class"


# --------------------------------------------------------------- batch


def walk_dlls(paths: list[str | Path]) -> list[AssemblyInfo]:
    """Parse a precomputed list of DLL paths, skipping failures.

    Caller is responsible for path resolution (the NuGet / output-dir
    walker lives in ``code_memory.extractor.nuget``); this helper just
    fans the parse out so the pipeline stays linear.
    """
    out: list[AssemblyInfo] = []
    for p in paths:
        info = parse_assembly(p)
        if info is not None:
            out.append(info)
    return out


# --------------------------------------------------------------- members (on-demand)


def parse_type_members(
    dll_path: str | Path,
    namespace: str,
    name: str,
) -> list[MemberRef] | None:
    """Return the public methods declared on ``namespace.name`` in ``dll_path``.

    Read-once, no caching — designed to back an MCP tool that queries
    members lazily rather than bulk-indexing every member of every
    referenced assembly (which would multiply the graph by 50-100x).

    Returns:
    * a list (possibly empty for a type with no public methods),
    * ``None`` when the assembly can't be parsed, the type isn't
      found, or dnfile isn't installed.
    """
    p = Path(dll_path).resolve()
    try:
        import dnfile
    except ImportError:
        return None
    try:
        pe = dnfile.dnPE(str(p), fast_load=True)
        pe.parse_data_directories()
    except Exception:  # noqa: BLE001
        return None
    if pe.net is None or pe.net.mdtables is None:
        return None

    td_table = pe.net.mdtables.TypeDef
    if td_table is None:
        return None

    target_row = None
    target_idx = None
    for i, row in enumerate(td_table.rows):
        if _row_text(row, "TypeName") == name and (_row_text(row, "TypeNamespace") or "") == namespace:
            target_row = row
            target_idx = i
            break
    if target_row is None or target_idx is None:
        return None

    methods = _members_for_type(td_table, target_row, target_idx)
    return methods


def _members_for_type(
    td_table: object, row: object, idx: int
) -> list[MemberRef]:
    """Return public methods declared directly on this TypeDef.

    Methods inherited from base types are NOT listed — the row's
    MethodList only contains declarations local to the type. Adding
    inherited members requires walking the BaseType pointer chain,
    which we skip for the same balloon-the-graph reason as bulk
    members.
    """
    method_list = getattr(row, "MethodList", None)
    if not method_list:
        return []

    # The next TypeDef row's MethodList tells us where this row's
    # methods end. dnfile resolves the inclusive range for us via the
    # MDTableIndex pointers — each entry is one MethodDef row.
    out: list[MemberRef] = []
    seen: set[tuple[str, int, bool]] = set()
    for idx_ref in method_list:
        try:
            method_row = idx_ref.table.rows[idx_ref.row_index - 1]
        except (AttributeError, IndexError):
            continue
        flags = getattr(method_row, "Flags", None)
        if flags is None:
            continue
        if not getattr(flags, "mdPublic", False):
            continue
        name = _row_text(method_row, "Name") or ""
        if not name:
            continue
        is_ctor = name in (".ctor", ".cctor")
        param_count = _method_param_count(method_row)
        static = bool(getattr(flags, "mdStatic", False))
        key = (name, param_count, static)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            MemberRef(
                name=name,
                kind="constructor" if is_ctor else "method",
                static=static,
                params=param_count,
            )
        )
    return out


def _method_param_count(method_row: object) -> int:
    """Best-effort param count from the MethodDef's ParamList length.

    The ParamList includes the return value slot for some signatures
    (when the method has marshalling/attribute metadata on its
    return). We can't disambiguate that without parsing the method
    signature blob — which is out of scope here. Off-by-one on rare
    methods is acceptable; the goal is overload disambiguation, not
    exact reflection.
    """
    plist = getattr(method_row, "ParamList", None)
    if plist is None:
        return 0
    try:
        return len(plist)
    except TypeError:
        return 0
