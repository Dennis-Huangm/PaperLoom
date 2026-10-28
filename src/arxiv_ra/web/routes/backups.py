"""backups page adapters."""
from __future__ import annotations

from ...runtime.jobs.manager import JobManager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from typing import Any, Callable
import re
from pathlib import Path
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from ...papers.backup import BackupService


def register_routes(
    app: FastAPI,
    config_path: Path,
    context: Callable[..., dict[str, Any]],
    jobs: JobManager,
    templates: Jinja2Templates,
) -> None:
    def backup_service():
        return BackupService(config_path)

    def backup_context(request, **values):
        service = backup_service()
        return context(request, "settings", backups=service.list(), backup_folder=str(service.backups),
                       restore_folder=str(service.restores), pending_restores=service.pending(), **values)

    @app.get("/backups", response_class=HTMLResponse)
    def backups_page(request: Request):
        return templates.TemplateResponse(request, "backups.html", backup_context(request))

    @app.post("/backups/create", response_class=HTMLResponse)
    def create_backup(request: Request, profile_id: str = Form(...), reports: bool = Form(False),
                      pdfs: bool = Form(False), secrets: bool = Form(False)):
        if jobs.active_count or jobs.pending:
            raise HTTPException(409, "请等待后台任务完成后再备份，以避免快照在生成期间变化")
        try:
            path = backup_service().create(reports=reports, pdfs=pdfs, secrets=secrets)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return templates.TemplateResponse(request, "backups.html", backup_context(request,
            message=f"备份已完成并通过完整性校验：{path.name}"))

    @app.get("/backups/download/{name}")
    def download_backup(name: str):
        if not re.fullmatch(r"paperloom-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\.zip", name):
            raise HTTPException(404, "没有找到备份")
        path = backup_service().backups / name
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "没有找到备份")
        return FileResponse(path, filename=name, media_type="application/zip", headers={"Cache-Control": "no-store"})

    @app.post("/backups/preview", response_class=HTMLResponse)
    def preview_backup(request: Request, profile_id: str = Form(...), archive_path: str = Form(...),
                       name: str = Form(...), mode: str = Form("keep")):
        try:
            plan = backup_service().preview(Path(archive_path), name, mode)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return templates.TemplateResponse(request, "backups.html", backup_context(request, plan=plan))

    @app.post("/backups/restore", response_class=HTMLResponse)
    def restore_backup(request: Request, profile_id: str = Form(...), archive_path: str = Form(...),
                       name: str = Form(...), token: str = Form(...), mode: str = Form("keep")):
        try:
            result = backup_service().restore(Path(archive_path), name, token=token, mode=mode)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return templates.TemplateResponse(request, "backups.html", backup_context(request, restored=result))

    @app.post("/backups/reconcile", response_class=HTMLResponse)
    def reconcile_restore(request: Request, profile_id: str = Form(...)):
        try:
            count = backup_service().recover_interrupted()
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return templates.TemplateResponse(request, "backups.html", backup_context(request,
            message=f"已整理 {count} 条中断恢复记录。请重新预览后再执行恢复。"))
