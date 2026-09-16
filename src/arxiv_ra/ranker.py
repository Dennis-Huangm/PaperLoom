from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .config import DiscoveryConfig, RankingConfig
from .models import Paper
from .utils import normalize_space


def _phrase_count(text: str, phrase: str) -> int:
    text = text.casefold()
    phrase = normalize_space(phrase).casefold()
    if not phrase:
        return 0
    if re.fullmatch(r"[a-z0-9 -]+", phrase):
        return len(re.findall(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))
    return text.count(phrase)


def matched_concept_groups(paper: Paper, discovery: DiscoveryConfig) -> int:
    text = f"{paper.title} {paper.abstract}"
    return sum(
        1
        for group in discovery.concept_groups
        if any(_phrase_count(text, phrase) > 0 for phrase in group)
    )


def score_paper(paper: Paper, discovery: DiscoveryConfig, ranking: RankingConfig) -> float:
    text = f"{paper.title} {paper.abstract}"
    positive = sum(min(_phrase_count(text, keyword), 3) for keyword in discovery.positive_keywords)
    negative = sum(min(_phrase_count(text, keyword), 3) for keyword in discovery.negative_keywords)
    category_hits = sum(1 for category in paper.categories if category in discovery.arxiv_categories)
    age_hours = max((datetime.now(timezone.utc) - paper.published).total_seconds() / 3600, 0) if paper.published else None
    recency = math.exp(-age_hours / 96) if age_hours is not None else 0.0
    title_bonus = sum(1.5 for keyword in discovery.positive_keywords if _phrase_count(paper.title, keyword))
    group_hits = matched_concept_groups(paper, discovery)
    group_bonus = group_hits * 4.0
    if discovery.concept_groups and group_hits == len(discovery.concept_groups):
        group_bonus += 8.0
    return round(
        ranking.category_weight * category_hits
        + ranking.keyword_weight * (positive + title_bonus)
        - ranking.negative_weight * negative
        + ranking.recency_weight * recency
        + group_bonus,
        4,
    )


def rank_papers(
    papers: list[Paper], discovery: DiscoveryConfig, ranking: RankingConfig
) -> list[Paper]:
    for paper in papers:
        paper.lexical_score = score_paper(paper, discovery, ranking)
    return sorted(papers, key=lambda paper: (paper.final_score, paper.published_sort_key), reverse=True)
