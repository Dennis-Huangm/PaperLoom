"""comparison page adapters."""
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
from ...reports.comparison import ComparisonService, available_papers, comparison_library
from ..web_catalog import report_library, preferred_report_index
from ...runtime.jobs.manager import JobContext
from ...runtime.jobs.job_requests import capture_request


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    jobs: JobManager,
    output_root: Path,
    project_root: Path,
    templates: Jinja2Templates,
) -> None:
    @app.get("/compare", response_class=HTMLResponse)
    def comparison_page(request: Request):
        current = current_config()
        choices = available_papers(output_root, current.profile_id)
        reports = preferred_report_index(report_library(output_root), current.profile_id)
        for choice in choices:
            paper = choice["paper"]
            report = reports.get((paper["arxiv_id"], paper["version"]))
            choice["quality"] = report.get("report_quality", "unknown") if report and report.get("profile_id") == (current.profile_id or "default") else "metadata"
        return templates.TemplateResponse(request, "compare.html", context(request, "reports", choices=choices,
            comparisons=comparison_library(output_root, current.profile_id)))

    @app.post("/api/jobs/compare", response_class=JSONResponse)
    def create_comparison_job(papers: list[str] = Form(...), question: str = Form("")):
        task = JobContext.capture(current_config(), project_root)
        with ComparisonService(task.config, project_root) as service:
            try:
                snapshot = service.prepare(papers, question)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = jobs.submit_request(f"{task.config.profile_name} · {len(papers)} 篇论文",
                          capture_request("compare", task.config, project_root, snapshot=snapshot),
                          identity=task.identity("compare", sources=snapshot["sources"], question=snapshot["question"]))
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)
