from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import yaml


@dataclass(slots=True)
class DiscoveryConfig:
    # `auto` keeps arXiv as the source of truth and uses alphaXiv only after
    # arXiv API retries are exhausted. `arxiv` disables the fallback.
    provider: str = "auto"
    alphaxiv_fallback_enabled: bool = True
    alphaxiv_endpoint: str = "https://api.alphaxiv.org/mcp/v1"
    alphaxiv_api_key_env: str = "ALPHAXIV_API_KEY"
    alphaxiv_difficulty: int = 5
    interest_description: str = ""
    lookback_days: int = 2
    max_candidates: int = 300
    prefilter_count: int = 30
    recommendation_count: int = 5
    min_score: float = 0.5
    arxiv_categories: list[str] = field(default_factory=lambda: ["cs.AI", "cs.LG"])
    arxiv_query_terms: list[str] = field(default_factory=list)
    positive_keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    seed_papers: list[str] = field(default_factory=list)
    concept_groups: list[list[str]] = field(default_factory=list)
    minimum_concept_groups: int = 0


@dataclass(slots=True)
class RankingConfig:
    category_weight: float = 1.0
    keyword_weight: float = 3.0
    negative_weight: float = 4.0
    recency_weight: float = 1.0
    llm_rerank: bool = True
    llm_min_score: float = 6.5


@dataclass(slots=True)
class LLMConfig:
    model: str = "gpt-5-mini"
    api_key_env: str = "LLM_API_KEY"
    base_url_env: str = "LLM_BASE_URL"
    temperature: float = 0.1
    max_chunk_chars: int = 18000


@dataclass(slots=True)
class MetadataConfig:
    openalex_email: str = ""
    openalex_api_key_env: str = "OPENALEX_API_KEY"
    semantic_scholar_api_key_env: str = "SEMANTIC_SCHOLAR_API_KEY"
    semantic_scholar_min_interval: float = 1.1
    semantic_scholar_max_retries: int = 3
    verify_venue: bool = True


@dataclass(slots=True)
class PDFConfig:
    parser: str = "pymupdf"
    use_docling_if_available: bool = True
    max_pages: int = 60
    figure_dpi: int = 160


@dataclass(slots=True)
class DeliveryConfig:
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_ssl: bool = True
    email_address_env: str = "QQ_EMAIL"
    username: str = ""
    password_env: str = "SMTP_PASSWORD"
    from_address: str = ""
    to_addresses: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ZoteroConfig:
    enabled: bool = True
    mode: str = "local"
    base_url: str = "http://127.0.0.1:23119/api"
    api_key_env: str = "ZOTERO_LOCAL_API_KEY"
    collection_name: str = "arXiv Research Assistant"
    attach_pdf: bool = True
    pdf_attachment_mode: str = "imported_file"
    attach_report: bool = True
    add_profile_tag: bool = True


@dataclass(slots=True)
class ObsidianConfig:
    enabled: bool = False
    vault_path: str = ""
    root_folder: str = "arXiv Research Assistant"
    home_folder: str = "Home"
    daily_folder: str = "Daily"
    papers_folder: str = "Papers"
    concepts_folder: str = "Topics"
    weekly_folder: str = "Reviews/Weekly"
    attachments_folder: str = "Attachments"
    profiles_folder: str = "System/Profiles"
    indexes_folder: str = "System/Indexes"
    auto_sync: bool = True
    sync_daily: bool = True
    sync_reports: bool = True
    sync_weekly: bool = True
    sync_feedback: bool = True
    copy_figures: bool = True
    copy_pdf: bool = False
    generate_concepts: bool = True


@dataclass(slots=True)
class JobsConfig:
    max_parallel: int = 3


@dataclass(slots=True)
class WeeklyConfig:
    enabled: bool = True
    days: int = 7
    max_papers: int = 50
    include_deep_reports: bool = True
    auto_generate: bool = True
    weekday: int = 6


@dataclass(slots=True)
class VersionTrackingConfig:
    enabled: bool = True
    auto_check: bool = True
    max_tracked: int = 50
    include_reports: bool = True
    include_feedback: bool = True
    include_zotero: bool = True
    analyze_pdf_diff: bool = True


@dataclass(slots=True)
class CitationConfig:
    enabled: bool = True
    max_references: int = 15
    max_citations: int = 15
    max_similar: int = 15
    min_interval: float = 1.1
    max_retries: int = 3


@dataclass(slots=True)
class AppConfig:
    timezone: str = "Asia/Shanghai"
    output_dir: str = "run"
    profile_id: str = ""
    profile_name: str = "默认方向"
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    pdf: PDFConfig = field(default_factory=PDFConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    zotero: ZoteroConfig = field(default_factory=ZoteroConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    jobs: JobsConfig = field(default_factory=JobsConfig)
    weekly: WeeklyConfig = field(default_factory=WeeklyConfig)
    version_tracking: VersionTrackingConfig = field(default_factory=VersionTrackingConfig)
    citations: CitationConfig = field(default_factory=CitationConfig)


def _section(cls: type, data: dict[str, Any], name: str):
    return cls(**(data.get(name) or {}))


def load_config(path: Path) -> AppConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profile_id = ""
    profile_name = "默认方向"
    active_path = path.parent / "profiles" / "active.txt"
    if active_path.exists():
        profile_id = active_path.read_text(encoding="utf-8").strip()
        profile_path = path.parent / "profiles" / f"{profile_id}.yaml"
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", profile_id) and profile_path.exists():
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
            profile_name = str(profile.get("name") or profile_id)
            if profile.get("discovery"):
                data["discovery"] = profile["discovery"]
            if profile.get("ranking"):
                data["ranking"] = profile["ranking"]
        else:
            profile_id = ""
    return AppConfig(
        timezone=data.get("timezone", "Asia/Shanghai"),
        output_dir=data.get("output_dir", "run"),
        profile_id=profile_id,
        profile_name=profile_name,
        discovery=_section(DiscoveryConfig, data, "discovery"),
        ranking=_section(RankingConfig, data, "ranking"),
        llm=_section(LLMConfig, data, "llm"),
        metadata=_section(MetadataConfig, data, "metadata"),
        pdf=_section(PDFConfig, data, "pdf"),
        delivery=_section(DeliveryConfig, data, "delivery"),
        zotero=_section(ZoteroConfig, data, "zotero"),
        obsidian=_section(ObsidianConfig, data, "obsidian"),
        jobs=_section(JobsConfig, data, "jobs"),
        weekly=_section(WeeklyConfig, data, "weekly"),
        version_tracking=_section(VersionTrackingConfig, data, "version_tracking"),
        citations=_section(CitationConfig, data, "citations"),
    )
