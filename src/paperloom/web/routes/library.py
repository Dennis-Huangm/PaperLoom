"""library page adapters."""
from __future__ import annotations

from ...config import AppConfig
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Any, Callable
from ...discovery import abstracts
from fastapi import Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from ...discovery.abstracts import localize_abstracts
from ...papers.feedback import FeedbackStore
from ...papers.library import PaperLibraryStore
from ...papers.reading_state import READING_STATUSES, ReadingConflict
from ...papers.paper_data import base_id
from ..web_catalog import report_library, preferred_report_index
from ...runtime.model_access.settings import _list_field

from ..validation import ARXIV_ID_RE


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    output_root: Path,
    selected_paper: Callable[..., dict[str, Any]],
    templates: Jinja2Templates,
) -> None:
    @app.get("/library", response_class=HTMLResponse)
    def paper_library(request: Request, q: str = "", reading_status: str = "") -> HTMLResponse:
        current = current_config()
        store = PaperLibraryStore(output_root, current.profile_id)
        if reading_status and reading_status not in READING_STATUSES:
            raise HTTPException(status_code=400, detail="无效的阅读进度")
        reading_records = store.state.snapshot().get("reading", {})
        feedback = FeedbackStore(output_root, current.profile_id).all()
        reports_by_id = preferred_report_index(report_library(output_root), current.profile_id)
        entries = list(store.all().values())
        incomplete = [
            entry
            for entry in entries
            if not (entry.get("paper") or {}).get("abstract_zh")
            or not (entry.get("paper") or {}).get("recommendation_detail")
        ]
        if incomplete:
            localization_items = [
                {
                    "paper": dict(entry.get("paper") or {}),
                    "verified": entry.get("verified") or {},
                }
                for entry in incomplete
            ]
            # GET /library must stay responsive: use cached/report-derived text
            # only and reserve network generation for explicit write actions.
            abstracts.localize_abstracts(
                current, output_root, localization_items, generate=False
            )
            for entry, item in zip(incomplete, localization_items):
                store.refresh(entry, item)
            entries = list(store.all().values())
        for entry in entries:
            arxiv_id = str(entry.get("arxiv_id") or "")
            entry["reading"] = reading_records.get(arxiv_id, {"status": "unread", "notes": "", "tags": [],
                                                            "updated_at": "", "read_version": None})
            entry["feedback"] = feedback.get(arxiv_id)
            report = reports_by_id.get((arxiv_id, (entry.get("paper") or {}).get("version")))
            entry["report_url"] = report.get("report_url") if report else None
            entry["has_report"] = bool(report)
        library_counts = {value: sum(entry['reading']['status'] == value for entry in entries)
                          for value in READING_STATUSES}
        if q.strip():
            needle = q.casefold().strip()
            entries = [
                entry
                for entry in entries
                if needle
                in " ".join(
                    [
                        str(entry.get("arxiv_id") or ""),
                        str((entry.get("paper") or {}).get("title") or ""),
                        entry["reading"].get("notes", ""),
                        " ".join(entry["reading"].get("tags", [])),
                        " ".join(
                            str(author.get("name") or "")
                            for author in ((entry.get("paper") or {}).get("authors") or [])
                        ),
                    ]
                ).casefold()
            ]
        if reading_status:
            entries = [entry for entry in entries if entry["reading"]["status"] == reading_status]
        entries.sort(key=lambda entry: str(entry.get("saved_at") or ""), reverse=True)
        return templates.TemplateResponse(
            request,
            "library.html",
            context(
                request,
                "library",
                entries=entries,
                library_counts=library_counts,
                query=q.strip(),
                reading_status=reading_status,
                reading_statuses=READING_STATUSES,
            ),
        )

    @app.post("/api/library/reading", response_class=JSONResponse)
    def update_reading(arxiv_id: str = Form(...), reading_status: str = Form(...),
                       notes: str = Form(""), tags: str = Form(""), read_version: int | None = Form(None),
                       expected_updated_at: str = Form("")) -> JSONResponse:
        current = current_config()
        try:
            record = PaperLibraryStore(output_root, current.profile_id).state.update_reading(
                base_id(arxiv_id), status=reading_status, notes=notes, tags=_list_field(tags),
                read_version=read_version, expected_updated_at=expected_updated_at)
        except ReadingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"reading": record, "label": READING_STATUSES[record["status"]],
                             "message": "阅读进度与个人笔记已保存"})

    @app.post("/api/library/toggle", response_class=JSONResponse)
    def toggle_paper_library(arxiv_id: str = Form(...), origin: str = Form(""),
                             source_date: str = Form(""), report_id: str = Form("")) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID")
        current = current_config()
        store = PaperLibraryStore(output_root, current.profile_id)
        previous = store.all().get(base_id(arxiv_id))
        item = previous or selected_paper(arxiv_id, current, origin=origin,
                                         source_date=source_date, report_id=report_id)
        saved = store.toggle(item, current.profile_name,
                             source_date=str(item.get("_source_date") or item.get("source_date") or ""))
        return JSONResponse(
            {
                "saved": saved,
                "feedback": None,
                "message": "已加入文献库，并作为后续推荐的相关样本" if saved else "已从当前方向的文献库移除",
            }
        )

    @app.get("/api/library/status", response_class=JSONResponse)
    def paper_library_status(arxiv_id: str) -> JSONResponse:
        current = current_config()
        saved = PaperLibraryStore(output_root, current.profile_id).contains(
            base_id(arxiv_id.strip())
        )
        return JSONResponse({"saved": saved})

    @app.post("/api/library/add", response_class=JSONResponse)
    def add_paper_library(arxiv_id: str = Form(...), origin: str = Form(""),
                          source_date: str = Form(""), report_id: str = Form("")) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID")
        current = current_config()
        store = PaperLibraryStore(output_root, current.profile_id)
        already_saved = store.contains(base_id(arxiv_id))
        item = selected_paper(arxiv_id, current, origin=origin,
                              source_date=source_date, report_id=report_id)
        store.add(item, current.profile_name, source_date=str(item.get("_source_date") or ""))
        return JSONResponse(
            {
                "saved": True,
                "already_saved": already_saved,
                "message": "已在文献库中，并作为后续推荐的相关样本"
                if already_saved
                else "已加入文献库，并作为后续推荐的相关样本",
            }
        )

    @app.post("/library/remove")
    def remove_from_paper_library(arxiv_id: str = Form(...)) -> RedirectResponse:
        current = current_config()
        PaperLibraryStore(output_root, current.profile_id).remove(arxiv_id.strip())
        return RedirectResponse("/library", status_code=status.HTTP_303_SEE_OTHER)
