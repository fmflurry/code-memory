"""Tests for the cross-encoder rerank stage.

No torch / sentence-transformers import — we inject a fake reranker through the
test seam so the suite stays runnable on every host (Linux CI, CPU-only
Mac, Metal Mac).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_memory.orchestrator import rerank
from code_memory.orchestrator.rerank import (
    DEFAULT_MODEL,
    ENV_ALPHA,
    ENV_MODE,
    RerankPolicy,
    decide_policy,
    maybe_cross_encode,
    set_reranker_for_tests,
)
from code_memory.vector import VectorHit


# ---------------------------------------------------------------- fixtures


class _FakeReranker:
    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self._scores = scores_by_text
        self.calls: list[list[tuple[str, str]]] = []

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.calls.append(list(pairs))
        # Score by exact-match on the doc text; default 0.0 keeps
        # untouched candidates demoted unambiguously.
        return [self._scores.get(doc, 0.0) for _, doc in pairs]


@pytest.fixture(autouse=True)
def _reset_singleton():
    set_reranker_for_tests(None, None)
    yield
    set_reranker_for_tests(None, None)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _hit(path: Path, start: int, end: int, score: float, hit_id: str) -> VectorHit:
    return VectorHit(
        id=hit_id,
        score=score,
        payload={"path": str(path), "start": start, "end": end},
    )


# ---------------------------------------------------------------- policy


def test_policy_force_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MODE, "0")
    p = decide_policy()
    assert p.enabled is False
    assert p.device == "off"


def test_policy_force_on_without_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MODE, "1")
    monkeypatch.setattr(rerank, "_detect_device", lambda: "off")
    p = decide_policy()
    assert p.enabled is False
    assert "torch" in p.reason


def test_policy_force_on_with_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MODE, "1")
    monkeypatch.setattr(rerank, "_detect_device", lambda: "cpu")
    p = decide_policy()
    assert p.enabled is True
    assert p.device == "cpu"


def test_policy_auto_skips_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_MODE, raising=False)
    monkeypatch.setattr(rerank, "_detect_device", lambda: "cpu")
    p = decide_policy()
    assert p.enabled is False
    assert p.device == "cpu"


def test_policy_auto_enables_on_metal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_MODE, raising=False)
    monkeypatch.setattr(rerank, "_detect_device", lambda: "mps")
    p = decide_policy()
    assert p.enabled is True
    assert p.device == "mps"


def test_policy_default_model() -> None:
    p = decide_policy()
    assert p.model == DEFAULT_MODEL


# ---------------------------------------------------------------- maybe_cross_encode


def test_disabled_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MODE, "0")
    f = _write(tmp_path, "a.ts", "line1\nline2\nline3\n")
    hits = [_hit(f, 1, 3, 0.42, "a")]
    out = maybe_cross_encode("query", hits)
    assert out is hits


def test_empty_hits_is_noop() -> None:
    set_reranker_for_tests(_FakeReranker({}), RerankPolicy(True, "mps", "m", "test"))
    assert maybe_cross_encode("q", []) == []


def test_reorders_by_cross_encoder_scores(tmp_path: Path) -> None:
    f1 = _write(tmp_path, "a.ts", "auth login flow\n")
    f2 = _write(tmp_path, "b.ts", "image rendering pipeline\n")
    hits = [
        _hit(f1, 1, 1, 0.30, "a"),  # bi-encoder ranks a low
        _hit(f2, 1, 1, 0.90, "b"),  # bi-encoder ranks b high
    ]
    fake = _FakeReranker(
        {
            "auth login flow": 0.95,  # cross-encoder lifts a
            "image rendering pipeline": 0.10,
        }
    )
    set_reranker_for_tests(fake, RerankPolicy(True, "mps", "m", "test"))

    out = maybe_cross_encode("how does auth work", hits)
    by_id = {h.id: h.score for h in out}
    # Default α=0.5: final = 0.5·bi + 0.5·ce
    assert by_id["a"] == pytest.approx(0.5 * 0.30 + 0.5 * 0.95)
    assert by_id["b"] == pytest.approx(0.5 * 0.90 + 0.5 * 0.10)
    # The original payload is preserved.
    assert next(h for h in out if h.id == "a").payload["path"] == str(f1)


def test_missing_file_falls_back_to_original_score(tmp_path: Path) -> None:
    real = _write(tmp_path, "real.ts", "exists\n")
    missing = tmp_path / "ghost.ts"
    hits = [
        _hit(real, 1, 1, 0.50, "real"),
        _hit(missing, 1, 1, 0.42, "ghost"),
    ]
    fake = _FakeReranker({"exists": 0.88})
    set_reranker_for_tests(fake, RerankPolicy(True, "mps", "m", "test"))

    out = maybe_cross_encode("q", hits)
    scores = {h.id: h.score for h in out}
    assert scores["real"] == pytest.approx(0.5 * 0.50 + 0.5 * 0.88)
    assert scores["ghost"] == pytest.approx(0.42)  # unscorable -> unchanged
    # Only one pair fed to the reranker.
    assert len(fake.calls[0]) == 1


def test_reranker_failure_returns_original_hits(tmp_path: Path) -> None:
    f = _write(tmp_path, "a.ts", "body\n")
    hits = [_hit(f, 1, 1, 0.5, "a")]

    class Boom:
        def score(self, pairs):
            raise RuntimeError("metal exploded")

    set_reranker_for_tests(Boom(), RerankPolicy(True, "mps", "m", "test"))
    out = maybe_cross_encode("q", hits)
    assert out == hits


def test_payload_without_path_skipped(tmp_path: Path) -> None:
    f = _write(tmp_path, "a.ts", "ok\n")
    good = _hit(f, 1, 1, 0.5, "good")
    bad = VectorHit(id="bad", score=0.4, payload={})  # no path/start/end
    fake = _FakeReranker({"ok": 0.99})
    set_reranker_for_tests(fake, RerankPolicy(True, "mps", "m", "test"))

    out = maybe_cross_encode("q", [good, bad])
    by_id = {h.id: h.score for h in out}
    assert by_id["good"] == pytest.approx(0.5 * 0.5 + 0.5 * 0.99)
    assert by_id["bad"] == pytest.approx(0.4)  # no path -> unchanged


def test_alpha_one_replaces_bi_encoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ALPHA, "1.0")
    f = _write(tmp_path, "a.ts", "body\n")
    hits = [_hit(f, 1, 1, 0.10, "a")]
    fake = _FakeReranker({"body": 0.99})
    set_reranker_for_tests(fake, RerankPolicy(True, "mps", "m", "test"))

    out = maybe_cross_encode("q", hits)
    assert out[0].score == pytest.approx(0.99)


def test_alpha_zero_keeps_bi_encoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ALPHA, "0.0")
    f = _write(tmp_path, "a.ts", "body\n")
    hits = [_hit(f, 1, 1, 0.10, "a")]
    fake = _FakeReranker({"body": 0.99})
    set_reranker_for_tests(fake, RerankPolicy(True, "mps", "m", "test"))

    out = maybe_cross_encode("q", hits)
    assert out[0].score == pytest.approx(0.10)


def test_alpha_invalid_falls_back_to_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ALPHA, "not-a-number")
    f = _write(tmp_path, "a.ts", "body\n")
    hits = [_hit(f, 1, 1, 0.10, "a")]
    fake = _FakeReranker({"body": 0.90})
    set_reranker_for_tests(fake, RerankPolicy(True, "mps", "m", "test"))

    out = maybe_cross_encode("q", hits)
    assert out[0].score == pytest.approx(0.5 * 0.10 + 0.5 * 0.90)


def test_all_unscorable_returns_original(tmp_path: Path) -> None:
    bad = VectorHit(id="x", score=0.1, payload={})
    fake = _FakeReranker({})
    set_reranker_for_tests(fake, RerankPolicy(True, "mps", "m", "test"))

    out = maybe_cross_encode("q", [bad])
    assert out == [bad]
    assert fake.calls == []  # reranker never called
