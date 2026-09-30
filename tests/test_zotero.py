from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from arxiv_ra.config import ZoteroConfig
from arxiv_ra.zotero import ZoteroClient, ZoteroConflict


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
                tags = {tag['tag'] for tag in item['tags']}
                assert {'cs.CV', 'AgenticT2I'} <= tags
                assert len([tag for tag in tags if tag.startswith('PaperLoom-receipt:')]) == 1
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


def test_title_only_match_requires_user_selection(monkeypatch) -> None:
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

    with pytest.raises(ZoteroConflict) as error:
        zotero.save_paper(_paper(), {}, "AgenticT2I", collection_key="__root__")
    assert [item["key"] for item in error.value.candidates] == ["BYTITLE1"]


def test_duplicate_matches_require_user_selection(monkeypatch) -> None:
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

    with pytest.raises(ZoteroConflict) as error:
        zotero.save_paper(_paper(), {}, "AgenticT2I", collection_key="__root__")
    assert {item["key"] for item in error.value.candidates} == {"NEWER001", "OLDEST01"}


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
                return httpx.Response(200, json=[{"data": {"key": "EXISTING", "title": _paper()["title"], "archiveID": "2407.05600"}}])
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
    import hashlib
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
                            "md5": hashlib.md5(pdf.read_bytes()).hexdigest(),
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


@pytest.mark.parametrize('status,payload', [(503, []), (200, {'error': 'invalid'})])
def test_failed_lookup_never_creates_a_paper(monkeypatch, status, payload):
    from arxiv_ra.zotero import ZoteroError
    monkeypatch.setenv('ZOTERO_LOCAL_API_KEY', 'secret')
    writes = []
    def handler(request):
        if request.method != 'GET':
            writes.append(request.url.path)
            return httpx.Response(500)
        if request.url.path == '/api/':
            return httpx.Response(200, headers={'Zotero-Server-ID': 'SERVER1'})
        return httpx.Response(status, json=payload)
    adapter = ZoteroClient(ZoteroConfig(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ZoteroError, match='读取|格式'):
        adapter.save_paper(_paper(), {}, 'Research', collection_key='__root__')
    assert writes == []


def test_interrupted_upload_resumes_existing_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv('ZOTERO_LOCAL_API_KEY', 'test')
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF-retained-revision')
    children, uploads = [], []
    def handler(request):
        path = request.url.path
        if path == '/api/':
            return httpx.Response(200, headers={'Zotero-Server-ID': 'SERVER1'})
        if path == '/api/users/0/items/top':
            return httpx.Response(200, json=[{'data': {'key': 'PARENT01', 'archiveID': '2407.05600'}}])
        if path == '/api/users/0/items/PARENT01':
            return httpx.Response(200, json={'data': {'key': 'PARENT01', 'archiveID': '2407.05600'}})
        if path.endswith('/children'):
            return httpx.Response(200, json=children)
        if path == '/api/items/new':
            return httpx.Response(404)
        if path == '/api/users/0/items' and request.method == 'POST':
            item = json.loads(request.content)[0]
            children.append({'data': {**item, 'key': 'ATTACH01'}})
            return httpx.Response(200, json={'successful': {'0': {'key': 'ATTACH01'}}})
        if path == '/api/users/0/items/ATTACH01':
            return httpx.Response(200, json=children[0])
        if path.endswith('/file'):
            uploads.append(path)
            return httpx.Response(503 if len(uploads) == 1 else 200, json={'exists': 1})
        return httpx.Response(404)
    receipt = {}
    for attempt in range(2):
        adapter = ZoteroClient(ZoteroConfig(), client=httpx.Client(transport=httpx.MockTransport(handler)))
        if attempt == 0:
            from arxiv_ra.zotero import ZoteroError
            with pytest.raises(ZoteroError):
                adapter.save_paper(_paper(), {}, 'Test', pdf_path=pdf, collection_key='__root__', receipt=receipt)
        else:
            adapter.save_paper(_paper(), {}, 'Test', pdf_path=pdf, collection_key='__root__', receipt=receipt)
    assert len(children) == 1
    assert uploads == ['/api/users/0/items/ATTACH01/file'] * 2


def test_lookup_reads_all_pages_before_choosing_match(monkeypatch):
    monkeypatch.setenv('ZOTERO_LOCAL_API_KEY', 'test')
    def handler(request):
        if request.url.path == '/api/':
            return httpx.Response(200, headers={'Zotero-Server-ID': 'SERVER1'})
        if request.url.path.endswith('/top'):
            key = 'FIRST001' if request.url.params.get('start', '0') == '0' else 'SECOND01'
            return httpx.Response(200, headers={'Total-Results': '2'}, json=[
                {'data': {'key': key, 'archiveID': '2407.05600'}}])
        return httpx.Response(500)
    adapter = ZoteroClient(ZoteroConfig(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ZoteroConflict) as error:
        adapter.save_paper(_paper(), {}, 'Test', collection_key='__root__')
    assert {c['key'] for c in error.value.candidates} == {'FIRST001', 'SECOND01'}


def test_uncertain_parent_creation_reuses_persisted_identity(monkeypatch):
    monkeypatch.setenv('ZOTERO_LOCAL_API_KEY', 'test')
    parents, receipt = {}, {}
    def handler(request):
        if request.url.path == '/api/':
            return httpx.Response(200, headers={'Zotero-Server-ID': 'SERVER1'})
        if request.url.path.endswith('/top'):
            # Search indexing has not caught up, but unfiltered receipt lookup works.
            return httpx.Response(200, json=[] if request.url.params.get('q') else list(parents.values()))
        if request.url.path == '/api/items/new':
            return httpx.Response(404)
        if request.url.path.endswith('/children'):
            return httpx.Response(200, json=[])
        if request.url.path == '/api/users/0/items' and request.method == 'POST':
            item = json.loads(request.content)[0]
            if item['itemType'] == 'note':
                return httpx.Response(200, json={'successful': {'0': {'key': 'NOTE0001'}}})
            assert 'key' not in item
            key = f'KEY{len(parents):05}'
            parents[key] = {'data': {**item, 'key': key}}
            raise httpx.ReadError('response lost after external write', request=request)
        key = request.url.path.split('/')[-1]
        if key in parents:
            return httpx.Response(200, json=parents[key])
        return httpx.Response(404)
    adapter = ZoteroClient(ZoteroConfig(attach_pdf=False), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.ReadError):
        adapter.save_paper(_paper(), {}, 'Test', collection_key='__root__', receipt=receipt)
    adapter = ZoteroClient(ZoteroConfig(attach_pdf=False), client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.save_paper(_paper(), {}, 'Test', collection_key='__root__', receipt=receipt)
    assert len(parents) == 1
    assert result.item_key in parents
