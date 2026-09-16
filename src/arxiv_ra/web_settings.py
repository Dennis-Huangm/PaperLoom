from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .config import AppConfig
from .utils import atomic_write_text


ARXIV_ID_RE = re.compile(r"^(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$", re.I)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _list_field(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\r\n,]+", value) if item.strip()]


def _concept_groups_field(value: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for line in value.splitlines():
        terms = [item.strip() for item in re.split(r"[,|;]+", line) if item.strip()]
        if terms:
            groups.append(terms)
    return groups


def _int_value(form: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        value = int(str(form.get(name, "")).strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _float_value(form: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        value = float(str(form.get(name, "")).strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _env_name(form: Any, name: str) -> str:
    value = str(form.get(name, "")).strip()
    if not ENV_NAME_RE.match(value):
        raise ValueError(f"{name} 不是有效的环境变量名")
    return value


def build_config_update(form: Any) -> dict[str, Any]:
    provider = str(form.get("discovery_provider", "auto")).strip()
    if provider not in {"auto", "arxiv", "hybrid"}:
        raise ValueError("无效的检索来源模式")
    alphaxiv_endpoint = str(
        form.get("alphaxiv_endpoint", "https://api.alphaxiv.org/mcp/v1")
    ).strip().rstrip("/")
    alphaxiv_url = urlparse(alphaxiv_endpoint)
    if alphaxiv_url.scheme != "https" or alphaxiv_url.hostname != "api.alphaxiv.org":
        raise ValueError("alphaXiv 地址必须是 https://api.alphaxiv.org")
    timezone = str(form.get("timezone", "")).strip()
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone 不是有效的 IANA 时区") from exc
    output_dir = str(form.get("output_dir", "")).strip()
    if not output_dir:
        raise ValueError("output_dir 不能为空")
    llm_model = str(form.get("llm_model", "")).strip()
    if not llm_model:
        raise ValueError("llm_model 不能为空")
    pdf_parser = str(form.get("pdf_parser", "")).strip()
    if not pdf_parser:
        raise ValueError("pdf_parser 不能为空")
    email_enabled = "email_enabled" in form
    smtp_host = str(form.get("smtp_host", "")).strip()
    if email_enabled and not smtp_host:
        raise ValueError("启用邮件发送时 smtp_host 不能为空")
    categories = _list_field(str(form.get("arxiv_categories", "")))
    if not categories:
        raise ValueError("至少需要一个 arXiv 类别")
    seeds = _list_field(str(form.get("seed_papers", "")))
    invalid_seeds = [seed for seed in seeds if not ARXIV_ID_RE.match(seed)]
    if invalid_seeds:
        raise ValueError(f"无效的种子论文 ID：{', '.join(invalid_seeds)}")
    max_candidates = _int_value(form, "max_candidates", 1, 5000)
    prefilter_count = _int_value(form, "prefilter_count", 1, max_candidates)
    recommendation_count = _int_value(form, "recommendation_count", 1, prefilter_count)
    concept_groups = _concept_groups_field(str(form.get("concept_groups", "")))
    minimum_concept_groups = _int_value(form, "minimum_concept_groups", 0, len(concept_groups))
    zotero_mode = str(form.get("zotero_mode", "local")).strip()
    if zotero_mode != "local":
        raise ValueError("当前版本仅支持 Zotero local 模式")
    zotero_base_url = str(form.get("zotero_base_url", "")).strip().rstrip("/")
    zotero_url = urlparse(zotero_base_url)
    if (
        zotero_url.scheme != "http"
        or zotero_url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or zotero_url.username
        or zotero_url.password
    ):
        raise ValueError("Zotero 本地 API 必须使用 127.0.0.1 或 localhost")
    pdf_attachment_mode = str(form.get("zotero_pdf_attachment_mode", "imported_file")).strip()
    if pdf_attachment_mode not in {"imported_file", "linked_file"}:
        raise ValueError("无效的 Zotero PDF 附件模式")
    obsidian_enabled = "obsidian_enabled" in form
    obsidian_vault_path = str(form.get("obsidian_vault_path", "")).strip()
    if obsidian_enabled:
        vault = Path(obsidian_vault_path).expanduser()
        if not vault.is_absolute() or not vault.is_dir():
            raise ValueError("启用 Obsidian 时 vault 必须是存在的绝对目录")
    obsidian_folders: dict[str, str] = {}
    for field in (
        "root_folder",
        "home_folder",
        "daily_folder",
        "papers_folder",
        "weekly_folder",
        "profiles_folder",
        "concepts_folder",
        "attachments_folder",
        "indexes_folder",
    ):
        value = str(form.get(f"obsidian_{field}", "")).strip()
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"obsidian_{field} 必须是安全的相对目录")
        obsidian_folders[field] = value
    return {
        "timezone": timezone,
        "output_dir": output_dir,
        "discovery": {
            "provider": provider,
            "alphaxiv_fallback_enabled": "alphaxiv_fallback_enabled" in form,
            "alphaxiv_endpoint": alphaxiv_endpoint,
            "alphaxiv_api_key_env": _env_name(form, "alphaxiv_api_key_env"),
            "alphaxiv_difficulty": _int_value(form, "alphaxiv_difficulty", 1, 10),
            "alphaxiv_max_candidates": _int_value({"value": form.get("alphaxiv_max_candidates", "15")}, "value", 1, 15),
            "alphaxiv_minimum_concept_groups": _int_value({"value": form.get("alphaxiv_minimum_concept_groups", "1")}, "value", 0, 100),
            "interest_description": str(form.get("interest_description", "")).strip(),
            "lookback_days": _int_value(form, "lookback_days", 1, 365),
            "max_candidates": max_candidates,
            "prefilter_count": prefilter_count,
            "recommendation_count": recommendation_count,
            "min_score": _float_value(form, "min_score", 0, 100),
            "arxiv_categories": categories,
            "arxiv_query_terms": _list_field(str(form.get("arxiv_query_terms", ""))),
            "positive_keywords": _list_field(str(form.get("positive_keywords", ""))),
            "negative_keywords": _list_field(str(form.get("negative_keywords", ""))),
            "seed_papers": seeds,
            "concept_groups": concept_groups,
            "minimum_concept_groups": minimum_concept_groups,
        },
        "ranking": {
            "category_weight": _float_value(form, "category_weight", 0, 100),
            "keyword_weight": _float_value(form, "keyword_weight", 0, 100),
            "negative_weight": _float_value(form, "negative_weight", 0, 100),
            "recency_weight": _float_value(form, "recency_weight", 0, 100),
            "llm_rerank": "llm_rerank" in form,
            "llm_min_score": _float_value(form, "llm_min_score", 0, 10),
        },
        "llm": {
            "model": llm_model,
            "api_key_env": _env_name(form, "llm_api_key_env"),
            "base_url_env": _env_name(form, "llm_base_url_env"),
            "temperature": _float_value(form, "llm_temperature", 0, 2),
            "max_chunk_chars": _int_value(form, "max_chunk_chars", 4000, 100000),
        },
        "metadata": {
            "openalex_email": str(form.get("openalex_email", "")).strip(),
            "openalex_api_key_env": _env_name(form, "openalex_api_key_env"),
            "semantic_scholar_api_key_env": _env_name(form, "semantic_scholar_api_key_env"),
            "semantic_scholar_min_interval": _float_value(
                form, "semantic_scholar_min_interval", 0, 60
            ),
            "semantic_scholar_max_retries": _int_value(
                form, "semantic_scholar_max_retries", 0, 10
            ),
            "verify_venue": "verify_venue" in form,
        },
        "pdf": {
            "parser": pdf_parser,
            "use_docling_if_available": "use_docling_if_available" in form,
            "max_pages": _int_value(form, "max_pages", 1, 500),
            "figure_dpi": _int_value(form, "figure_dpi", 72, 600),
        },
        "delivery": {
            "email_enabled": email_enabled,
            "smtp_host": smtp_host,
            "smtp_port": _int_value(form, "smtp_port", 1, 65535),
            "smtp_ssl": "smtp_ssl" in form,
            "email_address_env": _env_name(form, "email_address_env"),
            "username": str(form.get("smtp_username", "")).strip(),
            "password_env": _env_name(form, "smtp_password_env"),
            "from_address": str(form.get("from_address", "")).strip(),
            "to_addresses": _list_field(str(form.get("to_addresses", ""))),
        },
        "zotero": {
            "enabled": "zotero_enabled" in form,
            "mode": zotero_mode,
            "base_url": zotero_base_url,
            "api_key_env": _env_name(form, "zotero_api_key_env"),
            "collection_name": str(form.get("zotero_collection_name", "")).strip(),
            "attach_pdf": "zotero_attach_pdf" in form,
            "pdf_attachment_mode": pdf_attachment_mode,
            "attach_report": "zotero_attach_report" in form,
            "add_profile_tag": "zotero_add_profile_tag" in form,
        },
        "obsidian": {
            "enabled": obsidian_enabled,
            "vault_path": obsidian_vault_path,
            **obsidian_folders,
            "auto_sync": "obsidian_auto_sync" in form,
            "sync_daily": "obsidian_sync_daily" in form,
            "sync_reports": "obsidian_sync_reports" in form,
            "sync_weekly": "obsidian_sync_weekly" in form,
            "sync_feedback": "obsidian_sync_feedback" in form,
            "copy_figures": "obsidian_copy_figures" in form,
            "copy_pdf": "obsidian_copy_pdf" in form,
            "generate_concepts": "obsidian_generate_concepts" in form,
        },
        "jobs": {
            "max_parallel": _int_value(form, "jobs_max_parallel", 1, 8),
        },
        "weekly": {
            "enabled": "weekly_enabled" in form,
            "days": _int_value(form, "weekly_days", 1, 31),
            "max_papers": _int_value(form, "weekly_max_papers", 1, 200),
            "include_deep_reports": "weekly_include_deep_reports" in form,
            "auto_generate": "weekly_auto_generate" in form,
            "weekday": _int_value(form, "weekly_weekday", 0, 6),
        },
        "version_tracking": {
            "enabled": "version_tracking_enabled" in form,
            "auto_check": "version_tracking_auto_check" in form,
            "max_tracked": _int_value(form, "version_tracking_max_tracked", 1, 500),
            "include_reports": "version_tracking_include_reports" in form,
            "include_feedback": "version_tracking_include_feedback" in form,
            "include_zotero": "version_tracking_include_zotero" in form,
            "analyze_pdf_diff": "version_tracking_analyze_pdf_diff" in form,
        },
        "citations": {
            "enabled": "citations_enabled" in form,
            "max_references": _int_value(form, "citations_max_references", 1, 100),
            "max_citations": _int_value(form, "citations_max_citations", 1, 100),
            "max_similar": _int_value(form, "citations_max_similar", 1, 100),
            "min_interval": _float_value(form, "citations_min_interval", 0, 60),
            "max_retries": _int_value(form, "citations_max_retries", 0, 10),
        },
    }


def save_config_settings(config_path: Path, values: dict[str, Any]) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    for key, value in values.items():
        if isinstance(value, dict):
            payload.setdefault(key, {}).update(value)
        else:
            payload[key] = value
    atomic_write_text(
        config_path,
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
    )


def save_discovery_settings(config_path: Path, values: dict[str, Any]) -> None:
    save_config_settings(config_path, {"discovery": values})


def update_dotenv(path: Path, updates: dict[str, str], clear: set[str] | None = None) -> None:
    clear = clear or set()
    for key, value in updates.items():
        if not ENV_NAME_RE.match(key):
            raise ValueError(f"无效的环境变量名：{key}")
        if any(character in value for character in "\r\n\0"):
            raise ValueError(f"{key} 包含不允许的换行或空字符")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    assignments = {
        match.group(1): index
        for index, line in enumerate(lines)
        if (match := re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line))
    }
    for key in clear:
        if key in assignments:
            lines[assignments[key]] = ""
        os.environ.pop(key, None)
    for key, value in updates.items():
        rendered = f"{key}={value}"
        if key in assignments:
            lines[assignments[key]] = rendered
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(rendered)
        os.environ[key] = value
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")


def credential_specs(config: AppConfig) -> list[dict[str, Any]]:
    definitions = [
        ("llm_api_key", "LLM API Key", config.llm.api_key_env, True, "模型服务访问密钥"),
        ("llm_base_url", "LLM Base URL", config.llm.base_url_env, False, "OpenAI-compatible API 地址；官方 OpenAI 可留空"),
        ("openalex_api_key", "OpenAlex API Key", config.metadata.openalex_api_key_env, True, "用于提升 OpenAlex 请求额度"),
        ("semantic_scholar_api_key", "Semantic Scholar API Key", config.metadata.semantic_scholar_api_key_env, True, "可选的学术元数据增强源"),
        ("alphaxiv_api_key", "alphaXiv API Key", config.discovery.alphaxiv_api_key_env, True, "主动语义发现与备用检索；在 alphaXiv Settings → API Keys 创建"),
        ("email_address", "QQ 邮箱地址", config.delivery.email_address_env, False, "SMTP 登录、默认发件人与收件地址"),
        ("smtp_password", "QQ SMTP 授权码", config.delivery.password_env, True, "QQ 邮箱生成的 SMTP 授权码，不是登录密码"),
        ("zotero_api_key", "Zotero 本地写入授权", config.zotero.api_key_env, True, "由 Zotero 授权窗口自动生成，通常无需手工填写"),
    ]
    return [
        {
            "field": field,
            "label": label,
            "env_name": env_name,
            "secret": secret,
            "help": help_text,
            "configured": bool(os.getenv(env_name)),
        }
        for field, label, env_name, secret, help_text in definitions
    ]
