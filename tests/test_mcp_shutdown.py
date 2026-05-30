"""Graceful teardown of the in-process watcher on MCP server shutdown."""

from __future__ import annotations

import signal

import pytest

from code_memory import mcp_server


class _FakeWatcher:
    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


@pytest.fixture(autouse=True)
def _clean_refs():
    mcp_server._BOOTSTRAP_REFS.pop("watcher", None)
    yield
    mcp_server._BOOTSTRAP_REFS.pop("watcher", None)


def test_teardown_watcher_stops_and_clears() -> None:
    fw = _FakeWatcher()
    mcp_server._BOOTSTRAP_REFS["watcher"] = fw

    mcp_server._teardown_watcher()

    assert fw.stopped == 1
    assert "watcher" not in mcp_server._BOOTSTRAP_REFS


def test_teardown_watcher_is_idempotent() -> None:
    fw = _FakeWatcher()
    mcp_server._BOOTSTRAP_REFS["watcher"] = fw

    mcp_server._teardown_watcher()
    mcp_server._teardown_watcher()

    assert fw.stopped == 1  # second call is a no-op


def test_teardown_watcher_noop_when_absent() -> None:
    mcp_server._teardown_watcher()  # must not raise


def test_teardown_watcher_swallows_stop_errors() -> None:
    class _Boom:
        def stop(self) -> None:
            raise RuntimeError("boom")

    mcp_server._BOOTSTRAP_REFS["watcher"] = _Boom()
    mcp_server._teardown_watcher()  # must not raise
    assert "watcher" not in mcp_server._BOOTSTRAP_REFS


def test_install_shutdown_hooks_registers_atexit_and_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: list = []
    handlers: dict = {}
    monkeypatch.setattr(mcp_server.atexit, "register", registered.append)
    monkeypatch.setattr(mcp_server.signal, "signal", lambda s, h: handlers.__setitem__(s, h))

    mcp_server._install_shutdown_hooks()

    assert mcp_server._teardown_watcher in registered
    assert signal.SIGTERM in handlers

    # The SIGTERM handler tears the watcher down, then exits cleanly.
    fw = _FakeWatcher()
    mcp_server._BOOTSTRAP_REFS["watcher"] = fw
    with pytest.raises(SystemExit):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert fw.stopped == 1


def test_install_shutdown_hooks_tolerates_non_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server.atexit, "register", lambda fn: None)

    def _raise(*_a, **_k):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(mcp_server.signal, "signal", _raise)
    mcp_server._install_shutdown_hooks()  # must not raise
