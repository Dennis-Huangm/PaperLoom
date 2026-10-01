from __future__ import annotations

from contextlib import contextmanager

import threading
import uuid
import copy
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .web_catalog import result_artifact_url
from .config import AppConfig
from .task_runtime import TaskCancelled, TaskHooks, bind_task_hooks
from .job_store import JobStore
from .job_requests import RECOVERABLE, recovery_note, recovery_request, recovery_runner, request_runner, restore_config
from .paper_data import base_id


@dataclass(frozen=True)
class JobContext:
    """Submission-time research intent, independent of the active GUI profile."""

    config: AppConfig = field(repr=False)
    project_root: Path

    @classmethod
    def capture(cls, config: AppConfig, project_root: Path) -> "JobContext":
        return cls(copy.deepcopy(config), project_root.resolve())

    def identity(self, kind: str, **parameters: object) -> str:
        payload = {"kind": kind, "parameters": parameters, "config": asdict(self.config),
                   "project_root": str(self.project_root)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


JOB_LABELS = {
    "profile-preview": "研究方向试搜",
    "search-index": "更新本地搜索索引",
    "report": "生成完整阅读报告",
    "digest": "刷新每日推荐",
    "weekly": "生成研究周报",
    "compare": "生成跨论文比较",
    "versions": "检查 arXiv 版本更新",
    "version-sync": "同步论文修订版",
    "version-batch": "批量同步论文修订版",
    "citation": "生成相关工作地图",
    "obsidian": "同步 Obsidian 知识库",
}

FINISHED_STATUSES = {"succeeded", "succeeded_with_warnings", "failed", "cancelled", "interrupted"}


@dataclass(slots=True)
class BackgroundJob:
    id: str
    kind: str
    label: str
    status: str
    created_at: str
    updated_at: str
    detail: str = ""
    progress: int | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)
    result_url: str | None = None
    profile_id: str = ""
    recoverable: bool = False
    recovery_note: str = ""
    retry_of: str = ""
    retry_job_id: str = ""
    submitted_detail: str = ""
    report_progress: dict = field(default_factory=dict)


class JobManager:
    """Bounded GUI queue with durable records and explicit recovery requests.

    The executor always owns eight worker threads while ``max_parallel`` controls
    how many jobs may be dispatched. This lets the user change concurrency at
    runtime without replacing an executor that may still have active work.
    """

    def __init__(self, output_root: Path, max_parallel: int = 3) -> None:
        self.output_root = output_root.resolve()
        self.store = JobStore(self.output_root)
        self.executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="arxiv-ra-gui"
        )
        self.jobs: dict[str, BackgroundJob] = {}
        self.pending: list[tuple[str, Callable[[], Path | None]]] = []
        self.active_count = 0
        self.max_parallel = self._bounded_parallelism(max_parallel)
        self.lock = threading.RLock()
        self.identities: dict[str, str] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.closed = False
        self.requests: dict[str, dict | None] = {}
        self.checkpoints: dict[str, dict] = {}
        self.load_errors: list[str] = []
        try:
            self._restore()
        except Exception:
            self.store.close()
            self.executor.shutdown(wait=False)
            raise

    def _record(self, job):
        return {"version": 1, "job": asdict(job), "identity": self.identities[job.id],
                "request": self.requests.get(job.id), "checkpoint": self.checkpoints.get(job.id, {})}

    def _save(self, job):
        self.store.save(self._record(job))

    @staticmethod
    def _pdf_failure(job: BackgroundJob) -> str | None:
        if job.kind == "report":
            return next((warning["message"] for warning in job.warnings
                         if warning.get("component") == "PDF 下载与解析"), None)
        return None

    def _restore(self):
        loaded = []
        for path, value, error in self.store.records():
            try:
                if error:
                    raise ValueError(str(error))
                job = BackgroundJob(**value["job"])
                if (job.kind not in JOB_LABELS or not re.fullmatch(r"[0-9a-f]{12}", job.id)
                        or job.status not in {"queued", "running", "cancelling", "cancelled", "failed", "interrupted", "succeeded", "succeeded_with_warnings"}
                        or not isinstance(value.get("identity"), str)
                        or not isinstance(value.get("checkpoint", {}), dict)
                        or not isinstance(job.report_progress, dict)):
                    raise ValueError("任务类型或标识无效")
                request = value.get("request")
                if request:
                    cfg = restore_config(request, self.output_root)
                    if cfg.profile_id != job.profile_id or request["kind"] != job.kind:
                        raise ValueError("任务配置与记录不匹配")
                loaded.append((job, value))
            except (TypeError, ValueError, KeyError, AttributeError):
                self.load_errors.append(f"任务记录 {path.name} 无法读取，原文件已保留。")
        for job, value in sorted(loaded, key=lambda item: (item[0].created_at, item[0].id)):
            self.jobs[job.id] = job
            self.identities[job.id] = value["identity"]
            self.requests[job.id] = value.get("request")
            self.checkpoints[job.id] = value.get("checkpoint") or {}
            self.cancel_events[job.id] = threading.Event()
            job.recoverable = bool(self.requests[job.id]) and job.kind in RECOVERABLE
            job.recovery_note = recovery_note(job.kind)
            pdf_failure = self._pdf_failure(job)
            if job.status == "succeeded_with_warnings" and pdf_failure:
                job.status = "failed"
                job.detail = f"任务失败：{pdf_failure}"
                job.progress = None
                self._save(job)
            if job.status in {"queued", "running", "cancelling"}:
                previous = job.detail
                job.status = "interrupted"
                job.detail = "应用退出，任务已中断；上次进度：" + previous
                job.progress = None
                job.updated_at = datetime.now().isoformat(timespec="seconds")
                self._save(job)
        # A persisted recovery replaces its finished parent, including when
        # the application exited between creating the child and deleting it.
        for job in list(self.jobs.values()):
            parent = self.jobs.get(job.retry_of)
            if parent and parent.status in FINISHED_STATUSES and parent.profile_id == job.profile_id:
                self._forget(parent.id)

    def _checkpoint_data(self, job_id, key, value):
        with self.lock:
            # A nested daily/version workflow must not replace a report request.
            if self.jobs[job_id].kind == "report" and key in {"paper", "report_work", "report_progress"}:
                self.checkpoints[job_id][key] = copy.deepcopy(value)
                if key == "report_progress":
                    self.jobs[job_id].report_progress = copy.deepcopy(value)
                self._save(self.jobs[job_id])

    @staticmethod
    def _bounded_parallelism(value: int) -> int:
        return max(1, min(8, int(value)))

    def submit(
        self, kind: str, detail: str, function: Callable[[], Path | None],
        *, identity: str | None = None, profile_id: str = "",
        request: dict | None = None, retry_of: str = "", checkpoint: dict | None = None,
    ) -> BackgroundJob:
        if kind not in JOB_LABELS:
            raise ValueError(f"未知任务类型：{kind}")
        now = datetime.now().isoformat(timespec="microseconds")
        identity = identity or json.dumps([kind, detail])
        with self.lock:
            if self.closed:
                raise RuntimeError("任务队列已关闭")
            if request:
                config = restore_config(request, self.output_root)
                if config.profile_id != profile_id or request["kind"] != kind:
                    raise ValueError("任务配置与记录不匹配")
            duplicate = next(
                (
                    job
                    for job in self.jobs.values()
                    if self.identities[job.id] == identity
                    and job.status in {"queued", "running", "cancelling"}
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
                detail=f"排队中 · {detail}",
                profile_id=profile_id,
                recoverable=bool(request) and kind in RECOVERABLE,
                recovery_note=recovery_note(kind), retry_of=retry_of,
                submitted_detail=detail,
                report_progress=copy.deepcopy((checkpoint or {}).get("report_progress", {})),
            )
            # Persist before dispatch. A failed write must not start side effects.
            self.store.save({"version": 1, "job": asdict(job), "identity": identity,
                             "request": copy.deepcopy(request), "checkpoint": copy.deepcopy(checkpoint or {})})
            self.jobs[job.id] = job
            self.identities[job.id] = identity
            self.requests[job.id] = copy.deepcopy(request)
            self.checkpoints[job.id] = copy.deepcopy(checkpoint or {})
            self.cancel_events[job.id] = threading.Event()
            self.pending.append((job.id, function))
            self._dispatch_locked()
        return job

    def submit_request(self, detail: str, request: dict, *, identity: str) -> BackgroundJob:
        """Submit captured intent as both the durable record and execution source."""
        request = copy.deepcopy(request)
        config = restore_config(request, self.output_root)
        return self.submit(request["kind"], detail, request_runner(request, self.output_root),
                           identity=identity, profile_id=config.profile_id, request=request)

    def set_max_parallel(self, value: int) -> None:
        with self.lock:
            self.max_parallel = self._bounded_parallelism(value)
            self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        while not self.closed and self.pending and self.active_count < self.max_parallel:
            job_id, function = self.pending.pop(0)
            self.active_count += 1
            self.executor.submit(self._execute, job_id, function)

    def _execute(self, job_id: str, function: Callable[[], Path | None]) -> None:
        cancel_event = self.cancel_events[job_id]
        committed = False

        @contextmanager
        def commit():
            nonlocal committed
            with self.lock:
                if cancel_event.is_set():
                    raise TaskCancelled()
                yield
                committed = True

        try:
            if cancel_event.is_set():
                self._update(
                    job_id, status="cancelled", detail="任务已取消", progress=None
                )
                return
            self._update(job_id, status="running", detail="正在启动任务…", progress=0)
            hooks = TaskHooks(
                progress=lambda detail, percent: self._progress(job_id, detail, percent),
                warning=lambda component, message: self._warning(
                    job_id, component, message
                ),
                is_cancelled=cancel_event.is_set,
                checkpoint_data=lambda key, value: self._checkpoint_data(job_id, key, value),
                commit=commit,
            )
            with bind_task_hooks(hooks):
                result = function()
            with self.lock:
                if cancel_event.is_set() and not committed:
                    raise TaskCancelled()
                pdf_failure = self._pdf_failure(self.jobs[job_id])
                self._update(
                    job_id,
                    status=(
                        "failed" if pdf_failure else "succeeded_with_warnings"
                        if self.jobs[job_id].warnings
                        else "succeeded"
                    ),
                    detail=(
                        f"任务失败：{pdf_failure}" if pdf_failure else
                        f"任务已完成，但有 {len(self.jobs[job_id].warnings)} 个组件异常"
                        if self.jobs[job_id].warnings
                        else "任务已完成"
                    ),
                    progress=None if pdf_failure else 100,
                    result_url=result_artifact_url(result, self.output_root) if result is not None else None,
                )
        except TaskCancelled:
            self._update(
                job_id, status="cancelled", detail="任务已取消", progress=None,
                result_url=None,
            )
        except Exception as exc:
            if cancel_event.is_set():
                self._update(
                    job_id, status="cancelled", detail="任务已取消", progress=None,
                    result_url=None,
                )
            else:
                self._update(
                    job_id, status="failed", detail=f"{type(exc).__name__}: {exc}",
                    progress=None,
                )
        finally:
            with self.lock:
                self.active_count = max(0, self.active_count - 1)
                self._dispatch_locked()
                if self.closed and self.active_count == 0:
                    self.store.close()

    def _progress(self, job_id: str, detail: str, percent: int | None) -> None:
        with self.lock:
            job = self.jobs[job_id]
            if job.status != "running":
                return
            job.detail = detail
            if percent is None:
                job.progress = None
            else:
                bounded = max(0, min(100, int(percent)))
                job.progress = max(job.progress or 0, bounded)
            job.updated_at = datetime.now().isoformat(timespec="seconds")
            self._save(job)

    def _warning(self, job_id: str, component: str, message: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            if job.status != "running":
                return
            warning = {
                "component": " ".join(str(component).split())[:80] or "未知组件",
                "message": " ".join(str(message).split())[:600] or "组件不可用",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            if not any(
                item["component"] == warning["component"]
                and item["message"] == warning["message"]
                for item in job.warnings
            ):
                job.warnings.append(warning)
                job.updated_at = warning["created_at"]
                self._save(job)

    def _update(self, job_id: str, **values: object) -> None:
        with self.lock:
            job = self.jobs[job_id]
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = datetime.now().isoformat(timespec="seconds")
            self._save(job)

    def get(self, job_id: str) -> BackgroundJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def clear_finished(self) -> int:
        """Forget finished queue entries while preserving active work and result files."""
        with self.lock:
            deleted = 0
            for job_id, job in list(self.jobs.items()):
                if job.status not in FINISHED_STATUSES:
                    continue
                self._forget(job_id)
                deleted += 1
            return deleted

    def _forget(self, job_id: str) -> None:
        self.store.delete(job_id)
        del self.jobs[job_id]
        self.identities.pop(job_id, None)
        self.requests.pop(job_id, None)
        self.checkpoints.pop(job_id, None)
        self.cancel_events.pop(job_id, None)

    def cancel(self, job_id: str) -> BackgroundJob | None:
        """Cancel queued work immediately or request cancellation at a safe boundary."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job.status == "queued":
                self.pending = [item for item in self.pending if item[0] != job_id]
                self.cancel_events[job_id].set()
                self._update(
                    job_id, status="cancelled", detail="任务已取消", progress=None,
                    result_url=None,
                )
            elif job.status == "running":
                self.cancel_events[job_id].set()
                self._update(
                    job_id, status="cancelling",
                    detail="正在取消；当前步骤结束后将停止…", progress=None,
                )
            return job

    def find_identity(self, identity: str) -> BackgroundJob | None:
        """Find any attempt, including completed/interrupted ones, for dispatch repair."""
        with self.lock:
            return next((copy.deepcopy(job) for job_id, job in self.jobs.items()
                         if self.identities.get(job_id) == identity), None)

    def active_for(self, kind: str, profile_id: str) -> bool:
        with self.lock:
            return any(job.kind == kind and job.profile_id == profile_id
                       and job.status in {"queued", "running", "cancelling"} for job in self.jobs.values())

    def active_report_for(self, arxiv_id: str) -> bool:
        with self.lock:
            return any(
                job.kind == "report" and job.status in {"queued", "running", "cancelling"}
                and base_id(str(((self.requests.get(job.id) or {}).get("parameters") or {}).get("arxiv_id") or "")) == arxiv_id
                for job in self.jobs.values()
            )

    def forget_result_urls(self, urls: set[str]) -> None:
        """Remove task links to artifacts that were explicitly deleted."""
        with self.lock:
            for job in self.jobs.values():
                if job.result_url in urls:
                    job.result_url = None
                    self._save(job)

    def recent(self, limit: int = 8) -> list[BackgroundJob]:
        with self.lock:
            ordered = list(reversed(list(self.jobs.values())))
            return [job for index, job in enumerate(ordered) if index < limit
                    or job.status in {"queued", "running", "cancelling", "interrupted"} and not job.retry_job_id]

    def history(self, page: int, size: int = 25) -> dict:
        with self.lock:
            ordered = list(reversed(list(self.jobs.values())))
            pages = max(1, (len(ordered) + size - 1) // size)
            page = max(1, min(page, pages))
            selected = ordered[(page - 1) * size:page * size]
            if page == 1:
                shown = {job.id for job in selected}
                selected.extend(job for job in ordered if job.id not in shown and not job.retry_job_id
                                and (job.status in {"queued", "running", "cancelling", "interrupted"}
                                     or job.recoverable and (job.status in {"failed", "cancelled"}
                                         or job.status == "succeeded_with_warnings" and job.report_progress.get("resumable"))))
            return {"items": [asdict(job) for job in selected], "page": page, "pages": pages}

    def retry(self, job_id: str, profile_id: str) -> BackgroundJob:
        with self.lock:
            previous = self.jobs.get(job_id)
            # Keep repeated recovery requests idempotent after removing the parent.
            child = next((job for job in self.jobs.values() if job.retry_of == job_id), None)
            if child:
                if child.profile_id != profile_id:
                    raise ValueError("请切换到该任务原来的研究方向后再恢复")
                if previous and previous.status in FINISHED_STATUSES:
                    self._forget(job_id)
                return child
            if previous is None:
                raise KeyError(job_id)
            if previous.profile_id != profile_id:
                raise ValueError("请切换到该任务原来的研究方向后再恢复")
            # Retried attempts form a chain; repeated POSTs never repeat a success.
            child = self.jobs.get(previous.retry_job_id) or next((job for job in self.jobs.values() if job.retry_of == job_id), None)
            if child:
                return child
            fallback_report = (previous.kind == "report" and previous.status == "succeeded_with_warnings"
                               and previous.report_progress.get("resumable")
                               and self.checkpoints[job_id].get("report_work"))
            if not previous.recoverable or (previous.status not in {"interrupted", "failed", "cancelled"} and not fallback_report):
                raise ValueError("该任务当前不可重新执行")
            request = recovery_request(self.requests[job_id])
            checkpoint = copy.deepcopy(self.checkpoints[job_id])
            runner = recovery_runner(request, self.output_root, checkpoint)
            job = self.submit(previous.kind, previous.submitted_detail or previous.label, runner,
                              # Resolved checkpoints/cutoffs may differ from a new
                              # active request with the original submission hash.
                              identity=f"recovery:{job_id}", profile_id=profile_id, request=request,
                              retry_of=job_id, checkpoint=checkpoint)
            self._forget(job_id)
            return job

    def close(self) -> None:
        """Release executor threads when the FastAPI application shuts down."""
        with self.lock:
            self.closed = True
            for job_id, _ in self.pending:
                self._update(job_id, status="interrupted" if self.requests[job_id] else "failed",
                             detail="应用关闭，任务尚未开始；可在重启后重新执行")
            self.pending.clear()
            if self.active_count == 0:
                self.store.close()
        self.executor.shutdown(wait=False, cancel_futures=False)
