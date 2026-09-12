from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Paper
from .utils import read_json, write_json


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
        suffix = profile_id or "default"
        self.path = output_root / f"feedback-{suffix}.json"

    def all(self) -> dict[str, dict[str, Any]]:
        payload = read_json(self.path, {}) or {}
        items = payload.get("items", payload)
        return items if isinstance(items, dict) else {}

    def set(self, paper: dict[str, Any], verdict: str) -> dict[str, Any]:
        if verdict not in VERDICTS:
            raise ValueError("无效的阅读反馈")
        arxiv_id = str(paper.get("arxiv_id") or "").strip()
        if not arxiv_id:
            raise ValueError("反馈缺少 arXiv ID")
        items = self.all()
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
            "terms": _paper_terms(str(paper.get("title") or "")),
        }
        items[arxiv_id] = entry
        write_json(self.path, {"version": 2, "items": items})
        return entry

    def remove(self, arxiv_id: str) -> bool:
        items = self.all()
        removed = items.pop(arxiv_id, None) is not None
        if removed:
            write_json(self.path, {"version": 2, "items": items})
        return removed

    def learned_terms(
        self,
        library_entries: dict[str, dict[str, Any]] | None = None,
        library_limit: int = DEFAULT_LIBRARY_SIGNAL_LIMIT,
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for entry in self.all().values():
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
    ) -> None:
        learned = self.learned_terms(library_entries, library_limit)
        if not learned:
            return
        for paper in papers:
            text = f"{paper.title} {paper.abstract}".casefold()
            raw = sum(weight for term, weight in learned.items() if term in text)
            paper.feedback_score = round(max(-3.0, min(3.0, raw * 0.28)), 4)

    def blocks(self, paper: Paper) -> bool:
        """Hard-filter papers that closely match a user-declared negative pattern."""
        text = f"{paper.title} {paper.abstract}".casefold()
        for entry in self.all().values():
            if str(entry.get("verdict") or "") != "not_relevant":
                continue
            if str(entry.get("arxiv_id") or "") == paper.arxiv_id:
                return True
            matches = {
                str(term)
                for term in (entry.get("terms") or [])
                if str(term) and str(term) in text
            }
            if any(" " in term for term in matches):
                return True
            if len(matches) >= 2:
                return True
        return False
