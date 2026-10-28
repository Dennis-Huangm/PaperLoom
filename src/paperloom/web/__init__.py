from __future__ import annotations
from ..discovery import abstracts
from ..discovery import arxiv_client
from ..integrations import zotero as zotero_adapter

import hashlib

import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime
from paperloom.resources import PACKAGE_ROOT
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .validation import ARXIV_ID_RE
from .routes import (
    recommendations as recommendations_routes,
    library as library_routes,
    reports as reports_routes,
    schedules as schedules_routes,
    backups as backups_routes,
    search as search_routes,
    comparison as comparison_routes,
    versions as versions_routes,
    related_work as related_work_routes,
    profiles as profiles_routes,
    settings as settings_routes,
    zotero as zotero_routes,
    feedback as feedback_routes,
    jobs as jobs_routes,
)
from .. import __version__
from ..discovery.abstracts import localize_abstracts
from ..discovery.arxiv_client import ArxivClient
from ..reports.render import summary_html
from ..reports.summary_cleaning import clean_summary_text
from ..config import AppConfig, load_config
from ..papers.feedback import FeedbackStore
from ..papers.models import Paper
from ..papers.paper_data import local_paper_item, base_id, requested_version
from ..reports.report_store import explicit_report, StoredReport
from ..profiles.profiles import ProfileManager
from .web_profiles import register_profile_routes
from .web_conferences import register_conference_routes
from ..discovery.conference_scope import CONFERENCES, MODES
from ..runtime.jobs.scheduler import ProfileScheduler
from ..integrations.zotero import ZoteroAuthorizationRequired, ZoteroClient, ZoteroError, ZoteroUnavailable
from .web_catalog import (
    DATE_DIR_RE,
    latest_recommendations,
    output_root_for,
    recommendation_history,
    recommendations_for_date,
    result_artifact_url,
)
from ..runtime.jobs.manager import JobManager
from .web_artifacts import ReportStaticFiles
from ..runtime.model_access.settings import save_discovery_settings, update_dotenv


def create_app(config_path: Path | str) -> FastAPI:
    config_path = Path(config_path).resolve()
    project_root = config_path.parent
    profiles = ProfileManager(project_root)
    profiles.ensure_default(config_path)
    config = load_config(config_path)
    output_root = output_root_for(config, project_root)
    output_root.mkdir(parents=True, exist_ok=True)
    package_root = PACKAGE_ROOT
    templates = Jinja2Templates(directory=str(package_root / "templates"))
    templates.env.globals["asset_version"] = lambda name: hashlib.sha256(
        (package_root / "static" / name).read_bytes()
    ).hexdigest()[:16]
    templates.env.globals.update(conference_choices=CONFERENCES, discovery_modes=MODES,
                                 conference_current_year=datetime.now().year)
    templates.env.filters["config_dict"] = asdict
    templates.env.filters["summary_html"] = summary_html
    templates.env.filters["clean_summary_text"] = clean_summary_text
    jobs = JobManager(output_root)
    scheduler = ProfileScheduler(config_path, output_root, jobs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.close()
            jobs.close()

    app = FastAPI(
        title="PaperLoom",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config_path = config_path
    app.state.project_root = project_root
    app.state.output_root = output_root
    app.state.jobs = jobs
    app.state.profiles = profiles
    app.state.scheduler = scheduler
    app.mount("/static", StaticFiles(directory=str(package_root / "static")), name="static")
    app.mount("/artifacts", ReportStaticFiles(directory=str(output_root), html=True), name="artifacts")

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(package_root / "static" / "app-icon.ico", media_type="image/x-icon")

    request_config: ContextVar[AppConfig | None] = ContextVar("request_config", default=None)

    def current_config() -> AppConfig:
        return request_config.get() or load_config(config_path)

    @app.middleware("http")
    async def enforce_local_browser_boundary(request: Request, call_next):
        local_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
        # Loopback binding alone does not reject an external hostname resolving
        # to this machine. Protect reads as well as mutations from that route.
        try:
            host = request.url.hostname
        except ValueError:
            host = None
        if host not in local_hosts:
            return JSONResponse({"detail": "仅允许通过本机地址访问"}, status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            try:
                origin_url = urlparse(origin) if origin is not None else None
                valid_origin = origin_url is None or (
                    origin_url.scheme in {"http", "https"} and origin_url.hostname in local_hosts
                )
            except ValueError:
                valid_origin = False
            if not valid_origin or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "已拒绝跨站请求"}, status_code=403)
        current = load_config(config_path)
        supplied_profile = request.headers.get("x-paperloom-profile")
        if request.method == "POST":
            # Cache the body before parsing so route handlers can read it again.
            await request.body()
            if "form" in request.headers.get("content-type", ""):
                form = await request.form()
                form_profile = form.get("profile_id")
                if form_profile is not None:
                    if supplied_profile is not None and supplied_profile != str(form_profile):
                        return JSONResponse({"detail": "请求中的研究方向不一致，请刷新页面"}, status_code=409)
                    supplied_profile = str(form_profile)
        if supplied_profile is not None and supplied_profile != current.profile_id:
            return JSONResponse({"detail": "研究方向已切换，请刷新页面后重试"}, status_code=409)
        token = request_config.set(current)
        try:
            response = await call_next(request)
        finally:
            request_config.reset(token)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/artifacts/") and (request.url.path.endswith("/report.html") or "/citations/" in request.url.path):
            # Also protect legacy reports which have not been regenerated.
            response.headers["Content-Security-Policy"] = (
                "script-src 'self'; script-src-attr 'none'; object-src 'none'; "
                "base-uri 'none'; frame-src 'none'; form-action 'none'"
            )
        if request.url.path.startswith(("/reading", "/api/reading", "/settings", "/search", "/conferences", "/backups", "/schedules")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def context(request: Request, active: str, **values: Any) -> dict[str, Any]:
        from ..integrations.collaboration import PaperCollection
        current = current_config()
        collection = PaperCollection(current, project_root)
        links = collection.page_links()
        date_label, recommendations = latest_recommendations(
            output_root, current.profile_id
        )
        base = {
            "request": request,
            "active": active,
            "config": current,
            "date_label": date_label,
            "recommendation_count": len(recommendations),
            "recommendation_scope_label": "今日推荐",
            "jobs": [asdict(job) for job in jobs.recent()],
            "job_load_errors": jobs.load_errors,
            "active_profile": current.profile_id,
            "feedback": FeedbackStore(output_root, current.profile_id).all(),
            "collection_links": lambda aid: links.get(base_id(aid), {}),
        }
        base.update(values)
        return base

    register_conference_routes(app, templates, context, current_config, jobs, output_root, project_root)
    from .web_reading import register_reading_routes
    register_reading_routes(app, templates, context, current_config, jobs, output_root)

    def raise_zotero_http_error(exc: Exception) -> None:
        if isinstance(exc, ZoteroAuthorizationRequired):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, ZoteroUnavailable):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    def recommendation_item(arxiv_id: str, current: AppConfig | None = None) -> dict[str, Any]:
        current = current or current_config()
        item = local_paper_item(output_root, current.profile_id, arxiv_id)
        if item:
            abstracts.localize_abstracts(current, output_root, [item])
            return item
        raise HTTPException(status_code=404, detail="当前推荐中没有这篇论文")

    def selected_report(arxiv_id: str, report_id: str) -> StoredReport:
        report = explicit_report(output_root, base_id(arxiv_id), report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="没有找到对应的阅读报告")
        revision = requested_version(arxiv_id)
        if revision and report.paper.get("version") != revision:
            raise HTTPException(status_code=409, detail="报告修订版不匹配")
        return report

    def selected_paper(arxiv_id: str, current: AppConfig, *, origin: str = "",
                       source_date: str = "", report_id: str = "") -> dict[str, Any]:
        if origin not in {"", "recommendation", "library", "report"}:
            raise HTTPException(status_code=400, detail="无效的论文来源")
        if origin == "report":
            return selected_report(arxiv_id, report_id).source_item()
        if origin == "recommendation" and not DATE_DIR_RE.fullmatch(source_date):
            raise HTTPException(status_code=400, detail="历史推荐需要有效的推荐日期")
        item = local_paper_item(output_root, current.profile_id, arxiv_id,
                                source_date=source_date, origin=origin)
        if not item:
            raise HTTPException(status_code=404, detail="没有找到对应的论文快照")
        # Saving a paper should never wait for or pay for a model request.
        abstracts.localize_abstracts(current, output_root, [item], generate=False)
        return item

    def cached_zotero_pdf(paper: dict[str, Any], enabled: bool) -> Path | None:
        if not enabled:
            return None
        arxiv_id = base_id(str(paper.get("arxiv_id") or ""))
        if not ARXIV_ID_RE.fullmatch(arxiv_id):
            raise ZoteroError("论文缺少有效的 arXiv ID")
        pdf_url = str(paper.get("pdf_url") or "")
        if not pdf_url:
            return None
        if urlparse(pdf_url).hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise ZoteroError("PDF 地址不是可信的 arXiv 地址")
        version = paper.get("version") or requested_version(str(paper.get("arxiv_id") or ""))
        if version:
            version = int(version)
            if version < 1:
                raise ZoteroError("无效的论文修订版")
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}v{version}"
        # Unknown revisions never reuse an unversioned cache across requests.
        revision = f"v{version}" if version else f"unknown-{uuid.uuid4().hex}"
        destination = output_root / "zotero-cache" / f"{arxiv_id.replace('/', '-')}-{revision}.pdf"
        client = arxiv_client.ArxivClient()
        try:
            if version:
                client.download_version(arxiv_id, version, destination)
            else:
                client.download_pdf(Paper.from_dict({**paper, "pdf_url": pdf_url}), destination)
        finally:
            client.client.close()
        return destination

    def save_recommendation_to_zotero(
        item: dict[str, Any], collection_key: str = "", current: AppConfig | None = None
    ) -> dict[str, Any]:
        current = current or current_config()
        from ..integrations.collaboration import PaperCollection, zotero_result
        operation = PaperCollection(current, project_root).collect(
            item, zotero=True, collection_key=collection_key,
            pdf_loader=cached_zotero_pdf, zotero_factory=zotero_adapter.ZoteroClient)
        return zotero_result(operation)

    from .web_collection import register_collection_routes
    register_collection_routes(app, current_config, selected_paper, project_root, cached_zotero_pdf,
                               templates, context, selected_report=selected_report)

    generate_profile_draft = register_profile_routes(
        app, profiles, current_config, templates, context, jobs, project_root)

    recommendations_routes.register_routes(app, context, current_config, jobs, output_root, templates)


    library_routes.register_routes(app, context, current_config, output_root, selected_paper, templates)


    reports_routes.register_routes(app, context, current_config, jobs, output_root, profiles, templates)


    schedules_routes.register_routes(app, context, profiles, scheduler, templates)


    backups_routes.register_routes(app, config_path, context, jobs, templates)


    search_routes.register_routes(app, context, current_config, jobs, output_root, project_root, templates)


    comparison_routes.register_routes(app, context, current_config, jobs, output_root, project_root, templates)


    versions_routes.register_routes(app, config_path, context, current_config, jobs, output_root, profiles, project_root, templates)


    related_work_routes.register_routes(app, context, current_config, jobs, output_root, project_root, templates)

    profiles_routes.register_routes(app, context, generate_profile_draft, profiles, templates)


    settings_routes.register_routes(app, config_path, context, current_config, output_root, profiles, project_root, templates)


    zotero_routes.register_routes(app, current_config, output_root, profiles, project_root, raise_zotero_http_error, recommendation_item, save_recommendation_to_zotero)


    feedback_routes.register_routes(app, context, current_config, output_root, project_root, selected_paper, templates)


    jobs_routes.register_routes(app, current_config, jobs, project_root, selected_paper)


    return app
