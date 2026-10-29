"""Transient-only retry helper with jittered exponential backoff (stdlib only)."""

from __future__ import annotations

import random
import socket
import time
import urllib.error
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

RETRY_AFTER_CAP_SECONDS = 30.0
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """True for failures worth retrying: timeouts, resets, 429 and 5xx."""
    transient = getattr(exc, "transient", None)
    if transient is not None:
        return bool(transient)
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status in RETRYABLE_HTTP_STATUS
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_HTTP_STATUS
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return True
    return False


def backoff_delay(
    attempt: int, base_seconds: float, retry_after: Optional[float] = None
) -> float:
    """Exponential backoff with full jitter, honoring a capped Retry-After hint."""
    delay = base_seconds * (2 ** attempt)
    if retry_after and retry_after > 0:
        delay = max(delay, min(float(retry_after), RETRY_AFTER_CAP_SECONDS))
    return delay + random.uniform(0.0, max(delay, 0.001) * 0.25)


def call_with_retry(
    operation: Callable[[], T],
    attempts: int = 3,
    base_seconds: float = 0.5,
    should_retry: Callable[[BaseException], bool] = is_transient,
    retry_after_of: Callable[[BaseException], Optional[float]] = lambda exc: None,
) -> T:
    """Run *operation*, retrying only transient failures.

    Non-transient exceptions propagate immediately. The last exception is
    re-raised once *attempts* are exhausted.
    """
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if attempt == attempts - 1 or not should_retry(exc):
                raise
            time.sleep(backoff_delay(attempt, base_seconds, retry_after_of(exc)))
    raise RuntimeError("unreachable")  # pragma: no cover
