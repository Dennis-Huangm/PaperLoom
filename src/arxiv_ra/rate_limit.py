from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator


_STATE_LOCK = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
_LAST_REQUEST: dict[str, float] = {}
_COOLDOWN_UNTIL: dict[str, float] = {}


def defer_rate_limit(key: str, seconds: float) -> None:
    """Make subsequent requests wait after an upstream rate-limit response."""
    until = time.monotonic() + max(0.0, float(seconds))
    with _STATE_LOCK:
        _COOLDOWN_UNTIL[key] = max(_COOLDOWN_UNTIL.get(key, 0.0), until)


@contextmanager
def shared_rate_limit(key: str, minimum_interval: float, checkpoint: Callable[[], None] | None = None) -> Iterator[None]:
    """Serialize a rate-limited external API across all worker instances."""
    with _STATE_LOCK:
        lock = _LOCKS.setdefault(key, threading.Lock())
    if checkpoint is None:
        lock.acquire()
    else:
        while not lock.acquire(timeout=.1):
            checkpoint()
    try:
        last = _LAST_REQUEST.get(key, 0.0)
        elapsed = time.monotonic() - last
        cooldown = _COOLDOWN_UNTIL.get(key, 0.0)
        interval_wait = max(0.0, float(minimum_interval) - elapsed) if last else 0.0
        cooldown_wait = max(0.0, cooldown - time.monotonic())
        wait = max(interval_wait, cooldown_wait)
        if wait:
            if checkpoint is None:
                time.sleep(wait)
            else:
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    checkpoint()
                    time.sleep(min(.1, max(0, deadline - time.monotonic())))
        yield
    finally:
        _LAST_REQUEST[key] = time.monotonic()
        lock.release()
