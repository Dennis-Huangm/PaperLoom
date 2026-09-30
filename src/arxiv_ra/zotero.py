from __future__ import annotations

import html
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import ZoteroConfig
from .utils import normalize_title


class ZoteroError(RuntimeError):
    pass


class ZoteroUnavailable(ZoteroError):
    pass


class ZoteroAuthorizationRequired(ZoteroError):
    pass


class ZoteroConflict(ZoteroError):
    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates = list(candidates)


@dataclass(slots=True)
class ZoteroSaveResult:
    item_key: str
    created: bool
    attachments_added: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "created": self.created,
            "attachments_added": self.attachments_added,
        }


class ZoteroClient:
    def __init__(
        self,
        config: ZoteroConfig,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Zotero local API 只允许本机 HTTP 地址")
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._server_id = ""

    def status(self) -> dict[str, Any]:
        try:
            response = self.client.get(
                f"{self.base_url}/",
                headers={"Zotero-API-Version": "3"},
            )
        except httpx.HTTPError as exc:
            return {"ready": False, "authorized": False, "detail": type(exc).__name__}
        if response.status_code != 200:
            return {
                "ready": False,
                "authorized": False,
                "detail": f"HTTP {response.status_code}",
            }
        self._server_id = response.headers.get("Zotero-Server-ID", "")
        return {
            "ready": True,
            "authorized": bool(os.getenv(self.config.api_key_env)),
            "detail": f"Zotero API {response.headers.get('Zotero-API-Version', '3')}",
            "server_id": self._server_id,
        }

    def authorize(self) -> str:
        server_id = self._require_server_id()
        try:
            response = self.client.post(
                f"{self.base_url}/local/authorize",
                json={"appName": "PaperLoom"},
                headers={
                    "Zotero-API-Version": "3",
                    "Zotero-Server-ID": server_id,
                },
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            raise ZoteroUnavailable("无法连接 Zotero 授权窗口") from exc
        if response.status_code == 403:
            raise ZoteroAuthorizationRequired("Zotero 写入授权被拒绝")
        if response.status_code != 200:
            raise ZoteroError(f"Zotero 授权失败：HTTP {response.status_code}")
        key = str((response.json() or {}).get("key") or "")
        if not key:
            raise ZoteroError("Zotero 未返回本地 API key")
        return key

    def save_paper(
        self,
        paper: dict[str, Any],
        verified: dict[str, Any] | None,
        profile_name: str,
        report_path: Path | None = None,
        pdf_path: Path | None = None,
        collection_key: str | None = None,
        *, receipt: dict | None = None, checkpoint=None,
        selected_key: str = "",
    ) -> ZoteroSaveResult:
        if not self.config.enabled:
            raise ZoteroError("Zotero 联动尚未启用")
        self._require_write_access()
        self._receipt = receipt if receipt is not None else {}
        self._checkpoint = checkpoint or (lambda: None)
        existing = selected_key or self._receipt.get("item_key")
        if existing:
            if self.get_item(existing) is None:
                raise ZoteroConflict("关联的 Zotero 条目已删除，请修复关联或明确重新收录")
        else:
            existing = self.find_paper(paper, verified or {})
        if collection_key == "__root__":
            target_collection = ""
        elif collection_key:
            target_collection = self._ensure_collection("", collection_key)
        else:
            target_collection = self._ensure_collection(self.config.collection_name)
        created = existing is None
        if existing:
            item_key = existing
            if target_collection:
                self._add_item_to_collection(item_key, target_collection)
        else:
            item_key = self._create_parent(
                paper, verified or {}, profile_name, target_collection
            )
            self._receipt['summary_pending'] = True
        self._receipt["item_key"] = item_key
        self._checkpoint()
        if self._receipt.get('summary_pending'):
            self._create_summary_note(item_key, paper, profile_name)
            self._receipt['summary_pending'] = False
            self._checkpoint()
        attachments_added = 0
        revision_label = f" · v{paper['version']}" if paper.get("version") else ""
        if self.config.attach_report and report_path and report_path.exists():
            attachments_added += int(
                self._create_linked_attachment(
                    item_key,
                    report_path,
                    f"PaperLoom 阅读报告 · {profile_name}{revision_label}",
                    "text/html",
                )
            )
        if self.config.attach_pdf and pdf_path and pdf_path.exists():
            if self.config.pdf_attachment_mode == "linked_file":
                added = self._create_linked_attachment(
                    item_key, pdf_path, f"论文 PDF{revision_label}", "application/pdf"
                )
            else:
                added = self._create_imported_attachment(
                    item_key, pdf_path, f"论文 PDF{revision_label}", "application/pdf"
                )
            attachments_added += int(added)
        return ZoteroSaveResult(item_key, created, attachments_added)

    def library_identity(self) -> str:
        """The local server identifies this personal library, independent of collections."""
        return self._require_server_id() + "/users/0"

    def get_item(self, key: str) -> dict | None:
        if not re.fullmatch(r"[A-Z0-9]{8}", key):
            raise ZoteroError("无效的 Zotero 条目 key")
        response = self.client.get(f"{self.base_url}/users/0/items/{key}", headers=self._headers())
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise ZoteroAuthorizationRequired("Zotero 授权已失效，请重新连接")
        if response.status_code != 200:
            raise ZoteroError(f"Zotero 读取失败：HTTP {response.status_code}")
        try:
            wrapper = response.json()
            data = wrapper.get("data", wrapper)
            if not isinstance(data, dict) or not data.get("key"):
                raise ValueError()
        except (ValueError, AttributeError) as exc:
            raise ZoteroError("Zotero 条目响应格式错误") from exc
        return None if data.get("deleted") else data

    def list_trackable_papers(self) -> list[dict[str, str]]:
        """Translate Zotero fields into revision-independent arXiv identities."""
        if not self.status().get("ready"):
            raise ZoteroUnavailable("Zotero 本地 API 未连接")
        identity = r"(?:[a-z-]+(?:\.[a-z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?"
        pattern = re.compile(r"arxiv(?:\.org/(?:abs|pdf)/|[:/\s]+)(" + identity + r")", re.I)
        papers = {}
        for wrapper in self._get_items("/users/0/items/top", {"format": "json"}):
            data = wrapper.get("data", wrapper)
            archive_id = str(data.get("archiveID") or "").strip()
            match = pattern.search(" ".join(str(data.get(field) or "") for field in ("archiveID", "extra", "url")))
            aid = match.group(1) if match else archive_id if re.fullmatch(identity, archive_id, re.I) else ""
            if aid:
                aid = re.sub(r"v\d+$", "", aid, flags=re.I)
                papers[aid] = {"arxiv_id": aid, "title": str(data.get("title") or "")}
        return list(papers.values())

    def list_collections(self) -> list[dict[str, str]]:
        items = self._get_items("/users/0/collections", {"format": "json"})
        by_key: dict[str, dict[str, str]] = {}
        for item in items:
            data = item.get("data", item)
            key = str(data.get("key") or item.get("key") or "")
            if not key:
                continue
            by_key[key] = {
                "key": key,
                "name": str(data.get("name") or key),
                "parent_key": str(data.get("parentCollection") or ""),
            }

        def label_for(key: str, seen: set[str] | None = None) -> str:
            seen = set(seen or ())
            if key in seen or key not in by_key:
                return by_key.get(key, {}).get("name", key)
            seen.add(key)
            item = by_key[key]
            parent = item["parent_key"]
            return (
                f"{label_for(parent, seen)} / {item['name']}"
                if parent and parent in by_key
                else item["name"]
            )

        result = [
            {**item, "label": label_for(key)} for key, item in by_key.items()
        ]
        result.sort(key=lambda item: item["label"].casefold())
        return result

    def _require_server_id(self) -> str:
        if self._server_id:
            return self._server_id
        status = self.status()
        if not status.get("ready") or not status.get("server_id"):
            raise ZoteroUnavailable("Zotero 未运行或本地 API 未启用")
        return str(status["server_id"])

    def _require_write_access(self) -> None:
        self._require_server_id()
        if not os.getenv(self.config.api_key_env):
            raise ZoteroAuthorizationRequired("请先在配置中心连接并授权 Zotero")

    def _headers(self, write: bool = False) -> dict[str, str]:
        headers = {"Zotero-API-Version": "3"}
        key = os.getenv(self.config.api_key_env)
        if key:
            headers["Zotero-API-Key"] = key
        if write:
            headers["Zotero-Server-ID"] = self._require_server_id()
            headers["Zotero-Write-Token"] = uuid.uuid4().hex
        return headers

    def _get_items(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        values = dict(params or {})
        values.setdefault('limit', 100)
        result: list[dict[str, Any]] = []
        while True:
            values['start'] = len(result)
            response = self.client.get(f"{self.base_url}{path}", params=values, headers=self._headers())
            if response.status_code == 401:
                raise ZoteroAuthorizationRequired("Zotero 授权已失效，请重新连接")
            if response.status_code != 200:
                raise ZoteroError(f"Zotero 读取失败：HTTP {response.status_code}")
            try:
                payload = response.json()
                total = int(response.headers.get('Total-Results', len(result) + len(payload)))
            except (ValueError, TypeError) as exc:
                raise ZoteroError("Zotero 响应格式错误") from exc
            if (not isinstance(payload, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get('data', item), dict) for item in payload)):
                raise ZoteroError("Zotero 列表响应格式错误")
            if not payload and len(result) < total:
                raise ZoteroError('Zotero 分页结果不完整，未执行写入')
            result.extend(payload)
            if len(result) >= total:
                return result
            if len(result) >= 100000:
                raise ZoteroError('Zotero 查询结果过多，未执行写入')

    def _post_objects(self, path: str, objects: list[dict[str, Any]]) -> str:
        response = self.client.post(
            f"{self.base_url}{path}",
            json=objects,
            headers=self._headers(write=True),
        )
        if response.status_code == 401:
            raise ZoteroAuthorizationRequired("Zotero 授权已失效，请重新连接")
        if response.status_code != 200:
            detail = ""
            try:
                detail = str(response.json())[:300]
            except Exception:
                detail = response.text[:300]
            raise ZoteroError(f"Zotero 写入失败：HTTP {response.status_code} {detail}")
        payload = response.json() or {}
        successful = payload.get("successful") or payload.get("success") or {}
        first = successful.get("0") if isinstance(successful, dict) else None
        if isinstance(first, dict):
            first = first.get("key") or (first.get("data") or {}).get("key")
        if not first:
            raise ZoteroError(f"Zotero 未返回新条目 key：{str(payload)[:300]}")
        return str(first)

    def _template(self, item_type: str, **params: str) -> dict[str, Any]:
        response = self.client.get(
            f"{self.base_url}/items/new",
            params={"itemType": item_type, **params},
            headers=self._headers(),
        )
        if response.status_code in {404, 405, 501}:
            return self._fallback_template(item_type, params.get("linkMode", ""))
        if response.status_code != 200:
            raise ZoteroError(f"无法获取 Zotero {item_type} 模板")
        payload = response.json()
        return payload.get("data", payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _fallback_template(item_type: str, link_mode: str = "") -> dict[str, Any]:
        if item_type == "preprint":
            return {
                "itemType": "preprint",
                "title": "",
                "creators": [],
                "abstractNote": "",
                "genre": "",
                "repository": "",
                "archiveID": "",
                "place": "",
                "date": "",
                "series": "",
                "seriesNumber": "",
                "DOI": "",
                "url": "",
                "accessDate": "",
                "archive": "",
                "archiveLocation": "",
                "language": "",
                "extra": "",
                "tags": [],
                "collections": [],
                "relations": {},
            }
        if item_type == "journalArticle":
            return {
                "itemType": "journalArticle",
                "title": "",
                "creators": [],
                "abstractNote": "",
                "publicationTitle": "",
                "date": "",
                "DOI": "",
                "url": "",
                "extra": "",
                "tags": [],
                "collections": [],
                "relations": {},
            }
        if item_type == "note":
            return {"itemType": "note", "note": "", "tags": [], "relations": {}}
        if item_type == "attachment":
            return {
                "itemType": "attachment",
                "linkMode": link_mode or "linked_url",
                "title": "",
                "accessDate": "",
                "note": "",
                "tags": [],
                "collections": [],
                "relations": {},
                "contentType": "",
                "charset": "",
                "filename": "",
                "md5": None,
                "mtime": None,
            }
        raise ZoteroError(f"不支持的 Zotero 条目模板：{item_type}")

    def find_paper(self, paper: dict[str, Any], verified: dict[str, Any]) -> str | None:
        arxiv_id = re.sub(r"v\d+$", "", str(paper.get("arxiv_id") or "").casefold())
        doi = self._doi(verified.get("doi") or paper.get("doi"))
        title = str(paper.get("title") or "").strip()
        candidates = {}
        for query in dict.fromkeys(value for value in (arxiv_id, doi, title) if value):
            for item in self._get_items("/users/0/items/top", {"q": query, "format": "json"}):
                data = item.get("data", item)
                key = str(data.get("key") or item.get("key") or "")
                if key and not data.get("deleted"):
                    candidates[key] = data
        reliable, suggestions = [], []
        for key, data in candidates.items():
            ids = self._arxiv_ids(data)
            item_doi = self._doi(data.get("DOI"))
            exact = (arxiv_id and arxiv_id in ids) or (doi and doi == item_doi)
            conflict = (ids and arxiv_id not in ids) or (doi and item_doi and doi != item_doi)
            candidate = {"key": key, "title": str(data.get("title") or key)}
            if exact and not conflict:
                reliable.append(candidate)
            elif exact or (title and normalize_title(data.get("title", "")) == normalize_title(title)):
                suggestions.append(candidate)
        if len(reliable) == 1 and not suggestions:
            return reliable[0]["key"]
        if reliable or suggestions:
            raise ZoteroConflict("发现多个匹配或仅标题匹配，请选择 Zotero 条目", reliable + suggestions)
        return None

    @staticmethod
    def _doi(value):
        return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value or "").strip(), flags=re.I).casefold()

    @staticmethod
    def _arxiv_ids(data):
        text = " ".join(str(data.get(field) or "") for field in ("archiveID", "extra", "url"))
        return {match.casefold() for match in re.findall(
            r"(?<![\w.])((?:[a-z-]+(?:\.[a-z]{2})?/\d{7}|\d{4}\.\d{4,5}))(?:v\d+)?(?![\w.])", text, re.I)}

    def _ensure_collection(self, name: str, preferred_key: str = "") -> str:
        collections = self.list_collections()
        if preferred_key:
            if any(item["key"] == preferred_key for item in collections):
                return preferred_key
            raise ZoteroError("选择的 Zotero 分类已不存在，请重新选择")
        if not name.strip():
            return ""
        for item in collections:
            if item["name"].strip().casefold() == name.strip().casefold():
                return item["key"]
        return self._post_objects(
            "/users/0/collections",
            [{"name": name.strip(), "parentCollection": False}],
        )

    def _add_item_to_collection(self, item_key: str, collection_key: str) -> bool:
        response = self.client.get(
            f"{self.base_url}/users/0/items/{item_key}",
            headers=self._headers(),
        )
        if response.status_code == 401:
            raise ZoteroAuthorizationRequired("Zotero 授权已失效，请重新连接")
        if response.status_code != 200:
            raise ZoteroError(f"无法读取 Zotero 条目：HTTP {response.status_code}")
        wrapper = response.json() or {}
        data = wrapper.get("data", wrapper)
        collections = list(data.get("collections") or [])
        if collection_key in collections:
            return False
        collections.append(collection_key)
        patch: dict[str, Any] = {"collections": collections}
        version = data.get("version") or wrapper.get("version")
        if version is not None:
            patch["version"] = version
        updated = self.client.patch(
            f"{self.base_url}/users/0/items/{item_key}",
            json=patch,
            headers=self._headers(write=True),
        )
        if updated.status_code == 401:
            raise ZoteroAuthorizationRequired("Zotero 授权已失效，请重新连接")
        if updated.status_code not in {200, 204}:
            raise ZoteroError(f"加入 Zotero 分类失败：HTTP {updated.status_code}")
        return True

    def _create_parent(
        self,
        paper: dict[str, Any],
        verified: dict[str, Any],
        profile_name: str,
        collection_key: str,
    ) -> str:
        try:
            item = self._template("preprint")
        except ZoteroError:
            item = self._template("journalArticle")
        item.update(
            {
                "title": str(paper.get("title") or ""),
                "creators": [
                    {"creatorType": "author", "name": str(author.get("name") or "")}
                    for author in (paper.get("authors") or [])
                    if author.get("name")
                ],
                "abstractNote": str(paper.get("abstract") or ""),
                "date": str(paper.get("published") or "")[:10],
                "url": str(paper.get("abs_url") or ""),
                "DOI": str(verified.get("doi") or paper.get("doi") or ""),
                "repository": "arXiv",
                "archive": "arXiv",
                "archiveID": str(paper.get("arxiv_id") or ""),
                "extra": f"arXiv: {paper.get('arxiv_id', '')}",
                "tags": self._tags(paper, profile_name),
                "collections": [collection_key] if collection_key else [],
            }
        )
        for field in list(item):
            if field in {"key", "version", "dateAdded", "dateModified"}:
                item.pop(field, None)
        return self._post_objects("/users/0/items", [item])

    def _tags(self, paper: dict[str, Any], profile_name: str) -> list[dict[str, str]]:
        values = [str(paper.get("primary_category") or "")]
        if self.config.add_profile_tag:
            values.append(profile_name)
        return [{"tag": value} for value in dict.fromkeys(value for value in values if value)]

    def _create_summary_note(
        self, item_key: str, paper: dict[str, Any], profile_name: str
    ) -> None:
        marker = f'PaperLoom-summary:{profile_name}'
        children = self._get_items(f'/users/0/items/{item_key}/children', {'format': 'json'})
        if any(any(tag.get('tag') == marker for tag in child.get('data', child).get('tags', [])) for child in children):
            return
        note = self._template("note")
        reason = html.escape(str(paper.get("recommendation_reason") or ""))
        arxiv_id = html.escape(str(paper.get("arxiv_id") or ""))
        note.update(
            {
                "parentItem": item_key,
                "note": (
                    "<h2>PaperLoom</h2>"
                    f"<p><strong>研究方向：</strong>{html.escape(profile_name)}</p>"
                    f"<p><strong>推荐理由：</strong>{reason or '由当前研究方向自动筛选。'}</p>"
                    f"<p><strong>arXiv ID：</strong>{arxiv_id}</p>"
                ),
                "tags": [{"tag": "PaperLoom"}, {"tag": marker}],
            }
        )
        self._post_objects("/users/0/items", [note])

    def _create_linked_attachment(
        self,
        item_key: str,
        path: Path,
        title: str,
        content_type: str,
    ) -> bool:
        resolved = path.resolve()
        children = self._get_items(f"/users/0/items/{item_key}/children", {"format": "json"})
        for child in children:
            data = child.get("data", child)
            if str(data.get("path") or "").casefold() == str(resolved).casefold():
                return False
        attachment = self._template("attachment", linkMode="linked_file")
        for stored_only_field in ("filename", "md5", "mtime"):
            attachment.pop(stored_only_field, None)
        attachment.update(
            {
                "parentItem": item_key,
                "linkMode": "linked_file",
                "title": title,
                "path": str(resolved),
                "contentType": content_type,
            }
        )
        self._post_objects("/users/0/items", [attachment])
        return True

    def _create_imported_attachment(
        self,
        item_key: str,
        path: Path,
        title: str,
        content_type: str,
    ) -> bool:
        resolved = path.resolve()
        digest = hashlib.md5(resolved.read_bytes()).hexdigest()
        receipt = getattr(self, "_receipt", {})
        checkpoint = getattr(self, "_checkpoint", lambda: None)
        material = receipt.setdefault("files", {}).setdefault(digest, {})
        children = self._get_items(f"/users/0/items/{item_key}/children", {"format": "json"})
        for child in children:
            data = child.get("data", child)
            if (
                str(data.get("contentType") or "").casefold() == content_type.casefold()
                and str(data.get("md5") or "").casefold() == digest
            ):
                material.update(key=data.get("key") or child.get("key"), status="succeeded")
                checkpoint()
                return False
        marker = f"PaperLoom-file:{digest}"
        recoverable = [child.get("data", child) for child in children
                       if any(tag.get("tag") == marker for tag in child.get("data", child).get("tags", []))]
        if not material.get("key") and len(recoverable) == 1:
            material["key"] = recoverable[0]["key"]
        if len(recoverable) > 1:
            raise ZoteroConflict("发现多个待上传附件，请先在 Zotero 中核对")
        if material.get("key") and self.get_item(material["key"]) is None:
            raise ZoteroConflict("已收录附件被删除，请明确重新收录")
        attachment = self._template("attachment", linkMode="imported_file")
        attachment.update(
            {
                "parentItem": item_key,
                "linkMode": "imported_file",
                "title": title,
                "contentType": content_type,
                "filename": resolved.name,
                "tags": [{"tag": marker}],
            }
        )
        attachment_key = material.get("key") or self._post_objects("/users/0/items", [attachment])
        material.update(key=attachment_key, status="uploading")
        checkpoint()
        file_endpoint = f"{self.base_url}/users/0/items/{attachment_key}/file"
        headers = self._headers(write=True)
        headers["If-None-Match"] = "*"
        authorization = self.client.post(
            file_endpoint,
            data={
                "md5": digest,
                "filename": resolved.name,
                "filesize": resolved.stat().st_size,
                "mtime": int(resolved.stat().st_mtime * 1000),
            },
            headers=headers,
        )
        if authorization.status_code == 401:
            raise ZoteroAuthorizationRequired("Zotero 授权已失效，请重新连接")
        if authorization.status_code != 200:
            raise ZoteroError(f"Zotero PDF 上传授权失败：HTTP {authorization.status_code}")
        upload = authorization.json() or {}
        if upload.get("exists") == 1:
            material["status"] = "succeeded"
            checkpoint()
            return True
        upload_url = str(upload.get("url") or "")
        upload_key = str(upload.get("uploadKey") or "")
        if not upload_url or not upload_key:
            raise ZoteroError("Zotero 未返回 PDF 上传地址")
        if urlparse(upload_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ZoteroError("Zotero 本地 API 返回了非本机上传地址，已拒绝传输 PDF")
        body = (
            str(upload.get("prefix") or "").encode("utf-8")
            + resolved.read_bytes()
            + str(upload.get("suffix") or "").encode("utf-8")
        )
        uploaded = self.client.post(
            upload_url,
            content=body,
            headers={"Content-Type": str(upload.get("contentType") or "application/octet-stream")},
        )
        if uploaded.status_code != 201:
            raise ZoteroError(f"Zotero PDF 文件上传失败：HTTP {uploaded.status_code}")
        register_headers = self._headers(write=True)
        register_headers["If-None-Match"] = "*"
        registered = self.client.post(
            file_endpoint,
            data={"upload": upload_key},
            headers=register_headers,
        )
        if registered.status_code != 204:
            raise ZoteroError(f"Zotero PDF 注册失败：HTTP {registered.status_code}")
        material["status"] = "succeeded"
        checkpoint()
        return True
