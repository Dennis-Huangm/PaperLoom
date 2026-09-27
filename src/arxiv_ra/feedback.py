from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Paper
from .reading_state import ReadingStateStore
from .ranker import _phrase_count


VERDICTS = {
    "not_relevant": "不相关",
}

VERDICT_WEIGHTS = {
    "not_relevant": -1.6,
}

LIBRARY_WEIGHT = 0.8
DEFAULT_LIBRARY_SIGNAL_LIMIT = 30

STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "from", "with", "without", "via",
    "of", "to", "in", "on", "as", "by", "towards", "toward", "using", "based",
    "new", "approach", "method", "model", "models", "learning", "deep", "paper",
    "study", "framework", "system", "systems", "task", "tasks", "large", "language",
    "multimodal", "unified", "efficient", "improving", "enhancing", "generation",
}


def _paper_terms(title: str) -> list[str]:
    tokens = [
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]+", title)
        if len(token) >= 3 and token.casefold() not in STOPWORDS
    ]
    terms: list[str] = []
    for width in (2, 1):
        for index in range(len(tokens) - width + 1):
            term = " ".join(tokens[index : index + width])
            if term and term not in terms:
                terms.append(term)
    return terms[:16]


class FeedbackStore:
    def __init__(self, output_root: Path, profile_id: str) -> None:
        self.state = ReadingStateStore(output_root, profile_id)
        self.path = self.state.path

    def all(self) -> dict[str, dict[str, Any]]:
        return self.state.snapshot()["feedback"]

    def set(self, paper: dict[str, Any], verdict: str, *, scope: str = "paper",
            terms: list[str] | None = None) -> dict[str, Any]:
        if verdict not in VERDICTS:
            raise ValueError("无效的阅读反馈")
        arxiv_id = str(paper.get("arxiv_id") or "").strip()
        if not arxiv_id:
            raise ValueError("反馈缺少 arXiv ID")
        if scope not in {"paper", "topic"}:
            raise ValueError("无效的反馈范围")
        terms = list(dict.fromkeys(term.strip().casefold() for term in (terms or []) if term.strip()))
        if scope == "topic" and (not terms or len(terms) > 10 or any(len(t) < 2 or len(t) > 80 for t in terms)):
            raise ValueError("主题屏蔽需要 1–10 个明确的词或短语，每项 2–80 字")
        entry = {
            "arxiv_id": arxiv_id,
            "verdict": verdict,
            "label": VERDICTS[verdict],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "paper": {
                "title": str(paper.get("title") or ""),
                "abstract": str(paper.get("abstract") or ""),
                "primary_category": str(paper.get("primary_category") or ""),
                "abs_url": str(paper.get("abs_url") or ""),
            },
            "scope": scope,
            "terms": terms if scope == "topic" else [],
        }
        return self.state.dismiss(entry)

    def remove(self, arxiv_id: str) -> bool:
        return self.state.remove("feedback", arxiv_id)

    def learned_terms(
        self,
        library_entries: dict[str, dict[str, Any]] | None = None,
        library_limit: int = DEFAULT_LIBRARY_SIGNAL_LIMIT,
        *, feedback_entries: dict | None = None,
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for entry in (self.all() if feedback_entries is None else feedback_entries).values():
            if entry.get("scope") == "paper":
                continue
            weight = VERDICT_WEIGHTS.get(str(entry.get("verdict") or ""), 0.0)
            for term in entry.get("terms") or []:
                scores[str(term)] = scores.get(str(term), 0.0) + weight
        recent_library = sorted(
            (library_entries or {}).values(),
            key=lambda entry: str(entry.get("saved_at") or ""),
            reverse=True,
        )[: max(0, library_limit)]
        for entry in recent_library:
            title = str((entry.get("paper") or {}).get("title") or "")
            for term in _paper_terms(title):
                scores[term] = scores.get(term, 0.0) + LIBRARY_WEIGHT
        return scores

    def apply(
        self,
        papers: list[Paper],
        library_entries: dict[str, dict[str, Any]] | None = None,
        library_limit: int = DEFAULT_LIBRARY_SIGNAL_LIMIT,
        *, feedback_entries: dict | None = None,
    ) -> None:
        learned = self.learned_terms(library_entries, library_limit, feedback_entries=feedback_entries)
        for paper in papers:
            text = f"{paper.title} {paper.abstract}".casefold()
            raw = sum(weight for term, weight in learned.items() if _phrase_count(text, term))
            paper.feedback_score = round(max(-3.0, min(3.0, raw * 0.28)), 4)
            paper.ranking_explanation["feedback_matches"] = [
                {"term": term, "weight": weight} for term, weight in learned.items()
                if weight and _phrase_count(text, term)]

    def blocks(self, paper: Paper) -> bool:
        return bool(self.block_reasons(paper))

    def block_reasons(self, paper: Paper, entries: dict | None = None) -> list[dict]:
        """Explicit paper/topic rules; unscoped legacy rules retain their old meaning."""
        reasons = []
        text = f"{paper.title} {paper.abstract}".casefold()
        for entry in (self.all() if entries is None else entries).values():
            if str(entry.get("verdict") or "") != "not_relevant":
                continue
            if str(entry.get("arxiv_id") or "") == paper.arxiv_id:
                reasons.append({"rule_id": entry["arxiv_id"], "reason": "已排除此论文", "terms": []})
                continue
            if entry.get("scope") == "paper":
                continue
            matches = {
                str(term)
                for term in (entry.get("terms") or [])
                if str(term) and (_phrase_count(text, str(term)) if entry.get("scope") == "topic" else str(term) in text)
            }
            if ((entry.get("scope") == "topic" and matches)
                    or any(" " in term for term in matches) or len(matches) >= 2):
                reasons.append({"rule_id": entry["arxiv_id"], "reason": "命中主题屏蔽" if entry.get("scope") else "命中历史相似主题规则",
                                "terms": sorted(matches)})
        return reasons
