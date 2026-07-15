"""Lightweight .gitignore matcher with no external deps.

Supports the subset of gitignore syntax that matters in practice:

- blank lines + ``#`` comments
- ``!`` negation
- ``/`` anchored (root-relative) vs unanchored patterns
- trailing ``/`` directory-only patterns
- ``*`` (no slash), ``?``, ``**`` globs
- collects ``.gitignore`` files recursively from the root downward

This is deliberately not a full re-implementation of git's matcher; the goal
is to avoid indexing build artifacts (``.angular/``, ``tmp/``, ``dist/``…)
that repos already ignore.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _Rule:
    base: Path  # directory containing the .gitignore that owns the rule
    pattern: str
    negate: bool
    dir_only: bool
    anchored: bool
    regex: re.Pattern[str]


def _compile(pattern: str) -> re.Pattern[str]:
    parts = pattern.split("/")
    out: list[str] = []
    for i, seg in enumerate(parts):
        if seg == "**":
            out.append(".*")
        else:
            # fnmatch.translate adds anchors we don't want — strip them
            tr = fnmatch.translate(seg)
            # python's translate yields "(?s:...)\\Z" ("\\z" since 3.13);
            # extract inner
            m = re.match(r"\(\?s:(.*)\)\\[Zz]", tr)
            inner = m.group(1) if m else tr
            out.append(inner)
        if i != len(parts) - 1:
            out.append("/")
    body = "".join(out)
    # collapse leading "**/" -> "(?:.*/)?" to allow zero-segment match
    body = re.sub(r"^\.\*/", "(?:.*/)?", body)
    return re.compile(rf"^{body}$")


def _parse_line(line: str, base: Path) -> _Rule | None:
    raw = line.rstrip("\n").rstrip("\r")
    if not raw or raw.lstrip().startswith("#"):
        return None
    negate = raw.startswith("!")
    if negate:
        raw = raw[1:]
    raw = raw.strip()
    if not raw:
        return None
    dir_only = raw.endswith("/")
    if dir_only:
        raw = raw[:-1]
    anchored = "/" in raw and not raw.startswith("**/")
    if raw.startswith("/"):
        raw = raw[1:]
    return _Rule(
        base=base,
        pattern=raw,
        negate=negate,
        dir_only=dir_only,
        anchored=anchored,
        regex=_compile(raw),
    )


class GitignoreMatcher:
    """Walk-aware gitignore matcher.

    Load all ``.gitignore`` files under ``root`` upfront, then call
    :meth:`match` per candidate path during the walk.
    """

    def __init__(self, root: Path, rules: list[_Rule]) -> None:
        self._root = root.resolve()
        self._rules = rules

    @classmethod
    def from_root(cls, root: str | Path) -> GitignoreMatcher:
        root_path = Path(root).resolve()
        rules: list[_Rule] = []
        if not root_path.is_dir():
            return cls(root_path, rules)
        # always seed with .git/ so we never index the git directory
        rules.append(
            _Rule(
                base=root_path,
                pattern=".git",
                negate=False,
                dir_only=True,
                anchored=False,
                regex=_compile(".git"),
            )
        )
        for gi in root_path.rglob(".gitignore"):
            try:
                lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            base = gi.parent.resolve()
            for line in lines:
                rule = _parse_line(line, base)
                if rule is not None:
                    rules.append(rule)
        return cls(root_path, rules)

    def match(self, path: Path, *, is_dir: bool) -> bool:
        """Return True if ``path`` is ignored. Last matching rule wins.

        Per gitignore semantics, a file is ignored if any of its ancestor
        directories is ignored, so we evaluate the chain root -> path and
        return the final state.
        """
        try:
            abs_path = path.resolve()
        except OSError:
            return False
        # Build list of (candidate_path, is_dir) from root toward the leaf.
        chain: list[tuple[Path, bool]] = []
        for parent in reversed(abs_path.parents):
            if self._root in (parent, *parent.parents) or parent == self._root:
                chain.append((parent, True))
        chain.append((abs_path, is_dir))

        ignored = False
        for candidate, candidate_is_dir in chain:
            if candidate == self._root:
                continue
            for rule in self._rules:
                if rule.dir_only and not candidate_is_dir:
                    continue
                try:
                    rel_from_base = candidate.relative_to(rule.base)
                except ValueError:
                    continue
                rel_str = rel_from_base.as_posix()
                if rel_str in ("", "."):
                    continue
                if rule.anchored:
                    hit = bool(rule.regex.match(rel_str))
                else:
                    hit = bool(rule.regex.match(rel_str)) or any(
                        rule.regex.match(part) for part in rel_from_base.parts
                    )
                if hit:
                    ignored = not rule.negate
        return ignored
