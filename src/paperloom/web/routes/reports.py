"""reports page adapters."""
from __future__ import annotations

from ...config import AppConfig
from ...profiles.profiles import ProfileManager
from ...runtime.jobs.manager import JobManager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Any, Callable
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from ...papers.library import PaperLibraryStore
from ...papers.paper_data import base_id
from ...reports.report_store import delete_report_attempts
from ...papers.activity import collect_activity
from ..web_catalog import artifact_url, report_library, grouped_report_library, weekly_library

from ..validation import ARXIV_ID_RE


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    jobs: JobManager,
    output_root: Path,
    profiles: ProfileManager,
    templates: Jinja2Templates,
) -> None:
    @app.get("/api/reports", response_class=JSONResponse)
    def report_catalog() -> JSONResponse:
        profile_names = {profile["id"]: profile["name"] for profile in profiles.list()}
        items = grouped_report_library(report_library(output_root), profile_names)
        items.sort(key=lambda item: item["report_path"].stat().st_mtime_ns, reverse=True)
        fields = ("title", "authors", "arxiv_id", "version", "directions", "date",
                  "updated_at", "report_url", "report_id")
        return JSONResponse(
            {"reports": [{field: item[field] for field in fields} for item in items]},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, q: str = "", direction: str = "") -> HTMLResponse:
        current = current_config()
        profile_names = {profile["id"]: profile["name"] for profile in profiles.list()}
        all_items = grouped_report_library(report_library(output_root), profile_names)
        directions = sorted(
            {item["id"]: item["name"] for report in all_items for item in report["directions"]}.items(),
            key=lambda item: item[1].casefold(),
        )
        items = all_items
        if direction:
            items = [item for item in items if any(part["id"] == direction for part in item["directions"])]
        saved_papers = PaperLibraryStore(output_root, current.profile_id).all()
        for item in items:
            item["in_library"] = str(item.get("arxiv_id") or "") in saved_papers
        if q.strip():
            needle = q.casefold().strip()
            items = [
                item for item in items
                if needle in f"{item['title']} {item['arxiv_id']} {' '.join(item['authors'])}".casefold()
            ]
        return templates.TemplateResponse(
            request,
            "reports.html",
            context(request, "reports", reports=items, query=q, direction=direction,
                    directions=directions, catalog_ready=True),
        )

    @app.post("/reports/delete")
    def delete_report(arxiv_id: str = Form(""), report_id: str = Form(...),
                      q: str = Form(""), direction: str = Form("")) -> RedirectResponse:
        if arxiv_id and (not ARXIV_ID_RE.fullmatch(arxiv_id) or base_id(arxiv_id) != arxiv_id):
            raise HTTPException(status_code=400, detail="无效的 arXiv ID")
        current = next((item for item in grouped_report_library(report_library(output_root))
                        if item["arxiv_id"] == arxiv_id
                        and (arxiv_id or item["report_id"] == report_id)), None)
        if current is None:
            raise HTTPException(status_code=404, detail="本地报告不存在")
        if current["report_id"] != report_id:
            raise HTTPException(status_code=409, detail="报告已更新，请刷新页面后再删除")
        if arxiv_id and jobs.active_report_for(arxiv_id):
            raise HTTPException(status_code=409, detail="这篇论文正在生成报告，请等待任务结束后再删除")
        removed = delete_report_attempts(output_root, arxiv_id, report_id=report_id)
        if not removed:
            raise HTTPException(status_code=409, detail="报告已更新，请刷新页面后再删除")
        jobs.forget_result_urls({artifact_url(output_root / item, output_root) for item in removed})
        query = urlencode({key: value for key, value in {"q": q, "direction": direction}.items() if value})
        return RedirectResponse(f"/reports?{query}" if query else "/reports", status_code=303)

    @app.post("/reports/delete-batch")
    def delete_reports(report_ids: list[str] = Form(...), q: str = Form(""),
                       direction: str = Form("")) -> RedirectResponse:
        catalog = {item["report_id"]: item
                   for item in grouped_report_library(report_library(output_root))}
        selected = []
        # Validate the entire selection before deleting any paper's artifacts.
        for report_id in dict.fromkeys(report_ids):
            item = catalog.get(report_id)
            if item is None:
                raise HTTPException(status_code=409, detail="所选报告已更新或不存在，请刷新页面后再删除")
            arxiv_id = item["arxiv_id"]
            if arxiv_id and (not ARXIV_ID_RE.fullmatch(arxiv_id) or base_id(arxiv_id) != arxiv_id):
                raise HTTPException(status_code=400, detail="无效的 arXiv ID")
            if arxiv_id and jobs.active_report_for(arxiv_id):
                raise HTTPException(status_code=409, detail=f"论文 {arxiv_id} 正在生成报告，请等待任务结束后再删除")
            selected.append((arxiv_id, report_id))
        for arxiv_id, report_id in selected:
            removed = delete_report_attempts(output_root, arxiv_id, report_id=report_id)
            jobs.forget_result_urls({artifact_url(output_root / item, output_root) for item in removed})
            if not removed:
                raise HTTPException(status_code=409, detail="报告已更新，请刷新页面后再删除")
        query = urlencode({key: value for key, value in {"q": q, "direction": direction}.items() if value})
        return RedirectResponse(f"/reports?{query}" if query else "/reports", status_code=303)

    @app.get("/generate", response_class=HTMLResponse)
    def generate(request: Request, history_page: int = 1) -> HTMLResponse:
        history = jobs.history(history_page)
        return templates.TemplateResponse(request, "generate.html", context(request, "generate",
            jobs=history["items"], job_history=history))

    @app.get("/weekly", response_class=HTMLResponse)
    def weekly_page(request: Request) -> HTMLResponse:
        current = current_config()
        return templates.TemplateResponse(
            request,
            "weekly.html",
            context(
                request,
                "weekly",
                weekly_reports=weekly_library(output_root, current.profile_id),
                activity=collect_activity(output_root, current.profile_id, datetime.now(ZoneInfo(current.timezone)), current.weekly.days),
            ),
        )
