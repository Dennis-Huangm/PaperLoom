from __future__ import annotations

import copy

import importlib.util
import os
import re
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import __version__
from .abstracts import localize_abstracts
from .arxiv_client import ArxivClient
from .render import summary_html
from .summary_cleaning import clean_summary_text
from .config import AppConfig, VersionSyncConfig, load_config
from .citation_graph import CitationExplorer
from .feedback import FeedbackStore, VERDICTS
from .library import PaperLibraryStore
from .reading_state import READING_STATUSES, ReadingConflict
from .models import Paper
from .paper_data import local_paper_item, base_id, requested_version
from .report_store import delete_report_attempts, explicit_report, html_reports, StoredReport
from .obsidian import ObsidianError, ObsidianExporter, discover_obsidian_vaults
from .profiles import ProfileManager
from .web_profiles import register_profile_routes
from .utils import read_json
from .storage import clear_recommendations
from .activity import collect_activity
from .backup import BackupService
from .scheduler import ProfileScheduler, WEEKDAYS, STATUS_LABELS as SCHEDULE_STATUS_LABELS
from .search import SearchIndex, KINDS as SEARCH_KINDS, QUALITIES as SEARCH_QUALITIES
from .comparison import ComparisonService, available_papers, comparison_library
from .version_tracker import VersionTracker
from .version_sync import PaperVersionSync, STEP_LABELS
from .version_batch import VersionSyncBatch, update_candidates, BATCH_LABELS, MAX_BATCH_SIZE
from .version_auto import AutomaticVersionSync
from .zotero import (
    ZoteroAuthorizationRequired,
    ZoteroClient,
    ZoteroError,
    ZoteroUnavailable,
)
from .web_catalog import (
    DATE_DIR_RE,
    artifact_url,
    citation_library,
    latest_recommendations,
    output_root_for,
    recommendation_history,
    recommendations_for_date,
    report_library,
    grouped_report_library,
    preferred_report_index,
    result_artifact_url,
    version_tracking_data,
    weekly_library,
)
from .web_jobs import BackgroundJob, JobContext, JobManager
from .job_requests import capture_request
from .web_artifacts import ReportStaticFiles
from .web_settings import (
    _int_value,
    _list_field,
    build_config_update,
    credential_specs,
    save_config_settings,
    save_discovery_settings,
    update_dotenv,
)


ARXIV_ID_RE = re.compile(r"^(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$", re.I)
def service_status(config: AppConfig, config_path: Path) -> list[dict[str, Any]]:
    zotero = ZoteroClient(config.zotero, timeout=2.0).status()
    obsidian = ObsidianExporter(config, config_path.parent).status()
    return [
        {"name": "配置文件", "ready": config_path.exists(), "detail": str(config_path)},
        {
            "name": "LLM",
            "ready": bool(os.getenv(config.llm.api_key_env) or os.getenv(config.llm.base_url_env)),
            "detail": config.llm.model,
        },
        {
            "name": "OpenAlex",
            "ready": bool(os.getenv(config.metadata.openalex_api_key_env)),
            "detail": "API key 已配置" if os.getenv(config.metadata.openalex_api_key_env) else "公共 API 模式",
        },
        {
            "name": "Semantic Scholar",
            "ready": bool(os.getenv(config.metadata.semantic_scholar_api_key_env)),
            "detail": "可选增强源",
        },
        {
            "name": "alphaXiv 语义检索",
            "ready": bool(
                config.discovery.provider in {"auto", "hybrid"}
                and config.discovery.alphaxiv_fallback_enabled
                and os.getenv(config.discovery.alphaxiv_api_key_env)
            ),
            "detail": (
                ("与 arXiv 共同召回，arXiv 补全正式数据" if config.discovery.provider == "hybrid"
                 else "仅在 arXiv API 重试耗尽后启用")
                if config.discovery.provider in {"auto", "hybrid"} and config.discovery.alphaxiv_fallback_enabled
                else "语义检索未启用"
            ),
        },
        {
            "name": "QQ SMTP",
            "ready": bool(
                config.delivery.email_enabled
                and os.getenv(config.delivery.email_address_env)
                and os.getenv(config.delivery.password_env)
            ),
            "detail": "每日推荐邮件",
        },
        {
            "name": "PDF 解析",
            "ready": importlib.util.find_spec("pymupdf") is not None,
            "detail": "Docling 可用" if importlib.util.find_spec("docling") else "PyMuPDF 模式",
        },
        {
            "name": "Zotero",
            "ready": bool(zotero.get("ready") and zotero.get("authorized")),
            "detail": (
                "本地 API 已连接并授权"
                if zotero.get("ready") and zotero.get("authorized")
                else "本地 API 已连接，等待写入授权"
                if zotero.get("ready")
                else "请启动 Zotero 并启用本地 API"
            ),
        },
        {
            "name": "Obsidian",
            "ready": bool(obsidian.get("ready")),
            "detail": obsidian.get("detail") or "本地 Markdown 知识库",
        },
    ]


def create_app(config_path: Path | str) -> FastAPI:
    config_path = Path(config_path).resolve()
    project_root = config_path.parent
    profiles = ProfileManager(project_root)
    profiles.ensure_default(config_path)
    config = load_config(config_path)
    output_root = output_root_for(config, project_root)
    output_root.mkdir(parents=True, exist_ok=True)
    package_root = Path(__file__).parent
    templates = Jinja2Templates(directory=str(package_root / "templates"))
    templates.env.filters["summary_html"] = summary_html
    templates.env.filters["clean_summary_text"] = clean_summary_text
    jobs = JobManager(output_root, config.jobs.max_parallel)
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
        if request.url.path.startswith(("/settings", "/search", "/backups", "/schedules")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def context(request: Request, active: str, **values: Any) -> dict[str, Any]:
        from .collaboration import PaperCollection
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
            localize_abstracts(current, output_root, [item])
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
        localize_abstracts(current, output_root, [item], generate=False)
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
        client = ArxivClient()
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
        from .collaboration import PaperCollection, zotero_result
        operation = PaperCollection(current, project_root).collect(
            item, zotero=True, collection_key=collection_key,
            pdf_loader=cached_zotero_pdf, zotero_factory=ZoteroClient)
        return zotero_result(operation)

    from .web_collection import register_collection_routes
    register_collection_routes(app, current_config, selected_paper, project_root, cached_zotero_pdf,
                               templates, context, selected_report=selected_report)

    generate_profile_draft = register_profile_routes(
        app, profiles, current_config, templates, context, jobs, project_root)

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
        localize_abstracts(
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
            localize_abstracts(
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
    def delete_report(arxiv_id: str = Form(...), report_id: str = Form(...),
                      q: str = Form(""), direction: str = Form("")) -> RedirectResponse:
        if not ARXIV_ID_RE.fullmatch(arxiv_id) or base_id(arxiv_id) != arxiv_id:
            raise HTTPException(status_code=400, detail="无效的 arXiv ID")
        current = next((item for item in grouped_report_library(report_library(output_root))
                        if item["arxiv_id"] == arxiv_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="本地报告不存在")
        if current["report_id"] != report_id:
            raise HTTPException(status_code=409, detail="报告已更新，请刷新页面后再删除")
        if jobs.active_report_for(arxiv_id):
            raise HTTPException(status_code=409, detail="这篇论文正在生成报告，请等待任务结束后再删除")
        removed = delete_report_attempts(output_root, arxiv_id)
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
            if not ARXIV_ID_RE.fullmatch(arxiv_id) or base_id(arxiv_id) != arxiv_id:
                raise HTTPException(status_code=400, detail="无效的 arXiv ID")
            if jobs.active_report_for(arxiv_id):
                raise HTTPException(status_code=409, detail=f"论文 {arxiv_id} 正在生成报告，请等待任务结束后再删除")
            selected.append(arxiv_id)
        for arxiv_id in selected:
            removed = delete_report_attempts(output_root, arxiv_id)
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

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request, saved: str = ""):
        try:
            schedule = scheduler.view()
            error = ""
        except (OSError, ValueError, TypeError, KeyError) as exc:
            schedule = {"enabled": False, "plans": [], "history": [], "errors": {}}
            error = str(exc)
        return templates.TemplateResponse(request, "schedules.html", context(request, "profiles",
            schedule=schedule, schedule_error=error, profiles=profiles.list(), saved=saved,
            plans_by_id={item["profile_id"]: item for item in schedule["plans"]},
            weekdays=WEEKDAYS, schedule_statuses=SCHEDULE_STATUS_LABELS))

    @app.post("/schedules/enabled")
    def set_schedule_enabled(profile_id: str = Form(...), enabled: bool = Form(False)):
        try:
            scheduler.set_enabled(enabled)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/schedules?saved=enabled", status_code=303)

    @app.post("/schedules/profile/{target_profile}")
    def save_schedule(target_profile: str, profile_id: str = Form(...), enabled: bool = Form(False),
                      time: str = Form(...), timezone: str = Form(...), weekdays: list[int] = Form(...),
                      send_email: bool = Form(False)):
        try:
            scheduler.save_profile(target_profile, {"enabled": enabled, "time": time, "timezone": timezone,
                "weekdays": weekdays, "send_email": send_email})
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/schedules?saved=profile", status_code=303)

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

    @app.get("/versions", response_class=HTMLResponse)
    def versions_page(request: Request, saved: str = "") -> HTMLResponse:
        current = current_config()
        tracking = version_tracking_data(output_root, current.profile_id)
        batches = VersionSyncBatch(current, project_root)
        batch_history = []
        for batch in batches.recent():
            batch["summary"] = batches.summary(batch)
            batch["result_url"] = artifact_url(batches._path(batch["id"]).with_name("index.html"), output_root)
            batch["created_label"] = datetime.fromisoformat(batch["created_at"]).astimezone(
                ZoneInfo(current.timezone)).strftime("%Y-%m-%d %H:%M")
            batch_history.append(batch)
        return templates.TemplateResponse(
            request,
            "versions.html",
            context(request, "versions", tracking=tracking, updates=update_candidates(tracking["items"]),
                    batches=batch_history, batch_labels=BATCH_LABELS, step_labels=STEP_LABELS,
                    max_batch_size=MAX_BATCH_SIZE, policy_saved=saved == "policy",
                    automatic=AutomaticVersionSync(current, project_root).latest()),
        )

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

    @app.get("/citations", response_class=HTMLResponse)
    def citations_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "citations.html",
            context(request, "citations", citation_maps=citation_library(output_root, current_config().profile_id)),
        )

    @app.get("/profiles", response_class=HTMLResponse)
    def profile_page(request: Request, saved: str = "", switched: str = "", updated: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "profiles.html",
            context(
                request,
                "profiles",
                profiles=profiles.list(),
                drafts=profiles.drafts(),
                deleted_drafts=profiles.deleted_drafts(),
                draft_request_id=uuid.uuid4().hex,
                saved=saved,
                switched=switched,
                updated=updated,
            ),
        )

    @app.post("/profiles/create")
    async def create_profile(request: Request) -> RedirectResponse:
        form = await request.form()
        try:
            data = {key: str(form.get(key, "")).strip() for key in ("name", "description")}
            data["request_id"] = str(form.get("request_id", ""))
            for key in ("keywords", "negative_keywords", "reference_ids", "required", "excluded"):
                data[key] = _list_field(str(form.get(key, "")))
            data["recommendation_count"] = _int_value(form, "recommendation_count", 1, 50)
            draft = await generate_profile_draft(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/profiles/drafts/{draft['id']}", status_code=303)

    @app.post("/profiles/{profile_id}/activate")
    def activate_profile(profile_id: str) -> RedirectResponse:
        try:
            profiles.activate(profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(
            f"/profiles?switched={quote(profile_id)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, saved: str = "") -> HTMLResponse:
        current = current_config()
        zotero_connection = ZoteroClient(current.zotero, timeout=2.0).status()
        obsidian_connection = ObsidianExporter(current, project_root).status()
        obsidian_open_url = ""
        if obsidian_connection.get("ready"):
            vault_name = Path(str(obsidian_connection.get("vault") or "")).name
            root = current.obsidian.root_folder.strip("./\\")
            home = current.obsidian.home_folder.strip("./\\")
            file_path = "/".join(part for part in (root, home, "Research Hub") if part)
            obsidian_open_url = (
                f"obsidian://open?vault={quote(vault_name)}&file={quote(file_path)}"
            )
        return templates.TemplateResponse(
            request,
            "settings.html",
            context(
                request,
                "settings",
                saved=saved,
                credentials=credential_specs(current),
                zotero_connection=zotero_connection,
                obsidian_connection=obsidian_connection,
                obsidian_vaults=discover_obsidian_vaults(),
                obsidian_open_url=obsidian_open_url,
            ),
        )

    @app.post("/settings/config")
    async def update_settings(request: Request) -> RedirectResponse:
        form = await request.form()
        current = current_config()
        protected = ("interest_description", "arxiv_categories", "arxiv_query_terms", "positive_keywords",
                     "negative_keywords", "seed_papers", "concept_groups", "minimum_concept_groups")
        if current.discovery.search_plan.get("version") == 2:
            form = dict(form)
            for key in protected:
                value = getattr(current.discovery, key)
                form[key] = "\n".join(value) if isinstance(value, list) else str(value)
        try:
            values = build_config_update(form)
            discovery = values.pop("discovery")
            ranking = values.pop("ranking")
            save_config_settings(config_path, values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if current.discovery.search_plan.get("version") == 2:
            discovery["search_plan"] = current.discovery.search_plan
            for key in protected:
                discovery[key] = getattr(current.discovery, key)
        if current.profile_id:
            profile = profiles.get(current.profile_id)
            profile.update(discovery=discovery, ranking=ranking)
            profiles.save(profile)
        else:
            save_config_settings(config_path, {"discovery": discovery, "ranking": ranking})
        jobs.set_max_parallel(load_config(config_path).jobs.max_parallel)
        return RedirectResponse("/settings?saved=config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/settings/credentials/reveal", response_class=JSONResponse)
    def reveal_credential(field: str = Form(...)) -> JSONResponse:
        item = next((item for item in credential_specs(current_config()) if item["field"] == field), None)
        if item is None:
            raise HTTPException(status_code=400, detail="未知的凭据字段")
        return JSONResponse({"value": os.getenv(item["env_name"], "")})

    @app.post("/settings/credentials")
    async def update_credentials(request: Request) -> RedirectResponse:
        form = await request.form()
        current = current_config()
        updates: dict[str, str] = {}
        clear: set[str] = set()
        for item in credential_specs(current):
            field = item["field"]
            env_name = item["env_name"]
            value = str(form.get(field, ""))
            if f"clear_{field}" in form:
                clear.add(env_name)
            elif value:
                updates[env_name] = value.strip() if not item["secret"] else value
        clear.difference_update(updates)
        try:
            update_dotenv(project_root / ".env", updates, clear)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/settings?saved=credentials", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/api/zotero/authorize", response_class=JSONResponse)
    async def authorize_zotero() -> JSONResponse:
        current = current_config()
        try:
            key = await run_in_threadpool(ZoteroClient(current.zotero).authorize)
            update_dotenv(project_root / ".env", {current.zotero.api_key_env: key})
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        return JSONResponse({"message": "Zotero 已连接并获得本地写入授权"})

    @app.get("/api/zotero/collections", response_class=JSONResponse)
    def zotero_collections() -> JSONResponse:
        current = current_config()
        client = ZoteroClient(current.zotero, timeout=5.0)
        connection = client.status()
        if not connection.get("ready"):
            raise HTTPException(status_code=503, detail="Zotero 未运行或本地 API 未启用")
        return JSONResponse(
            {
                "default_name": current.zotero.collection_name,
                "collections": client.list_collections(),
            }
        )

    @app.post("/api/zotero/save-paper", response_class=JSONResponse)
    async def save_zotero_paper(
        arxiv_id: str = Form(...), collection_key: str = Form(""),
        origin: str = Form(""), source_date: str = Form("")
    ) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID")
        current = current_config()
        if origin not in {"", "recommendation", "library"}:
            raise HTTPException(status_code=400, detail="无效的论文来源")
        if origin == "recommendation" and not DATE_DIR_RE.fullmatch(source_date):
            raise HTTPException(status_code=400, detail="历史推荐需要有效的推荐日期")
        if origin:
            item = local_paper_item(output_root, current.profile_id, arxiv_id,
                                    source_date=source_date, origin=origin)
            if not item:
                raise HTTPException(status_code=404, detail="没有找到对应的论文快照")
        else:
            item = recommendation_item(arxiv_id, current)
        try:
            result = await run_in_threadpool(
                save_recommendation_to_zotero, item, collection_key, current
            )
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        message = "已创建 Zotero 条目" if result["created"] else "Zotero 中已存在该论文"
        if result["attachments_added"]:
            message += f"，新增 {result['attachments_added']} 个附件"
        if result.get("status") != "succeeded":
            message = result.get("error") or "部分步骤未完成，请查看收录回执"
        status = 200
        if result.get('status') != 'succeeded':
            status = 503 if 'ZoteroUnavailable' in message else 409 if 'ZoteroAuthorizationRequired' in message or result.get('status') == 'needs_attention' else 502
        return JSONResponse({**result, "message": message, "detail": message}, status_code=status)

    @app.post("/api/zotero/save-today", response_class=JSONResponse)
    async def save_zotero_today(
        collection_key: str = Form(""), date_label: str = Form("")
    ) -> JSONResponse:
        current = current_config()
        if date_label and not DATE_DIR_RE.fullmatch(date_label):
            raise HTTPException(status_code=400, detail="日期格式必须为 YYYY-MM-DD")
        if date_label:
            recommendations = recommendations_for_date(
                output_root, date_label, current.profile_id
            )
        else:
            _, recommendations = latest_recommendations(
                output_root, current.profile_id
            )
        if not recommendations:
            raise HTTPException(status_code=404, detail="当前没有可保存的推荐")

        def save_all() -> list[dict[str, Any]]:
            return [
                save_recommendation_to_zotero(item, collection_key, current)
                for item in recommendations
            ]

        try:
            results = await run_in_threadpool(save_all)
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        created = sum(1 for item in results if item["created"])
        failed = sum(item.get("status") != "succeeded" for item in results)
        existing = len(results) - created - failed
        attachments = sum(int(item["attachments_added"]) for item in results)
        return JSONResponse(
            {
                "created": created,
                "results": results,
                "existing": existing,
                "attachments_added": attachments,
                "message": f"Zotero 收录：新增 {created} 篇，已存在 {existing} 篇，未完成 {failed} 篇，新增附件 {attachments} 个；逐篇结果已保存",
            }
        )

    @app.post("/api/zotero/save-report", response_class=JSONResponse)
    async def save_zotero_report(
        arxiv_id: str = Form(...), collection_key: str = Form(""), report_id: str = Form("")
    ) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        matches = [item for item in html_reports(output_root)
                   if item.paper.get("arxiv_id", "") == arxiv_id]
        if report_id:
            matches = [item for item in matches if item.report_id == report_id]
        elif len(matches) > 1:
            raise HTTPException(status_code=409, detail="这篇论文有多份报告，请从报告列表选择具体报告")
        report = matches[0] if matches else None
        if not report:
            raise HTTPException(status_code=404, detail="没有找到这篇论文的本地报告")

        payload = report.metadata
        current = current_config()
        source_profile = str(payload.get("profile_id") or "")
        profile_name = str(payload.get("profile_name") or "")
        if not profile_name and source_profile:
            try:
                profile_name = str(profiles.get(source_profile).get("name") or source_profile)
            except (KeyError, ValueError):
                profile_name = source_profile

        def save_report() -> dict[str, Any]:
            from .collaboration import PaperCollection, zotero_result
            source_config = copy.deepcopy(current)
            source_config.profile_name = profile_name or current.profile_name
            operation = PaperCollection(source_config, project_root).collect(
                payload, zotero=True, report_path=report.markdown_path,
                pdf_path=report.pdf_path if report.pdf_path.exists() else None,
                collection_key=collection_key, zotero_factory=ZoteroClient)
            return zotero_result(operation)


        try:
            result = await run_in_threadpool(save_report)
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        message = "报告已保存到 Zotero"
        if result["attachments_added"]:
            message += f"，新增 {result['attachments_added']} 个附件"
        if result.get("status") != "succeeded":
            message = result.get("error") or "部分步骤未完成，请查看收录回执"
        status = 200
        if result.get('status') != 'succeeded':
            status = 503 if 'ZoteroUnavailable' in message else 409 if 'ZoteroAuthorizationRequired' in message or result.get('status') == 'needs_attention' else 502
        return JSONResponse({**result, "message": message, "detail": message}, status_code=status)

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        current = current_config()
        return templates.TemplateResponse(
            request,
            "status.html",
            context(
                request,
                "status",
                services=service_status(current, config_path),
                output_root=str(output_root),
            ),
        )

    @app.post("/api/feedback", response_class=JSONResponse)
    async def save_feedback(
        arxiv_id: str = Form(...), verdict: str = Form(...), origin: str = Form(""),
        source_date: str = Form(""), scope: str = Form("paper"), terms: str = Form("")
    ) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if verdict not in VERDICTS:
            raise HTTPException(status_code=400, detail="无效的阅读反馈")
        current = current_config()
        item = selected_paper(arxiv_id, current, origin=origin, source_date=source_date)
        store = FeedbackStore(output_root, current.profile_id)
        try:
            entry = store.set(item.get("paper") or {}, verdict, scope=scope,
                              terms=re.split(r"[,，\n]+", terms))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        obsidian_synced = False
        obsidian_warning = ""
        if (
            current.obsidian.enabled
            and current.obsidian.auto_sync
            and current.obsidian.sync_feedback
        ):
            try:
                with ObsidianExporter(current, project_root) as exporter:
                    await run_in_threadpool(
                        exporter.sync_feedback,
                        item.get("paper") or {},
                        entry,
                        item.get("verified") or {},
                    )
                obsidian_synced = True
            except ObsidianError as exc:
                obsidian_warning = str(exc)
        return JSONResponse(
            {
                "feedback": entry,
                "in_library": False,
                "learned_term_count": len(
                    store.learned_terms(
                        PaperLibraryStore(output_root, current.profile_id).all()
                    )
                ),
                "message": "已排除此论文，并屏蔽命中所填主题词的论文，可在反馈记录中撤销" if scope == "topic" else "已仅排除此论文，可撤销；未新增主题屏蔽规则",
                "obsidian_synced": obsidian_synced,
                "obsidian_warning": obsidian_warning,
            }
        )

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback_page(request: Request, audit: str = ""):
        current = current_config()
        entries = FeedbackStore(output_root, current.profile_id).all()
        name_pattern = re.compile(re.escape(f"selection-{current.profile_id or 'default'}-") + r"[0-9a-f]{32}\.json")
        paths = sorted((p for p in output_root.glob("????-??-??/selection-*.json") if name_pattern.fullmatch(p.name)),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        runs = []
        payload: dict[str, Any] = {}
        for path in paths:
            key = path.relative_to(output_root).as_posix()
            runs.append({"id": key, "label": f"{path.parent.name} · {datetime.fromtimestamp(path.stat().st_mtime).strftime('%H:%M:%S')} · {path.stem[-6:]}"})
        selected = audit or (runs[0]["id"] if runs else "")
        if selected:
            if selected not in {r["id"] for r in runs}:
                raise HTTPException(status_code=404, detail="筛选记录不存在")
            payload = read_json(output_root / selected, {}) or {}
            if payload.get("profile_id", "") != current.profile_id:
                raise HTTPException(status_code=404, detail="筛选记录不属于当前方向")
        return templates.TemplateResponse(request, "feedback.html", context(
            request, "dashboard", feedback_entries=sorted(entries.values(), key=lambda e: e.get("updated_at", ""), reverse=True),
            audit_runs=runs, selected_audit=selected, selection_audit=payload))

    @app.post("/api/feedback/undo", response_class=JSONResponse)
    def undo_feedback(arxiv_id: str = Form(...), expected_updated_at: str = Form("")):
        current = current_config()
        if not ARXIV_ID_RE.fullmatch(arxiv_id):
            raise HTTPException(status_code=400, detail="无效的 arXiv ID")
        try:
            saved = FeedbackStore(output_root, current.profile_id).state.undo_feedback(base_id(arxiv_id), expected_updated_at)
        except ReadingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"feedback": None, "in_library": saved,
                             "message": "反馈已撤销" + ("，原收藏已恢复" if saved else "") + "；下一次推荐按当前规则重新筛选"})

    @app.post("/api/jobs/report", response_class=JSONResponse)
    def create_report_job(arxiv_id: str = Form(...), origin: str = Form(""),
                          source_date: str = Form(""), report_id: str = Form("")) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID，例如 2407.05600")

        task = JobContext.capture(current_config(), project_root)
        current = task.config
        if origin not in {"", "recommendation", "library", "report"}:
            raise HTTPException(status_code=400, detail="无效的论文来源")
        snapshot = None
        if origin:
            if origin == "recommendation" and not DATE_DIR_RE.fullmatch(source_date):
                raise HTTPException(status_code=400, detail="历史推荐需要有效的推荐日期")
            item = selected_paper(arxiv_id, current, origin=origin,
                                  source_date=source_date, report_id=report_id)
            if not item:
                raise HTTPException(status_code=404, detail="没有找到对应的论文快照")
            snapshot = Paper.from_dict(item["paper"])
            if not snapshot.version:
                raise HTTPException(status_code=409, detail="这条历史记录没有可靠版本号；请在生成页面输入 ID 获取最新版本，或输入指定版本号")

        job = jobs.submit_request(f"{current.profile_name} · arXiv:{arxiv_id}",
                          capture_request("report", current, project_root, arxiv_id=arxiv_id,
                                          snapshot=snapshot.to_dict() if snapshot else None),
                          identity=task.identity("report", arxiv_id=arxiv_id, origin=origin,
                                                 source_date=source_date, report_id=report_id,
                                                 snapshot=snapshot.to_dict() if snapshot else None))
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/digest", response_class=JSONResponse)
    def create_digest_job(
        force: bool = Form(False),
        send_email: bool = Form(False),
    ) -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        current = task.config
        detail = f"{current.profile_id or 'default'} · {'刷新并发送邮件' if send_email else '仅刷新本地推荐'} · {'强制' if force else '仅新论文'}"
        job = jobs.submit_request(detail,
                          capture_request("digest", current, project_root, force=force, send_email=send_email),
                          identity=task.identity("digest", force=force, send_email=send_email))
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/weekly", response_class=JSONResponse)
    def create_weekly_job(include_notes: bool = Form(False)) -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        current = task.config
        cutoff = datetime.now(ZoneInfo(current.timezone))
        job = jobs.submit_request(current.profile_name,
                          capture_request("weekly", current, project_root, now=cutoff.isoformat(), include_notes=include_notes),
                          identity=task.identity("weekly", include_notes=include_notes))
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/versions", response_class=JSONResponse)
    def create_versions_job() -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        current = task.config
        def run_versions() -> Path:
            with VersionTracker(current, project_root) as tracker:
                return tracker.check()

        job = jobs.submit("versions", current.profile_name, run_versions,
                          identity=task.identity("versions"), profile_id=current.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/version-batches/preview", response_class=JSONResponse)
    def preview_version_batch(arxiv_ids: list[str] = Form(...), report: bool = Form(False),
                              zotero: bool = Form(False), obsidian: bool = Form(False)) -> JSONResponse:
        current = current_config()
        service = VersionSyncBatch(current, project_root)
        try:
            state = service.preview(arxiv_ids, version_tracking_data(service.output_root, current.profile_id)["items"],
                                    report=report, zotero=zotero, obsidian=obsidian)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"id": state["id"], "profile_name": state["profile_name"], "items": state["items"],
                             "options": state["options"], "summary": service.summary(state),
                             "model": current.llm.model if report or obsidian else "",
                             "zotero_collection": current.zotero.collection_name if zotero else "",
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

    @app.post("/api/jobs/obsidian", response_class=JSONResponse)
    def create_obsidian_job() -> JSONResponse:
        task = JobContext.capture(current_config(), project_root)
        current = task.config
        def run_obsidian() -> Path:
            with ObsidianExporter(current, project_root) as exporter:
                return exporter.sync_all()

        job = jobs.submit(
            "obsidian", current.profile_name, run_obsidian,
            identity=task.identity("obsidian"), profile_id=current.profile_id
        )
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.delete("/api/jobs/completed", response_class=JSONResponse)
    def clear_completed_jobs() -> JSONResponse:
        return JSONResponse({"deleted": jobs.clear_finished()})

    @app.get("/api/jobs/{job_id}", response_class=JSONResponse)
    def job_status(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或记录无法读取")
        return JSONResponse(asdict(job))

    @app.post("/api/jobs/{job_id}/retry", response_class=JSONResponse)
    def retry_job(job_id: str) -> JSONResponse:
        try:
            job = jobs.retry(job_id, current_config().profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/{job_id}/cancel", response_class=JSONResponse)
    def cancel_job(job_id: str) -> JSONResponse:
        job = jobs.cancel(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或记录无法读取")
        if job.status not in {"cancelled", "cancelling"}:
            raise HTTPException(status_code=409, detail="任务已经结束，无法取消")
        return JSONResponse(asdict(job))

    return app
