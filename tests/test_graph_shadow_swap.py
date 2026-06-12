"""Tests for the graph shadow-swap fix in Pipeline._ingest_full.

Three invariants verified:
  (i)  ``_ingest_full`` must write graph data to a SHADOW FalkorStore, not the
       live ``self.graph``, so the live graph is never emptied before the
       rebuild commits.
  (ii) An interrupted rebuild (exception mid-walk) must leave the live graph
       intact — ``clear_graph`` must never be called on it, and ``promote_shadow``
       must never be called either (i.e. the swap never fires on failure).
  (iii) ``FalkorStore.promote_shadow`` must delete the live graph BEFORE copying
        the shadow over, then delete the shadow — in that exact order.

The tests target NEW API that does NOT yet exist:
  - ``Pipeline._active_graph: FalkorStore`` (used by ``_upsert_graph`` instead of
    ``self.graph`` during full rebuild).
  - ``FalkorStore.promote_shadow(shadow_graph_name: str)`` — atomic swap.

All tests therefore go RED until the implementation is added to src/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers shared across tests — mirror test_ensure_fresh_no_blocking.py style
# ---------------------------------------------------------------------------


def _make_fake_extracted_file() -> Any:
    """Return a minimal MagicMock that satisfies _upsert_graph's attribute reads."""
    return MagicMock(
        symbols=[],
        imports=[],
        calls=[],
        references=[],
        injects=[],
        path="fake/file.py",
        lang="py",
        generated=False,
    )


def _build_pipeline(tmp_path: Path) -> Any:
    """Construct a Pipeline via __new__ (no real backends) with safe defaults.

    Mirrors the pattern established in test_ensure_fresh_no_blocking.py.
    """
    from code_memory.config import CONFIG
    from code_memory.orchestrator.pipeline import Pipeline

    mock_vector = MagicMock()
    mock_graph = MagicMock()
    mock_episodic = MagicMock()
    mock_state = MagicMock()
    mock_state.get.return_value = None

    mock_vector._inspect_collection.return_value = "hybrid"
    mock_vector.client = MagicMock()
    mock_vector.client.scroll.return_value = ([], None)

    pipe = Pipeline.__new__(Pipeline)
    pipe.slug = "test-project"
    pipe.cfg = CONFIG.for_project("test-project")
    pipe.skip_vectors = True
    pipe.embedder = MagicMock()
    pipe.vector = mock_vector
    pipe.graph = mock_graph
    pipe.episodic = mock_episodic
    pipe.state = mock_state
    pipe._active_code_collection = pipe.cfg.qdrant_code

    return pipe


# ---------------------------------------------------------------------------
# (i) Happy-path: full rebuild must write into the SHADOW graph, not the live one
# ---------------------------------------------------------------------------


def test_ingest_full_builds_into_shadow_graph_not_live(
    tmp_path: Path,
) -> None:
    """_ingest_full must route all graph upserts to a shadow FalkorStore.

    Specifically:
    - ``self.graph.clear_graph`` must NEVER be called (live graph stays intact).
    - A shadow FalkorStore is constructed with
      ``graph_name == cfg.falkor_graph + "__shadow"``.
    - ``upsert_nodes`` / ``upsert_edges`` during the walk hit the SHADOW mock,
      not ``self.graph``.
    """
    from code_memory.orchestrator.pipeline import Pipeline

    pipe = _build_pipeline(tmp_path)
    mock_graph = pipe.graph  # the live graph mock
    expected_shadow_name = pipe.cfg.falkor_graph + "__shadow"

    # Track which graph_name the FalkorStore factory was called with, and
    # return a distinct shadow mock so we can assert writes landed there.
    shadow_mock = MagicMock()
    shadow_mock.upsert_nodes = MagicMock()
    shadow_mock.upsert_edges = MagicMock()

    created_with: list[str] = []

    def _falkor_factory(**kwargs: Any) -> MagicMock:
        name = kwargs.get("graph_name", "")
        created_with.append(name)
        return shadow_mock

    fake_file = _make_fake_extracted_file()

    with patch(
        "code_memory.orchestrator.pipeline.FalkorStore", side_effect=_falkor_factory
    ):
        with patch("code_memory.orchestrator.pipeline.Extractor") as MockExtractor:
            MockExtractor.return_value.walk.return_value = iter([fake_file])
            with patch.object(pipe, "_ingest_dotnet_projects"):
                # Patch the promote step so the test stays at unit scope.
                with patch.object(pipe, "_commit_shadow_collection"):
                    # promote_shadow on the graph mock — no real FalkorDB.
                    with patch.object(mock_graph, "promote_shadow", create=True):
                        pipe._ingest_full(tmp_path, dry_run=False)

    # --- Assertions ---

    # 1. The live graph must never have been emptied.
    mock_graph.clear_graph.assert_not_called()

    # 2. A shadow FalkorStore was constructed with the correct shadow name.
    assert expected_shadow_name in created_with, (
        f"Expected FalkorStore to be constructed with "
        f"graph_name={expected_shadow_name!r}; got calls with: {created_with}"
    )

    # 3. Graph upserts during the walk hit the SHADOW store, not the live one.
    assert shadow_mock.upsert_nodes.called or shadow_mock.upsert_edges.called, (
        "Expected upsert_nodes or upsert_edges to be called on the shadow FalkorStore, "
        "but neither was called. The implementation must route writes through "
        "_active_graph (the shadow), not self.graph."
    )
    mock_graph.upsert_nodes.assert_not_called()
    mock_graph.upsert_edges.assert_not_called()


# ---------------------------------------------------------------------------
# (ii) Interrupted rebuild: live graph must survive an exception mid-walk
# ---------------------------------------------------------------------------


def test_interrupted_rebuild_leaves_live_graph_intact(
    tmp_path: Path,
) -> None:
    """An exception mid-rebuild must never have cleared the live graph.

    This is the core RED test: if _ingest_full still calls
    ``self.graph.clear_graph()`` before the walk, an interrupted rebuild
    leaves the live graph empty.

    After the fix:
    - ``clear_graph`` is never called on the live graph.
    - ``promote_shadow`` is never called (the swap fires only on success).
    - The shadow FalkorStore WAS created (rebuild had started building into it).
    """
    from code_memory.orchestrator.pipeline import Pipeline

    pipe = _build_pipeline(tmp_path)
    mock_graph = pipe.graph
    expected_shadow_name = pipe.cfg.falkor_graph + "__shadow"

    shadow_mock = MagicMock()
    created_with: list[str] = []

    def _falkor_factory(**kwargs: Any) -> MagicMock:
        name = kwargs.get("graph_name", "")
        created_with.append(name)
        return shadow_mock

    def _failing_walk(root: Path) -> Any:
        """Yield one file then simulate a crash — mirrors the Qdrant reference test."""
        yield _make_fake_extracted_file()
        raise RuntimeError("simulated crash mid-rebuild")

    with patch(
        "code_memory.orchestrator.pipeline.FalkorStore", side_effect=_falkor_factory
    ):
        with patch("code_memory.orchestrator.pipeline.Extractor") as MockExtractor:
            MockExtractor.return_value.walk.side_effect = _failing_walk
            with pytest.raises(RuntimeError, match="simulated crash mid-rebuild"):
                pipe._ingest_full(tmp_path, dry_run=False)

    # 1. Live graph must never have been cleared.
    mock_graph.clear_graph.assert_not_called()

    # 2. The atomic swap must NOT have fired (rebuild did not complete).
    mock_graph.promote_shadow.assert_not_called()  # type: ignore[attr-defined]

    # 3. The shadow FalkorStore WAS created (rebuild had started).
    assert expected_shadow_name in created_with, (
        f"Expected shadow FalkorStore to be created with "
        f"graph_name={expected_shadow_name!r} before the crash; "
        f"got: {created_with}"
    )


# ---------------------------------------------------------------------------
# (iii) FalkorStore.promote_shadow call-order: delete dest → copy → delete shadow
# ---------------------------------------------------------------------------


def test_promote_shadow_deletes_dest_before_copy(
    tmp_path: Path,
) -> None:
    """promote_shadow must execute operations in strict order:

    1. DELETE the LIVE graph ("proj") — so Falkor won't error on an existing
       destination during GRAPH.COPY.
    2. GRAPH.COPY proj__shadow → proj — atomic copy of the shadow into the
       live graph name.
    3. DELETE the shadow graph ("proj__shadow") — clean up the temporary store.

    This test asserts against ``store.db.connection.execute_command`` mock
    calls for the GRAPH.COPY command, and that a delete of "proj" precedes it.

    Implementation note for the coder: ``promote_shadow`` should call
    ``self.db.connection.execute_command("GRAPH.DELETE", self.graph_name)``
    BEFORE ``self.db.connection.execute_command("GRAPH.COPY", shadow_graph_name,
    self.graph_name)``, and ``self.db.connection.execute_command("GRAPH.DELETE",
    shadow_graph_name)`` AFTER. The ``self.graph`` handle should then be
    rebound to ``self.db.select_graph(self.graph_name)`` so subsequent queries
    use the freshly-copied graph.
    """
    from code_memory.graph.falkor_store import FalkorStore

    # Build a bare FalkorStore instance without calling __init__ (no live FalkorDB).
    store = FalkorStore.__new__(FalkorStore)
    store.graph_name = "proj"  # type: ignore[attr-defined]

    # Mock the FalkorDB connection so no real server is needed.
    mock_db = MagicMock()
    mock_connection = MagicMock()
    mock_db.connection = mock_connection
    store.db = mock_db  # type: ignore[attr-defined]

    # After promote_shadow the store rebinds self.graph; pre-populate with a mock.
    mock_live_graph = MagicMock()
    store.graph = mock_live_graph  # type: ignore[attr-defined]

    # promote_shadow does NOT exist yet — this call goes RED.
    store.promote_shadow("proj__shadow")  # type: ignore[attr-defined]

    # Collect all execute_command calls.
    all_calls = mock_connection.execute_command.call_args_list

    # Extract just the command names and first positional arg (graph name).
    # Expected shape: [("GRAPH.DELETE", "proj"), ("GRAPH.COPY", "proj__shadow", "proj"),
    #                  ("GRAPH.DELETE", "proj__shadow")]
    delete_live_idx: int | None = None
    copy_idx: int | None = None
    delete_shadow_idx: int | None = None

    for i, c in enumerate(all_calls):
        args = c.args if c.args else tuple(c[0])
        if not args:
            continue
        cmd = args[0]
        if cmd == "GRAPH.DELETE" and len(args) >= 2 and args[1] == "proj":
            delete_live_idx = i
        elif cmd == "GRAPH.COPY":
            copy_idx = i
        elif cmd == "GRAPH.DELETE" and len(args) >= 2 and args[1] == "proj__shadow":
            delete_shadow_idx = i

    assert delete_live_idx is not None, (
        "promote_shadow must call execute_command('GRAPH.DELETE', 'proj') "
        f"to clear the destination before copying. Calls seen: {all_calls}"
    )
    assert copy_idx is not None, (
        "promote_shadow must call execute_command('GRAPH.COPY', 'proj__shadow', 'proj') "
        f"to copy the shadow into the live graph. Calls seen: {all_calls}"
    )
    assert delete_shadow_idx is not None, (
        "promote_shadow must call execute_command('GRAPH.DELETE', 'proj__shadow') "
        f"to clean up the shadow after copying. Calls seen: {all_calls}"
    )

    assert delete_live_idx < copy_idx, (
        f"GRAPH.DELETE 'proj' (idx {delete_live_idx}) must precede "
        f"GRAPH.COPY (idx {copy_idx}). Actual order: {all_calls}"
    )
    assert copy_idx < delete_shadow_idx, (
        f"GRAPH.COPY (idx {copy_idx}) must precede "
        f"GRAPH.DELETE 'proj__shadow' (idx {delete_shadow_idx}). "
        f"Actual order: {all_calls}"
    )

    # After promote_shadow, self.graph must be rebound to the newly-copied graph.
    mock_db.select_graph.assert_called_with("proj")
