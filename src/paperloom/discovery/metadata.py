from __future__ import annotations

from difflib import SequenceMatcher
import re
import time
import math
import unicodedata
from typing import Any

import httpx

from .. import __version__
from ..config import MetadataConfig
from ..papers.models import Author, Paper, VerifiedMetadata
from ..runtime.model_access.rate_limit import shared_rate_limit, defer_rate_limit
from ..utils import env, normalize_title
from ..runtime.jobs.task_runtime import task_warning


def _similar_title(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def _publication_doi(value):
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value or "", flags=re.I).strip()
    return value if value and not value.casefold().startswith("10.48550/arxiv.") else None


def _author_keys(names):
    keys = set()
    for name in names:
        normalized = unicodedata.normalize("NFKD", name or "").casefold()
        words = re.findall(r"[^\W_]+", "".join(c for c in normalized if not unicodedata.combining(c)))
        if words:
            keys.add((words[0][0], words[-1]))
    return keys


def _venue_key(name):
    aliases = {
        "acm international conference on multimedia acm mm": "acm multimedia",
        "international conference on language resources and evaluation": "lrec",
        "language resources and evaluation conference lrec": "lrec",
    }
    key = normalize_title(name)
    return aliases.get(key, key)


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
    # A conference name in submission status or a reference to earlier work is
    # not this paper's publication declaration. Prefer explicit acceptance;
    # retain the common terse "NeurIPS 2024 Spotlight" announcement form.
    clauses = re.split(r"[.;\n]", comment)
    candidates = []
    for clause in clauses:
        if re.search(r"\b(?:submit\w*|submission\w*|reject\w*|withdraw\w*|under\s+review|not|never)\b",
                     clause, re.IGNORECASE):
            continue
        explicit = DECLARED_VENUE_RE.search(clause)
        if explicit:
            return explicit.group(1).strip()
        terse = KNOWN_VENUE_RE.fullmatch(clause.strip())
        if terse:
            candidates.append(terse.group(1).strip())
    return candidates[0] if candidates else None


class MetadataVerifier:
    def __init__(self, config: MetadataConfig, timeout: float = 30.0) -> None:
        self.config = config
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": f"PaperLoom/{__version__} mailto:{config.openalex_email}"},
        )

    def verify(self, paper: Paper) -> VerifiedMetadata:
        result = VerifiedMetadata(title=paper.title, authors=paper.authors, doi=_publication_doi(paper.doi))
        try:
            openalex = self._openalex(paper)
        except Exception as exc:
            openalex = None
            result.conflicts.append(f"OpenAlex 查询失败：{type(exc).__name__}")
            task_warning(
                "OpenAlex 元数据",
                f"出版信息查询失败，已继续使用其他来源：{type(exc).__name__}: {exc}",
            )
        try:
            semantic = self._semantic_scholar(paper)
        except Exception as exc:
            semantic = None
            result.conflicts.append(f"Semantic Scholar 查询失败：{type(exc).__name__}")
            task_warning(
                "Semantic Scholar 元数据",
                f"引用与出版信息查询失败，已继续使用其他来源：{type(exc).__name__}: {exc}",
            )
        if openalex:
            self._merge_openalex(result, openalex, paper)
        if semantic:
            self._merge_semantic(result, semantic, paper)
        venue_conflict = any(note.startswith("会议/期刊来源冲突") for note in result.conflicts)
        if result.doi and (result.venue or venue_conflict):
            self._resolve_publication(result, paper)
        if paper.journal_ref and not result.venue and not venue_conflict:
            result.venue = paper.journal_ref
            result.venue_status = "declared_in_arxiv"
            result.sources.append("arXiv journal_ref")
        if not result.venue and not venue_conflict:
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
        expected = _author_keys(author.name for author in paper.authors)
        def authors_of(item):
            return _author_keys((a.get("author") or {}).get("display_name") for a in item.get("authorships") or [])

        if paper.doi:
            url = f"https://api.openalex.org/works/https://doi.org/{paper.doi}"
            response = self.client.get(url, params=params)
            if response.status_code == 200:
                item = response.json()
                authors = authors_of(item)
                if (_similar_title(paper.title, item.get("title") or "") >= 0.88
                        and (not expected or not authors or expected & authors)):
                    return item
        response = self.client.get("https://api.openalex.org/works", params={**params, "search": paper.title})
        if response.status_code != 200:
            return None
        matches = response.json().get("results", [])
        candidates = []
        for item in matches:
            title_score = _similar_title(paper.title, item.get("title") or "")
            authors = authors_of(item)
            locations = [item.get("primary_location") or {}, *(item.get("locations") or [])]
            exact = any(re.fullmatch(r"https?://arxiv\.org/(?:abs|pdf)/" + re.escape(paper.arxiv_id)
                                     + r"(?:v\d+)?(?:\.pdf)?/?", str(loc.get("landing_page_url")), re.I)
                        for loc in locations if loc)
            overlap = len(expected & authors)
            if title_score >= 0.88 and ((exact and (not expected or not authors or overlap))
                                        or (overlap and overlap * 2 >= min(len(expected), len(authors)))):
                candidates.append(((exact, overlap / max(1, len(expected | authors)), title_score), item))
        return max(candidates, key=lambda pair: pair[0])[1] if candidates else None

    def _semantic_scholar(self, paper: Paper) -> dict[str, Any] | None:
        fields = "title,authors,venue,year,publicationDate,externalIds,publicationVenue"
        headers = {}
        key = env(self.config.semantic_scholar_api_key_env)
        if key:
            headers["x-api-key"] = key
        url = f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{paper.arxiv_id}"
        attempts = max(1, self.config.semantic_scholar_max_retries + 1)
        for attempt in range(attempts):
            with shared_rate_limit("semantic-scholar", self.config.semantic_scholar_min_interval):
                response = self.client.get(url, params={"fields": fields}, headers=headers)
            if response.status_code == 200:
                return response.json()
            if response.status_code != 429:
                return None
            try:
                requested_delay = float(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                requested_delay = 0.0
            if not math.isfinite(requested_delay):
                requested_delay = 0.0
            delay = max(1.2, float(2**attempt), requested_delay)
            defer_rate_limit('semantic-scholar', delay)
            if attempt == attempts - 1:
                return None
            time.sleep(delay)
        return None

    def _resolve_publication(self, result: VerifiedMetadata, paper: Paper) -> None:
        """Prefer the publisher's DOI deposit, preserving its date precision."""
        try:
            response = self.client.get(f"https://api.crossref.org/works/{result.doi}")
            if response.status_code != 200:
                return
            item = response.json()["message"]
            authors = _author_keys(f"{a.get('given', '')} {a.get('family', '')}" for a in item.get("author", []))
            venues = item.get("container-title") or []
            if (not venues or not authors.intersection(_author_keys(a.name for a in paper.authors))
                    or _similar_title(paper.title, (item.get("title") or [""])[0]) < 0.88
                    or (item.get("DOI") or "").casefold() != result.doi.casefold()):
                return
            parts = (item.get("published") or {}).get("date-parts") or []
            if parts and parts[0]:
                deposited_date = "-".join(f"{v:02d}" for v in parts[0])
                if not result.publication_date or not result.publication_date.startswith(deposited_date):
                    result.publication_date = deposited_date
            result.venue = venues[-1]
            result.venue_status = "verified_metadata"
            result.sources.append("Crossref")
            result.conflicts = [note for note in result.conflicts if not note.startswith("会议/期刊来源冲突")
                                and not (result.publication_date and note.startswith("正式发表日期来源冲突"))]
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return

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
            date = item.get("publication_date")
            if date and (not paper.published or date != paper.published.date().isoformat()):
                result.publication_date = date
        doi = _publication_doi(item.get("doi"))
        if doi:
            expected = _publication_doi(paper.doi)
            if expected and expected.casefold() != doi.casefold():
                result.conflicts.append(f"DOI 来源冲突：arXiv={paper.doi}；OpenAlex={doi}")
            else:
                result.doi = doi
        if _similar_title(paper.title, item.get("title", "")) < 0.95:
            result.conflicts.append("OpenAlex 标题与 arXiv 标题不完全一致")

    def _merge_semantic(self, result: VerifiedMetadata, item: dict[str, Any], paper: Paper) -> None:
        arxiv = (item.get("externalIds") or {}).get("ArXiv")
        if arxiv and re.sub(r"v\d+$", "", str(arxiv), flags=re.I).casefold() != paper.arxiv_id.casefold():
            result.conflicts.append("Semantic Scholar 的 arXiv ID 不匹配，已忽略该记录")
            return
        authors = _author_keys(a.get("name") for a in item.get("authors") or [])
        expected = _author_keys(a.name for a in paper.authors)
        if authors and expected and not authors.intersection(expected):
            result.conflicts.append("Semantic Scholar 作者与论文不匹配，已忽略该记录")
            return
        if _similar_title(paper.title, item.get("title", "")) < 0.88:
            result.conflicts.append("Semantic Scholar 返回结果标题相似度不足，未采用其会议信息")
            return
        result.sources.append("Semantic Scholar")
        venue_obj = item.get("publicationVenue") or {}
        venue = venue_obj.get("name") or item.get("venue")
        is_preprint = venue and ("arxiv" in venue.casefold() or venue.strip().casefold() == "corr")
        if venue and not is_preprint:
            if result.venue and _venue_key(result.venue) != _venue_key(venue):
                result.conflicts.append(f"会议/期刊来源冲突：OpenAlex={result.venue}；Semantic Scholar={venue}")
                result.venue = result.publication_date = None
                result.venue_status = "unverified"
            else:
                result.venue = venue
                result.venue_status = "verified_metadata"
                # A preprint date (or a rejected, conflicting venue's date)
                # must not become the report's formal publication date.
                date = item.get("publicationDate")
                preprint_date = paper.published and paper.published.date().isoformat()
                if (date and date != preprint_date and result.publication_date
                        and not (date.startswith(result.publication_date) or result.publication_date.startswith(date))):
                    result.conflicts.append(f"正式发表日期来源冲突：OpenAlex={result.publication_date}；Semantic Scholar={date}")
                    result.publication_date = None
                elif date != preprint_date:
                    result.publication_date = result.publication_date or date
        external = item.get("externalIds") or {}
        doi = _publication_doi(external.get("DOI"))
        if doi:
            if result.doi and result.doi.casefold() != doi.casefold():
                result.conflicts.append(f"DOI 来源冲突：{result.doi}；Semantic Scholar={doi}")
                result.doi = _publication_doi(paper.doi)
            else:
                result.doi = result.doi or doi
