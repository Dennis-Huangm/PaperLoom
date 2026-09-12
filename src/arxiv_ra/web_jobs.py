from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .web_catalog import result_artifact_url


JOB_LABELS = {
    "report": "生成完整阅读报告",
    "digest": "刷新每日推荐",
    "weekly": "生成研究周报",
    "versions": "检查 arXiv 版本更新",
    "citation": "生成相关工作地图",
    "obsidian": "同步 Obsidian 知识库",
}


@dataclass(slots=True)
class BackgroundJob:
    id: str
    kind: str
    label: str
    status: str
    created_at: str
    updated_at: str
    detail: str = ""
    result_url: str | None = None


class JobManager:
    """Small in-process queue used by the local-only GUI.

    The executor always owns eight worker threads while ``max_parallel`` controls
    how many jobs may be dispatched. This lets the user change concurrency at
    runtime without replacing an executor that may still have active work.
    """

    def __init__(self, output_root: Path, max_parallel: int = 3) -> None:
        self.output_root = output_root
        self.executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="arxiv-ra-gui"
        )
        self.jobs: dict[str, BackgroundJob] = {}
        self.pending: list[tuple[str, Callable[[], Path]]] = []
        self.active_count = 0
        self.max_parallel = self._bounded_parallelism(max_parallel)
        self.lock = threading.RLock()

    @staticmethod
    def _bounded_parallelism(value: int) -> int:
        return max(1, min(8, int(value)))

    def submit(
        self, kind: str, detail: str, function: Callable[[], Path]
    ) -> BackgroundJob:
        if kind not in JOB_LABELS:
            raise ValueError(f"未知任务类型：{kind}")
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            duplicate = next(
                (
                    job
                    for job in self.jobs.values()
                    if job.kind == kind
                    and job.detail == detail
                    and job.status in {"queued", "running"}
                ),
                None,
            )
            if duplicate:
                return duplicate
            job = BackgroundJob(
                id=uuid.uuid4().hex[:12],
                kind=kind,
                label=JOB_LABELS[kind],
                status="queued",
                created_at=now,
                updated_at=now,
                detail=detail,
            )
            self.jobs[job.id] = job
            self.pending.append((job.id, function))
            self._dispatch_locked()
        return job

    def set_max_parallel(self, value: int) -> None:
        with self.lock:
            self.max_parallel = self._bounded_parallelism(value)
            self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        while self.pending and self.active_count < self.max_parallel:
            job_id, function = self.pending.pop(0)
            self.active_count += 1
            self.executor.submit(self._execute, job_id, function)

    def _execute(self, job_id: str, function: Callable[[], Path]) -> None:
        with self.lock:
            original_detail = self.jobs[job_id].detail
        self._update(job_id, status="running", detail=original_detail)
        try:
            result = function()
            self._update(
                job_id,
                status="succeeded",
                detail="任务已完成",
                result_url=result_artifact_url(result, self.output_root),
            )
        except Exception as exc:
            self._update(job_id, status="failed", detail=f"{type(exc).__name__}: {exc}")
        finally:
            with self.lock:
                self.active_count = max(0, self.active_count - 1)
                self._dispatch_locked()

    def _update(self, job_id: str, **values: object) -> None:
        with self.lock:
            job = self.jobs[job_id]
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = datetime.now().isoformat(timespec="seconds")

    def get(self, job_id: str) -> BackgroundJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def recent(self, limit: int = 8) -> list[BackgroundJob]:
        with self.lock:
            return list(reversed(list(self.jobs.values())))[:limit]

    def close(self) -> None:
        """Release executor threads when the FastAPI application shuts down."""
        self.executor.shutdown(wait=False, cancel_futures=False)
