"""Read-only preview orchestration, with independent artifacts and fixed limits."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from .config import DiscoveryConfig, RankingConfig
from .discovery import DiscoveryService, DiscoveryError
from .models import Paper
from .plan_selection import evaluate_plan, selectable
from .ranker import rank_papers
from .recent_interest import prefilter_candidates
from .research_clients import ResearchClients


def preview_profile(draft, config, *, references_only=False):
    discovery = DiscoveryConfig(**draft["discovery"])
    discovery.max_candidates = min(30, discovery.max_candidates)
    discovery.prefilter_count = min(30, discovery.prefilter_count)
    discovery.alphaxiv_max_candidates = min(5, discovery.alphaxiv_max_candidates)
    config = replace(config, profile_id=draft["id"], discovery=discovery, ranking=RankingConfig(**draft["ranking"]))
    sources = {}
    with ResearchClients(config) as clients:
        if references_only:
            papers = [Paper(r["arxiv_id"], r["title"], [], r["abstract"], r["categories"],
                            r["categories"][0] if r["categories"] else "", None, None,
                            f"https://arxiv.org/abs/{r['arxiv_id']}", "")
                      for r in draft["references"] if r["title"] and r["abstract"]]
        else:
            try:
                result = DiscoveryService(discovery, clients.arxiv, clients.alphaxiv).discover()
            except DiscoveryError as exc:
                return {"revision": draft["revision"], "status": "failed", "error": str(exc),
                        "sources": exc.discovery_result.sources, "selected": [], "rejected": []}
            papers, sources = result.papers, result.sources
        ranked = rank_papers(papers, discovery, config.ranking)
        candidates = prefilter_candidates(ranked, discovery.prefilter_count)
        accepted, rejected = evaluate_plan(candidates, config, clients.llm)
        selected = [p for p in accepted if selectable(p, config)]
        for p in accepted:
            if not selectable(p, config):
                rejected.append({"paper": p.to_dict(), "reasons": [{"reason": "低于相关性或综合分门槛"}]})
        candidate_ids = {p.arxiv_id for p in candidates}
        rejected.extend({"paper": p.to_dict(), "reasons": [{"reason": "超出预筛数量"}]} for p in ranked if p.arxiv_id not in candidate_ids)
        uncertain = sum(any(r.get("condition", {}).get("verdict") == "unknown" for r in p["reasons"]) for p in rejected)
        partial = any(s["status"] in {"failed", "partial", "skipped"} for s in sources.values())
        warnings = list(dict.fromkeys(p.ranking_explanation.get("condition_error", "") for p in candidates
                                     if p.ranking_explanation.get("condition_error")))
        status = "empty" if not papers else "uncertain" if uncertain and not selected else "filtered" if not selected else "partial" if partial else "ok"
        return {"revision": draft["revision"], "created_at": datetime.now(timezone.utc).isoformat(),
                "status": status, "sources": sources, "references_only": references_only, "warnings": warnings,
                "budget": {"arxiv": discovery.max_candidates, "alphaxiv": discovery.alphaxiv_max_candidates},
                "counts": {"retrieved": len(papers), "evaluated": len(candidates), "eligible": len(selected),
                           "rejected": len(rejected), "uncertain": uncertain},
                "selected": [p.to_dict() for p in selected[:10]], "rejected": rejected[:10]}
