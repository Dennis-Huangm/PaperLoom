"""Private durable GUI task records with one live queue owner per output root."""
from __future__ import annotations

import os
from pathlib import Path

from .utils import read_json, write_json


class JobStore:
    def __init__(self, output_root: Path, *, guard_restore: bool = True):
        self.restore_guard = None
        project = output_root.resolve().parent
        if guard_restore and project.parent.name == "restored" and (project / ".paperloom-restore.json").is_file():
            # Outside the restored directory so it remains locked across directory
            # publication, including on Windows where open files prevent renames.
            self.restore_guard = JobStore(project.parent / ".owners" / project.name, guard_restore=False)
        self.root = output_root.resolve() / ".jobs"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.handle = (self.root / "owner.lock").open("a+b")
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if hasattr(self, "handle"):
                self.handle.close()
            if self.restore_guard:
                self.restore_guard.close()
            raise RuntimeError("此输出目录已有 GUI 任务队列，请先关闭原 GUI") from exc

    def records(self):
        for path in sorted(self.root.glob("*.json")):
            try:
                value = read_json(path)
                if not isinstance(value, dict) or value.get("version") != 1 or value.get("job", {}).get("id") != path.stem:
                    raise ValueError("任务记录格式或版本不受支持")
                yield path, value, None
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                yield path, None, exc

    def save(self, value):
        write_json(self.root / f"{value['job']['id']}.json", value)

    def close(self):
        if not self.handle.closed:
            # Closing the file releases its OS lock, including after process loss.
            self.handle.close()
        if self.restore_guard:
            self.restore_guard.close()
