from __future__ import annotations

import importlib.util
import os
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import __version__
from .abstracts import localize_abstracts
from .config import AppConfig, load_config
from .citation_graph import CitationExplorer
from .feedback import FeedbackStore, VERDICTS
from .library import PaperLibraryStore
from .models import Paper
from .paper_data import local_paper_item
from .obsidian import ObsidianError, ObsidianExporter, discover_obsidian_vaults
from .pipeline import DailyPipeline
from .profiles import ProfileGenerator, ProfileManager
from .utils import read_json
from .weekly import WeeklySynthesizer
from .version_tracker import VersionTracker
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
    result_artifact_url,
    version_tracking_data,
    weekly_library,
)
from .web_jobs import BackgroundJob, JobContext, JobManager
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
    jobs = JobManager(output_root, config.jobs.max_parallel)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
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
    app.mount("/static", StaticFiles(directory=str(package_root / "static")), name="static")
    app.mount("/artifacts", StaticFiles(directory=str(output_root), html=True), name="artifacts")

    @app.middleware("http")
    async def enforce_local_browser_boundary(request: Request, call_next):
        local_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            origin_host = urlparse(origin).hostname if origin else None
            if (origin_host and origin_host not in local_hosts) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "已拒绝跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/settings"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def context(request: Request, active: str, **values: Any) -> dict[str, Any]:
        current = load_config(config_path)
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
            "active_profile": profiles.active_id(),
            "feedback": FeedbackStore(output_root, current.profile_id).all(),
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
        current = current or load_config(config_path)
        item = local_paper_item(output_root, current.profile_id, arxiv_id)
        if item:
            localize_abstracts(current, output_root, [item])
            return item
        raise HTTPException(status_code=404, detail="当前推荐中没有这篇论文")

    def add_to_paper_library(arxiv_id: str, current: AppConfig) -> dict[str, Any]:
        item = recommendation_item(arxiv_id, current)
        localize_abstracts(current, output_root, [item])
        entry = PaperLibraryStore(output_root, current.profile_id).add(
            item,
            current.profile_name,
            source_date=str(item.get("_source_date") or ""),
        )
        return entry

    def cached_zotero_pdf(paper: dict[str, Any], enabled: bool) -> Path | None:
        if not enabled:
            return None
        arxiv_id = str(paper.get("arxiv_id") or "").replace("/", "-")
        destination = output_root / "zotero-cache" / f"{arxiv_id}.pdf"
        if destination.exists() and destination.stat().st_size > 0:
            return destination
        pdf_url = str(paper.get("pdf_url") or "")
        if not pdf_url:
            return None
        if urlparse(pdf_url).hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise ZoteroError("PDF 地址不是可信的 arXiv 地址")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".pdf.part")
        with httpx.stream(
            "GET",
            pdf_url,
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": f"PaperLoom/{__version__} (personal research use)"},
        ) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        temporary.replace(destination)
        return destination

    def save_recommendation_to_zotero(
        item: dict[str, Any], collection_key: str = ""
    ) -> dict[str, Any]:
        current = load_config(config_path)
        paper = item.get("paper") or {}
        client = ZoteroClient(current.zotero)
        connection = client.status()
        if not connection.get("ready"):
            raise ZoteroUnavailable("Zotero 未运行或本地 API 未启用")
        if not os.getenv(current.zotero.api_key_env):
            raise ZoteroAuthorizationRequired("请先在配置中心连接并授权 Zotero")
        try:
            pdf_path = cached_zotero_pdf(paper, current.zotero.attach_pdf)
        except (httpx.HTTPError, OSError) as exc:
            raise ZoteroError(f"论文 PDF 下载失败：{type(exc).__name__}") from exc
        result = client.save_paper(
            paper,
            item.get("verified") or {},
            current.profile_name,
            pdf_path=pdf_path,
            collection_key=collection_key or None,
        )
        return result.to_dict()

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, date: str = "") -> HTMLResponse:
        current = load_config(config_path)
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
        reports_by_id: dict[tuple, dict[str, Any]] = {}
        for report in report_library(output_root):
            if report.get("profile_id") in ("", current.profile_id):
                reports_by_id.setdefault((str(report.get("arxiv_id") or ""), report.get("version")), report)
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
                recent_reports=report_library(output_root)[:4],
                recent_weekly=weekly_library(output_root, current.profile_id)[:2],
            ),
        )

    @app.get("/library", response_class=HTMLResponse)
    def paper_library(request: Request, q: str = "") -> HTMLResponse:
        current = load_config(config_path)
        store = PaperLibraryStore(output_root, current.profile_id)
        feedback = FeedbackStore(output_root, current.profile_id).all()
        reports_by_id: dict[tuple, dict[str, Any]] = {}
        for report in report_library(output_root):
            if report.get("profile_id") in ("", current.profile_id):
                reports_by_id.setdefault((str(report.get("arxiv_id") or ""), report.get("version")), report)
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
                        " ".join(
                            str(author.get("name") or "")
                            for author in ((entry.get("paper") or {}).get("authors") or [])
                        ),
                    ]
                ).casefold()
            ]
        entries.sort(key=lambda entry: str(entry.get("saved_at") or ""), reverse=True)
        return templates.TemplateResponse(
            request,
            "library.html",
            context(
                request,
                "library",
                entries=entries,
                query=q.strip(),
            ),
        )

    @app.post("/api/library/toggle", response_class=JSONResponse)
    def toggle_paper_library(arxiv_id: str = Form(...)) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID")
        current = load_config(config_path)
        store = PaperLibraryStore(output_root, current.profile_id)
        previous = store.all().get(arxiv_id)
        item = previous or recommendation_item(arxiv_id, current)
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
        current = load_config(config_path)
        saved = PaperLibraryStore(output_root, current.profile_id).contains(
            arxiv_id.strip()
        )
        return JSONResponse({"saved": saved})

    @app.post("/api/library/add", response_class=JSONResponse)
    def add_paper_library(arxiv_id: str = Form(...)) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID")
        current = load_config(config_path)
        store = PaperLibraryStore(output_root, current.profile_id)
        already_saved = store.contains(arxiv_id)
        if not already_saved:
            add_to_paper_library(arxiv_id, current)
        else:
            item = recommendation_item(arxiv_id, current)
            localize_abstracts(current, output_root, [item])
            store.add(
                item,
                current.profile_name,
                source_date=str((store.all().get(arxiv_id) or {}).get("source_date") or ""),
            )
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
        current = load_config(config_path)
        PaperLibraryStore(output_root, current.profile_id).remove(arxiv_id.strip())
        return RedirectResponse("/library", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, q: str = "") -> HTMLResponse:
        current = load_config(config_path)
        items = report_library(output_root)
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
            context(request, "reports", reports=items, query=q),
        )

    @app.get("/generate", response_class=HTMLResponse)
    def generate(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "generate.html", context(request, "generate"))

    @app.get("/weekly", response_class=HTMLResponse)
    def weekly_page(request: Request) -> HTMLResponse:
        current = load_config(config_path)
        return templates.TemplateResponse(
            request,
            "weekly.html",
            context(
                request,
                "weekly",
                weekly_reports=weekly_library(output_root, current.profile_id),
            ),
        )

    @app.get("/versions", response_class=HTMLResponse)
    def versions_page(request: Request) -> HTMLResponse:
        current = load_config(config_path)
        tracking = version_tracking_data(output_root, current.profile_id)
        return templates.TemplateResponse(
            request,
            "versions.html",
            context(request, "versions", tracking=tracking),
        )

    @app.get("/citations", response_class=HTMLResponse)
    def citations_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "citations.html",
            context(request, "citations", citation_maps=citation_library(output_root)),
        )

    @app.get("/profiles", response_class=HTMLResponse)
    def profile_page(request: Request, saved: str = "", switched: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "profiles.html",
            context(
                request,
                "profiles",
                profiles=profiles.list(),
                saved=saved,
                switched=switched,
            ),
        )

    @app.post("/profiles/create")
    async def create_profile(request: Request) -> RedirectResponse:
        form = await request.form()
        name = str(form.get("name", "")).strip()
        description = str(form.get("description", "")).strip()
        keywords = _list_field(str(form.get("keywords", "")))
        negative_keywords = _list_field(str(form.get("negative_keywords", "")))
        reference_ids = _list_field(str(form.get("reference_ids", "")))
        if not name:
            raise HTTPException(status_code=400, detail="请输入研究方向名称")
        if not keywords and not reference_ids:
            raise HTTPException(status_code=400, detail="关键词和参考论文至少填写一项")
        invalid_ids = [item for item in reference_ids if not ARXIV_ID_RE.match(item)]
        if invalid_ids:
            raise HTTPException(
                status_code=400,
                detail=f"无效的 arXiv ID：{', '.join(invalid_ids)}",
            )
        try:
            recommendation_count = _int_value(form, "recommendation_count", 1, 50)
            profile_id = profiles.unique_id(name)
            with ProfileGenerator(load_config(config_path)) as generator:
                payload = await run_in_threadpool(
                    generator.generate,
                    profile_id,
                    name,
                    keywords,
                    negative_keywords,
                    reference_ids,
                    description,
                    recommendation_count,
                )
            profiles.save(payload)
            if "activate" in form:
                profiles.activate(profile_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"研究方向生成失败：{type(exc).__name__}: {exc}",
            ) from exc
        return RedirectResponse(
            f"/profiles?saved={quote(profile_id)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

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
        current = load_config(config_path)
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
        try:
            values = build_config_update(form)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        discovery = values.pop("discovery")
        ranking = values.pop("ranking")
        save_config_settings(config_path, values)
        profiles.update_active_search(discovery, ranking)
        jobs.set_max_parallel(load_config(config_path).jobs.max_parallel)
        return RedirectResponse("/settings?saved=config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/settings/credentials")
    async def update_credentials(request: Request) -> RedirectResponse:
        form = await request.form()
        current = load_config(config_path)
        updates: dict[str, str] = {}
        clear: set[str] = set()
        for item in credential_specs(current):
            field = item["field"]
            env_name = item["env_name"]
            value = str(form.get(field, ""))
            if value:
                updates[env_name] = value.strip() if not item["secret"] else value
            elif f"clear_{field}" in form:
                clear.add(env_name)
        clear.difference_update(updates)
        try:
            update_dotenv(project_root / ".env", updates, clear)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/settings?saved=credentials", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/api/zotero/authorize", response_class=JSONResponse)
    async def authorize_zotero() -> JSONResponse:
        current = load_config(config_path)
        try:
            key = await run_in_threadpool(ZoteroClient(current.zotero).authorize)
            update_dotenv(project_root / ".env", {current.zotero.api_key_env: key})
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        return JSONResponse({"message": "Zotero 已连接并获得本地写入授权"})

    @app.get("/api/zotero/collections", response_class=JSONResponse)
    def zotero_collections() -> JSONResponse:
        current = load_config(config_path)
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
        arxiv_id: str = Form(...), collection_key: str = Form("")
    ) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID")
        item = recommendation_item(arxiv_id)
        try:
            result = await run_in_threadpool(
                save_recommendation_to_zotero, item, collection_key
            )
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        message = "已创建 Zotero 条目" if result["created"] else "Zotero 中已存在该论文"
        if result["attachments_added"]:
            message += f"，新增 {result['attachments_added']} 个附件"
        return JSONResponse({**result, "message": message})

    @app.post("/api/zotero/save-today", response_class=JSONResponse)
    async def save_zotero_today(
        collection_key: str = Form(""), date_label: str = Form("")
    ) -> JSONResponse:
        current = load_config(config_path)
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
                save_recommendation_to_zotero(item, collection_key)
                for item in recommendations
            ]

        try:
            results = await run_in_threadpool(save_all)
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        created = sum(1 for item in results if item["created"])
        existing = len(results) - created
        attachments = sum(int(item["attachments_added"]) for item in results)
        return JSONResponse(
            {
                "created": created,
                "existing": existing,
                "attachments_added": attachments,
                "message": f"Zotero 同步完成：新增 {created} 篇，已存在 {existing} 篇，新增附件 {attachments} 个",
            }
        )

    @app.post("/api/zotero/save-report", response_class=JSONResponse)
    async def save_zotero_report(
        arxiv_id: str = Form(...), collection_key: str = Form("")
    ) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        report = next(
            (item for item in report_library(output_root) if item["arxiv_id"] == arxiv_id),
            None,
        )
        if not report:
            raise HTTPException(status_code=404, detail="没有找到这篇论文的本地报告")

        def save_report() -> dict[str, Any]:
            payload = read_json(report["metadata_path"], {}) or {}
            current = load_config(config_path)
            result = ZoteroClient(current.zotero).save_paper(
                payload.get("paper") or {},
                payload.get("verified") or {},
                current.profile_name,
                report_path=report["report_path"],
                pdf_path=report["pdf_path"] if report["pdf_path"].exists() else None,
                collection_key=collection_key or None,
            )
            return result.to_dict()

        try:
            result = await run_in_threadpool(save_report)
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        message = "报告已保存到 Zotero"
        if result["attachments_added"]:
            message += f"，新增 {result['attachments_added']} 个附件"
        return JSONResponse({**result, "message": message})

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        current = load_config(config_path)
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
        arxiv_id: str = Form(...), verdict: str = Form(...)
    ) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if verdict not in VERDICTS:
            raise HTTPException(status_code=400, detail="无效的阅读反馈")
        current = load_config(config_path)
        item = recommendation_item(arxiv_id, current)
        store = FeedbackStore(output_root, current.profile_id)
        try:
            entry = store.set(item.get("paper") or {}, verdict)
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
                "message": "已标记为不相关，后续会降低类似论文的推荐优先级",
                "obsidian_synced": obsidian_synced,
                "obsidian_warning": obsidian_warning,
            }
        )

    @app.post("/api/jobs/report", response_class=JSONResponse)
    def create_report_job(arxiv_id: str = Form(...), origin: str = Form(""),
                          source_date: str = Form("")) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID，例如 2407.05600")

        task = JobContext.capture(load_config(config_path), project_root)
        current = task.config
        if origin not in {"", "recommendation", "library"}:
            raise HTTPException(status_code=400, detail="无效的论文来源")
        snapshot = None
        if origin:
            if origin == "recommendation" and not DATE_DIR_RE.fullmatch(source_date):
                raise HTTPException(status_code=400, detail="历史推荐需要有效的推荐日期")
            item = local_paper_item(output_root, current.profile_id, arxiv_id,
                                    source_date=source_date, origin=origin)
            if not item:
                raise HTTPException(status_code=404, detail="没有找到对应的论文快照")
            snapshot = Paper.from_dict(item["paper"])
            if not snapshot.version:
                raise HTTPException(status_code=409, detail="这条历史记录没有可靠版本号；请在生成页面输入 ID 获取最新版本，或输入指定版本号")

        def run_report() -> Path:
            with DailyPipeline(current, project_root) as pipeline:
                return pipeline.report_arxiv_id(arxiv_id, snapshot=snapshot)

        job = jobs.submit("report", f"{current.profile_name} · arXiv:{arxiv_id}", run_report,
                          identity=task.identity("report", arxiv_id=arxiv_id, origin=origin,
                                                 source_date=source_date, snapshot=snapshot.to_dict() if snapshot else None),
                          profile_id=current.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/digest", response_class=JSONResponse)
    def create_digest_job(
        force: bool = Form(False),
        send_email: bool = Form(False),
    ) -> JSONResponse:
        task = JobContext.capture(load_config(config_path), project_root)
        current = task.config
        def run_digest() -> Path:
            with DailyPipeline(current, project_root) as pipeline:
                return pipeline.run(force=force, demo=False, deliver=send_email)

        detail = f"{current.profile_id or 'default'} · {'刷新并发送邮件' if send_email else '仅刷新本地推荐'} · {'强制' if force else '仅新论文'}"
        job = jobs.submit("digest", detail, run_digest,
                          identity=task.identity("digest", force=force, send_email=send_email), profile_id=current.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/weekly", response_class=JSONResponse)
    def create_weekly_job() -> JSONResponse:
        task = JobContext.capture(load_config(config_path), project_root)
        current = task.config
        def run_weekly() -> Path:
            with WeeklySynthesizer(current, project_root) as synthesizer:
                return synthesizer.generate()

        job = jobs.submit("weekly", current.profile_name, run_weekly,
                          identity=task.identity("weekly"), profile_id=current.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/versions", response_class=JSONResponse)
    def create_versions_job() -> JSONResponse:
        task = JobContext.capture(load_config(config_path), project_root)
        current = task.config
        def run_versions() -> Path:
            with VersionTracker(current, project_root) as tracker:
                return tracker.check()

        job = jobs.submit("versions", current.profile_name, run_versions,
                          identity=task.identity("versions"), profile_id=current.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/citation", response_class=JSONResponse)
    def create_citation_job(arxiv_id: str = Form(...)) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if not ARXIV_ID_RE.match(arxiv_id):
            raise HTTPException(status_code=400, detail="请输入有效的 arXiv ID")

        task = JobContext.capture(load_config(config_path), project_root)
        current = task.config

        def run_citation() -> Path:
            with CitationExplorer(current, project_root) as explorer:
                return explorer.generate(arxiv_id)

        job = jobs.submit("citation", f"{current.profile_name} · arXiv:{arxiv_id}", run_citation,
                          identity=task.identity("citation", arxiv_id=arxiv_id), profile_id=current.profile_id)
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.post("/api/jobs/obsidian", response_class=JSONResponse)
    def create_obsidian_job() -> JSONResponse:
        task = JobContext.capture(load_config(config_path), project_root)
        current = task.config
        def run_obsidian() -> Path:
            with ObsidianExporter(current, project_root) as exporter:
                return exporter.sync_all()

        job = jobs.submit(
            "obsidian", current.profile_name, run_obsidian,
            identity=task.identity("obsidian"), profile_id=current.profile_id
        )
        return JSONResponse(asdict(job), status_code=status.HTTP_202_ACCEPTED)

    @app.get("/api/jobs/{job_id}", response_class=JSONResponse)
    def job_status(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或 GUI 已重启")
        return JSONResponse(asdict(job))

    return app
