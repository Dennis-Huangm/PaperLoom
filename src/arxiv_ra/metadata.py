from __future__ import annotations

from difflib import SequenceMatcher
import re
import time
from typing import Any

import httpx

from . import __version__
from .config import MetadataConfig
from .models import Author, Paper, VerifiedMetadata
from .rate_limit import shared_rate_limit
from .utils import env, normalize_title


def _similar_title(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


KNOWN_VENUE_RE = re.compile(
    r"\b((?:NeurIPS|ICLR|ICML|CVPR|ICCV|ECCV|AAAI|IJCAI|ACL|EMNLP|NAACL|SIGGRAPH)"
    r"(?:\s+\d{4})?(?:\s+(?:Spotlight|Oral|Poster))?)\b",
    re.IGNORECASE,
)
DECLARED_VENUE_RE = re.compile(
    r"(?:accepted\s+(?:to|at)|to\s+appear\s+(?:in|at)|published\s+(?:in|at))\s+([^.;]+)",
    re.IGNORECASE,
)


def declared_venue_from_comment(comment: str | None) -> str | None:
    if not comment:
        return None
    match = DECLARED_VENUE_RE.search(comment) or KNOWN_VENUE_RE.search(comment)
    return match.group(1).strip() if match else None


class MetadataVerifier:
    def __init__(self, config: MetadataConfig, timeout: float = 30.0) -> None:
        self.config = config
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": f"PaperLoom/{__version__} mailto:{config.openalex_email}"},
        )

    def verify(self, paper: Paper) -> VerifiedMetadata:
        result = VerifiedMetadata(title=paper.title, authors=paper.authors, doi=paper.doi)
        try:
            openalex = self._openalex(paper)
        except Exception as exc:
            openalex = None
            result.conflicts.append(f"OpenAlex 查询失败：{type(exc).__name__}")
        try:
            semantic = self._semantic_scholar(paper)
        except Exception as exc:
            semantic = None
            result.conflicts.append(f"Semantic Scholar 查询失败：{type(exc).__name__}")
        if openalex:
            self._merge_openalex(result, openalex, paper)
        if semantic:
            self._merge_semantic(result, semantic, paper)
        if paper.journal_ref and not result.venue:
            result.venue = paper.journal_ref
            result.venue_status = "declared_in_arxiv"
            result.sources.append("arXiv journal_ref")
        if not result.venue:
            declared_venue = declared_venue_from_comment(paper.comment)
            if declared_venue:
                result.venue = declared_venue
                result.venue_status = "declared_in_arxiv"
                result.sources.append("arXiv comment")
        if not result.sources:
            result.sources.append(paper.metadata_label)
        return result

    def _openalex(self, paper: Paper) -> dict[str, Any] | None:
        params: dict[str, Any] = {"per_page": 5}
        key = env(self.config.openalex_api_key_env)
        if key:
            params["api_key"] = key
        if self.config.openalex_email:
            params["mailto"] = self.config.openalex_email
        if paper.doi:
            url = f"https://api.openalex.org/works/https://doi.org/{paper.doi}"
            response = self.client.get(url, params=params)
            if response.status_code == 200:
                return response.json()
        response = self.client.get("https://api.openalex.org/works", params={**params, "search": paper.title})
        if response.status_code != 200:
            return None
        matches = response.json().get("results", [])
        matches = [item for item in matches if _similar_title(paper.title, item.get("title", "")) >= 0.88]
        return max(matches, key=lambda item: _similar_title(paper.title, item.get("title", "")), default=None)

    def _semantic_scholar(self, paper: Paper) -> dict[str, Any] | None:
        fields = "title,authors,venue,year,publicationDate,externalIds,publicationVenue"
        headers = {}
        key = env(self.config.semantic_scholar_api_key_env)
        if key:
            headers["x-api-key"] = key
        url = f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{paper.arxiv_id}"
        attempts = max(1, self.config.semantic_scholar_max_retries + 1)
        with shared_rate_limit(
            "semantic-scholar", self.config.semantic_scholar_min_interval
        ):
            for attempt in range(attempts):
                response = self.client.get(url, params={"fields": fields}, headers=headers)
                if response.status_code == 200:
                    return response.json()
                if response.status_code != 429 or attempt == attempts - 1:
                    return None
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = float(2**attempt)
                time.sleep(max(0.0, min(delay, 30.0)))
        return None

    def _merge_openalex(self, result: VerifiedMetadata, item: dict[str, Any], paper: Paper) -> None:
        result.sources.append("OpenAlex")
        result.citation_count = item.get("cited_by_count")
        authors: list[Author] = []
        for authorship in item.get("authorships") or []:
            name = (authorship.get("author") or {}).get("display_name", "")
            affiliations = [i.get("display_name", "") for i in authorship.get("institutions") or [] if i.get("display_name")]
            if name:
                authors.append(Author(name=name, affiliations=affiliations))
        if authors:
            result.authors = authors
        source_obj = (item.get("primary_location") or {}).get("source") or {}
        source = source_obj.get("display_name")
        source_type = str(source_obj.get("type") or "").casefold()
        is_preprint_repository = bool(source) and (
            "arxiv" in source.casefold() or source_type == "repository"
        )
        if source and not is_preprint_repository:
            result.venue = source
            result.venue_status = "verified_metadata"
            result.publication_date = item.get("publication_date") or result.publication_date
        doi = (item.get("doi") or "").removeprefix("https://doi.org/")
        if doi and not doi.casefold().startswith("10.48550/arxiv."):
            result.doi = doi
        if _similar_title(paper.title, item.get("title", "")) < 0.95:
            result.conflicts.append("OpenAlex 标题与 arXiv 标题不完全一致")

    def _merge_semantic(self, result: VerifiedMetadata, item: dict[str, Any], paper: Paper) -> None:
        if _similar_title(paper.title, item.get("title", "")) < 0.88:
            result.conflicts.append("Semantic Scholar 返回结果标题相似度不足，未采用其会议信息")
            return
        result.sources.append("Semantic Scholar")
        venue_obj = item.get("publicationVenue") or {}
        venue = venue_obj.get("name") or item.get("venue")
        if venue and venue.casefold() not in {"arxiv", "corr"}:
            if result.venue and normalize_title(result.venue) != normalize_title(venue):
                result.conflicts.append(f"会议/期刊来源冲突：OpenAlex={result.venue}；Semantic Scholar={venue}")
            else:
                result.venue = venue
                result.venue_status = "verified_metadata"
        result.publication_date = item.get("publicationDate") or result.publication_date
        external = item.get("externalIds") or {}
        result.doi = external.get("DOI") or result.doi
