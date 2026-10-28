"""jobs page adapters."""
from __future__ import annotations

from ...config import AppConfig
from ...runtime.jobs.manager import JobManager
from fastapi import FastAPI
from typing import Any, Callable
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from fastapi import Form, HTTPException, status
from fastapi.responses import JSONResponse
from ...papers.models import Paper
from ...integrations.obsidian import ObsidianExporter
from ..web_catalog import DATE_DIR_RE
from ...runtime.jobs.manager import JobContext
from ...runtime.jobs.job_requests import capture_request

from ..validation import ARXIV_ID_RE


def register_routes(
    app: FastAPI,
    current_config: Callable[[], AppConfig],
    jobs: JobManager,
    project_root: Path,
    selected_paper: Callable[..., dict[str, Any]],
) -> None:
    @app.post("/api/jobs/report", response_class=JSONResponse)
    def create_report_job(arxiv_id: str = Form(...), origin: str = Form(""),
                          source_date: str = Form(""), report_id: str = Form("")) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID，例如 2407.05600")

        task = JobContext.capture(current_config(), project_root)
        current = task.config
        if origin not in {"", "recommendation", "library", "report"}:
            raise HTTPException(status_code=400, detail="无效的论文来源")
        snapshot = None
        if origin:
            if origin == "recommendation" and not DATE_DIR_RE.fullmatch(source_date):
                raise HTTPException(status_code=400, detail="历史推荐需要有效的推荐日期")
            item = selected_paper(arxiv_id, current, origin=origin,
                                  source_date=source_date, report_id=report_id)
            if not item:
                raise HTTPException(status_code=404, detail="没有找到对应的论文快照")
            snapshot = Paper.from_dict(item["paper"])
            if not snapshot.version:
                raise HTTPException(status_code=409, detail="这条历史记录没有可靠版本号；请在生成页面输入 ID 获取最新版本，或输入指定版本号")

        job = jobs.submit_request(f"{current.profile_name} · arXiv:{arxiv_id}",
                          capture_request("report", current, project_root, arxiv_id=arxiv_id,
                                          snapshot=snapshot.to_dict() if snapshot else None),
                          identity=task.identity("report", arxiv_id=arxiv_id, origin=origin,
                                                 source_date=source_date, report_id=report_id,
                                                 snapshot=snapshot.to_dict() if snapshot else None))
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/digest", response_class=JSONResponse)
    def create_digest_job(
        force: bool = Form(False),
        send_email: bool = Form(False),
    ) -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        current = task.config
        detail = f"{current.profile_id or 'default'} · {'刷新并发送邮件' if send_email else '仅刷新本地推荐'} · {'强制' if force else '仅新论文'}"
        job = jobs.submit_request(detail,
                          capture_request("digest", current, project_root, force=force, send_email=send_email),
                          identity=task.identity("digest", force=force, send_email=send_email))
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/weekly", response_class=JSONResponse)
    def create_weekly_job(include_notes: bool = Form(False)) -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        current = task.config
        cutoff = datetime.now(ZoneInfo(current.timezone))
        job = jobs.submit_request(current.profile_name,
                          capture_request("weekly", current, project_root, now=cutoff.isoformat(), include_notes=include_notes),
                          identity=task.identity("weekly", include_notes=include_notes))
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/obsidian", response_class=JSONResponse)
    def create_obsidian_job() -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        current = task.config
        def run_obsidian() -> Path:
            with ObsidianExporter(current, project_root) as exporter:
                return exporter.sync_all()

        job = jobs.submit(
            "obsidian", current.profile_name, run_obsidian,
            identity=task.identity("obsidian"), profile_id=current.profile_id
        )
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.delete("/api/jobs/completed", response_class=JSONResponse)
    def clear_completed_jobs() -> JSONResponse:
        return JSONResponse({"deleted": jobs.clear_finished()})

    @app.get("/api/jobs/{job_id}", response_class=JSONResponse)
    def job_status(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或记录无法读取")
        return JSONResponse(asdict(job))

    @app.delete("/api/jobs/{job_id}", response_class=JSONResponse)
    def delete_finished_job(job_id: str) -> JSONResponse:
        try:
            jobs.delete_finished(job_id, current_config().profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="当前研究方向没有这条任务记录") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"deleted": job_id})

    @app.post("/api/jobs/{job_id}/retry", response_class=JSONResponse)
    def retry_job(job_id: str) -> JSONResponse:
        try:
            job = jobs.retry(job_id, current_config().profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/{job_id}/cancel", response_class=JSONResponse)
    def cancel_job(job_id: str) -> JSONResponse:
        job = jobs.cancel(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或记录无法读取")
        if job.status not in {"cancelled", "cancelling"}:
            raise HTTPException(status_code=409, detail="任务已经结束，无法取消")
        return JSONResponse(asdict(job))
