"""Connection readiness shown by the local application."""
from __future__ import annotations
from ..integrations import zotero as zotero_adapter
import importlib.util
import os
from pathlib import Path
from typing import Any
from ..config import AppConfig
from ..integrations.obsidian import ObsidianExporter
from ..integrations.zotero import ZoteroClient


def service_status(config: AppConfig, config_path: Path) -> list[dict[str, Any]]:
    zotero = zotero_adapter.ZoteroClient(config.zotero, timeout=2.0).status()
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
