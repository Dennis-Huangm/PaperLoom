from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator


class TaskCancelled(BaseException):
    """Cooperative cancellation signal for long-running background work."""


@dataclass(frozen=True)
class TaskHooks:
    progress: Callable[[str, int | None], None]
    warning: Callable[[str, str], None]
    is_cancelled: Callable[[], bool]
    checkpoint_data: Callable[[str, dict], None] | None = None
    commit: Callable | None = None


_CURRENT_HOOKS: ContextVar[TaskHooks | None] = ContextVar(
    "arxiv_ra_task_hooks", default=None
)


@contextmanager
def bind_task_hooks(hooks: TaskHooks) -> Iterator[None]:
    token = _CURRENT_HOOKS.set(hooks)
    try:
        yield
    finally:
        _CURRENT_HOOKS.reset(token)


def task_checkpoint() -> None:
    """Stop at a safe boundary when the current background job was cancelled."""
    hooks = _CURRENT_HOOKS.get()
    if hooks is not None and hooks.is_cancelled():
        raise TaskCancelled()


def task_checkpoint_data(key: str, value: dict) -> None:
    """Persist resolved input before starting work that depends on it."""
    task_checkpoint()
    hooks = _CURRENT_HOOKS.get()
    if hooks is not None and hooks.checkpoint_data is not None:
        hooks.checkpoint_data(key, value)


def supports_task_checkpoints() -> bool:
    hooks = _CURRENT_HOOKS.get()
    return hooks is not None and hooks.checkpoint_data is not None


def task_progress(detail: str, percent: int | None = None) -> None:
    """Publish a human-readable stage and then continue if not cancelled."""
    task_checkpoint()
    hooks = _CURRENT_HOOKS.get()
    if hooks is not None:
        hooks.progress(detail, percent)


def task_warning(component: str, message: str) -> None:
    """Publish a recoverable component failure without aborting the whole task."""
    task_checkpoint()
    hooks = _CURRENT_HOOKS.get()
    if hooks is not None:
        hooks.warning(component, message)


@contextmanager
def task_subtask(label: str, start: float, end: float, on_progress=None) -> Iterator[None]:
    """Map a child's progress into its share of the parent task."""
    parent = _CURRENT_HOOKS.get()

    def progress(detail, percent):
        if on_progress:
            on_progress(detail, percent)
        if parent:
            parent.progress(f"{label} · {detail}", round(start + (end - start) * (percent or 0) / 100))

    hooks = TaskHooks(progress=progress,
                      warning=lambda component, message: parent.warning(f"{label} · {component}", message) if parent else None,
                      is_cancelled=parent.is_cancelled if parent else lambda: False)
    with bind_task_hooks(hooks):
        yield


@contextmanager
def task_commit():
    """Serialize the final publication against a GUI cancellation request."""
    hooks = _CURRENT_HOOKS.get()
    task_checkpoint()
    if hooks is not None and hooks.commit is not None:
        with hooks.commit():
            yield
    else:
        yield
