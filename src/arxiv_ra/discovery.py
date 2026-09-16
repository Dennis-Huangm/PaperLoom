from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from .models import Paper


from .paper_data import PaperResolver, alpha_enabled, base_id, with_sources


@dataclass
class DiscoveryResult:
    papers: list[Paper] = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    hydration: dict = field(default_factory=dict)
    rejected_dates: int = 0
    merged_count: int = 0

    def to_dict(self) -> dict:
        return {
            "sources": self.sources, "hydration": self.hydration,
            "eligible_dates": len(self.papers), "rejected_dates": self.rejected_dates,
            "merged_count": self.merged_count,
            "papers": [{"arxiv_id": p.arxiv_id, "discovery_sources": p.discovery_sources,
                        "metadata_status": p.metadata_status} for p in self.papers],
        }


class DiscoveryError(RuntimeError):
    def __init__(self, message: str, result: DiscoveryResult):
        super().__init__(message)
        self.discovery_result = result


class DiscoveryService:
    """Coordinate independent discovery sources and hydrate their merged candidates."""

    def __init__(self, config, arxiv, alphaxiv, output_root: Path | None = None):
        self.config, self.arxiv, self.alphaxiv = config, arxiv, alphaxiv
        self.resolver = PaperResolver(config, arxiv, alphaxiv, output_root)

    def discover(self, excluded_ids: set[str] | None = None) -> DiscoveryResult:
        config = self.config
        if config.provider not in {"auto", "arxiv", "hybrid"}:
            raise ValueError("无效的检索来源模式")
        result = DiscoveryResult()
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=config.lookback_days)).date()
        excluded_ids = {base_id(item) for item in excluded_ids or set()}
        arxiv_papers, alpha_papers = [], []
        started = time.monotonic()
        arxiv_error = None
        try:
            arxiv_papers = self.arxiv.search(config.arxiv_categories, config.lookback_days,
                                            config.max_candidates, config.arxiv_query_terms)
            result.sources["arxiv"] = {"status": "ok", "count": len(arxiv_papers)}
        except Exception as exc:
            arxiv_error = exc
            result.sources["arxiv"] = {"status": "failed", "error": str(exc), "count": 0}
        result.sources["arxiv"]["seconds"] = round(time.monotonic() - started, 3)

        use_alpha = alpha_enabled(config) and (config.provider == "hybrid" or arxiv_error is not None)
        result.sources["alphaxiv"] = {"status": "disabled" if not alpha_enabled(config) else "not_needed", "count": 0}
        if use_alpha:
            started = time.monotonic()
            try:
                alpha_papers = self.alphaxiv.discover(
                    keywords=config.positive_keywords + config.arxiv_query_terms,
                    question=(f"研究主题：{config.interest_description}。关键词：{', '.join(config.positive_keywords)}。"
                              f"降低优先级：{', '.join(config.negative_keywords)}。种子论文：{', '.join(config.seed_papers)}。"),
                    published_after=cutoff.isoformat(),
                    limit=config.alphaxiv_max_candidates,
                    difficulty=config.alphaxiv_difficulty,
                    excluded_ids=excluded_ids,
                )
                result.sources["alphaxiv"] = {"status": "ok", "count": len(alpha_papers)}
            except Exception as exc:
                result.sources["alphaxiv"] = {"status": "failed", "error": str(exc), "count": 0}
            result.sources["alphaxiv"]["seconds"] = round(time.monotonic() - started, 3)
        if arxiv_error and result.sources["alphaxiv"]["status"] != "ok":
            detail = result.sources["alphaxiv"].get("error", "未启用")
            raise DiscoveryError(f"arXiv API 不可用：{arxiv_error}；alphaXiv：{detail}", result) from arxiv_error

        merged: dict[str, Paper] = {}
        for paper in arxiv_papers:
            paper = replace(paper, arxiv_id=base_id(paper.arxiv_id), discovery_sources=["arxiv"])
            merged[paper.arxiv_id] = paper
            self.resolver.remember(paper)
        for paper in alpha_papers:
            aid = base_id(paper.arxiv_id)
            if aid in merged:
                merged[aid] = with_sources(merged[aid], ["arxiv", "alphaxiv"])
            else:
                cached = self.resolver.cached(aid, fresh_only=arxiv_error is None)
                merged[aid] = with_sources(cached or replace(paper, arxiv_id=aid), ["alphaxiv"])

        result.merged_count = len(merged)
        missing = [aid for aid, p in merged.items() if p.metadata_status != "complete" and aid not in excluded_ids]
        result.hydration = {"requested": len(missing), "completed": 0, "status": "not_needed"}
        # An exhausted arXiv request must not be retried once for every alphaXiv ID.
        if missing and arxiv_error:
            result.hydration["status"] = "deferred"
        elif missing:
            try:
                for paper in self.arxiv.get_many(missing):
                    if paper.arxiv_id in missing:
                        paper = with_sources(paper, merged[paper.arxiv_id].discovery_sources)
                        merged[paper.arxiv_id] = paper
                        self.resolver.remember(paper)
                        result.hydration["completed"] += 1
                result.hydration["status"] = "ok" if result.hydration["completed"] == len(missing) else "partial"
            except Exception as exc:
                result.hydration.update(status="failed", error=str(exc))
        recovered = 0
        for aid, paper in merged.items():
            if paper.metadata_status != "complete":
                best = self.resolver.best_available(aid, paper)
                if best and best.metadata_status == "complete":
                    merged[aid] = with_sources(replace(best, resolution_note="补全暂不可用，复用本地完整元数据"), paper.discovery_sources)
                    recovered += 1
        result.hydration["cached_fallback"] = recovered
        for paper in merged.values():
            if paper.published and cutoff <= paper.published.date() <= now.date():
                result.papers.append(paper)
            else:
                result.rejected_dates += 1
        return result
