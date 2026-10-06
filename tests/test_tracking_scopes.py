from types import SimpleNamespace
import pytest
import httpx

from arxiv_ra.config import AppConfig
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.utils import write_json
from arxiv_ra.version_tracker import VersionTracker
from arxiv_ra.version_batch import VersionSyncBatch
from arxiv_ra.web_catalog import version_tracking_data
from test_version_sync import Arxiv, paper


def report(root, aid="2501.00001", version=1):
    folder = root / "2026-10-06" / "reports" / f"{aid}-v{version}"
    write_json(folder / "metadata.json", {"profile_id": "test", "paper": paper(version, aid).to_dict()})
    (folder / "report.html").write_text("<p>report</p>", encoding="utf-8")
    return folder


def test_check_tracks_existing_reports_not_favorites_or_downloads(tmp_path):
    config = AppConfig(output_dir=str(tmp_path), profile_id="test")
    config.zotero.enabled = False
    config.version_tracking.analyze_pdf_diff = False
    report(tmp_path)
    PaperLibraryStore(tmp_path, "test").add({"paper": paper(1, "2501.00002").to_dict()}, "Test")
    pending = report(tmp_path, "2501.00003")
    (pending / "report.html").unlink()
    downloaded = tmp_path / "papers/test/2501.00004/v1"
    write_json(downloaded / "metadata.json", {"paper": paper(1, "2501.00004").to_dict()})
    (downloaded / "paper.pdf").write_bytes(b"pdf")
    with VersionTracker(config, tmp_path, clients=SimpleNamespace(arxiv=Arxiv())) as tracker:
        tracker.check()
    items = version_tracking_data(tmp_path, "test")["items"]
    assert [item["arxiv_id"] for item in items] == ["2501.00001"]
    assert items[0]["sources"] == ["本地报告"]


def test_preview_pins_material_targets_per_paper_and_scope(tmp_path):
    config = AppConfig(output_dir=str(tmp_path), profile_id="test")
    config.zotero.enabled = config.zotero.attach_pdf = True
    service = VersionSyncBatch(config, tmp_path)
    candidates = [
        {"arxiv_id": "2501.00001", "sources": ["本地报告", "Zotero"], "report_version": 1,
         "zotero_version": 3, "local_version": 3, "latest_version": 3},
        {"arxiv_id": "2501.00002", "sources": ["Zotero"], "zotero_version": 1, "latest_version": 3},
    ]
    state = service.preview(["2501.00001", "2501.00002"], candidates, scope="all")
    assert [item["options"] for item in state["items"]] == [
        {"report": True, "zotero": False, "obsidian": False},
        {"report": False, "zotero": True, "obsidian": False},
    ]
    assert service.summary(state)["reports"] == 1
    with pytest.raises(ValueError):
        service.preview(["2501.00001"], candidates, scope="zotero")


def test_scoped_check_preserves_other_sources_and_offline_zotero(tmp_path, monkeypatch):
    config = AppConfig(output_dir=str(tmp_path), profile_id="test")
    config.version_tracking.analyze_pdf_diff = False
    config.version_sync.enabled = True
    report(tmp_path)
    write_json(tmp_path / "version-state-test.json", {"items": {
        "2501.00002": {"arxiv_id": "2501.00002", "sources": ["Zotero"], "latest_version": 2,
                       "zotero_version": 1, "zotero_keys": ["KEY"], "zotero_library": "library"}}})
    from arxiv_ra.zotero import ZoteroClient
    def offline(self):
        raise OSError("offline")
    monkeypatch.setattr(ZoteroClient, "list_trackable_papers", offline)
    config.zotero.enabled = True
    clients = SimpleNamespace(arxiv=Arxiv())
    with VersionTracker(config, tmp_path, clients=clients) as tracker:
        tracker.check(scope="reports", auto_sync=False)
        tracker.check(scope="zotero", auto_sync=False)
    items = {item["arxiv_id"]: item for item in version_tracking_data(tmp_path, "test")["items"]}
    assert set(items) == {"2501.00001", "2501.00002"}
    assert items["2501.00002"]["zotero_stale"] is True
    assert not clients.arxiv.downloads
    monkeypatch.setattr(ZoteroClient, "list_trackable_papers", lambda self: [])
    with VersionTracker(config, tmp_path, clients=clients) as tracker:
        tracker.check(scope="zotero", auto_sync=False)
    assert [item["arxiv_id"] for item in version_tracking_data(tmp_path, "test")["items"]] == ["2501.00001"]


def test_page_filters_statistics_and_preview_by_source(tmp_path):
    from fastapi.testclient import TestClient
    from arxiv_ra.profiles import ProfileManager
    from arxiv_ra.web import create_app
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\n", encoding="utf-8")
    profiles = ProfileManager(tmp_path)
    profiles.save({"id": "test", "name": "Test"})
    profiles.activate("test")
    root = tmp_path / "run"
    report(root)
    write_json(root / "version-state-test.json", {"coverage": {"ids": ["2501.00001"], "checked_ids": ["2501.00001"]}, "items": {
        "2501.00001": {"latest_version": 3},
        "2501.00002": {"arxiv_id": "2501.00002", "title": "Zotero only", "sources": ["Zotero"],
                       "latest_version": 3, "zotero_version": 3}}})
    with TestClient(create_app(config)) as client:
        response = client.get("/versions?scope=reports")
        assert response.status_code == 200
        assert "检查本地报告论文" in response.text
        assert "Zotero only" not in response.text
        preview = client.post("/api/version-batches/preview", data={"arxiv_ids": "2501.00001", "scope": "reports"})
        assert preview.status_code == 200
        assert preview.json()["items"][0]["options"]["report"] is True
        assert client.post("/api/version-batches/preview", data={"arxiv_ids": "2501.00001", "scope": "zotero"}).status_code == 400
        assert client.get("/versions?scope=invalid").status_code == 400
        assert "当前范围 1/2 篇" in client.get("/versions?scope=all").text
        assert "其余 1 篇" in client.get("/versions?scope=zotero").text


@pytest.mark.parametrize("attachment, expected", [
    ({"title": "论文 PDF · v2"}, 2),
    ({"filename": "2501.00001v3.pdf"}, 3),
    ({"title": "paper", "version": 99}, None),
])
def test_check_uses_pdf_evidence_not_zotero_object_or_parent_version(tmp_path, monkeypatch, attachment, expected):
    from arxiv_ra.zotero import ZoteroClient
    from arxiv_ra.config import ZoteroConfig
    def handler(request):
        if request.url.path == "/api/":
            return httpx.Response(200, headers={"Zotero-Server-ID": "TEST"})
        if request.url.path.endswith("/top"):
            return httpx.Response(200, json=[{"key": "ITEM", "data": {
                "archiveID": "2501.00001v7", "title": "external", "version": 88}}])
        if request.url.path.endswith("/children"):
            return httpx.Response(200, json=[{"data": {"contentType": "application/pdf", **attachment}}])
        return httpx.Response(404)
    monkeypatch.setattr("arxiv_ra.version_tracker.ZoteroClient", lambda *a, **k:
                        ZoteroClient(ZoteroConfig(), client=httpx.Client(transport=httpx.MockTransport(handler))))
    config = AppConfig(output_dir=str(tmp_path), profile_id="test")
    with VersionTracker(config, tmp_path, clients=SimpleNamespace(arxiv=Arxiv())) as tracker:
        tracker.check(auto_sync=False)
    item = version_tracking_data(tmp_path, "test")["items"][0]
    assert item["zotero_version"] == expected
    assert item["zotero_keys"] == ["ITEM"]


def test_unknown_attachment_requires_explicit_supplement_and_usable_source(tmp_path):
    config = AppConfig(output_dir=str(tmp_path), profile_id="test")
    service = VersionSyncBatch(config, tmp_path)
    item = {"arxiv_id": "2501.00001", "sources": ["Zotero"], "latest_version": 3,
            "zotero_keys": ["ITEM"], "zotero_library": "library"}
    with pytest.raises(ValueError):
        service.preview([item["arxiv_id"]], [item], scope="all")
    plan = service.preview([item["arxiv_id"]], [item], scope="zotero", supplement=True)
    assert plan["items"][0]["options"] == {"report": False, "zotero": True, "obsidian": False}
    item["zotero_stale"] = True
    with pytest.raises(ValueError):
        service.preview([item["arxiv_id"]], [item], scope="zotero", supplement=True)
