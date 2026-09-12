from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from arxiv_ra.config import ZoteroConfig
from arxiv_ra.zotero import ZoteroClient


def _paper() -> dict:
    return {
        "arxiv_id": "2407.05600",
        "title": "GenArtist: Multimodal LLM as an Agent",
        "authors": [{"name": "Zhenyu Wang"}, {"name": "Aoxue Li"}],
        "abstract": "A multimodal agent for image generation and editing.",
        "primary_category": "cs.CV",
        "published": "2024-07-08T00:00:00+00:00",
        "abs_url": "https://arxiv.org/abs/2407.05600",
        "pdf_url": "https://arxiv.org/pdf/2407.05600",
        "recommendation_reason": "与 AgenticT2I 高度相关。",
    }


def test_local_authorization_returns_key(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/":
            return httpx.Response(
                200,
                headers={"Zotero-Server-ID": "SERVER1", "Zotero-API-Version": "3"},
            )
        if request.method == "POST" and request.url.path == "/api/local/authorize":
            assert request.headers["Zotero-Server-ID"] == "SERVER1"
            return httpx.Response(200, json={"key": "local-secret", "remember": True})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    zotero = ZoteroClient(ZoteroConfig(), client=client)

    assert zotero.authorize() == "local-secret"


def test_save_paper_creates_collection_note_and_imported_pdf(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "secret")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test-content")
    calls = {"parent": 0, "note": 0, "attachment": 0, "uploaded": b""}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/":
            return httpx.Response(
                200,
                headers={"Zotero-Server-ID": "SERVER1", "Zotero-API-Version": "3"},
            )
        if request.method == "GET" and path == "/api/users/0/items/top":
            return httpx.Response(200, json=[])
        if request.method == "GET" and path == "/api/users/0/collections":
            return httpx.Response(200, json=[])
        if request.method == "POST" and path == "/api/users/0/collections":
            return httpx.Response(200, json={"successful": {"0": {"key": "COLL0001"}}})
        if request.method == "GET" and path == "/api/items/new":
            item_type = request.url.params.get("itemType")
            if item_type == "preprint":
                return httpx.Response(
                    200,
                    json={
                        "itemType": "preprint",
                        "title": "",
                        "creators": [],
                        "abstractNote": "",
                        "date": "",
                        "url": "",
                        "DOI": "",
                        "extra": "",
                        "tags": [],
                        "collections": [],
                    },
                )
            if item_type == "note":
                return httpx.Response(200, json={"itemType": "note", "note": "", "tags": []})
            if item_type == "attachment":
                return httpx.Response(
                    200,
                    json={
                        "itemType": "attachment",
                        "linkMode": "imported_file",
                        "title": "",
                        "contentType": "",
                        "filename": "",
                    },
                )
        if request.method == "POST" and path == "/api/users/0/items":
            item = json.loads(request.content)[0]
            if item["itemType"] == "preprint":
                calls["parent"] += 1
                assert item["collections"] == ["COLL0001"]
                assert {tag["tag"] for tag in item["tags"]} == {"cs.CV", "AgenticT2I"}
                return httpx.Response(200, json={"successful": {"0": {"key": "PARENT01"}}})
            if item["itemType"] == "note":
                calls["note"] += 1
                assert item["parentItem"] == "PARENT01"
                return httpx.Response(200, json={"successful": {"0": {"key": "NOTE0001"}}})
            if item["itemType"] == "attachment":
                calls["attachment"] += 1
                assert item["parentItem"] == "PARENT01"
                return httpx.Response(200, json={"successful": {"0": {"key": "ATTACH01"}}})
        if request.method == "GET" and path == "/api/users/0/items/PARENT01/children":
            return httpx.Response(200, json=[])
        if request.method == "POST" and path == "/api/users/0/items/ATTACH01/file":
            form = parse_qs(request.content.decode())
            if "md5" in form:
                return httpx.Response(
                    200,
                    json={
                        "url": "http://127.0.0.1:23119/upload/PDF1",
                        "uploadKey": "UPLOAD1",
                        "contentType": "application/octet-stream",
                        "prefix": "PREFIX",
                        "suffix": "SUFFIX",
                    },
                )
            assert form["upload"] == ["UPLOAD1"]
            return httpx.Response(204)
        if request.method == "POST" and path == "/upload/PDF1":
            calls["uploaded"] = request.content
            return httpx.Response(201)
        return httpx.Response(404, text=f"unexpected {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    zotero = ZoteroClient(ZoteroConfig(), client=client)

    result = zotero.save_paper(_paper(), {"doi": "10.1/example"}, "AgenticT2I", pdf_path=pdf)

    assert result.created is True
    assert result.item_key == "PARENT01"
    assert result.attachments_added == 1
    assert calls["parent"] == calls["note"] == calls["attachment"] == 1
    assert calls["uploaded"] == b"PREFIX%PDF-test-contentSUFFIX"


def test_existing_arxiv_item_is_not_created_again(monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "secret")
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/":
            return httpx.Response(200, headers={"Zotero-Server-ID": "SERVER1"})
        if request.method == "GET" and path == "/api/users/0/items/top":
            return httpx.Response(
                200,
                json=[{"data": {"key": "EXISTING", "title": _paper()["title"], "extra": "arXiv: 2407.05600"}}],
            )
        if request.method == "GET" and path == "/api/users/0/collections":
            return httpx.Response(200, json=[{"data": {"key": "COLL0001", "name": "arXiv Research Assistant"}}])
        if request.method == "POST":
            posts.append(path)
        return httpx.Response(404)

    config = ZoteroConfig(attach_pdf=False, attach_report=False)
    zotero = ZoteroClient(config, client=httpx.Client(transport=httpx.MockTransport(handler)))

    result = zotero.save_paper(_paper(), {}, "AgenticT2I", collection_key="__root__")

    assert result.created is False
    assert result.item_key == "EXISTING"
    assert posts == []


def test_title_query_finds_item_when_local_api_does_not_index_arxiv_id(monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/":
            return httpx.Response(200, headers={"Zotero-Server-ID": "SERVER1"})
        if request.method == "GET" and path == "/api/users/0/items/top":
            if request.url.params.get("q") == _paper()["title"]:
                return httpx.Response(
                    200,
                    json=[{"data": {"key": "BYTITLE1", "title": _paper()["title"]}}],
                )
            return httpx.Response(200, json=[])
        if request.method == "GET" and path == "/api/users/0/collections":
            return httpx.Response(
                200,
                json=[{"data": {"key": "COLL0001", "name": "arXiv Research Assistant"}}],
            )
        return httpx.Response(500)

    zotero = ZoteroClient(
        ZoteroConfig(attach_pdf=False, attach_report=False),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = zotero.save_paper(_paper(), {}, "AgenticT2I", collection_key="__root__")

    assert result.created is False
    assert result.item_key == "BYTITLE1"


def test_duplicate_matches_prefer_oldest_zotero_item(monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/":
            return httpx.Response(200, headers={"Zotero-Server-ID": "SERVER1"})
        if request.method == "GET" and path == "/api/users/0/items/top":
            if request.url.params.get("q") == _paper()["title"]:
                return httpx.Response(
                    200,
                    json=[
                        {"data": {"key": "NEWER001", "title": _paper()["title"], "dateAdded": "2026-08-19T10:00:00Z"}},
                        {"data": {"key": "OLDEST01", "title": _paper()["title"], "dateAdded": "2026-07-01T10:00:00Z"}},
                    ],
                )
            return httpx.Response(200, json=[])
        if request.method == "GET" and path == "/api/users/0/collections":
            return httpx.Response(200, json=[])
        if request.method == "POST" and path == "/api/users/0/collections":
            return httpx.Response(200, json={"successful": {"0": {"key": "COLL0001"}}})
        return httpx.Response(500)

    zotero = ZoteroClient(
        ZoteroConfig(attach_pdf=False, attach_report=False),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = zotero.save_paper(_paper(), {}, "AgenticT2I", collection_key="__root__")

    assert result.created is False
    assert result.item_key == "OLDEST01"


def test_collection_tree_labels_nested_collections() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/users/0/collections":
            return httpx.Response(
                200,
                json=[
                    {"data": {"key": "PARENT01", "name": "AI", "parentCollection": False}},
                    {"data": {"key": "CHILD001", "name": "Agents", "parentCollection": "PARENT01"}},
                ],
            )
        return httpx.Response(404)

    zotero = ZoteroClient(
        ZoteroConfig(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    collections = zotero.list_collections()

    assert [item["label"] for item in collections] == ["AI", "AI / Agents"]


def test_existing_item_is_added_to_selected_collection(monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "secret")
    patched = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/":
            return httpx.Response(200, headers={"Zotero-Server-ID": "SERVER1"})
        if request.method == "GET" and path == "/api/users/0/items/top":
            if request.url.params.get("q") == _paper()["title"]:
                return httpx.Response(200, json=[{"data": {"key": "EXISTING", "title": _paper()["title"]}}])
            return httpx.Response(200, json=[])
        if request.method == "GET" and path == "/api/users/0/collections":
            return httpx.Response(200, json=[{"data": {"key": "TARGET01", "name": "Reading"}}])
        if request.method == "GET" and path == "/api/users/0/items/EXISTING":
            return httpx.Response(200, json={"data": {"key": "EXISTING", "version": 12, "collections": []}})
        if request.method == "PATCH" and path == "/api/users/0/items/EXISTING":
            patched.update(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(500)

    zotero = ZoteroClient(
        ZoteroConfig(attach_pdf=False, attach_report=False),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = zotero.save_paper(_paper(), {}, "AgenticT2I", collection_key="TARGET01")

    assert result.created is False
    assert patched == {"collections": ["TARGET01"], "version": 12}


def test_local_client_rejects_non_local_api_url() -> None:
    with pytest.raises(ValueError, match="本机"):
        ZoteroClient(ZoteroConfig(base_url="https://example.com/api"))


def test_existing_pdf_attachment_prevents_duplicate_import(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "secret")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-new-copy")
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/children"):
            return httpx.Response(
                200,
                json=[
                    {
                        "data": {
                            "itemType": "attachment",
                            "contentType": "application/pdf",
                            "filename": "different-name.pdf",
                            "md5": "different-hash",
                        }
                    }
                ],
            )
        if request.method == "POST":
            posts.append(request.url.path)
        return httpx.Response(404)

    zotero = ZoteroClient(
        ZoteroConfig(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    zotero._server_id = "SERVER1"

    assert zotero._create_imported_attachment("PARENT01", pdf, "论文 PDF", "application/pdf") is False
    assert posts == []


def test_missing_local_template_endpoint_uses_v3_fallback() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, text="No endpoint found"))
    )
    zotero = ZoteroClient(ZoteroConfig(), client=client)

    preprint = zotero._template("preprint")
    attachment = zotero._template("attachment", linkMode="imported_file")

    assert preprint["itemType"] == "preprint"
    assert "archiveID" in preprint
    assert attachment["linkMode"] == "imported_file"


def test_linked_attachment_does_not_send_stored_file_filename(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "secret")
    report = tmp_path / "report.html"
    report.write_text("<h1>Report</h1>", encoding="utf-8")
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/":
            return httpx.Response(200, headers={"Zotero-Server-ID": "SERVER1"})
        if request.method == "GET" and request.url.path.endswith("/children"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/api/items/new":
            return httpx.Response(404)
        if request.method == "POST" and request.url.path == "/api/users/0/items":
            posted.update(json.loads(request.content)[0])
            return httpx.Response(200, json={"successful": {"0": {"key": "LINK0001"}}})
        return httpx.Response(404)

    zotero = ZoteroClient(
        ZoteroConfig(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    zotero._server_id = "SERVER1"

    assert zotero._create_linked_attachment("PARENT01", report, "报告", "text/html") is True
    assert posted["linkMode"] == "linked_file"
    assert posted["path"] == str(report.resolve())
    assert "filename" not in posted
