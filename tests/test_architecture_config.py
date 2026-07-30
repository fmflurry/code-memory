"""Tests for the ``[architecture]`` layer-convention table in config.py.

New symbols under test (do not exist yet — RED until implemented):

  - ``code_memory.config.LayerRule`` — frozen dataclass: ``name: str``,
    ``rank: int``, ``segments: tuple[str, ...]`` (lowercase directory
    segment names that identify the layer).
  - ``code_memory.config.DEFAULT_ARCHITECTURE_LAYERS`` — ``dict[str, LayerRule]``
    keyed by canonical layer name. A single flat table expresses BOTH
    the .NET clean-architecture convention (``Core``/``Domain`` = 0,
    ``Application`` = 1, ``Infrastructure`` = 2, ``Api``/``Presentation`` = 3)
    and the Angular clean-architecture convention (``domain`` = 0,
    ``application`` = 1, ``infrastructure`` = 2, ``presentation`` = 3) by
    unioning the segment spellings per rank — the two conventions agree
    on rank per concept, so no separate per-convention namespace is
    needed.
  - ``code_memory.config.resolve_architecture_layers(overrides=None)`` —
    merges a caller-supplied ``dict[str, LayerRule]`` on top of the
    defaults. Overrides replace a whole ``LayerRule`` for a given layer
    name (not a field-level merge) and may introduce wholly new,
    bespoke layer names. Must not mutate ``DEFAULT_ARCHITECTURE_LAYERS``.
"""

from __future__ import annotations

import pytest

from code_memory.config import (
    DEFAULT_ARCHITECTURE_LAYERS,
    LayerRule,
    resolve_architecture_layers,
)


# --------------------------------------------------------------- defaults


def test_default_layers_express_dotnet_domain_and_core_spellings() -> None:
    """.NET clean architecture: Core/Domain both mean the domain layer, rank 0."""
    rule = DEFAULT_ARCHITECTURE_LAYERS["domain"]
    assert rule.rank == 0
    assert set(rule.segments) == {"core", "domain"}


def test_default_layers_application_rank_is_one() -> None:
    rule = DEFAULT_ARCHITECTURE_LAYERS["application"]
    assert rule.rank == 1
    assert set(rule.segments) == {"application"}


def test_default_layers_infrastructure_rank_is_two() -> None:
    rule = DEFAULT_ARCHITECTURE_LAYERS["infrastructure"]
    assert rule.rank == 2
    assert set(rule.segments) == {"infrastructure"}


def test_default_layers_presentation_segments_is_bare_presentation_only() -> None:
    """UPDATED (round 3, "api collision" fix): ``"api"`` is deliberately
    NOT in ``presentation``'s ``segments`` tuple any more — it's both a
    legitimate .NET ``Module.Api`` folder-suffix AND an extremely common
    bare app/service name (Nx/Turborepo ``apps/api``, kebab-case
    ``payment-api``), so treating it as an ordinary layer segment let a
    top-level ``api`` app or a ``*-api`` service name swallow an entire
    unrelated subtree as presentation. This test used to assert
    ``segments == {"api", "presentation"}`` — that's the exact bug the
    fix removes."""
    rule = DEFAULT_ARCHITECTURE_LAYERS["presentation"]
    assert rule.rank == 3
    assert set(rule.segments) == {"presentation"}


def test_default_layers_dotnet_api_reaches_presentation_via_dot_tail_not_segments() -> (
    None
):
    """RENAMED from test_default_layers_express_dotnet_api_and_presentation_spellings
    — preserves that test's real intent (.NET's ``Api``/``Presentation``
    both mean the outermost layer, rank 3) now that ``"api"`` no longer
    lives in the ``segments`` tuple. The .NET ``Module.Api`` folder-
    suffix convention (``Billing.Api``) still reaches presentation, but
    via the dedicated DOT-only tail path (``_dot_tail_match`` +
    ``_DOT_ONLY_TAIL_WORD``), not via whole-segment/``segments``
    matching — exercised end-to-end through ``classify_file_layer``
    rather than asserting on ``segments`` directly, since ``"api"``
    is intentionally absent from that tuple now."""
    from code_memory.config import classify_file_layer

    result = classify_file_layer("src/Billing/Billing.Api/InvoiceController.cs")
    assert result == ("Billing", "presentation", 3)


def test_default_layers_cover_exactly_four_ranks() -> None:
    ranks = sorted(rule.rank for rule in DEFAULT_ARCHITECTURE_LAYERS.values())
    assert ranks == [0, 1, 2, 3]


# ----------------------------------------------------- resolve: defaults load


def test_resolve_architecture_layers_with_no_overrides_returns_defaults() -> None:
    resolved = resolve_architecture_layers()
    assert resolved == DEFAULT_ARCHITECTURE_LAYERS


def test_resolve_architecture_layers_with_none_argument_returns_defaults() -> None:
    resolved = resolve_architecture_layers(None)
    assert resolved == DEFAULT_ARCHITECTURE_LAYERS


# ------------------------------------------------ resolve: bespoke override


def test_resolve_architecture_layers_adds_bespoke_convention_layer() -> None:
    """A wholly custom org convention (e.g. 'kernel') can be layered on top
    without disturbing the built-in .NET/Angular spellings."""
    overrides = {"kernel": LayerRule(name="kernel", rank=0, segments=("kernel",))}
    resolved = resolve_architecture_layers(overrides)

    assert resolved["kernel"] == LayerRule(name="kernel", rank=0, segments=("kernel",))
    # Defaults remain intact alongside the bespoke addition.
    assert resolved["domain"] == DEFAULT_ARCHITECTURE_LAYERS["domain"]
    assert resolved["application"] == DEFAULT_ARCHITECTURE_LAYERS["application"]
    assert resolved["infrastructure"] == DEFAULT_ARCHITECTURE_LAYERS["infrastructure"]
    assert resolved["presentation"] == DEFAULT_ARCHITECTURE_LAYERS["presentation"]


def test_resolve_architecture_layers_override_replaces_known_layer_rule() -> None:
    """Overriding an existing layer name replaces its whole rule (segments
    included) rather than merging field-by-field."""
    overrides = {
        "domain": LayerRule(name="domain", rank=0, segments=("core", "domain", "kernel"))
    }
    resolved = resolve_architecture_layers(overrides)

    assert set(resolved["domain"].segments) == {"core", "domain", "kernel"}
    # Sibling layers are untouched by the single-layer override.
    assert resolved["application"] == DEFAULT_ARCHITECTURE_LAYERS["application"]
    assert resolved["infrastructure"] == DEFAULT_ARCHITECTURE_LAYERS["infrastructure"]
    assert resolved["presentation"] == DEFAULT_ARCHITECTURE_LAYERS["presentation"]


def test_resolve_architecture_layers_does_not_mutate_the_default_table() -> None:
    """Immutability guard: merging overrides must never leak back into the
    shared DEFAULT_ARCHITECTURE_LAYERS module constant."""
    resolve_architecture_layers(
        {"domain": LayerRule(name="domain", rank=0, segments=("mutated",))}
    )
    assert DEFAULT_ARCHITECTURE_LAYERS["domain"].segments == ("core", "domain")


# ------------------------------------ D3 (HIGH): generic context segments
#
# ``classify_file_layer`` (test_architecture_classify.py) needs a config-
# level set of "this directory name is a generic container, not a
# bounded-context name" segments so a monorepo's ``BcA/src/domain/...``
# and ``BcB/src/domain/...`` don't collapse into the same Layer context.
# Does not exist yet — RED (ImportError) until implemented.


def test_generic_context_segments_constant_has_expected_defaults() -> None:
    from code_memory.config import GENERIC_CONTEXT_SEGMENTS

    lowered = {s.lower() for s in GENERIC_CONTEXT_SEGMENTS}
    assert {"src", "lib", "app", "source"} <= lowered


# --------------------------------------- TEST_PATH_PATTERNS (spec/test file
# exclusion for FalkorStore.layer_violations)
#
# ``code_memory.config.TEST_PATH_PATTERNS`` — does not exist yet, RED
# (ImportError) until implemented. A ``frozenset[str]`` of literal
# substring markers matched CASE-SENSITIVELY (never lower-cased on either
# side) against a File node's ``key`` to decide whether that file is a
# test/spec file rather than production code. Consumed by
# ``FalkorStore.layer_violations()`` (tests in
# ``test_falkor_layer_queries.py``) to exclude test-authored dependencies
# from clean-architecture violation reports — an
# ``application/*.use-case.spec.ts`` file importing an infrastructure
# adapter to build a test double is not a layering violation, it's just
# how you write a unit test.
#
# Case-sensitivity is deliberate and load-bearing, not incidental: a
# case-INSENSITIVE comparison of the ``"Tests.cs"`` marker would ALSO
# match a legitimate production file named ``protests.cs`` (a "Protest"
# domain entity) because lower-casing both sides turns ``"Tests.cs"``
# into ``"tests.cs"``, which IS a literal substring of ``"protests.cs"``.
# Comparing with the original casing preserved, ``"protests.cs"`` has a
# lower-case ``t`` where the marker needs an upper-case ``T``, so it
# correctly does not match. Verified empirically against a real FalkorDB
# instance (``ANY(p IN patterns WHERE f.key CONTAINS p)``) before pinning
# these values: ``spectrum.service.ts``, ``latest.ts``, ``contest.ts``,
# ``attestation.ts``, ``protests.cs``, and a bare ``src/specs/`` directory
# segment all correctly evaluate to "not a test file" with this exact
# pattern set, while ``x.use-case.spec.ts``, ``Billing.Tests/InvoiceTests.cs``,
# ``Billing.Tests/Fixtures/InvoiceBuilder.cs`` (a helper file inside a
# ``.Tests`` project folder that doesn't itself end in ``Tests.cs``),
# ``test_invoice.py``, and ``invoice_test.py`` all correctly evaluate to
# "is a test file".
#
# Deliberately NOT covered by the default set (documented gaps, not
# oversights):
#   * Jest's ``__tests__/`` directory convention (files with no ``.spec.``/
#     ``.test.`` suffix at all, e.g. ``__tests__/invoice.ts``) — a bare
#     filename inside such a directory is visually indistinguishable from
#     a production file by name alone; adding it needs its own review.
#   * ``.spec.mjs``/``.spec.cjs``/``.test.mjs``/``.test.cjs`` — less common
#     ES module extension variants.
#   * Java/Kotlin conventions (``*Test.java``, ``FooTests.kt``) — out of
#     scope for this pass (no Java/Kotlin example in the brief).
#   * Cypress/Playwright E2E suffixes (``.e2e.ts``, ``.cy.ts``) — a
#     different class of test file (browser-driven, not the unit-level
#     "spec imports an adapter to fake it" scenario this exclusion
#     targets) and not requested.
#   * A bare ``.Test.`` (singular) .NET project-folder spelling — only the
#     pluralized ``.Tests.``/``Tests.cs`` convention from the brief is
#     covered.
#   * Case-insensitive matching entirely — rejected by design, per the
#     ``protests.cs`` false-positive above.


def test_test_path_patterns_constant_has_expected_defaults() -> None:
    from code_memory.config import TEST_PATH_PATTERNS

    assert TEST_PATH_PATTERNS == frozenset(
        {
            ".spec.ts",
            ".spec.tsx",
            ".spec.js",
            ".test.ts",
            ".test.tsx",
            ".test.js",
            "/test_",
            "_test.py",
            ".Tests/",
            "Tests.cs",
        }
    )


def test_test_path_patterns_is_a_frozenset_and_thus_immutable() -> None:
    from code_memory.config import TEST_PATH_PATTERNS

    assert isinstance(TEST_PATH_PATTERNS, frozenset)
    with pytest.raises(AttributeError):
        TEST_PATH_PATTERNS.add(".e2e.ts")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # ---- must be recognised as test/spec files ----
        ("/repo/application/invoice.use-case.spec.ts", True),
        ("/repo/application/invoice.use-case.spec.tsx", True),
        ("/repo/application/invoice.use-case.spec.js", True),
        ("/repo/application/invoice.use-case.test.ts", True),
        ("/repo/application/invoice.use-case.test.tsx", True),
        ("/repo/application/invoice.use-case.test.js", True),
        ("/repo/src/test_invoice.py", True),
        ("/repo/src/invoice_test.py", True),
        ("/repo/Billing.Tests/InvoiceTests.cs", True),
        # A helper file inside a .Tests project folder that doesn't
        # itself end in Tests.cs — still test-owned via the directory
        # marker.
        ("/repo/Billing.Tests/Fixtures/InvoiceBuilder.cs", True),
        # ---- must NOT be recognised as test/spec files (over-matching
        # guards named explicitly in the brief) ----
        ("/repo/infrastructure/spectrum.service.ts", False),
        ("/repo/domain/latest.ts", False),
        ("/repo/domain/contest.ts", False),
        ("/repo/domain/attestation.ts", False),
        ("/repo/src/specs/registration.ts", False),
        # Case-sensitivity guard: lower-case "tests" must not match the
        # PascalCase ".NET marker.
        ("/repo/domain/protests.cs", False),
        ("/repo/src/testing_utils.py", False),
    ],
)
def test_test_path_patterns_pure_python_containment_matches_expected_cases(
    path: str, expected: bool
) -> None:
    """Pins the exact same substring-containment semantics the Cypher
    predicate in ``FalkorStore.layer_violations()`` implements
    (``ANY(p IN $test_patterns WHERE key CONTAINS p)``), expressed in
    pure Python so this contract is checkable without a live FalkorDB.
    The live-Falkor exclusion behaviour itself is pinned separately in
    ``test_falkor_layer_queries.py``."""
    from code_memory.config import TEST_PATH_PATTERNS

    assert any(p in path for p in TEST_PATH_PATTERNS) is expected
