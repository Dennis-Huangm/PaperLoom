from unittest.mock import patch
import time

from fastapi.testclient import TestClient
import pymupdf
import pytest

from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.search import SearchIndex, snippet
from arxiv_ra.task_runtime import TaskCancelled, TaskHooks, bind_task_hooks
from arxiv_ra.utils import write_json
from arxiv_ra.web import create_app


def paper(aid="2407.05600", version=2):
    return {"arxiv_id": aid, "title": "研究 Transformer ＲＬＨＦ", "abstract": "稀疏注意力 Sparse Attention", "version": version}


def report(root, profile="a", version=2, quality="full"):
    path = root / f"2026-09-25/reports/{profile}-v{version}/report.md"
    write_json(path.with_name("metadata.json"), {"profile_id": profile, "paper": paper(version=version), "report_quality": quality})
    path.write_text("# 方法\n\n训练使用奖励模型。<script>alert(1)</script>\n\nUnique report body.", encoding="utf-8")
    path.with_suffix(".html").write_text("<p>report</p>")
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "First physical page.")
        pdf.new_page().insert_text((72, 72), "Reward calibration and policy optimization.")
        pdf.save(path.with_name("paper.pdf"))
    return path


def note(root, profile="a"):
    store = PaperLibraryStore(root, profile)
    store.add({"paper": paper()}, "Test")
    store.state.update_reading("2407.05600", status="read", notes="奖励稀疏，需要复现实验。Private note <script>bad()</script>",
                              tags=["待复现"], read_version=1, expected_updated_at="")
    return store


def test_search_unicode_short_terms_literal_syntax_and_filters(tmp_path):
    note(tmp_path)
    report(tmp_path)
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    assert index.search("稀疏", kind="notes")["total"] == 1
    assert index.search("ＲＬＨＦ sparse")["total"] >= 1
    assert index.search("transformer 奖励", kind="report", version=2, quality="full")["total"] == 1
    assert index.search("奖励", kind="notes", version=1)["total"] == 1
    assert index.search("奖励", kind="notes", version=2)["total"] == 0
    assert index.search("2407.05600v1", kind="notes")["total"] == 1
    assert index.search("2407.05600v2", kind="report")["total"] == 1
    assert index.search('" OR * % _ --')["total"] == 0
    assert index.search("script")["total"] == 2
    assert index.search("no-match")["total"] == 0
    with pytest.raises(ValueError):
        index.search("a" * 201)
    with pytest.raises(ValueError):
        index.search("bad\x00query")
    assert "ＲＬＨＦ" in snippet("前言" * 300 + "ＲＬＨＦ 的研究结果", ["rlhf"])


def test_exact_pdf_page_profile_and_source_identity(tmp_path):
    path = report(tmp_path)
    report(tmp_path, "b", version=7)
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    found = index.search("calibration", kind="pdf")["items"]
    assert len(found) == 1 and found[0]["page"] == 2 and found[0]["version"] == 2
    assert "a-v2/paper.pdf" in found[0]["url"]
    assert index.document(found[0]["id"])["body"].startswith("Reward")
    path.with_name("paper.pdf").unlink()
    with pytest.raises(ValueError, match="来源已更新"):
        index.document(found[0]["id"])
    index.refresh()
    assert not index.search("calibration")["items"]
    assert SearchIndex(tmp_path, "b").document(found[0]["id"]) is None


def test_incremental_index_note_edit_removal_and_retained_notes(tmp_path):
    store = note(tmp_path)
    report(tmp_path)
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    hit = index.search("Private", kind="notes")["items"][0]
    with patch("arxiv_ra.search.pymupdf.open", side_effect=AssertionError("unchanged PDFs must be reused")):
        assert index.refresh()["changed"] == 0
    record = store.state.snapshot()["reading"]["2407.05600"]
    store.state.update_reading("2407.05600", status="read", notes="修改后的研究结论", tags=[], read_version=1,
                              expected_updated_at=record["updated_at"])
    with pytest.raises(ValueError):
        index.document(hit["id"])
    store.remove("2407.05600")
    index.refresh()
    assert not index.search("Private")["items"]
    retained = index.search("研究结论", kind="notes")["items"][0]
    assert retained["version"] == 1 and retained["url"] == ""
    assert index.document(retained["id"])["body"].startswith("修改后")


def test_refresh_cancellation_rolls_back_and_rebuild_repairs_corruption(tmp_path):
    path = report(tmp_path)
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    path.write_text("replacement text", encoding="utf-8")
    count = 0
    def cancelled():
        nonlocal count
        count += 1
        return count > 5
    hooks = TaskHooks(progress=lambda *_: None, warning=lambda *_: None, is_cancelled=cancelled)
    with bind_task_hooks(hooks), pytest.raises(TaskCancelled):
        index.refresh()
    assert index.search("Unique", kind="report")["total"] == 1
    index.path.write_bytes(b"corrupt database")
    assert index.status().get("error")
    index.refresh(rebuild=True)
    assert index.search("replacement", kind="report")["total"] == 1
    assert index.search("Unique", kind="report")["total"] == 0


def test_damaged_and_blank_pdf_warnings_and_pagination(tmp_path):
    for number in range(23):
        p = paper(f"2407.{number:05d}")
        PaperLibraryStore(tmp_path, "a").add({"paper": p}, "Test")
    path = report(tmp_path)
    path.with_name("paper.pdf").write_bytes(b"bad pdf")
    index = SearchIndex(tmp_path, "a")
    assert index.refresh()["warnings"]
    first = index.search("稀疏", kind="metadata")
    second = index.search("稀疏", kind="metadata", page=2)
    assert first["total"] == 24 and len(first["items"]) == 20 and len(second["items"]) == 4
    assert not {x["id"] for x in first["items"]} & {x["id"] for x in second["items"]}
    path.with_name("paper.pdf").unlink()
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path.with_name("paper.pdf"))
    assert "未执行 OCR" in str(index.refresh()["warnings"])


def test_search_web_no_network_profile_boundary_escaping_and_private_index(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
    app = create_app(config)
    root = tmp_path / "run"
    note(root, "alpha")
    index = SearchIndex(root, "alpha")
    index.refresh()
    with TestClient(app) as client:
        response = client.get("/search", params={"q": "Private", "version": "", "profile_id": "alpha"})
        assert response.status_code == 200 and "找到 1 份材料" in response.text
        assert "<script>bad()" not in response.text and "&lt;script&gt;" in response.text
        hit = index.search("Private")["items"][0]
        url = f"/search/source/{hit['id']}?profile_id=alpha"
        source = client.get(url)
        assert source.status_code == 200 and source.headers["cache-control"] == "no-store"
        assert "&lt;script&gt;" in source.text
        for folder in (".search", ".SEARCH"):
            assert client.get(f"/artifacts/{folder}/{index.path.name}").status_code == 404
        assert client.get("/search", params={"q": "x", "version": "-1"}).status_code == 400
        job = client.post("/api/jobs/search-index", data={"profile_id": "alpha"})
        assert job.status_code == 202
        profiles = ProfileManager(tmp_path)
        profiles.save({"id": "beta", "name": "Beta"})
        profiles.activate("beta")
        assert client.get(url).status_code == 409
        assert client.get("/search?profile_id=alpha&q=Private").status_code == 409
        assert client.post("/api/jobs/search-index", data={"profile_id": "alpha"}).status_code == 409
        assert "Private note" not in client.get("/search?q=Private").text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = app.state.jobs.get(job.json()["id"])
            if state.status not in {"queued", "running"}:
                break
            time.sleep(.02)
        assert state.status == "succeeded" and state.result_url is None
        assert state.profile_id == "alpha"
        assert not SearchIndex(root, "beta").path.exists()


def test_recommendation_history_legacy_and_synced_pdf_scope(tmp_path):
    write_json(tmp_path / "2026-09-24/recommendations-a.json", [{"profile_id": "a", "paper": paper(version=1)}])
    write_json(tmp_path / "2026-09-25/recommendations-a.json", [{"profile_id": "a", "paper": paper(version=2)}])
    write_json(tmp_path / "2026-09-25/recommendations-b.json", [{"profile_id": "b", "paper": {
        **paper(), "abstract": "other direction confidential"}}])
    legacy = report(tmp_path, profile="", version=3)
    synced = tmp_path / "papers/a/2407.05600/v4"
    write_json(synced / "metadata.json", {"profile_id": "a", "paper": paper(version=4)})
    (synced / "paper.pdf").write_bytes(legacy.with_name("paper.pdf").read_bytes())
    write_json(tmp_path / ".jobs/private.json", {"secret": "unindexedsecret"})
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    old = index.search("稀疏", version=1)["items"]
    assert len(old) == 1 and old[0]["url"] == "/?date=2026-09-24"
    assert index.search("calibration", version=4)["items"][0]["url"].endswith("/papers/a/2407.05600/v4/paper.pdf")
    assert index.search("Unique", kind="report", version=3)["total"] == 1
    assert not index.search("confidential")["items"] and not index.search("unindexedsecret")["items"]
    second = SearchIndex(tmp_path, "b")
    second.refresh()
    assert second.search("Unique", version=3)["total"] == 1
    assert second.search("calibration", version=4)["total"] == 0


def test_empty_index_and_failed_reading_state_refresh_preserve_previous_index(tmp_path):
    index = SearchIndex(tmp_path, "a")
    assert index.search("test")["total"] == 0 and not index.path.exists()
    assert index.refresh()["documents"] == 0
    store = note(tmp_path)
    index.refresh()
    store.state.path.write_text("broken-json", encoding="utf-8")
    with pytest.raises(ValueError):
        index.refresh()
    assert index.search("Private")["total"] == 1


def test_source_changed_during_extraction_is_omitted(tmp_path):
    path = report(tmp_path)
    index = SearchIndex(tmp_path, "a")
    original = index._documents
    def changing_source(source, warnings):
        yield from original(source, warnings)
        if source.kind == "report":
            path.write_text("changed after extraction", encoding="utf-8")
    with patch.object(index, "_documents", side_effect=changing_source):
        result = index.refresh()
    assert result["warnings"]
    assert not index.search("Unique", kind="report")["items"]
    index.refresh()
    assert index.search("changed", kind="report")["total"] == 1
