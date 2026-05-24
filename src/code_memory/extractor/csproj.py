"""Minimal `.csproj` parser — enough to populate Project graph nodes.

We deliberately don't try to be MSBuild. Real evaluation would need to
expand properties, follow `<Import>` chains, conditionalise on
configurations, etc. Almost none of that matters for "which projects
reference which, and which NuGet packages do they pull in" — which is
the question the graph needs to answer for cross-project navigation.

Anything we can't statically extract (PackageReference Update,
ProjectReference behind a property, MSBuild-evaluated paths) is
skipped. The output is a best-effort snapshot, not a build plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)


# Modern SDK-style csprojs ship with no XML namespace; legacy
# (pre-2017) ones use http://schemas.microsoft.com/developer/msbuild/2003.
# Strip namespaces on parse so both layouts feed the same selectors.
def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _local_iter(root: ET.Element, name: str):
    """Iterate descendants with the given local name, ignoring XML namespace."""
    for el in root.iter():
        if _strip_ns(el.tag) == name:
            yield el


@dataclass(frozen=True)
class PackageRef:
    name: str
    version: str | None


@dataclass
class CsprojInfo:
    """One project's externally-visible structure."""

    path: str
    name: str
    assembly_name: str | None = None
    target_framework: str | None = None
    project_references: list[str] = field(default_factory=list)  # absolute paths
    package_references: list[PackageRef] = field(default_factory=list)
    sdk_style: bool = True


def parse_csproj(csproj_path: str | Path) -> CsprojInfo | None:
    """Parse a single `.csproj` (or `.fsproj` / `.vbproj`) file.

    Returns ``None`` when the file isn't valid XML — the .NET tooling
    can technically accept comment-only or empty files in some
    scenarios; we'd rather skip than crash the ingest.
    """
    p = Path(csproj_path).resolve()
    try:
        tree = ET.parse(p)
    except (ET.ParseError, OSError) as e:
        log.warning("csproj: skipping %s — %s", p, e)
        return None

    root = tree.getroot()
    sdk_style = root.attrib.get("Sdk") is not None or _strip_ns(root.tag) == "Project"

    info = CsprojInfo(
        path=str(p),
        name=p.stem,
        sdk_style=sdk_style,
    )

    # Assembly name — falls back to project filename per MSBuild defaults.
    for el in _local_iter(root, "AssemblyName"):
        if el.text:
            info.assembly_name = el.text.strip()
            break
    if info.assembly_name is None:
        info.assembly_name = info.name

    # Target framework — prefer <TargetFramework>, fall back to
    # <TargetFrameworks> (multi-target; keep the raw list).
    for el in _local_iter(root, "TargetFramework"):
        if el.text:
            info.target_framework = el.text.strip()
            break
    if info.target_framework is None:
        for el in _local_iter(root, "TargetFrameworks"):
            if el.text:
                info.target_framework = el.text.strip()
                break

    base_dir = p.parent

    # ProjectReference Include="..\Foo\Foo.csproj"
    for el in _local_iter(root, "ProjectReference"):
        include = el.attrib.get("Include")
        if not include:
            continue
        resolved = _resolve_project_path(base_dir, include)
        if resolved is None:
            continue
        info.project_references.append(str(resolved))

    # PackageReference Include="Foo.Bar" Version="1.2.3"
    seen_packages: set[str] = set()
    for el in _local_iter(root, "PackageReference"):
        name = el.attrib.get("Include") or el.attrib.get("Update")
        if not name:
            continue
        if name in seen_packages:
            continue
        seen_packages.add(name)
        version = el.attrib.get("Version")
        if version is None:
            # Some teams pin via <Version> child + Central Package Management.
            child = el.find("./{*}Version") or el.find("Version")
            if child is not None and child.text:
                version = child.text.strip()
        info.package_references.append(PackageRef(name=name, version=version))

    return info


def _resolve_project_path(base_dir: Path, include: str) -> Path | None:
    """Resolve an MSBuild ProjectReference include path.

    Handles forward + backward slashes and bare relative paths. Skips
    references whose path doesn't exist on disk so we never emit dead
    Project nodes.
    """
    normalized = include.replace("\\", "/")
    candidate = (base_dir / normalized).resolve()
    if candidate.exists():
        return candidate
    return None


# Project file extensions worth walking. Includes F# and VB so a
# polyglot solution doesn't lose half its project graph.
_PROJECT_FILE_SUFFIXES = (".csproj", ".fsproj", ".vbproj")


def walk_csprojs(root: str | Path) -> list[CsprojInfo]:
    """Walk ``root`` for project files and return parsed ``CsprojInfo``."""
    out: list[CsprojInfo] = []
    root_path = Path(root).resolve()
    for ext in _PROJECT_FILE_SUFFIXES:
        for p in root_path.rglob(f"*{ext}"):
            # Skip obvious build outputs to keep the project graph
            # tight; these don't reflect source structure.
            if any(part in {"bin", "obj", "node_modules"} for part in p.parts):
                continue
            info = parse_csproj(p)
            if info is not None:
                out.append(info)
    return out
