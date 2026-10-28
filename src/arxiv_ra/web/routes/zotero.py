"""zotero page adapters."""
from __future__ import annotations

from ...config import AppConfig
from ...profiles.profiles import ProfileManager
from fastapi import FastAPI
from pathlib import Path
from typing import Callable
from ...integrations import zotero as zotero_adapter
import copy
from typing import Any
from fastapi import Form, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from ...papers.paper_data import local_paper_item
from ...reports.report_store import html_reports
from ...integrations.zotero import ZoteroClient, ZoteroError
from ..web_catalog import DATE_DIR_RE, latest_recommendations, recommendations_for_date
from ...runtime.model_access.settings import update_dotenv

from ..validation import ARXIV_ID_RE


def register_routes(
    app: FastAPI,
    current_config: Callable[[], AppConfig],
    output_root: Path,
    profiles: ProfileManager,
    project_root: Path,
    raise_zotero_http_error: Callable[[Exception], None],
    recommendation_item: Callable[..., dict[str, Any]],
    save_recommendation_to_zotero: Callable[..., dict[str, Any]],
) -> None:
    @app.post("/api/zotero/authorize", response_class=JSONResponse)
    async def authorize_zotero() -> JSONResponse:
        current = current_config()
        try:
            key = await run_in_threadpool(zotero_adapter.ZoteroClient(current.zotero).authorize)
            update_dotenv(project_root / ".env", {current.zotero.api_key_env: key})
        except ZoteroError as exc:
            raise_zotero_http_error(exc)
        return JSONResponse({"message": "Zotero 已连接并获得本地写入授权"})

    @app.get("/api/zotero/collections", response_class=JSONResponse)
    def zotero_collections() -> JSONResponse:
        current = current_config()
        client = zotero_adapter.ZoteroClient(current.zotero, timeout=5.0)
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
            from ...integrations.collaboration import PaperCollection, zotero_result
            source_config = copy.deepcopy(current)
            source_config.profile_name = profile_name or current.profile_name
            operation = PaperCollection(source_config, project_root).collect(
                payload, zotero=True, report_path=report.markdown_path,
                pdf_path=report.pdf_path if report.pdf_path.exists() else None,
                collection_key=collection_key, zotero_factory=zotero_adapter.ZoteroClient)
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
