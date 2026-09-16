from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from . import __version__
from .models import Author, Paper
from .utils import extract_json_object, normalize_space


DEFAULT_ENDPOINT = "https://api.alphaxiv.org/mcp/v1"
ARXIV_ID_RE = re.compile(
    r"(?:arxiv:)?((?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?)",
    re.IGNORECASE,
)


class AlphaXivError(RuntimeError):
    """Base error for alphaXiv semantic discovery."""


class AlphaXivUnavailable(AlphaXivError):
    pass


class AlphaXivClient:
    """Minimal Streamable HTTP MCP client for alphaXiv discovery.

    The adapter uses ``discover_papers`` and marks results as partial. It does not treat
    alphaXiv as the authoritative source for venues, DOI, or publication dates;
    those fields continue through the existing metadata verifier.
    """

    def __init__(
        self,
        api_key: str = "",
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 45.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.endpoint = endpoint.rstrip("/") or DEFAULT_ENDPOINT
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": f"PaperLoom/{__version__} (alphaXiv discovery)"
            },
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def discover(
        self,
        *,
        keywords: list[str],
        question: str,
        published_after: str,
        limit: int = 15,
        difficulty: int = 5,
        excluded_ids: set[str] | None = None,
    ) -> list[Paper]:
        if not self.enabled:
            raise AlphaXivUnavailable(
                "alphaXiv 语义检索未配置 API Key；请在配置中心填写 API Key"
            )
        excluded_ids = {item.strip() for item in (excluded_ids or set()) if item.strip()}
        if excluded_ids:
            exclusions = ", ".join(sorted(excluded_ids)[:50])
            question = (
                f"{question.strip()}\nDo not return papers with these arXiv IDs because they were already recommended: {exclusions}. "
                "Return different relevant papers instead."
            )
        arguments = {
            "keywords": self._keywords(keywords),
            "question": question.strip() or "Recent research papers relevant to this topic.",
            "difficulty": max(1, min(10, int(difficulty))),
            "published_after": published_after,
            "prioritize": "default",
        }
        payload = self._call_tool("discover_papers", arguments)
        normalized = self._normalize_papers(payload)
        if isinstance(payload, dict) and payload.get("text") and not normalized:
            raise AlphaXivError("alphaXiv 返回的候选格式无法识别，未将解析失败当作空检索结果")
        cutoff = self._date(published_after)
        papers = [
            paper
            for paper in normalized
            if paper.arxiv_id not in excluded_ids
            and (paper.published is None or cutoff is None or paper.published.date() >= cutoff.date())
        ]
        return papers[: max(1, min(15, int(limit)))]

    def lookup(self, arxiv_id: str) -> Paper | None:
        """Resolve one exact arXiv ID through alphaXiv without substitutions."""
        papers = self.discover(
            keywords=[arxiv_id, "arXiv paper", "research paper"],
            question=(
                f"Find the exact paper with arXiv ID {arxiv_id}. "
                "Do not substitute a different paper."
            ),
            published_after="1991-01-01",
            limit=10,
            difficulty=1,
        )
        base_id = re.sub(r"v\d+$", "", arxiv_id, flags=re.I)
        return next((paper for paper in papers if paper.arxiv_id == base_id), None)

    def _headers(self, session_id: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-03-26",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _rpc(self, body: dict[str, Any], session_id: str = "") -> tuple[dict[str, Any], str]:
        response = self.client.post(
            self.endpoint,
            json=body,
            headers=self._headers(session_id),
        )
        if response.status_code in {401, 403}:
            raise AlphaXivUnavailable("alphaXiv API Key 无效或无权访问")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AlphaXivError(
                f"alphaXiv 请求失败（HTTP {response.status_code}）"
            ) from exc
        session = response.headers.get("Mcp-Session-Id", session_id)
        if "id" not in body:
            return {}, session  # Notifications normally receive 202 with no body.
        return self._response_json(response, body["id"]), session

    @staticmethod
    def _response_json(response: httpx.Response, request_id: int | None = None) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            try:
                value = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise AlphaXivError("alphaXiv 返回了无法解析的 JSON") from exc
            if not isinstance(value, dict) or (request_id is not None and value.get("id") != request_id):
                raise AlphaXivError("alphaXiv 返回了不匹配的 MCP 响应")
            return value
        events: list[dict[str, Any]] = []
        data: list[str] = []
        for line in [*response.text.splitlines(), ""]:
            if not line and data:
                try:
                    value = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    value = None
                data = []
                if isinstance(value, dict):
                    events.append(value)
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if request_id is not None:
            events = [item for item in events if item.get("id") == request_id and ("result" in item or "error" in item)]
        if not events:
            raise AlphaXivError("alphaXiv 返回了空的 MCP 事件流")
        return events[-1]

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        initialize, session_id = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "paperloom",
                        "version": __version__,
                    },
                },
            }
        )
        if initialize.get("error"):
            raise AlphaXivError(self._error_text(initialize["error"]))
        if (initialize.get("result") or {}).get("protocolVersion") != "2025-03-26":
            raise AlphaXivError("alphaXiv 协商了不支持的 MCP 协议版本")
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)
        response, _session_id = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            session_id,
        )
        if response.get("error"):
            raise AlphaXivError(self._error_text(response["error"]))
        result = response.get("result") or {}
        if result.get("isError"):
            raise AlphaXivError(self._content_text(result) or "alphaXiv 工具调用失败")
        if isinstance(result.get("structuredContent"), (dict, list)):
            return result["structuredContent"]
        text = self._content_text(result)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                try:
                    return extract_json_object(text)
                except (ValueError, json.JSONDecodeError):
                    return {"text": text}
        return result

    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(error)

    @staticmethod
    def _content_text(result: dict[str, Any]) -> str:
        content = result.get("content") or []
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()

    @staticmethod
    def _keywords(values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            clean = normalize_space(value)
            if clean and clean.casefold() not in {item.casefold() for item in result}:
                result.append(clean)
        return (result or ["AI", "multimodal", "agent"])[:4]

    @classmethod
    def _normalize_papers(cls, payload: Any) -> list[Paper]:
        if isinstance(payload, dict) and payload.get("text"):
            records = cls._records_from_text(str(payload["text"]))
        else:
            records = payload.get("papers", []) if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            return []
        papers: list[Paper] = []
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            arxiv_id = cls._extract_id(record)
            if not arxiv_id or arxiv_id in seen:
                continue
            title = normalize_space(str(record.get("title") or record.get("name") or ""))
            if not title:
                continue
            published = cls._date(record.get("published") or record.get("publication_date"))
            authors = []
            for author in record.get("authors") or record.get("contributors") or []:
                if isinstance(author, dict):
                    name = normalize_space(str(author.get("name") or author.get("full_name") or ""))
                else:
                    name = normalize_space(str(author))
                if name:
                    authors.append(Author(name))
            abstract = normalize_space(
                str(record.get("abstract") or record.get("abstract_preview") or record.get("summary") or "")
            )
            papers.append(
                Paper(
                    arxiv_id=arxiv_id,
                    title=title,
                    authors=authors,
                    abstract=abstract,
                    categories=[],
                    primary_category="",
                    published=published,
                    updated=None,
                    abs_url=f"https://arxiv.org/abs/{arxiv_id}",
                    pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                    version=cls._version(record),
                    discovery_sources=["alphaxiv"],
                    metadata_source="alphaxiv",
                    metadata_status="partial",
                    abstract_kind="preview",
                )
            )
            seen.add(arxiv_id)
        return papers

    @classmethod
    def _records_from_text(cls, text: str) -> list[dict[str, Any]]:
        """Parse alphaXiv's numbered Markdown search result format."""
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            id_match = re.search(r"\[ID=([^\]]+)\]", line, re.IGNORECASE)
            title_match = re.search(r"\*\*(.+?)\*\*", line)
            if not id_match or not title_match:
                continue
            date_match = re.search(r"\bPublished\s+(\d{4}-\d{2}-\d{2})\b", line)
            abstract_match = re.search(r"\bviews:\s*(.+)$", line, re.IGNORECASE)
            url_match = re.search(r"\((https?://[^)]+)\)", line)
            records.append(
                {
                    "arxiv_id": id_match.group(1).strip(),
                    "title": title_match.group(1).strip(),
                    "published": date_match.group(1) if date_match else "",
                    "abstract_preview": abstract_match.group(1).strip()
                    if abstract_match
                    else "",
                    "url": url_match.group(1) if url_match else "",
                }
            )
        return records

    @staticmethod
    def _extract_id(record: dict[str, Any]) -> str:
        candidates = [
            record.get("arxiv_id"),
            record.get("arxivId"),
            record.get("id"),
            record.get("url"),
            record.get("abs_url"),
        ]
        for value in candidates:
            match = ARXIV_ID_RE.search(str(value or ""))
            if match:
                return re.sub(r"v\d+$", "", match.group(1), flags=re.IGNORECASE)
        return ""

    @staticmethod
    def _version(record: dict[str, Any]) -> int | None:
        for key in ("arxiv_id", "arxivId", "id", "url", "abs_url"):
            match = ARXIV_ID_RE.search(str(record.get(key) or ""))
            version = re.search(r"v(\d+)$", match.group(1), re.I) if match else None
            if version:
                return int(version.group(1))
        return None

    @staticmethod
    def _date(value: Any) -> datetime | None:
        raw = str(value or "").strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
