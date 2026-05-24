"""Resolve `.csproj` references to physical DLL paths.

The graph layer wants Assembly nodes keyed by `Name, Version=…`.
Csproj parsing gives us PackageReference + ProjectReference logical
identities; this module turns those into concrete `.dll` files on
disk so the metadata reader has something to open.

Two sources, in priority order:

1. **NuGet global cache** at ``$NUGET_PACKAGES`` or ``~/.nuget/packages``.
   Standard layout:
   ``{cache}/{pkg_lower}/{version}/lib/{tfm}/{Foo}.dll``.
2. **Build output** of project references:
   ``{ref_csproj_dir}/bin/{config}/{tfm}/{AssemblyName}.dll``.
   We pick Debug if both exist; the contents differ only in PDBs /
   optimisation, not in the public type surface that we index.

Either source can be missing (offline machine, build never run);
unresolved references are skipped — we never emit fictional paths.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .csproj import CsprojInfo, PackageRef


def nuget_cache_dirs() -> list[Path]:
    """Return candidate roots in priority order, deduped.

    ``NUGET_PACKAGES`` overrides the default. We also probe both the
    POSIX (``~/.nuget``) and Windows-style (``%USERPROFILE%\\.nuget``)
    locations because cross-platform repos sometimes drag both.
    """
    candidates: list[Path] = []
    env = os.environ.get("NUGET_PACKAGES")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / ".nuget" / "packages")
    # Dedupe while preserving order.
    seen: set[Path] = set()
    out: list[Path] = []
    for c in candidates:
        r = c.resolve() if c.exists() else c
        if r in seen:
            continue
        seen.add(r)
        if r.exists():
            out.append(r)
    return out


# TFM compatibility table — only the broad strokes. Real NuGet
# compatibility is a directed graph of moniker rules; we cover the
# common parent-fallback so resolution doesn't silently miss anything
# obvious. Order matters: earlier = preferred.
_TFM_FALLBACKS: dict[str, tuple[str, ...]] = {
    "net8.0": (
        "net8.0",
        "net7.0",
        "net6.0",
        "net5.0",
        "netstandard2.1",
        "netstandard2.0",
        "netstandard1.6",
    ),
    "net7.0": (
        "net7.0",
        "net6.0",
        "net5.0",
        "netstandard2.1",
        "netstandard2.0",
    ),
    "net6.0": (
        "net6.0",
        "net5.0",
        "netstandard2.1",
        "netstandard2.0",
    ),
    "net5.0": ("net5.0", "netstandard2.1", "netstandard2.0"),
    "netstandard2.1": ("netstandard2.1", "netstandard2.0"),
    "netstandard2.0": ("netstandard2.0", "netstandard1.6", "netstandard1.4"),
    "net48": ("net48", "net472", "net471", "net47", "net462", "net461", "net46", "net45", "netstandard2.0"),
    "net472": ("net472", "net471", "net47", "net462", "net461", "net46", "net45", "netstandard2.0"),
    "net471": ("net471", "net47", "net462", "net461", "net46", "net45", "netstandard2.0"),
    "net47": ("net47", "net462", "net461", "net46", "net45", "netstandard2.0"),
    "net46": ("net46", "net45", "netstandard1.4", "netstandard1.3"),
    "net45": ("net45", "netstandard1.3", "netstandard1.2"),
}


def _candidate_tfms(target: str | None) -> list[str]:
    """Return TFMs to try, in priority order, for a project's <TargetFramework>.

    Multi-target projects come in as a semicolon-joined string
    (``net6.0;net8.0``); we expand each side and concatenate fallback
    chains, then dedupe.
    """
    if not target:
        return ["netstandard2.0", "netstandard2.1", "net8.0", "net6.0"]
    chunks = [t.strip() for t in target.split(";") if t.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        for tfm in _TFM_FALLBACKS.get(c, (c,)):
            if tfm not in seen:
                seen.add(tfm)
                out.append(tfm)
    return out


def resolve_package_dlls(
    pkg: PackageRef, target_framework: str | None
) -> list[Path]:
    """Find DLLs for a single ``PackageReference`` in any local NuGet cache.

    Returns all DLLs under the best-matching TFM directory — most
    packages ship one or two assemblies per TFM but some (Roslyn,
    BouncyCastle) ship a bag.
    """
    caches = nuget_cache_dirs()
    if not caches:
        return []

    name_lower = pkg.name.lower()
    versions = _versions_for(caches, name_lower, pkg.version)
    tfms = _candidate_tfms(target_framework)

    results: list[Path] = []
    for cache in caches:
        for ver in versions:
            lib_root = cache / name_lower / ver / "lib"
            if not lib_root.is_dir():
                continue
            for tfm in tfms:
                tfm_dir = lib_root / tfm
                if tfm_dir.is_dir():
                    results.extend(sorted(tfm_dir.glob("*.dll")))
                    if results:
                        return results
            # No exact/fallback TFM match — let any flat DLL stand in
            # for older packages that ship `lib/*.dll` without a TFM
            # subdir. Better than nothing for net2.0-era libs.
            for dll in sorted(lib_root.glob("*.dll")):
                if dll.is_file():
                    results.append(dll)
            if results:
                return results
    return results


def _versions_for(
    caches: list[Path], name_lower: str, requested: str | None
) -> list[str]:
    """Pick which on-disk version directories to consider, best first.

    Requested version present on disk → use it directly. Otherwise
    return all versions sorted descending so we try newer first
    (lexicographic on padded chunks is good enough for the
    Major.Minor.Patch[-suffix] layout NuGet emits).
    """
    if requested:
        # Direct hit if the requested version directory exists in any cache.
        for cache in caches:
            if (cache / name_lower / requested).is_dir():
                return [requested]
    # Fall back to anything we have on disk.
    seen: set[str] = set()
    for cache in caches:
        d = cache / name_lower
        if not d.is_dir():
            continue
        for child in d.iterdir():
            if child.is_dir():
                seen.add(child.name)
    return sorted(seen, key=_version_sort_key, reverse=True)


def _version_sort_key(version: str) -> tuple[int, ...]:
    """Coarse semver-ish sort: split on dots, zero-pad missing chunks.

    Pre-release suffixes (``1.2.3-beta``) compare lower than their
    release equivalents because the int parse strips at the first
    non-digit. Good enough for "newest first" without dragging in a
    real packaging dep.
    """
    parts = version.split(".")
    out: list[int] = []
    for p in parts[:4]:  # cap to four segments
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 4:
        out.append(0)
    return tuple(out)


def resolve_project_reference_dlls(
    ref_csproj: Path, target_framework: str | None
) -> list[Path]:
    """Find the built DLL for a sibling project, if any.

    Looks under ``{proj_dir}/bin/{config}/{tfm}/`` for each candidate
    tfm in priority order, preferring Debug over Release because dev
    workstations build Debug by default and that's the file most
    likely to exist.
    """
    base = ref_csproj.parent
    tfms = _candidate_tfms(target_framework)
    for config in ("Debug", "Release"):
        for tfm in tfms:
            out = base / "bin" / config / tfm
            if out.is_dir():
                dlls = sorted(out.glob("*.dll"))
                if dlls:
                    return dlls
    # Older non-SDK projects sometimes drop output straight in bin/.
    flat = base / "bin"
    if flat.is_dir():
        return sorted(flat.glob("*.dll"))
    return []


@dataclass
class ResolvedRefs:
    """All DLL paths resolved for one csproj."""

    package_dlls: dict[str, list[Path]]  # PackageRef.name -> dlls
    project_dlls: dict[str, list[Path]]  # referenced csproj path -> dlls

    def all_paths(self) -> list[Path]:
        out: list[Path] = []
        for v in self.package_dlls.values():
            out.extend(v)
        for v in self.project_dlls.values():
            out.extend(v)
        return out


def resolve_refs(info: CsprojInfo) -> ResolvedRefs:
    """Resolve every PackageReference + ProjectReference of one csproj.

    Returns an empty ``ResolvedRefs`` rather than raising on missing
    NuGet cache / absent build outputs — callers care about "what did
    we manage to find" more than about failures.
    """
    package_dlls: dict[str, list[Path]] = {}
    for pkg in info.package_references:
        dlls = resolve_package_dlls(pkg, info.target_framework)
        if dlls:
            package_dlls[pkg.name] = dlls

    project_dlls: dict[str, list[Path]] = {}
    for ref_path in info.project_references:
        dlls = resolve_project_reference_dlls(Path(ref_path), info.target_framework)
        if dlls:
            project_dlls[ref_path] = dlls
    return ResolvedRefs(package_dlls=package_dlls, project_dlls=project_dlls)


def all_referenced_dlls(infos: Iterable[CsprojInfo]) -> set[Path]:
    """Deduplicated set of every DLL referenced by any project."""
    out: set[Path] = set()
    for info in infos:
        refs = resolve_refs(info)
        out.update(refs.all_paths())
    return out
