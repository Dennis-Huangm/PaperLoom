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
from ...papers.reading_state import _locked
from ...reports.report_store import delete_selected_reports, iter_report_records
from ...papers.activity import collect_activity
from ..web_catalog import artifact_url, report_library, grouped_report_library, weekly_library
from ..web_catalog import REPORT_SORT_OPTIONS, sort_report_library

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
    def visible_catalog(deleted_profiles: set[str], *, for_deletion=False) -> list[dict[str, Any]]:
        profile_names = {profile["id"]: profile["name"] for profile in profiles.list()}
        visible, incomplete = [], {}
        for record in iter_report_records(output_root, catalog=True):
            if str(record.metadata.get("profile_id") or "") in deleted_profiles:
                continue
            if record.html_path.exists():
                visible.append(record)
            elif for_deletion and isinstance(record.paper, dict) and record.paper.get("arxiv_id"):
                aid = base_id(str(record.paper["arxiv_id"]))
                incomplete.setdefault(aid, []).append(record.report_id)
        reports = report_library(output_root, records=visible)
        return grouped_report_library(reports, profile_names, incomplete_attempts=incomplete)

    @app.get("/api/reports", response_class=JSONResponse)
    def report_catalog() -> JSONResponse:
        items = visible_catalog(profiles.deleted_ids())
        items.sort(key=lambda item: item["report_path"].stat().st_mtime_ns, reverse=True)
        fields = ("title", "authors", "arxiv_id", "version", "directions", "date",
                  "updated_at", "report_url", "report_id")
        return JSONResponse(
            {"reports": [{field: item[field] for field in fields} for item in items]},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, q: str = "", direction: str = "", sort: str = "opened_desc") -> HTMLResponse:
        sort = sort if sort in dict(REPORT_SORT_OPTIONS) else "opened_desc"
        current = current_config()
        all_items = visible_catalog(profiles.deleted_ids(), for_deletion=True)
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
        items = sort_report_library(items, sort)
        return templates.TemplateResponse(
            request,
            "reports.html",
            context(request, "reports", reports=items, query=q, direction=direction,
                    directions=directions, catalog_ready=True, sort=sort, sort_options=REPORT_SORT_OPTIONS),
        )

    def delete_selection(report_ids, tokens, q, direction, sort, *, expected_paper=None):
        # Same lock order as direction deletion. Publishers serialize the final catalog change.
        with jobs.lock, _locked(profiles.root / "profiles.lock"):
            hidden = profiles.deleted_ids()
            try:
                delete_selected_reports(output_root, report_ids, tokens, expected_paper=expected_paper,
                    hidden=hidden, load_catalog=lambda: visible_catalog(hidden, for_deletion=True),
                    active_report=jobs.active_report_for,
                    on_removed=lambda path: jobs.forget_result_urls({artifact_url(path, output_root)}))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(500, str(exc)) from exc
        query = urlencode({key: value for key, value in {"q": q, "direction": direction,
                           "sort": sort if sort in dict(REPORT_SORT_OPTIONS) else ""}.items() if value})
        return RedirectResponse(f"/reports?{query}" if query else "/reports", status_code=303)

    @app.post("/reports/delete")
    def delete_report(arxiv_id: str = Form(""), report_id: str = Form(...),
                      delete_tokens: list[str] = Form(default=[]), q: str = Form(""),
                      direction: str = Form(""), sort: str = Form("")) -> RedirectResponse:
        if arxiv_id and (not ARXIV_ID_RE.fullmatch(arxiv_id) or base_id(arxiv_id) != arxiv_id):
            raise HTTPException(400, "无效的 arXiv ID")
        return delete_selection([report_id], set(delete_tokens), q, direction, sort, expected_paper=arxiv_id)

    @app.post("/reports/delete-batch")
    def delete_reports(report_ids: list[str] = Form(...), delete_tokens: list[str] = Form(default=[]),
                       q: str = Form(""), direction: str = Form(""), sort: str = Form("")) -> RedirectResponse:
        return delete_selection(report_ids, set(delete_tokens), q, direction, sort)

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
