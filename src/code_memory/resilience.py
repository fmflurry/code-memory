import time
import httpx

def is_retryable(exc: Exception) -> bool:
    """True for transient failures that retry can fix."""
    if isinstance(exc, httpx.ConnectError):
        return True
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False

def with_retry(fn, *, max_retries=3, backoff_s=1.0, on_retry=None):
    """Call fn(), retry on transient httpx errors with exponential backoff.
    
    After max_retries attempts, re-raises the last exception.
    on_retry(attempt, exc) is called before each retry for logging.
    """
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            if attempt == max_retries:
                raise
            if not is_retryable(exc):
                raise
            if on_retry:
                on_retry(attempt + 1, exc)
            delay = backoff_s * (2 ** attempt)
            time.sleep(delay)
    raise RuntimeError("unreachable")

class CircuitBreaker:
    """Opens after `threshold` consecutive failures; stays open for `cooldown_s`.
    
    While open, raises CircuitBreakerOpenError immediately without calling fn.
    On first success after cooldown, transitions to half-open; on next success, closes.
    """
    
    def __init__(self, name="default", threshold=5, cooldown_s=30.0):
        self.name = name
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._failures = 0
        self._last_failure = 0.0
        self._state = "closed"  # closed | open | half_open
    
    @property
    def state(self) -> str:
        if self._state == "open" and time.time() - self._last_failure > self.cooldown_s:
            self._state = "half_open"
        return self._state
    
    def call(self, fn, *args, **kwargs):
        if self._state == "open" or (self._state == "open" and time.time() - self._last_failure <= self.cooldown_s):
            raise CircuitBreakerOpenError(self.name, self._failures)
        try:
            result = fn(*args, **kwargs)
            if self._state == "half_open":
                self._state = "closed"
            self._failures = 0
            return result
        except Exception as exc:
            self._failures += 1
            self._last_failure = time.time()
            if self._failures >= self.threshold:
                self._state = "open"
            raise

class CircuitBreakerOpenError(Exception):
    def __init__(self, name, failures):
        super().__init__(f"Circuit breaker '{name}' open after {failures} failures")
