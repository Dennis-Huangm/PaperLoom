from __future__ import annotations

import threading
import time
import os
import json
import math
import errno
from pathlib import Path
from contextlib import contextmanager
from typing import Callable, Iterator
from .utils import write_json


_STATE_LOCK = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
_LAST_REQUEST: dict[str, float] = {}
_COOLDOWN_UNTIL: dict[str, float] = {}


def defer_rate_limit(key: str, seconds: float) -> None:
    """Make subsequent requests wait after an upstream rate-limit response."""
    until = time.monotonic() + max(0.0, float(seconds))
    with _STATE_LOCK:
        _COOLDOWN_UNTIL[key] = max(_COOLDOWN_UNTIL.get(key, 0.0), until)
    if key == "semantic-scholar":
        with _semantic_file_lock() as state_path:
            state = _read_semantic_state(state_path)
            state['cooldown_until'] = max(state.get('cooldown_until', 0.0), time.time() + max(0, seconds))
            write_json(state_path, state)


@contextmanager
def _thread_rate_limit(key: str, minimum_interval: float, checkpoint: Callable[[], None] | None = None) -> Iterator[None]:
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


@contextmanager
def _semantic_file_lock(checkpoint=None):
    """OS lock shared by this user's GUI, CLI and other project checkouts."""
    default = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.cache'))) / 'PaperLoom/rate-limits'
    root = Path(os.environ.get('ARXIV_RA_RATE_LIMIT_DIR', str(default)))
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'semantic-scholar.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle, fcntl.LOCK_UN)
        while True:
            if checkpoint:
                checkpoint()
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                time.sleep(.1)
        try:
            yield root / 'semantic-scholar.json'
        finally:
            release()


def _read_semantic_state(path):
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding='utf-8'))
    for value in state.values():
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Semantic Scholar 限速状态无效，已停止请求')
    return state


@contextmanager
def shared_rate_limit(key: str, minimum_interval: float, checkpoint: Callable[[], None] | None = None) -> Iterator[None]:
    """Share S2's aggregate quota across processes, including every retry."""
    with _thread_rate_limit(key, minimum_interval, checkpoint):
        if key != 'semantic-scholar':
            yield
            return
        with _semantic_file_lock(checkpoint) as path:
            state = _read_semantic_state(path)
            interval = max(1.2, float(minimum_interval))
            # Completion-to-start spacing is conservative even for slow requests.
            deadline = max(state.get('last_completed', 0) + interval, state.get('cooldown_until', 0))
            while (remaining := deadline - time.time()) > 0:
                if checkpoint:
                    checkpoint()
                time.sleep(min(.1, remaining))
            try:
                if checkpoint:
                    checkpoint()
                yield
            finally:
                state['last_completed'] = time.time()
                write_json(path, state)
