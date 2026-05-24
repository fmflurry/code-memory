"""Minimal Visual Studio `.sln` parser.

A solution file is a project group. The parser pulls out just the
project list — name, csproj path, and a stable GUID — which is enough
to wire ``Solution`` graph nodes and ``MEMBER_OF`` edges from each
indexed Project to its containing Solution.

Folders ("solution items") are skipped; we want code projects, not
the IDE's tree organization. Unparseable lines are dropped silently
so a single corrupted entry never aborts the ingest.

Format reference (informal — Microsoft never published a real
grammar): https://learn.microsoft.com/en-us/visualstudio/extensibility/internals/solution-dot-sln-file
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ``Project("{type-guid}") = "Name", "RelativePath.csproj", "{proj-guid}"``
_PROJECT_LINE = re.compile(
    r'^Project\("\{(?P<type>[0-9A-Fa-f-]+)\}"\)\s*=\s*'
    r'"(?P<name>[^"]+)",\s*'
    r'"(?P<path>[^"]+)",\s*'
    r'"\{(?P<guid>[0-9A-Fa-f-]+)\}"'
)

# "Solution folder" type GUID per MS docs — these have no buildable
# output and shouldn't be indexed as projects.
_FOLDER_TYPE_GUID = "2150E333-8FDC-42A3-9474-1A3956D46DE8"


@dataclass(frozen=True)
class SolutionProject:
    """One project entry inside a .sln file."""

    name: str
    csproj_path: str  # absolute, resolved
    guid: str
    type_guid: str


@dataclass
class SolutionInfo:
    """Parsed view of one .sln file."""

    path: str
    name: str
    projects: list[SolutionProject] = field(default_factory=list)


def parse_sln(sln_path: str | Path) -> SolutionInfo | None:
    """Parse one `.sln`. Returns ``None`` on read failure."""
    p = Path(sln_path).resolve()
    try:
        # .sln files are Windows-encoded MBCS-or-UTF8 with BOM in
        # practice; ``utf-8-sig`` strips the BOM if present without
        # caring otherwise.
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        log.warning("sln: failed to read %s — %s", p, e)
        return None

    info = SolutionInfo(path=str(p), name=p.stem)
    base = p.parent
    for line in text.splitlines():
        m = _PROJECT_LINE.match(line.strip())
        if not m:
            continue
        type_guid = m.group("type").lower()
        if type_guid == _FOLDER_TYPE_GUID.lower():
            continue
        rel = m.group("path").replace("\\", "/")
        candidate = (base / rel).resolve()
        if not candidate.exists():
            # Some solutions reference projects outside the cloned
            # working tree (shared infra). Skip — we can't index what
            # we don't have on disk.
            continue
        info.projects.append(
            SolutionProject(
                name=m.group("name"),
                csproj_path=str(candidate),
                guid=m.group("guid").lower(),
                type_guid=type_guid,
            )
        )
    return info


def walk_solutions(root: str | Path) -> list[SolutionInfo]:
    """Find every `.sln` under ``root`` and parse it."""
    out: list[SolutionInfo] = []
    root_path = Path(root).resolve()
    for sln in root_path.rglob("*.sln"):
        # Skip artifacts in obvious build outputs; .sln rarely lives
        # there but the filter is cheap and matches the csproj rule.
        if any(part in {"bin", "obj", "node_modules"} for part in sln.parts):
            continue
        info = parse_sln(sln)
        if info is not None:
            out.append(info)
    return out
