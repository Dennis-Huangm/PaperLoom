from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
from typing import Any

from .utils import read_json, write_json


_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


@contextmanager
def _locked(path: Path):
    """Serialize all instances and processes; the OS releases locks on exit."""
    key = os.path.normcase(str(path.resolve()))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                # Windows permits locking beyond EOF. Do not initialize a byte:
                # another process may already own the lock during initialization.
                deadline = time.monotonic() + 30
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("阅读状态正在被其他进程更新，请重试")
                        time.sleep(0.02)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)


class ReadingStateStore:
    """One atomic, profile-scoped record of saved and dismissed papers.

    Legacy files are imported on first write and retained unchanged for recovery.
    Once the combined file exists it is the sole source of truth.
    """

    def __init__(self, output_root: Path, profile_id: str) -> None:
        suffix = profile_id or "default"
        self.path = output_root / f"reading-state-{suffix}.json"
        self.lock_path = output_root / f"reading-state-{suffix}.lock"
        self.library_path = output_root / f"paper-library-{suffix}.json"
        self.feedback_path = output_root / f"feedback-{suffix}.json"

    @staticmethod
    def _legacy(path: Path) -> dict:
        payload = read_json(path, {}) or {}
        items = payload.get("items", payload)
        if not isinstance(items, dict):
            raise ValueError(f"阅读状态格式错误：{path}")
        return items

    def _read(self) -> dict:
        if self.path.exists():
            state = read_json(self.path)
            if (not isinstance(state, dict) or state.get("version") != 1
                    or not isinstance(state.get("library"), dict)
                    or not isinstance(state.get("feedback"), dict)):
                raise ValueError(f"阅读状态格式错误：{self.path}")
            return state
        library, feedback = self._legacy(self.library_path), self._legacy(self.feedback_path)
        # Reconcile interrupted legacy transitions by their last update time.
        for aid in library.keys() & feedback.keys():
            saved_at = str(library[aid].get("updated_at") or library[aid].get("saved_at") or "")
            if str(feedback[aid].get("updated_at") or "") >= saved_at:
                del library[aid]
            else:
                del feedback[aid]
        return {"version": 1, "library": library, "feedback": feedback}

    def snapshot(self) -> dict:
        with _locked(self.lock_path):
            return self._read()

    def save(self, entry: dict[str, Any]) -> dict[str, Any]:
        with _locked(self.lock_path):
            state = self._read()
            entry = self._save_entry(state, entry)
            write_json(self.path, state)
            return entry

    @staticmethod
    def _save_entry(state: dict, entry: dict) -> dict:
        aid = entry["arxiv_id"]
        now = datetime.now(timezone.utc).isoformat()
        entry = {**entry, "saved_at": state["library"].get(aid, {}).get("saved_at") or now,
                 "updated_at": now}
        state["library"][aid] = entry
        state["feedback"].pop(aid, None)
        return entry

    def toggle(self, entry: dict) -> bool:
        with _locked(self.lock_path):
            state = self._read()
            removed = state["library"].pop(entry["arxiv_id"], None)
            if removed is None:
                self._save_entry(state, entry)
            write_json(self.path, state)
            return removed is None

    def dismiss(self, entry: dict[str, Any]) -> dict[str, Any]:
        with _locked(self.lock_path):
            state = self._read()
            state["feedback"][entry["arxiv_id"]] = entry
            state["library"].pop(entry["arxiv_id"], None)
            write_json(self.path, state)
            return entry

    def refresh_saved(self, previous: dict, item: dict) -> bool:
        """Repair metadata only if the user has not changed this entry meanwhile."""
        with _locked(self.lock_path):
            state = self._read()
            aid = previous["arxiv_id"]
            if state["library"].get(aid) != previous:
                return False
            state["library"][aid] = {**previous, "paper": item.get("paper") or {},
                                     "verified": item.get("verified") or {}}
            write_json(self.path, state)
            return True

    def remove(self, collection: str, arxiv_id: str) -> bool:
        if collection not in {"library", "feedback"}:
            raise ValueError("无效的阅读状态集合")
        with _locked(self.lock_path):
            state = self._read()
            removed = state[collection].pop(arxiv_id, None) is not None
            if removed:
                write_json(self.path, state)
            return removed
