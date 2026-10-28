"""recommendations page adapters."""
from __future__ import annotations

from ...config import AppConfig
from ...runtime.jobs.manager import JobManager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Any, Callable
from ...discovery import abstracts
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from ...discovery.abstracts import localize_abstracts
from ...papers.feedback import FeedbackStore
from ...papers.library import PaperLibraryStore
from ...papers.storage import clear_recommendations
from ..web_catalog import (
    DATE_DIR_RE,
    latest_recommendations,
    recommendation_history,
    recommendations_for_date,
    report_library,
    grouped_report_library,
    preferred_report_index,
    weekly_library,
)


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    jobs: JobManager,
    output_root: Path,
    templates: Jinja2Templates,
) -> None:
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, date: str = "") -> HTMLResponse:
        current = current_config()
        history = recommendation_history(output_root, current.profile_id)
        requested_date = date.strip()
        if requested_date and not DATE_DIR_RE.fullmatch(requested_date):
            raise HTTPException(status_code=400, detail="日期格式必须为 YYYY-MM-DD")
        if requested_date:
            date_label = requested_date
            recommendations = recommendations_for_date(
                output_root, requested_date, current.profile_id
            )
        else:
            date_label, recommendations = latest_recommendations(
                output_root, current.profile_id
            )
        is_latest = bool(date_label) and bool(history) and date_label == history[0]["date"]
        abstracts.localize_abstracts(
            current, output_root, recommendations, generate=False
        )
        feedback = FeedbackStore(output_root, current.profile_id).all()
        saved_papers = PaperLibraryStore(output_root, current.profile_id).all()
        reports_by_id = preferred_report_index(report_library(output_root), current.profile_id)
        for item in recommendations:
            arxiv_id = str((item.get("paper") or {}).get("arxiv_id") or "")
            item["feedback"] = feedback.get(arxiv_id)
            item["in_library"] = arxiv_id in saved_papers
            report = reports_by_id.get((arxiv_id, (item.get("paper") or {}).get("version")))
            item["report_url"] = report.get("report_url") if report else None
            item["has_report"] = bool(report)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(
                request,
                "dashboard",
                date_label=date_label,
                recommendation_count=len(recommendations),
                recommendation_scope_label="今日推荐" if is_latest else "当日推荐",
                recommendations=recommendations,
                recommendation_history=history,
                selected_date=date_label or "",
                is_latest=is_latest,
                recent_reports=grouped_report_library(report_library(output_root))[:4],
                recent_weekly=weekly_library(output_root, current.profile_id)[:2],
            ),
        )

    @app.post("/recommendations/clear")
    def clear_daily_recommendations(date_label: str = Form(...),
                                    profile_id: str = Form(...)) -> RedirectResponse:
        if profile_id != current_config().profile_id:
            raise HTTPException(409, "研究方向已切换，请刷新页面后重试")
        if jobs.active_for("digest", profile_id):
            raise HTTPException(409, "当前研究方向正在刷新推荐，请等待任务结束后再清除")
        try:
            clear_recommendations(output_root, date_label, profile_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    @app.post("/api/recommendations/delete", response_class=JSONResponse)
    def delete_selected_recommendations(date_label: str = Form(...),
                                        profile_id: str = Form(...),
                                        arxiv_ids: list[str] = Form(...)) -> JSONResponse:
        if profile_id != current_config().profile_id:
            raise HTTPException(409, "研究方向已切换，请刷新页面后重试")
        if jobs.active_for("digest", profile_id):
            raise HTTPException(409, "当前研究方向正在刷新推荐，请等待任务结束后再删除")
        try:
            clear_recommendations(output_root, date_label, profile_id, set(arxiv_ids))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse({"message": f"已删除 {len(set(arxiv_ids))} 篇推荐"})
