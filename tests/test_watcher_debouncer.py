"""Debouncer coalesces bursts."""

from __future__ import annotations

import threading
import time

from code_memory.sync.watcher import Debouncer


def test_single_event_fires_after_window() -> None:
    fired = threading.Event()
    d = Debouncer(0.05, fired.set)
    d.bump()
    assert fired.wait(0.5)


def test_burst_coalesces_into_single_fire() -> None:
    count = {"n": 0}

    def flush() -> None:
        count["n"] += 1

    d = Debouncer(0.08, flush)
    for _ in range(20):
        d.bump()
        time.sleep(0.005)
    time.sleep(0.3)
    assert count["n"] == 1


def test_cancel_prevents_fire() -> None:
    fired = threading.Event()
    d = Debouncer(0.05, fired.set)
    d.bump()
    d.cancel()
    assert not fired.wait(0.2)
