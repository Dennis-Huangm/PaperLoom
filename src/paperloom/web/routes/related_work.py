"""related work page adapters."""
from __future__ import annotations

from ...config import AppConfig
from ...runtime.jobs.manager import JobManager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from typing import Any, Callable
from dataclasses import asdict
from pathlib import Path
from fastapi import Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from ...related_work.citation_graph import CitationExplorer
from ..web_catalog import citation_library
from ...runtime.jobs.manager import JobContext

from ..validation import ARXIV_ID_RE


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    jobs: JobManager,
    output_root: Path,
    project_root: Path,
    templates: Jinja2Templates,
) -> None:
    @app.get("/citations", response_class=HTMLResponse)
    def citations_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "citations.html",
            context(request, "citations", citation_maps=citation_library(output_root, current_config().profile_id)),
        )

    @app.post("/api/jobs/citation", response_class=JSONResponse)
    def create_citation_job(arxiv_id: str = Form(...)) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.fullmatch(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID")

        task = JobContext.capture(current_config(), project_root)
        current = task.config
        if not current.citations.enabled:
            raise HTTPException(status_code=409, detail="相关工作地图生成已关闭")

        def run_citation() -> Path:
            with CitationExplorer(current, project_root) as explorer:
                return explorer.generate(arxiv_id)

        job = jobs.submit("citation", f"{current.profile_name} · arXiv:{arxiv_id}", run_citation,
                          identity=task.identity("citation", arxiv_id=arxiv_id), profile_id=current.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)
