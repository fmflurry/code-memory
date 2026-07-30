"""Tests for ``classify_file_layer`` — pure path -> (context, layer, rank).

New symbol under test (does not exist yet — RED until implemented):

  ``code_memory.config.classify_file_layer(file_path, layers=None)
      -> tuple[str, str, int] | None``

Contract pinned down by these tests (UPDATED — see the "New RED round"
section below for the three classifier-semantics fixes and exactly
which older tests in this file encoded the bugs being fixed):

* Directory segments are matched case-insensitively against the
  configured ``LayerRule.segments`` for each layer, OR against the
  dot/dash-delimited TAIL of a segment (``Billing.Core`` / ``billing-domain``)
  — see the D2 section below.
* When more than one path segment matches a configured layer, the
  **shallowest** (outermost, closest to the project root) match wins —
  REVERSED from the original "deepest wins" rule; see the D1 section
  below for why.
* ``context`` is the nearest ancestor path segment that is NOT itself a
  generic container directory (``src``, ``lib``, ``app``, ``source`` —
  ``code_memory.config.GENERIC_CONTEXT_SEGMENTS``); classify keeps
  walking up past generic segments looking for a meaningful bounded-
  context / module name. See the D3 section below. Falls back to the
  literal string ``"root"`` when nothing but generic segments (or
  nothing at all) precedes the match.
* A path with no configured segment anywhere, and no dot/dash tail
  match, returns ``None``.
* Passing an explicit ``layers`` table overrides the default lookup
  entirely (no implicit merge with defaults inside ``classify_file_layer``
  itself — merging is ``resolve_architecture_layers``'s job).
"""

from __future__ import annotations

from code_memory.config import (
    LayerRule,
    classify_file_layer,
    resolve_architecture_layers,
)


# --------------------------------------------------------------- no match


def test_classify_returns_none_when_no_segment_matches_any_layer() -> None:
    assert classify_file_layer("/repo/src/utils/helpers.ts") is None


def test_classify_returns_none_for_empty_layers_table() -> None:
    assert classify_file_layer("/repo/src/Domain/Foo.cs", layers={}) is None


# ------------------------------------------------------------ basic match


def test_classify_matches_dotnet_style_core_segment() -> None:
    """UPDATED for D3: ``src`` is a generic container segment
    (``GENERIC_CONTEXT_SEGMENTS``), so context-resolution walks past it
    to ``repo`` — the original assertion (``context == "src"``) encoded
    the D3 bug (a meaningless generic dir name leaking in as the
    "bounded context"). See the D3 section below for the full story."""
    result = classify_file_layer("/repo/src/Core/Entities/Invoice.cs")
    assert result == ("repo", "domain", 0)


def test_classify_bare_api_segment_no_longer_matches() -> None:
    """UPDATED for the HIGH "api is both a layer word and a common
    app/service name" fix (round 3): bare ``api`` (no dot/dash tail
    convention) is REMOVED from the ``presentation`` rule's ``segments``
    — see the "api collision" section below for the full story. This
    test used to assert this path resolved to
    ``("repo", "presentation", 3)`` (via the bare ``Api`` whole-segment
    match); that's the exact ambiguity the fix removes. ``Api`` here has
    no dot/dash delimiter either, so it doesn't qualify for tail
    matching — the path now has no configured segment anywhere and
    returns ``None``. This is an ACCEPTED regression (a bare
    ``src/BcA/Api/...`` convention now needs a per-repo ``[architecture]``
    config override) — pinned explicitly, not an oversight."""
    result = classify_file_layer("/repo/src/Api/Controllers/InvoiceController.cs")
    assert result is None


def test_classify_case_insensitive_segment_match() -> None:
    """Directory casing (``DOMAIN``, ``Domain``, ``domain``) must not
    matter. UPDATED for D3 — same generic-``src``-skip reasoning."""
    result = classify_file_layer("/repo/src/DOMAIN/entity.ts")
    assert result == ("repo", "domain", 0)


# ------------------------------------------------- D1 (HIGH): shallowest
# match wins (REVERSED from the original "deepest wins" rule)
#
# ``config.py:544-550`` used to pick the DEEPEST matching segment. For a
# real angular-clean-architecture path like
# ``bc-a/infrastructure/api/invoice.adapter.ts``, the deeper ``api``
# segment (a ``presentation`` spelling) beat the shallower
# ``infrastructure`` segment, so a genuine infrastructure adapter
# misclassified as rank-3 presentation, with ``context`` becoming the
# literal string ``"infrastructure"`` — a LAYER NAME posing as a bounded-
# context name. ``infrastructure/api/*.adapter.ts`` is the standard
# adapter location in this convention; it must classify as
# infrastructure. Intended fix: prefer the SHALLOWEST matching segment.


def test_classify_shallowest_segment_wins_over_deeper_nested_layer_dir() -> None:
    result = classify_file_layer(
        "src/app/features/bc-a/infrastructure/api/invoice.adapter.ts"
    )
    assert result == ("bc-a", "infrastructure", 2)


def test_classify_context_is_never_itself_a_layer_segment() -> None:
    """Guard against the fix overshooting in the other direction: no
    matter which segment wins, ``context`` must never itself be a value
    that is a configured layer segment (that would mean we accidentally
    returned a LAYER name where a bounded-context name belongs)."""
    configured_segment_names = {
        "core", "domain", "application", "infrastructure", "api", "presentation",
    }
    cases = [
        "src/app/features/bc-a/infrastructure/api/invoice.adapter.ts",
        "/repo/src/infrastructure/adapters/domain/entity.ts",
        "src/Domain/Application/Foo.cs",
    ]
    for path in cases:
        result = classify_file_layer(path)
        assert result is not None, path
        context, _name, _rank = result
        assert context.lower() not in configured_segment_names, (
            f"{path} -> context={context!r} is itself a layer segment"
        )


def test_classify_shallowest_wins_does_not_overshoot_single_match_cases() -> None:
    """Pin the inverse cases so the D1 fix doesn't overcorrect: a path
    with only ONE matching segment must still resolve exactly as before,
    whether that segment is presentation or domain."""
    assert classify_file_layer(
        "src/app/features/bc-a/presentation/controllers/x.ts"
    ) == ("bc-a", "presentation", 3)
    assert classify_file_layer("src/app/features/bc-a/domain/invoice.ts") == (
        "bc-a", "domain", 0,
    )


def test_classify_picks_shallowest_most_specific_layer_segment() -> None:
    """RENAMED from test_classify_picks_deepest_most_specific_layer_segment
    — that name and assertion encoded the D1 bug (deepest wins). Combined
    with the D3 fix (generic-``src``-skip), the shallower 'infrastructure'
    segment wins over the deeper 'domain', and context-resolution walks
    past the generic 'src' ancestor up to 'repo'."""
    result = classify_file_layer("/repo/src/infrastructure/adapters/domain/entity.ts")
    assert result == ("repo", "infrastructure", 2)


def test_classify_nested_angular_feature_picks_domain() -> None:
    """Real-world nested Angular clean-architecture path."""
    result = classify_file_layer(
        "src/app/features/invoicing/domain/invoice.ts"
    )
    assert result is not None
    context, name, rank = result
    assert name == "domain"
    assert rank == 0
    assert context == "invoicing"


# ------------------------------------------------------- context fallback


def test_classify_top_level_layer_segment_uses_root_sentinel_for_context() -> None:
    """When the matched segment is the very first path component there is
    no ancestor folder to use as context — fall back to 'root'."""
    result = classify_file_layer("Domain/Foo.cs")
    assert result == ("root", "domain", 0)


# ------------------------------------------------------- unknown segment


def test_classify_unknown_segment_returns_no_layer() -> None:
    """A directory name that isn't configured anywhere returns None, even
    when other configured segments exist elsewhere in the layer table."""
    result = classify_file_layer("/repo/src/Widgets/Foo.cs")
    assert result is None


# --------------------------------------------------- custom layers table


def test_classify_respects_custom_layers_argument() -> None:
    """Passing a bespoke layers table overrides the default lookup — a
    path matching only the default 'domain' spelling now returns None,
    while the bespoke 'kernel' spelling resolves. UPDATED for D3: 'src'
    is generic, so context walks up to 'repo' (was 'src')."""
    custom = {"kernel": LayerRule(name="kernel", rank=0, segments=("kernel",))}

    assert classify_file_layer("/repo/src/Domain/Foo.cs", layers=custom) is None
    result = classify_file_layer("/repo/src/Kernel/Foo.cs", layers=custom)
    assert result == ("repo", "kernel", 0)


# ---------------------------------------- root-relative-path boundary guard
#
# classify_file_layer's OWN contract is unaffected by the (separate,
# prior-round) "classify on path relative to ingest root" fix in
# ``Pipeline._index_architecture`` (test_pipeline_architecture.py): that
# fix moves responsibility for computing "path relative to root" into
# the CALLER. This function has always classified whatever path string
# it is handed, relative or absolute — that part of the contract must
# not change; a future "fix" must not make classify_file_layer itself
# root-aware.


def test_classify_relative_path_without_leading_slash_still_works() -> None:
    """UPDATED for D3: 'src' is the only ancestor and it's generic, so
    context falls all the way back to the 'root' sentinel (was 'src')."""
    result = classify_file_layer("src/domain/invoice.ts")
    assert result == ("root", "domain", 0)


# ------------------------------------------------- D2 (HIGH): dot/dash-
# delimited layer-name TAIL matching
#
# The matcher only ever did a whole-segment lookup
# (``segment_to_rule.get(parts[index].lower())``), so the very common
# .NET "module-per-folder" convention — ``Billing.Core``,
# ``Billing.Infrastructure``, ``Billing.Api`` — classified as ``None``.
# That's silent under-coverage: ``layer_violations()`` returns ``[]`` and
# the repo looks clean when it's actually just invisible to the
# classifier. Intended fix: when the WHOLE segment doesn't match, also
# try the segment's dot/dash-delimited TAIL token (``Billing.Core`` ->
# tail ``Core`` -> domain; ``billing-domain`` -> tail ``domain`` ->
# domain). A segment with no ``.``/``-`` delimiter at all never takes
# this path, so ``MyCoreUtils`` / ``Coordinator`` do NOT match ``core``
# (substring matching is explicitly NOT what this is).
#
# ``context`` for a tail-match is the PREFIX of that SAME path segment
# (everything before the last delimiter) — e.g. ``Billing.Core`` ->
# context ``Billing`` — NOT the ancestor directory (whole-segment
# matches keep using the ancestor-directory rule). Chosen because the
# module name IS the segment itself for this convention; there is no
# separate ancestor folder carrying that information the way there is
# for the Angular ``bc-a/domain/`` layout.


def test_classify_dotted_dotnet_module_layer_suffix_billing_core() -> None:
    result = classify_file_layer("src/Modules/Billing/Billing.Core/Invoice.cs")
    assert result == ("Billing", "domain", 0)


def test_classify_dotted_dotnet_module_layer_suffix_billing_infrastructure() -> None:
    result = classify_file_layer(
        "src/Modules/Billing/Billing.Infrastructure/InvoiceRepo.cs"
    )
    assert result == ("Billing", "infrastructure", 2)


def test_classify_dotted_dotnet_module_layer_suffix_billing_api() -> None:
    result = classify_file_layer(
        "src/Modules/Billing/Billing.Api/InvoiceController.cs"
    )
    assert result == ("Billing", "presentation", 3)


def test_classify_dash_delimited_layer_suffix() -> None:
    result = classify_file_layer("billing-domain/Invoice.ts")
    assert result == ("billing", "domain", 0)


def test_classify_tail_match_requires_delimiter_not_substring() -> None:
    """Over-matching guard: a segment with no dot/dash delimiter must
    never match via substring containment, even when a configured layer
    name is embedded in it."""
    assert classify_file_layer("src/MyCoreUtils/Foo.cs") is None
    assert classify_file_layer("src/Coordinator/Foo.cs") is None


# --------------------------------------------- D3 (HIGH): generic
# intermediate directories must not become the "bounded context"
#
# ``context = parts[index - 1]`` unconditionally used the immediately
# preceding path segment. For ``BcA/src/domain/Invoice.cs`` and
# ``BcB/src/domain/Invoice.cs``, that segment is ``src`` in BOTH cases —
# a generic container directory, not a bounded-context name — so both
# files resolved to the SAME ``('src', 'domain', 0)`` and
# ``_index_architecture`` merges two distinct bounded contexts into one
# ``Layer`` node. Intended fix: when the immediate ancestor is a generic
# container segment (``code_memory.config.GENERIC_CONTEXT_SEGMENTS`` —
# ``src``, ``lib``, ``app``, ``source``, matched case-insensitively),
# keep walking up the path for a meaningful context. Falls back to the
# ``"root"`` sentinel when nothing but generic segments (or nothing at
# all) precedes the match.


def test_classify_skips_generic_src_dir_to_find_bc_a_context() -> None:
    result = classify_file_layer("BcA/src/domain/Invoice.cs")
    assert result == ("BcA", "domain", 0)


def test_classify_skips_generic_src_dir_to_find_bc_b_context() -> None:
    result = classify_file_layer("BcB/src/domain/Invoice.cs")
    assert result == ("BcB", "domain", 0)


def test_classify_two_bcs_behind_generic_src_produce_different_contexts() -> None:
    """The money test: this is the user's exact reported scenario — two
    bounded contexts must NOT collapse into the same Layer context."""
    a = classify_file_layer("BcA/src/domain/Invoice.cs")
    b = classify_file_layer("BcB/src/domain/Invoice.cs")
    assert a is not None and b is not None
    assert a[0] != b[0]
    assert a == ("BcA", "domain", 0)
    assert b == ("BcB", "domain", 0)


def test_classify_all_generic_ancestry_falls_back_to_root_sentinel() -> None:
    """Degenerate case: walking up finds NOTHING but generic segments
    before hitting the start of the path. Falls back to 'root', same
    sentinel as the "matched segment is the first component" case."""
    assert classify_file_layer("src/domain/Invoice.cs") == ("root", "domain", 0)
    assert classify_file_layer("src/app/domain/Invoice.ts") == ("root", "domain", 0)


# --------------------------------------------- HIGH (round 3): "api" is
# both a layer word and a common app/service name
#
# Reproduced against the real deployed classifier: ``apps/api/src/
# billing/domain/x.ts`` -> ``("apps", "presentation", 3)`` (should be
# domain) and ``services/payment-api/src/domain/invoice.ts`` ->
# ``("payment", "presentation", 3)``. Shallowest-wins (D1) makes a
# top-level ``api`` folder, or a ``*-api`` app/service name matched via
# the D2 tail rule, swallow the ENTIRE subtree beneath it — silently
# defeating layer-violation detection for a whole class of Nx/Turborepo
# monorepo layouts where "api" names an app/service, not a layer.
#
# USER DECISION ("drop bare api, keep the .Api tail"): remove ``"api"``
# from the ``presentation`` rule's ``segments`` in
# ``DEFAULT_ARCHITECTURE_LAYERS``; ``presentation`` keeps only
# ``("presentation",)``. The user explicitly accepted that a bare
# ``src/BcA/Api/Endpoints.cs`` directory now returns ``None`` (fix
# per-repo via the ``[architecture]`` config override).
#
# IMPLEMENTATION NOTE (mine, not literally spelled out in the decision,
# but required to satisfy it — see report): "remove api from segments
# entirely" is not enough on its own to satisfy BOTH pinned rows below
# simultaneously, because whole-segment and tail-delimited matching
# share ONE lookup table today (``segment_to_rule``, built purely from
# ``rule.segments``) — removing "api" from segments would ALSO kill the
# ``Billing.Api`` tail match the decision explicitly wants kept, and
# would NOT by itself stop ``payment-api`` (dash-tail) from matching,
# since that's ALSO reached via the tail path. Verified empirically: a
# naive "just drop api from segments" run on the real classifier gives
# ``Billing.Api -> None`` (violates the decision) while doing nothing at
# all to fix ``payment-api`` (still matches via its dash-tail, since a
# still-registered "api" tail entry would still be hit either way).
# Resolution: the ``.Api`` DOT-tail convention (the .NET
# Module.Layer-per-folder pattern already covered by D2) is a much
# narrower, more intentional architectural signal than a DASH-suffixed
# kebab-case app/service name (``payment-api``, ``user-api``,
# ``search-api`` are just "the API service for X", not a presentation
# folder) — the two are delimiter-distinguishable. So "api" needs a
# DOT-only tail-match path, separate from (and not reachable through)
# whole-segment matching or dash-tail matching. This is the mechanism
# these tests pin; the exact config shape (e.g. a new ``LayerRule``
# field, a separate module constant) is the implementer's choice as
# long as the observable behaviour below holds.


def test_classify_top_level_api_dir_no_longer_swallows_domain_subtree() -> None:
    result = classify_file_layer("apps/api/src/billing/domain/x.ts")
    assert result == ("billing", "domain", 0)


def test_classify_dash_suffixed_api_service_name_no_longer_matches_presentation() -> (
    None
):
    """DISCREPANCY FLAGGED (see report): the review's pinned expectation
    for this path states context ``"services"``. Verified empirically
    against the deployed (unmodified, already-landed) D1+D3 algorithm
    that with ``api`` removed from matching, this path's classification
    is genuinely ``("payment-api", "domain", 0)`` — ``payment-api`` is
    the nearest NON-generic ancestor once the generic ``src`` segment is
    skipped (identical mechanism to the ``apps/api/.../billing/domain``
    case just above, where the immediate non-generic ancestor
    ``billing`` — not the outer ``apps`` — is what's used). Reaching
    ``"services"`` instead would require ALSO treating ``payment-api``
    itself as a generic container segment, which nothing in this
    review asks for and isn't done anywhere else. Pinning the value
    the algorithm can actually, consistently produce; the rank/layer
    part (domain, 0 — the actual bug being fixed) matches the review
    exactly regardless of this context disagreement."""
    result = classify_file_layer("services/payment-api/src/domain/invoice.ts")
    assert result == ("payment-api", "domain", 0)


def test_classify_infrastructure_api_adapter_path_still_works() -> None:
    """Unaffected by this fix — ``infrastructure`` is shallower than the
    nested ``api`` folder and wins via D1 regardless of whether bare
    ``api`` still matches anything at all."""
    result = classify_file_layer("bc-a/infrastructure/api/z.adapter.ts")
    assert result == ("bc-a", "infrastructure", 2)


def test_classify_dotted_billing_api_tail_still_resolves_to_presentation() -> None:
    """The decision's explicit "keep the .Api tail" requirement — must
    still work exactly as the existing D2 dotted-tail test already
    pins (test_classify_dotted_dotnet_module_layer_suffix_billing_api
    above); restated here alongside its dash-suffix counterpart for
    direct side-by-side contrast."""
    result = classify_file_layer("src/Billing/Billing.Api/E.cs")
    assert result == ("Billing", "presentation", 3)


def test_classify_dot_tail_api_resolves_but_dash_tail_api_does_not() -> None:
    """The crux of reconciling the two decision requirements: ``.Api``
    (dot) and ``-api`` (dash) must behave OPPOSITELY. Same tail token
    ("api"), same prefix concept, different delimiter — proves the fix
    can't just be "toggle api on/off" globally, it has to be
    delimiter-aware."""
    assert classify_file_layer("src/Billing/Billing.Api/E.cs") == (
        "Billing", "presentation", 3,
    )
    assert classify_file_layer("src/Billing/billing-api/E.cs") is None


def test_classify_bare_api_dir_returns_none_accepted_regression() -> None:
    """Explicitly pinned accepted regression, per the decision: a bare
    ``.NET``-style ``Api`` folder (no dot/dash tail convention) that
    used to classify as presentation now returns ``None``. Fix per-repo
    via a custom ``[architecture]`` layers override (proven working by
    the escape-hatch test below)."""
    result = classify_file_layer("src/BcA/Api/Endpoints.cs")
    assert result is None


def test_classify_custom_layers_override_can_reinstate_bare_api_segment() -> None:
    """The documented escape hatch must actually work: a repo that wants
    the old bare-``api`` behaviour back can restore it via an explicit
    ``[architecture]`` override that puts ``"api"`` back into
    ``presentation``'s segments. Passes both before and after the fix —
    it's proving the ESCAPE HATCH, not the default-table change."""
    custom = resolve_architecture_layers(
        {
            "presentation": LayerRule(
                name="presentation", rank=3, segments=("api", "presentation")
            ),
        }
    )
    result = classify_file_layer("src/BcA/Api/Endpoints.cs", layers=custom)
    assert result == ("BcA", "presentation", 3)


# ------------------------------------------ canonical cases kept green
#
# Pinned explicitly per the review: these work today and must keep
# working unchanged by D1/D2/D3.


def test_classify_dotnet_module_folder_still_provides_context() -> None:
    result = classify_file_layer("src/BcA/Domain/Invoice.cs")
    assert result == ("BcA", "domain", 0)


def test_classify_angular_feature_folder_still_provides_context() -> None:
    result = classify_file_layer("src/app/features/bc-a/domain/invoice.model.ts")
    assert result == ("bc-a", "domain", 0)


def test_classify_angular_application_layer_still_resolves() -> None:
    result = classify_file_layer(
        "src/app/features/bc-a/application/use-cases/foo.ts"
    )
    assert result == ("bc-a", "application", 1)


def test_classify_unmatched_dotnet_path_still_returns_none() -> None:
    assert classify_file_layer("src/Billing/Models/Invoice.cs") is None
