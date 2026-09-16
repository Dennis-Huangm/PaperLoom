from __future__ import annotations

import re
import time
import uuid
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import __version__
from .models import Author, Paper
from .rate_limit import defer_rate_limit, shared_rate_limit
from .utils import normalize_space

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def _text(node: ET.Element, path: str, default: str = "") -> str:
    child = node.find(path)
    return normalize_space(child.text or "") if child is not None else default


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _arxiv_id(abs_url: str) -> str:
    value = abs_url.rstrip("/").split("/abs/", 1)[-1]
    return re.sub(r"v\d+$", "", value)


def _arxiv_version(abs_url: str) -> int:
    match = re.search(r"v(\d+)$", abs_url.rstrip("/"))
    return int(match.group(1)) if match else 1


def parse_feed(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for entry in root.findall(f"{ATOM}entry"):
        abs_url = _text(entry, f"{ATOM}id")
        links = {
            item.attrib.get("title", item.attrib.get("rel", "")): item.attrib.get("href", "")
            for item in entry.findall(f"{ATOM}link")
        }
        authors = [Author(_text(a, f"{ATOM}name")) for a in entry.findall(f"{ATOM}author")]
        categories = [c.attrib.get("term", "") for c in entry.findall(f"{ATOM}category")]
        primary = entry.find(f"{ARXIV}primary_category")
        primary_category = primary.attrib.get("term", "") if primary is not None else (categories[0] if categories else "")
        papers.append(
            Paper(
                arxiv_id=_arxiv_id(abs_url),
                title=_text(entry, f"{ATOM}title"),
                authors=authors,
                abstract=_text(entry, f"{ATOM}summary"),
                categories=categories,
                primary_category=primary_category,
                published=_parse_time(_text(entry, f"{ATOM}published")),
                updated=_parse_time(_text(entry, f"{ATOM}updated")),
                abs_url=abs_url,
                pdf_url=links.get("pdf", f"https://arxiv.org/pdf/{_arxiv_id(abs_url)}"),
                version=_arxiv_version(abs_url),
                doi=_text(entry, f"{ARXIV}doi") or None,
                journal_ref=_text(entry, f"{ARXIV}journal_ref") or None,
                comment=_text(entry, f"{ARXIV}comment") or None,
            )
        )
    return papers


class ArxivClient:
    def __init__(
        self,
        timeout: float = 45.0,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        min_interval: float = 3.0,
        rate_limit_key: str = "arxiv-api",
    ) -> None:
        self.max_retries = max(0, max_retries)
        self.retry_base_delay = max(0.0, retry_base_delay)
        self.min_interval = max(0.0, min_interval)
        self.rate_limit_key = rate_limit_key
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": f"PaperLoom/{__version__} (personal research use)"},
        )

    def _get(self, url: str) -> httpx.Response:
        """Retry transient failures while respecting arXiv's API rate limits."""
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                with shared_rate_limit(self.rate_limit_key, self.min_interval):
                    response = self.client.get(url)
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After", "")
                        try:
                            requested_delay = float(retry_after)
                        except (TypeError, ValueError):
                            try:
                                deadline = parsedate_to_datetime(retry_after)
                                if deadline.tzinfo is None:
                                    deadline = deadline.replace(tzinfo=timezone.utc)
                                requested_delay = (deadline - datetime.now(timezone.utc)).total_seconds()
                            except (TypeError, ValueError, OverflowError):
                                requested_delay = 0.0
                        if not math.isfinite(requested_delay):
                            requested_delay = 0.0
                        delay = max(
                            requested_delay,
                            10.0 * (2**attempt),
                        )
                        defer_rate_limit(self.rate_limit_key, delay)
            except httpx.TransportError as exc:
                if attempt == attempts - 1:
                    raise RuntimeError(
                        f"arXiv API 连接失败，已自动重试 {self.max_retries} 次；请稍后再次刷新每日推荐"
                    ) from exc
                delay = self.retry_base_delay * (2**attempt)
            else:
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return response
                if attempt == attempts - 1:
                    if response.status_code == 429:
                        raise RuntimeError(
                            f"arXiv API 当前限流（HTTP 429），已按 API 规则自动退避重试 {self.max_retries} 次；请稍后再刷新每日推荐"
                        )
                    raise RuntimeError(
                        f"arXiv API 暂时不可用（HTTP {response.status_code}），已自动重试 {self.max_retries} 次"
                    )
                if response.status_code == 429:
                    delay = max(10.0 * (2**attempt), self.retry_base_delay)
                else:
                    delay = self.retry_base_delay * (2**attempt)
            time.sleep(max(0.0, min(delay, 30.0)))
        raise RuntimeError("arXiv API 请求失败")

    def search(
        self,
        categories: list[str],
        lookback_days: int,
        max_results: int,
        query_terms: list[str] | None = None,
    ) -> list[Paper]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=lookback_days)
        category_query = " OR ".join(f"cat:{category}" for category in categories)
        date_query = f"submittedDate:[{start:%Y%m%d%H%M} TO {now:%Y%m%d%H%M}]"
        search_query = f"({category_query}) AND {date_query}"
        if query_terms:
            safe_terms = [term.replace('"', "") for term in query_terms if term.strip()]
            term_query = " OR ".join(f'all:"{term}"' for term in safe_terms)
            if term_query:
                search_query += f" AND ({term_query})"
        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        response = self._get(f"https://export.arxiv.org/api/query?{urlencode(params)}")
        response.raise_for_status()
        return parse_feed(response.text)

    def get(self, arxiv_id: str) -> Paper:
        params = {"id_list": arxiv_id, "max_results": 1}
        response = self._get(f"https://export.arxiv.org/api/query?{urlencode(params)}")
        response.raise_for_status()
        papers = parse_feed(response.text)
        if not papers:
            raise LookupError(f"arXiv paper not found: {arxiv_id}")
        return papers[0]

    def get_many(self, arxiv_ids: list[str]) -> list[Paper]:
        unique = list(dict.fromkeys(item.strip() for item in arxiv_ids if item.strip()))
        papers: list[Paper] = []
        for start in range(0, len(unique), 50):
            batch = unique[start:start + 50]
            params = {"id_list": ",".join(batch), "max_results": len(batch)}
            response = self._get(f"https://export.arxiv.org/api/query?{urlencode(params)}")
            response.raise_for_status()
            papers.extend(parse_feed(response.text))
        return papers

    def download_pdf(self, paper: Paper, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".~{uuid.uuid4().hex[:12]}.part")
        try:
            with self.client.stream("GET", paper.pdf_url) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            if temporary.stat().st_size <= 10 * 1024:
                raise ValueError(
                    f"arXiv PDF 响应过小，疑似错误页面：{paper.arxiv_id}"
                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def download_version(self, arxiv_id: str, version: int, destination) -> None:
        destination = destination.resolve()
        if destination.exists() and destination.stat().st_size > 10 * 1024:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        url = f"https://arxiv.org/pdf/{arxiv_id}v{version}"
        for attempt in range(2):
            with self.client.stream("GET", url) as response:
                if response.status_code == 429 and attempt == 0:
                    time.sleep(5)
                    continue
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            if temporary.stat().st_size <= 10 * 1024:
                temporary.unlink(missing_ok=True)
                raise ValueError(f"arXiv PDF 响应过小，疑似错误页面：{arxiv_id}v{version}")
            temporary.replace(destination)
            time.sleep(1)
            return
        raise RuntimeError(f"arXiv PDF 下载频率受限：{arxiv_id}v{version}")


def offline_demo_papers() -> list[Paper]:
    """Small deterministic dataset used by `demo` without network access."""
    now = datetime.now(timezone.utc)
    return [
        Paper(
            arxiv_id="2501.00001",
            title="Reasoning Agents with Verifiable Tool Use",
            authors=[Author("Ada Researcher"), Author("Lin Scientist")],
            abstract=(
                "We study large language model agents that use external tools. "
                "The method adds verifiable execution traces and reinforcement learning, "
                "improving reasoning reliability on agent benchmarks."
            ),
            categories=["cs.AI", "cs.LG"],
            primary_category="cs.AI",
            published=now - timedelta(hours=12),
            updated=now - timedelta(hours=12),
            abs_url="https://arxiv.org/abs/2501.00001",
            pdf_url="https://arxiv.org/pdf/2501.00001",
        ),
        Paper(
            arxiv_id="2501.00002",
            title="A Survey of Legacy Image Compression",
            authors=[Author("Example Author")],
            abstract="This survey reviews classical image compression algorithms.",
            categories=["cs.CV"],
            primary_category="cs.CV",
            published=now - timedelta(hours=20),
            updated=now - timedelta(hours=20),
            abs_url="https://arxiv.org/abs/2501.00002",
            pdf_url="https://arxiv.org/pdf/2501.00002",
        ),
    ]
