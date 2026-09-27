from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any

from .utils import read_json, write_json


_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()

READING_STATUSES = {"unread": "待读", "reading": "阅读中", "read": "已读", "paused": "暂缓"}


class ReadingConflict(ValueError):
    """An older editor must not replace a newer personal note."""


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

    @staticmethod
    def _activity(state: dict, kind: str, paper: dict, at: str, **details) -> None:
        state.setdefault("activity_started_at", at)
        state.setdefault("activity", []).append({"id": uuid.uuid4().hex, "kind": kind,
            "at": at, "paper": paper, **details})

    def update_reading(self, arxiv_id: str, *, status: str, notes: str,
                       tags: list[str], read_version: int | None,
                       expected_updated_at: str) -> dict:
        if status not in READING_STATUSES:
            raise ValueError("无效的阅读进度")
        tags = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
        if len(notes) > 20000 or len(tags) > 20 or any(len(tag) > 40 for tag in tags):
            raise ValueError("笔记最多 20000 字，标签最多 20 个且每个不超过 40 字")
        if read_version is not None and read_version < 1:
            raise ValueError("阅读修订版必须大于零")
        with _locked(self.lock_path):
            state = self._read()
            saved = state["library"].get(arxiv_id)
            if not saved:
                raise ReadingConflict("论文已移出文献库，请刷新后重试；已有笔记仍保留")
            records = state.setdefault("reading", {})
            if not isinstance(records, dict):
                raise ValueError("阅读笔记格式错误")
            previous = records.get(arxiv_id, {})
            if previous.get("updated_at", "") != expected_updated_at:
                raise ReadingConflict("阅读记录已在其他页面修改，请先复制当前输入，再刷新查看最新记录")
            version = (saved.get("paper") or {}).get("version")
            if version and read_version and read_version > version:
                raise ValueError("阅读修订版不能高于当前收藏版本")
            if status == "read" and read_version is None:
                read_version = version
            now = datetime.now(timezone.utc).isoformat()
            history = list(previous.get("history") or [])
            changed = status != previous.get("status") or read_version != previous.get("read_version")
            if changed:
                history.append({"status": status, "read_version": read_version, "at": now})
            record = {"status": status, "notes": notes, "tags": tags,
                      "read_version": read_version, "updated_at": now,
                      "completed_at": (now if changed else previous.get("completed_at", now)) if status == "read" else "",
                      "history": history}
            records[arxiv_id] = record
            if changed:
                self._activity(state, "reading", saved["paper"], now, status=status, read_version=read_version)
            if notes != previous.get("notes", "") or tags != previous.get("tags", []):
                self._activity(state, "notes", saved["paper"], now, notes=notes, tags=tags,
                               read_version=read_version)
            write_json(self.path, state)
            return record

    def save(self, entry: dict[str, Any]) -> dict[str, Any]:
        with _locked(self.lock_path):
            state = self._read()
            entry = self._save_entry(state, entry)
            write_json(self.path, state)
            return entry

    @staticmethod
    def _save_entry(state: dict, entry: dict) -> dict:
        aid = entry["arxiv_id"]
        previous = state["library"].get(aid, {})
        old_version = int((previous.get("paper") or {}).get("version") or 0)
        new_version = int((entry.get("paper") or {}).get("version") or 0)
        if old_version > new_version:
            return previous
        now = datetime.now(timezone.utc).isoformat()
        entry = {**entry, "saved_at": state["library"].get(aid, {}).get("saved_at") or now,
                 "updated_at": now}
        state["library"][aid] = entry
        state["feedback"].pop(aid, None)
        if not previous or new_version > old_version:
            ReadingStateStore._activity(state, "saved" if not previous else "library_version", entry.get("paper") or {},
                                        now, from_version=old_version or None)
        return entry

    def toggle(self, entry: dict) -> bool:
        with _locked(self.lock_path):
            state = self._read()
            removed = state["library"].pop(entry["arxiv_id"], None)
            if removed is None:
                self._save_entry(state, entry)
            else:
                self._activity(state, "removed", removed.get("paper") or {}, datetime.now(timezone.utc).isoformat())
            write_json(self.path, state)
            return removed is None

    def dismiss(self, entry: dict[str, Any]) -> dict[str, Any]:
        with _locked(self.lock_path):
            state = self._read()
            previous = state["feedback"].get(entry["arxiv_id"], {})
            entry = {**entry, "previous_library": state["library"].get(entry["arxiv_id"])
                     or previous.get("previous_library")}
            state["feedback"][entry["arxiv_id"]] = entry
            removed = state["library"].pop(entry["arxiv_id"], None)
            if removed:
                self._activity(state, "removed", removed.get("paper") or {}, entry["updated_at"])
            write_json(self.path, state)
            return entry

    def undo_feedback(self, arxiv_id: str, expected_updated_at: str) -> bool:
        with _locked(self.lock_path):
            state = self._read()
            entry = state["feedback"].get(arxiv_id)
            if not entry or entry.get("updated_at", "") != expected_updated_at:
                raise ReadingConflict("反馈已变更，请刷新后重试")
            del state["feedback"][arxiv_id]
            if entry.get("previous_library") and arxiv_id not in state["library"]:
                state["library"][arxiv_id] = {**entry["previous_library"],
                                             "updated_at": datetime.now(timezone.utc).isoformat()}
                self._activity(state, "restored", state["library"][arxiv_id].get("paper") or {},
                               state["library"][arxiv_id]["updated_at"])
            write_json(self.path, state)
            return arxiv_id in state["library"]

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
            removed = state[collection].pop(arxiv_id, None)
            if removed is not None:
                if collection == "library":
                    self._activity(state, "removed", removed.get("paper") or {}, datetime.now(timezone.utc).isoformat())
                write_json(self.path, state)
            return removed is not None
