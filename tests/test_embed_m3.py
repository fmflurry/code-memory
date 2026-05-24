"""Unit tests for the m3 embedder adapter — no model load."""

from __future__ import annotations

import numpy as np

from code_memory.embed.m3 import HybridVec, SparseVec, _to_qdrant_sparse


def test_to_qdrant_sparse_filters_nonpositive() -> None:
    raw = {"1": np.float32(0.5), "2": np.float32(-0.1), "3": np.float32(0.0)}
    out = _to_qdrant_sparse(raw)
    assert out.indices == [1]
    assert out.values == [0.5]


def test_to_qdrant_sparse_drops_nan() -> None:
    raw = {"1": np.float32(float("nan")), "2": np.float32(0.3)}
    out = _to_qdrant_sparse(raw)
    assert out.indices == [2]


def test_to_qdrant_sparse_skips_non_int_keys() -> None:
    raw = {"abc": np.float32(0.9), "5": np.float32(0.2)}
    out = _to_qdrant_sparse(raw)
    assert out.indices == [5]


def test_hybridvec_immutable() -> None:
    hv = HybridVec(dense=[1.0, 2.0], sparse=SparseVec(indices=[1], values=[0.5]))
    assert hv.dense == [1.0, 2.0]
    assert hv.sparse.indices == [1]
