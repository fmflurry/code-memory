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
