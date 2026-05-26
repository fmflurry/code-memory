import time
import httpx
import pytest
from unittest.mock import MagicMock, patch

from code_memory.resilience import (
    with_retry,
    CircuitBreaker,
    CircuitBreakerOpenError,
    is_retryable,
)


def test_with_retry_succeeds_first_attempt():
    fn = MagicMock(return_value="ok")
    assert with_retry(fn) == "ok"
    fn.assert_called_once()


def test_with_retry_retries_on_connect_error():
    fn = MagicMock()
    fn.side_effect = [httpx.ConnectError("connection refused"), "ok"]

    result = with_retry(fn, max_retries=3, backoff_s=0.01)
    assert result == "ok"
    assert fn.call_count == 2


def test_with_retry_does_not_retry_4xx():
    resp = httpx.Response(400, request=httpx.Request("GET", "http://x"))
    fn = MagicMock(side_effect=httpx.HTTPStatusError("bad request", request=resp.request, response=resp))

    with pytest.raises(httpx.HTTPStatusError):
        with_retry(fn, max_retries=3, backoff_s=0.01)
    fn.assert_called_once()


def test_with_retry_raises_after_exhausting():
    fn = MagicMock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(httpx.ConnectError):
        with_retry(fn, max_retries=2, backoff_s=0.01)
    assert fn.call_count == 3  # initial + 2 retries


def test_circuit_breaker_starts_closed():
    cb = CircuitBreaker("test", threshold=3, cooldown_s=60)
    assert cb.state == "closed"


def test_circuit_breaker_opens_after_threshold_failures():
    cb = CircuitBreaker("test", threshold=3, cooldown_s=60)

    fn = MagicMock(side_effect=ValueError("fail"))
    for _ in range(3):
        with pytest.raises(ValueError):
            cb.call(fn)

    assert cb.state == "open"
    assert cb._failures == 3


def test_circuit_breaker_stays_open_during_cooldown():
    cb = CircuitBreaker("test", threshold=2, cooldown_s=60)
    fn_fail = MagicMock(side_effect=ValueError("fail"))

    for _ in range(2):
        with pytest.raises(ValueError):
            cb.call(fn_fail)

    assert cb.state == "open"

    fn_ok = MagicMock(return_value="ok")
    with pytest.raises(CircuitBreakerOpenError):
        cb.call(fn_ok)
    fn_ok.assert_not_called()


def test_circuit_breaker_transitions_to_half_open_after_cooldown():
    cb = CircuitBreaker("test", threshold=2, cooldown_s=0.01)
    fn_fail = MagicMock(side_effect=ValueError("fail"))

    for _ in range(2):
        with pytest.raises(ValueError):
            cb.call(fn_fail)

    assert cb.state == "open"

    time.sleep(0.02)

    # state property should transition to half_open after cooldown
    assert cb.state == "half_open"


def test_circuit_breaker_closes_after_success_in_half_open():
    cb = CircuitBreaker("test", threshold=2, cooldown_s=0.01)
    fn_fail = MagicMock(side_effect=ValueError("fail"))

    for _ in range(2):
        with pytest.raises(ValueError):
            cb.call(fn_fail)

    assert cb._state == "open"

    time.sleep(0.02)
    assert cb.state == "half_open"

    fn_ok = MagicMock(return_value="success")
    result = cb.call(fn_ok)
    assert result == "success"
    assert cb.state == "closed"
