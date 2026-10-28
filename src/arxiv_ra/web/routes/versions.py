"""versions page adapters."""
from __future__ import annotations

from ...config import AppConfig
from ...profiles.profiles import ProfileManager
from ...runtime.jobs.manager import JobManager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from typing import Any, Callable
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from fastapi import Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from ...config import VersionSyncConfig
from ...papers.paper_data import requested_version
from ...versions.version_tracker import VersionTracker
from ...versions.version_scope import (
    SCOPES,
    STATUS_LABELS as MATERIAL_LABELS,
    describe,
    in_scope,
    validate_scope,
)
from ...versions.version_sync import PaperVersionSync, STEP_LABELS
from ...versions.version_batch import VersionSyncBatch, update_candidates, BATCH_LABELS, MAX_BATCH_SIZE
from ...versions.version_auto import AutomaticVersionSync
from ..web_catalog import artifact_url, version_tracking_data
from ...runtime.jobs.manager import JobContext
from ...runtime.jobs.job_requests import capture_request
from ...runtime.model_access.settings import save_config_settings

from ..validation import ARXIV_ID_RE


def register_routes(
    app: FastAPI,
    config_path: Path,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    jobs: JobManager,
    output_root: Path,
    profiles: ProfileManager,
    project_root: Path,
    templates: Jinja2Templates,
) -> None:
    @app.get("/versions", response_class=HTMLResponse)
    def versions_page(request: Request, saved: str = "", scope: str = "", view: str = "outdated") -> HTMLResponse:
        current = current_config()
        tracking = version_tracking_data(output_root, current.profile_id)
        scope = scope or request.cookies.get("version_scope_" + (current.profile_id or "default"), "all")
        try:
            validate_scope(scope)
            if view not in {"outdated", "unknown", "all"}:
                raise ValueError("无效的状态筛选")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        counts = {key: sum(in_scope(item, key) for item in tracking["items"]) for key in SCOPES}
        tracking["items"] = [describe(item, scope) for item in tracking["items"] if in_scope(item, scope)]
        visible_ids = {item["arxiv_id"] for item in tracking["items"]}
        tracking["events"] = [event for event in tracking["events"] if event["arxiv_id"] in visible_ids]
        coverage = tracking["coverage"]
        if "ids" in coverage:
            checked = set(coverage.get("checked_ids", [])) & visible_ids
            tracking["coverage"] = {"total": len(visible_ids), "checked": len(checked), "pending": len(visible_ids - checked)}
        else:
            tracking["coverage"] = {}
        rows = [item for item in tracking["items"] if view == "all" or item[view]]
        batches = VersionSyncBatch(current, project_root)
        batch_history = []
        for batch in batches.recent():
            batch["summary"] = batches.summary(batch)
            batch["result_url"] = artifact_url(batches._path(batch["id"]).with_name("index.html"), output_root)
            batch["created_label"] = datetime.fromisoformat(batch["created_at"]).astimezone(
                ZoneInfo(current.timezone)).strftime("%Y-%m-%d %H:%M")
            batch_history.append(batch)
        response = templates.TemplateResponse(
            request,
            "versions.html",
            context(request, "versions", tracking=tracking, updates=update_candidates(tracking["items"], scope),
                    rows=rows, scope=scope, scopes=SCOPES, source_counts=counts, view=view,
                    unknown_count=sum(item["unknown"] for item in tracking["items"]), material_labels=MATERIAL_LABELS,
                    batches=batch_history, batch_labels=BATCH_LABELS, step_labels=STEP_LABELS,
                    max_batch_size=MAX_BATCH_SIZE, policy_saved=saved == "policy",
                    automatic=AutomaticVersionSync(current, project_root).latest()),
        )

        response.set_cookie("version_scope_" + (current.profile_id or "default"), scope,
                            max_age=31536000, httponly=True, samesite="lax")
        return response

    @app.post("/versions/policy")
    def save_version_policy(profile_id: str = Form(""), enabled: bool = Form(False),
                            max_papers: int = Form(10), max_model_papers: int = Form(2),
                            max_model_calls: int = Form(20),
                            report: bool = Form(False), zotero: bool = Form(False),
                            obsidian: bool = Form(False)) -> RedirectResponse:
        current = current_config()
        if profile_id != current.profile_id:
            raise HTTPException(status_code=409, detail="研究方向已切换，请刷新追踪页后重新保存策略")
        policy = VersionSyncConfig(enabled=enabled, max_papers=max_papers, max_model_papers=max_model_papers,
                                   max_model_calls=max_model_calls, report=report, zotero=zotero, obsidian=obsidian)
        try:
            policy.validate()
            if enabled:
                if not current.version_tracking.enabled:
                    raise ValueError("请先在配置页启用版本追踪")
                if zotero and (not current.zotero.enabled or not current.zotero.attach_pdf):
                    raise ValueError("请先启用 Zotero 和 PDF 附件同步")
                if obsidian and (not current.obsidian.enabled or not current.obsidian.vault_path):
                    raise ValueError("请先配置并启用 Obsidian")
                if obsidian and report and not current.obsidian.sync_reports:
                    raise ValueError("请先启用 Obsidian 报告同步")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if current.profile_id:
            payload = profiles.get(current.profile_id)
            payload["version_sync"] = asdict(policy)
            profiles.save(payload)
        else:
            save_config_settings(config_path, {"version_sync": asdict(policy)})
        return RedirectResponse("/versions?saved=policy", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/api/jobs/versions", response_class=JSONResponse)
    def create_versions_job(scope: str = Form("all"), profile_id: str = Form("")) -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        current = task.config
        if profile_id and profile_id != current.profile_id:
            raise HTTPException(status_code=409, detail="研究方向已切换，请刷新页面")
        try:
            validate_scope(scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        def run_versions() -> Path:
            with VersionTracker(current, project_root) as tracker:
                return tracker.check(scope=scope, auto_sync=False)

        job = jobs.submit("versions", current.profile_name, run_versions,
                          identity=task.identity("versions", scope=scope, auto_sync=False), profile_id=current.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/version-batches/preview", response_class=JSONResponse)
    def preview_version_batch(arxiv_ids: list[str] = Form(...), report: bool = Form(False),
                              zotero: bool = Form(False), obsidian: bool = Form(False),
                              scope: str = Form("all"), supplement: bool = Form(False),
                              profile_id: str = Form("")) -> JSONResponse:
        current = current_config()
        if profile_id and profile_id != current.profile_id:
            raise HTTPException(status_code=409, detail="研究方向已切换，请刷新页面")
        service = VersionSyncBatch(current, project_root)
        try:
            state = service.preview(arxiv_ids, version_tracking_data(service.output_root, current.profile_id)["items"],
                                    scope=scope, supplement=supplement)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"id": state["id"], "profile_name": state["profile_name"], "items": state["items"],
                             "options": state["options"], "summary": service.summary(state),
                             "model": current.llm.model if state["options"]["report"] or state["options"]["obsidian"] else "",
                             "zotero_collection": "已有条目（保留所属分类）" if state["options"]["zotero"] else "",
                             "obsidian_vault": current.obsidian.vault_path if obsidian else ""})

    @app.post("/api/jobs/version-batch/{batch_id}", response_class=JSONResponse)
    def create_version_batch_job(batch_id: str) -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        service = VersionSyncBatch(task.config, project_root)
        try:
            batch = service.read(batch_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        job = jobs.submit_request(f"{batch['profile_name']} · {len(batch['items'])} 篇",
                          capture_request("version-batch", task.config, project_root, batch_id=batch_id),
                          identity=f"version-batch:{service.root}:{batch_id}")
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/version-sync", response_class=JSONResponse)
    def create_version_sync_job(arxiv_id: str = Form(...), target_version: int | None = Form(None),
                                retry: bool = Form(False), report: bool = Form(False),
                                zotero: bool = Form(False), obsidian: bool = Form(False)) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.fullmatch(arxiv_id) or requested_version(arxiv_id) is not None:
            raise HTTPException(status_code=400, detail="请输入不带修订版后缀的 arXiv ID")
        if (target_version is not None and target_version < 1) or (retry and target_version is None):
            raise HTTPException(status_code=400, detail="重试需要有效的目标修订版")
        task = JobContext.capture(current_config(), project_root)
        options = dict(target_version=target_version, retry=retry, report=report, zotero=zotero, obsidian=obsidian)

        def run_sync() -> Path:
            with PaperVersionSync(task.config, project_root) as synchronizer:
                return synchronizer.sync(arxiv_id, **options)

        job = jobs.submit("version-sync", f"{task.config.profile_name} · {arxiv_id}", run_sync,
                          identity=task.identity("version-sync", arxiv_id=arxiv_id, **options),
                          profile_id=task.config.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)
