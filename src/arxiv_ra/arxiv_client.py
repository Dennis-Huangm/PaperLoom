from __future__ import annotations

import re
import time
import uuid
import math
import ssl
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import __version__
from .models import Author, Paper
from .rate_limit import defer_rate_limit, shared_rate_limit
from .task_runtime import task_checkpoint, task_progress
from .utils import normalize_space
from .pdf_download import reusable_pdf, validate_pdf

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
        # Match the TLS 1.3 capability advertised by stdlib HTTPSConnection and
        # urllib3. arXiv cold queries returned empty HTTP 406 with httpx's default
        # handshake, while this single option allowed the identical request.
        # Keep httpx's CA environment handling and certificate verification.
        tls = httpx.create_ssl_context()
        if ssl.HAS_TLSv1_3:
            tls.post_handshake_auth = True
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": f"PaperLoom/{__version__} (personal research use)"},
            verify=tls,
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
        *, query_groups: list[list[str]] | None = None,
    ) -> list[Paper]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=lookback_days)
        category_query = " OR ".join(f"cat:{category}" for category in categories)
        date_query = f"submittedDate:[{start:%Y%m%d%H%M} TO {now:%Y%m%d%H%M}]"
        search_query = f"({category_query}) AND {date_query}"
        if query_groups is not None:
            if not query_groups or any(not group for group in query_groups):
                raise ValueError("结构化查询必须包含非空概念组")
            groups = []
            for group in query_groups:
                if any(not isinstance(term, str) or not term.strip() or re.search(r'["\\\[\]():]', term) for term in group):
                    raise ValueError("查询短语不接受原始查询语法")
                groups.append("(" + " OR ".join(f'all:"{term}"' for term in group) + ")")
            search_query += " AND (" + " AND ".join(groups) + ")"
        elif query_terms:
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
        self._download_pdf(paper.pdf_url, destination, paper.arxiv_id)

    def _download_pdf(self, url: str, destination: Path, label: str) -> None:
        """Resume interrupted identity streams only with a known size and validator."""
        backend = os.environ.get("ARXIV_PDF_BACKEND", "httpx").strip().lower()
        if backend == "curl-direct":
            from .pdf_download import download_with_curl
            download_with_curl(url, destination, label, self.max_retries)
            return
        if backend != "httpx":
            raise ValueError("ARXIV_PDF_BACKEND 仅支持 httpx 或 curl-direct")
        destination.parent.mkdir(parents=True, exist_ok=True)
        retry_range = False
        temporary = destination.with_name(f".~{uuid.uuid4().hex[:12]}.part")
        total_size = None
        validator = None
        started = time.monotonic()
        try:
            for attempt in range(self.max_retries + 1):
                resume_size = temporary.stat().st_size if temporary.exists() and total_size and validator else 0
                task_progress(
                    f"正在连接论文 PDF，续传 {resume_size / (1024 * 1024):.1f} MiB…"
                    if resume_size else "正在连接论文 PDF…", 27,
                )
                try:
                    headers = {"Accept-Encoding": "identity"}
                    if resume_size:
                        headers.update({"Range": f"bytes={resume_size}-", "If-Range": validator[1]})
                    elif retry_range:
                        headers["Range"] = "bytes=0-"
                    with self.client.stream("GET", url, headers=headers) as response:
                        response.raise_for_status()
                        identity = response.headers.get("Content-Encoding", "identity") == "identity"
                        if response.status_code == 206:
                            span = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                            if (not span or int(span[1]) != resume_size
                                    or int(span[2]) + 1 != int(span[3]) or not identity
                                    or resume_size and total_size != int(span[3])
                                    or resume_size and response.headers.get(validator[0], validator[1]) != validator[1]):
                                total_size, validator = None, None
                                raise httpx.RemoteProtocolError("arXiv PDF 返回了不完整或不匹配的文件范围")
                            total_size = int(span[3])
                        else:
                            # A 200 response to If-Range is a fresh complete body.
                            resume_size = 0
                            content_length = response.headers.get("Content-Length", "")
                            total_size = int(content_length) if identity and content_length.isdecimal() else None
                        etag = response.headers.get("ETag", "")
                        if etag and not etag.startswith("W/"):
                            validator = ("ETag", etag)
                        elif response.headers.get("Last-Modified"):
                            validator = ("Last-Modified", response.headers["Last-Modified"])
                        elif not resume_size:
                            validator = None
                        received_size = resume_size
                        last_update = time.monotonic() - 1.0

                        def publish_progress(now: float) -> None:
                            elapsed = max(now - started, 0.001)
                            size = f"{received_size / (1024 * 1024):.1f}"
                            if total_size:
                                size += f" / {total_size / (1024 * 1024):.1f}"
                            percent = 27 + int(12 * min(received_size / total_size, 1)) if total_size else 27
                            task_progress(
                                f"正在下载论文 PDF：{size} MiB · "
                                f"{received_size / 1024 / elapsed:.0f} KiB/s · 已用 {elapsed:.0f} 秒",
                                percent,
                            )

                        with temporary.open("ab" if resume_size else "wb") as handle:
                            for chunk in response.iter_bytes():
                                task_checkpoint()
                                handle.write(chunk)
                                received_size += len(chunk)
                                now = time.monotonic()
                                if now - last_update >= 1.0:
                                    publish_progress(now)
                                    last_update = now
                        if total_size is not None and received_size != total_size:
                            raise httpx.RemoteProtocolError(
                                f"arXiv PDF 文件长度不符：received {received_size}, expected {total_size}"
                            )
                        publish_progress(time.monotonic())
                    validate_pdf(temporary, label)
                    task_checkpoint()
                    temporary.replace(destination)
                    return
                except httpx.TransportError as exc:
                    if isinstance(exc, httpx.RemoteProtocolError):
                        retry_range = True
                    if attempt == self.max_retries:
                        raise RuntimeError(
                            f"arXiv PDF 下载失败：{label}，已自动重试 {self.max_retries} 次；"
                            f"最后一次错误：{type(exc).__name__}: {exc}"
                        ) from exc
                    if not (total_size and validator and temporary.exists()
                            and 0 < temporary.stat().st_size < total_size):
                        temporary.unlink(missing_ok=True)
                    task_progress(
                        f"PDF 下载连接中断（{type(exc).__name__}），准备第 {attempt + 1}/{self.max_retries} 次重试…",
                        27,
                    )
                time.sleep(min(self.retry_base_delay * (2**attempt), 30.0))
        finally:
            temporary.unlink(missing_ok=True)

    def download_version(self, arxiv_id: str, version: int, destination) -> None:
        destination = destination.resolve()
        if reusable_pdf(destination):
            return
        url = f"https://arxiv.org/pdf/{arxiv_id}v{version}"
        for attempt in range(2):
            try:
                self._download_pdf(url, destination, f"{arxiv_id}v{version}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt == 0:
                    time.sleep(5)
                    continue
                raise
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
