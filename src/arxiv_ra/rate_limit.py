from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator


_STATE_LOCK = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
_LAST_REQUEST: dict[str, float] = {}


@contextmanager
def shared_rate_limit(key: str, minimum_interval: float) -> Iterator[None]:
    """Serialize a rate-limited external API across all worker instances."""
    with _STATE_LOCK:
        lock = _LOCKS.setdefault(key, threading.Lock())
    lock.acquire()
    try:
        last = _LAST_REQUEST.get(key, 0.0)
        elapsed = time.monotonic() - last
        wait = max(0.0, float(minimum_interval) - elapsed) if last else 0.0
        if wait:
            time.sleep(wait)
        yield
    finally:
        _LAST_REQUEST[key] = time.monotonic()
        lock.release()
