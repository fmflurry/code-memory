"""Tests for the FalkorDB connection guard (``get_falkor_db``).

The redis default of no socket timeout turned any half-open endpoint
(WSL2-forwarded port without a responder, per-process flow inspection by
endpoint security) into an infinite hang — reingest/watch processes piled
up by the dozen. Guards:

1. Socket timeouts are always passed to the client.
2. The constructor is retried once — first-flow quarantine eats exactly
   one connection attempt, the next one succeeds.
3. Two failures surface the error (no infinite loop).
4. The (host, port) singleton cache still works.
"""

from __future__ import annotations

import pytest

from code_memory.graph import falkor_store


@pytest.fixture(autouse=True)
def fresh_registry():
    falkor_store._DBS.clear()
    yield
    falkor_store._DBS.clear()


class FlakyFalkor:
    """Times out on the first N constructions, then succeeds."""

    fail_first = 0
    calls: list[dict] = []

    def __init__(self, **kwargs):
        type(self).calls.append(kwargs)
        if len(type(self).calls) <= type(self).fail_first:
            raise TimeoutError("Timeout connecting to server")


def _install(monkeypatch: pytest.MonkeyPatch, *, fail_first: int) -> type[FlakyFalkor]:
    class Fake(FlakyFalkor):
        pass

    Fake.fail_first = fail_first
    Fake.calls = []
    monkeypatch.setattr(falkor_store, "FalkorDB", Fake)
    return Fake


def test_socket_timeouts_always_set(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, fail_first=0)

    falkor_store.get_falkor_db("127.0.0.1", 6379)

    kwargs = fake.calls[0]
    assert kwargs["socket_connect_timeout"] == 10
    # Must exceed the server-side TIMEOUT_MAX (60 s) so long graph
    # queries don't become false failures.
    assert kwargs["socket_timeout"] > 60


def test_retries_once_after_first_flow_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, fail_first=1)

    db = falkor_store.get_falkor_db("127.0.0.1", 6379)

    assert db is not None
    assert len(fake.calls) == 2


def test_two_failures_surface_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, fail_first=99)

    with pytest.raises(TimeoutError):
        falkor_store.get_falkor_db("127.0.0.1", 6379)

    assert len(fake.calls) == 2  # bounded — no infinite retry loop
    assert ("127.0.0.1", 6379) not in falkor_store._DBS


def test_singleton_cache_per_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, fail_first=0)

    a = falkor_store.get_falkor_db("127.0.0.1", 6379)
    b = falkor_store.get_falkor_db("127.0.0.1", 6379)
    c = falkor_store.get_falkor_db("falkor.internal", 6379)

    assert a is b
    assert a is not c
    assert len(fake.calls) == 2  # one per distinct endpoint
