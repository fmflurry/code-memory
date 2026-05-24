"""Sanity-check that the round-trip detector flags real chops."""

from __future__ import annotations

from code_memory.extractor.sanity import (
    SUSPECT_THRESHOLD,
    SanitySummary,
    SanityViolation,
    is_checkable,
    violations_in,
)
from code_memory.extractor.treesitter import ExtractedFile, Symbol


def _sym(name: str, snippet: str, *, kind: str = "function_declaration") -> Symbol:
    return Symbol(name=name, kind=kind, start_line=10, end_line=20, snippet=snippet)


def _ex(*symbols: Symbol) -> ExtractedFile:
    return ExtractedFile(
        path="/r/x.cs",
        lang="csharp",
        symbols=list(symbols),
        source="dummy",
    )


# --------------------------------------------------------------- is_checkable


def test_plain_identifier_is_checkable() -> None:
    assert is_checkable(_sym("CommandeRules", "class CommandeRules {}"))
    assert is_checkable(_sym("_internal", "var _internal = 1"))


def test_generic_name_is_skipped() -> None:
    # The extractor strips the ``<T>`` already, but be defensive.
    assert not is_checkable(_sym("Foo<T>", "class Foo<T> {}"))


def test_operator_name_is_skipped() -> None:
    assert not is_checkable(_sym("operator +", "public static T operator +"))


def test_empty_name_is_skipped() -> None:
    assert not is_checkable(_sym("", ""))


# --------------------------------------------------------------- violations_in


def test_clean_file_has_no_violations() -> None:
    ex = _ex(
        _sym("CommandeRules", "class CommandeRules { }"),
        _sym("Sauver", "public void Sauver() { }"),
    )
    assert violations_in(ex) == []


def test_chopped_name_flagged() -> None:
    """Simulates the historical UTF-8 chop bug."""
    ex = _ex(_sym("mmandeRules", "class CommandeRules { }"))
    v = violations_in(ex)
    assert len(v) == 1
    assert v[0].name == "mmandeRules"
    assert v[0].path == "/r/x.cs"


def test_non_plain_names_skipped_even_when_chopped() -> None:
    # Operator + still appears as 'operator +' in snippet; we skip it
    # whether or not the snippet contains it, because the matcher would
    # be unreliable anyway.
    ex = _ex(_sym("operator +", "public static T operator + (T a, T b)"))
    assert violations_in(ex) == []


# --------------------------------------------------------------- SanitySummary


def test_summary_aggregates() -> None:
    summary = SanitySummary()
    summary.record(
        _ex(
            _sym("OK", "OK {}"),
            _sym("Chopped", "ActuallyName {}"),
        )
    )
    summary.record(
        _ex(_sym("AlsoOK", "AlsoOK"))
    )
    assert summary.symbols_checked == 3
    assert summary.symbols_failed == 1
    assert summary.failure_rate == 1 / 3


def test_summary_caps_sample_size() -> None:
    summary = SanitySummary()
    for i in range(20):
        summary.record(_ex(_sym(f"Bad{i}", "completely unrelated")))
    assert summary.symbols_failed == 20
    assert len(summary.sample_violations) == 10  # default keep_samples
    assert all(isinstance(v, SanityViolation) for v in summary.sample_violations)


def test_word_boundary_catches_truncation() -> None:
    """The historic UTF-8 chop bug truncated names from the front.

    ``CommandeRules`` became ``mmandeRules`` — a strict substring
    check would miss that because the truncated form is still
    contained in the snippet. The word-boundary check must catch it.
    """
    ex = _ex(_sym("mmandeRules", "public class CommandeRules { }"))
    v = violations_in(ex)
    assert len(v) == 1, "word-boundary check must flag mid-word substring matches"


def test_word_boundary_catches_suffix_truncation() -> None:
    """If the front gets chopped severely (`InitialiserDocument` -> `tDocument`)."""
    ex = _ex(_sym("tDocument", "private void InitialiserDocument() {}"))
    assert len(violations_in(ex)) == 1


def test_suspect_threshold_is_strict() -> None:
    # The threshold should be tight enough that any real regression
    # (the historical bug pushed it to ~100%) trips it, while still
    # tolerating a handful of weird syntax edge cases in normal files.
    assert SUSPECT_THRESHOLD <= 0.05
