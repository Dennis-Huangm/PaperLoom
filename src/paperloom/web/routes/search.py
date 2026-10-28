"""search page adapters."""
from __future__ import annotations

from ...config import AppConfig
from ...runtime.jobs.manager import JobManager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Any, Callable
from dataclasses import asdict
from fastapi import Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from ...papers.search import SearchIndex, KINDS as SEARCH_KINDS, QUALITIES as SEARCH_QUALITIES
from ...runtime.jobs.manager import JobContext


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    jobs: JobManager,
    output_root: Path,
    project_root: Path,
    templates: Jinja2Templates,
) -> None:
    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", kind: str = "", quality: str = "",
                    version: str = "", page: int = 1, profile_id: str | None = None):
        current = current_config()
        if profile_id is not None and profile_id != current.profile_id:
            raise HTTPException(409, "研究方向已切换，请从导航重新打开搜索")
        index = SearchIndex(output_root, current.profile_id)
        try:
            revision = int(version) if version.strip() else None
            results = index.search(q, kind=kind, quality=quality, version=revision, page=page)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return templates.TemplateResponse(request, "search.html", context(request, "search", query=q,
            kind=kind, quality=quality, version=version, results=results, index_status=index.status(),
            search_kinds=SEARCH_KINDS, search_qualities=SEARCH_QUALITIES))

    @app.get("/search/source/{doc_id}", response_class=HTMLResponse)
    def search_source(request: Request, doc_id: str, profile_id: str):
        if profile_id != current_config().profile_id:
            raise HTTPException(409, "研究方向已切换，请从导航重新打开搜索")
        try:
            document = SearchIndex(output_root, profile_id).document(doc_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if document is None:
            raise HTTPException(404, "索引中没有这条记录，请重新搜索")
        return templates.TemplateResponse(request, "search_source.html", context(request, "search",
            document=document, search_kinds=SEARCH_KINDS, search_qualities=SEARCH_QUALITIES))

    @app.post("/api/jobs/search-index", response_class=JSONResponse)
    def create_search_index_job(profile_id: str = Form(...), rebuild: bool = Form(False)):
        task = JobContext.capture(current_config(), project_root)
        def run_index():
            SearchIndex(output_root, task.config.profile_id).refresh(rebuild=rebuild)
        job = jobs.submit("search-index", f"{task.config.profile_name} · {'重建' if rebuild else '更新'}搜索索引",
                          run_index, identity=task.identity("search-index"), profile_id=task.config.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)
