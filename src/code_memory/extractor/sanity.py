"""Extraction sanity checks — catch UTF-8 / parser drift at ingest time.

The historical UTF-8 byte-vs-str slicing bug silently chopped every
identifier in non-ASCII files, and nobody noticed until a user got
empty callers/callees a year later. This module exists so that class
of regression fails loudly at ingest, not at user-report.

The check is intentionally narrow: for each extracted Symbol whose
name is a plain identifier, the snippet must contain that identifier
as a substring. Anything more sophisticated would have to mirror the
extractor's logic, which is exactly what we're trying to validate —
so we keep this check independent and dumb on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .treesitter import ExtractedFile, Symbol

# Plain identifier — letters / digits / underscore. Skips generics
# (``Foo<T>``), operator overloads (``operator +``), F# parameterised
# names, and anything else the extractor reasonably emits but where a
# literal substring check would false-positive.
_PLAIN_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SanityViolation:
    """One symbol whose snippet didn't contain its name verbatim."""

    path: str
    name: str
    kind: str
    start_line: int


def is_checkable(symbol: Symbol) -> bool:
    """Whether this symbol's name is safe to round-trip against the snippet."""
    return bool(_PLAIN_IDENT.match(symbol.name))


def _contains_as_word(haystack: str, needle: str) -> bool:
    """``needle`` appears in ``haystack`` as a complete word.

    A plain substring check would miss the historical UTF-8 chop bug:
    when ``CommandeRules`` got truncated to ``mmandeRules``, the
    truncated name is still a substring of the snippet containing the
    real ``CommandeRules`` declaration. Word boundaries close that
    hole — ``mmandeRules`` is not a whole-word occurrence inside
    ``CommandeRules``.
    """
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, haystack) is not None


def violations_in(ex: ExtractedFile) -> list[SanityViolation]:
    """Return symbols whose snippet doesn't contain their plain-identifier name.

    Non-plain names (generics, operators, F# parameterised) are skipped
    rather than flagged — they aren't reliably substring-checkable.
    Returns ``[]`` on a clean file.
    """
    out: list[SanityViolation] = []
    for sym in ex.symbols:
        if not is_checkable(sym):
            continue
        if _contains_as_word(sym.snippet, sym.name):
            continue
        out.append(
            SanityViolation(
                path=ex.path,
                name=sym.name,
                kind=sym.kind,
                start_line=sym.start_line,
            )
        )
    return out


@dataclass
class SanitySummary:
    """Aggregate counts across one ingest run."""

    symbols_checked: int = 0
    symbols_failed: int = 0
    sample_violations: list[SanityViolation] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.sample_violations is None:
            self.sample_violations = []

    @property
    def failure_rate(self) -> float:
        if self.symbols_checked == 0:
            return 0.0
        return self.symbols_failed / self.symbols_checked

    def record(self, ex: ExtractedFile, *, keep_samples: int = 10) -> None:
        for sym in ex.symbols:
            if not is_checkable(sym):
                continue
            self.symbols_checked += 1
            if _contains_as_word(sym.snippet, sym.name):
                continue
            self.symbols_failed += 1
            if len(self.sample_violations) < keep_samples:
                self.sample_violations.append(
                    SanityViolation(
                        path=ex.path,
                        name=sym.name,
                        kind=sym.kind,
                        start_line=sym.start_line,
                    )
                )


# Threshold above which the ingest run flags itself as suspect. Tuned
# from real corpora: a healthy ingest sits at 0% (every plain
# identifier round-trips). The historical UTF-8 bug pushed the
# failure rate close to 100% on French C# repos. Anything above ~2%
# almost certainly means a real bug, not edge-case syntax.
SUSPECT_THRESHOLD = 0.02
