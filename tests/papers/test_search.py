from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import subprocess
import sys
import threading
import time

from fastapi.testclient import TestClient
import pymupdf
import pytest

from paperloom.papers.library import PaperLibraryStore
from paperloom.profiles.profiles import ProfileManager
from paperloom.papers.search import SearchIndex, snippet
from paperloom.runtime.jobs.task_runtime import TaskCancelled, TaskHooks, bind_task_hooks
from paperloom.utils import write_json
from paperloom.web import create_app


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
    with patch("paperloom.papers.search.pymupdf.open", side_effect=AssertionError("unchanged PDFs must be reused")):
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


@pytest.mark.parametrize("rebuild", [False, True])
def test_refresh_cancellation_rolls_back_and_rebuild_repairs_corruption(tmp_path, rebuild):
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
        index.refresh(rebuild=rebuild)
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


@pytest.mark.parametrize("mode", ["first", "update", "rebuild"])
def test_search_navigation_during_index_update(tmp_path, mode):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
    app = create_app(config)
    root = tmp_path / "run"
    path = report(root, "alpha")
    index = SearchIndex(root, "alpha")
    hit = None
    if mode != "first":
        index.refresh()
        hit = index.search("Sparse", kind="metadata")["items"][0]
        # Existing installations start with a rollback journal.
        with closing(sqlite3.connect(index.path)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
    # Exceed SQLite's page cache before PDF extraction pauses the background job.
    path.write_text("Replacement report content and training observations.\n" * 70000, encoding="utf-8")
    pdf = path.with_name("paper.pdf")
    pdf.write_bytes(pdf.read_bytes())
    extracting = threading.Event()
    resume = threading.Event()
    original = SearchIndex._documents

    def paused_documents(self, source, warnings):
        if source.kind == "pdf":
            extracting.set()
            assert resume.wait(15), "test did not release PDF extraction"
        yield from original(self, source, warnings)

    with (
        TestClient(app) as client,
        patch.object(SearchIndex, "_documents", paused_documents),
        # Settings probes Zotero independently of jobs; keep network latency out
        # of the assertion about background-task lock contention.
        patch("paperloom.integrations.zotero.ZoteroClient.status", return_value={"ready": False}),
    ):
        with ThreadPoolExecutor(max_workers=1) as requests:
            try:
                response = client.post("/api/jobs/search-index", data={
                    "profile_id": "alpha", "rebuild": str(mode == "rebuild").lower()})
                assert response.status_code == 202
                job_id = response.json()["id"]
                assert extracting.wait(10), "index did not reach PDF extraction"
                for url in ("/generate", "/library", "/reports", "/reading", "/settings",
                            f"/api/jobs/{job_id}", "/search", "/search?q=Unique", "/search?q=Replacement"):
                    try:
                        response = requests.submit(client.get, url).result(timeout=1)
                    except TimeoutError as exc:
                        raise AssertionError(f"{url} blocked while indexing") from exc
                    assert response.status_code == 200
                    if url == f"/api/jobs/{job_id}":
                        assert response.json()["status"] == "running"
                    assert "搜索索引无法读取" not in response.text
                    if url == "/search?q=Unique" and hit:
                        assert "找到 1 份材料" in response.text
                    if url == "/search?q=Replacement":
                        assert "找到 0 份材料" in response.text
                if hit:
                    source = requests.submit(client.get, f"/search/source/{hit['id']}?profile_id=alpha")
                    assert source.result(timeout=1).status_code == 200
            finally:
                resume.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = app.state.jobs.get(job_id)
            if job.status not in {"queued", "running"}:
                break
            time.sleep(.02)
        assert job.status == "succeeded", job.detail
        assert index.search("Replacement")["total"] == 1
        assert index.search("Unique")["total"] == 0


def test_rebuild_during_open_search_snapshot(tmp_path):
    path = report(tmp_path)
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    with closing(index._read()) as reader:
        reader.execute("BEGIN")
        previous = reader.execute("SELECT body FROM docs WHERE kind='report'").fetchall()
        previous_terms = reader.execute("SELECT rowid FROM terms WHERE terms MATCH 'Unique'").fetchall()
        path.write_text("Replacement report", encoding="utf-8")
        index.refresh(rebuild=True)
        # Existing readers finish their old snapshot; new requests see the rebuild.
        assert reader.execute("SELECT body FROM docs WHERE kind='report'").fetchall() == previous
        assert reader.execute("SELECT rowid FROM terms WHERE terms MATCH 'Unique'").fetchall() == previous_terms
        assert index.search("Replacement")["total"] == 1


def test_rebuild_repairs_corrupt_fts_segments(tmp_path):
    report(tmp_path)
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    with closing(sqlite3.connect(index.path)) as db, db:
        db.execute("UPDATE terms_data SET block=x'00' WHERE id > 10")
    assert not index.status().get("error")
    with pytest.raises(sqlite3.DatabaseError):
        index.search("Unique")
    index.refresh(rebuild=True)
    assert index.search("Unique")["total"] == 1


def test_rebuild_ignores_corrupt_previous_source_warnings(tmp_path):
    report(tmp_path)
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    with closing(sqlite3.connect(index.path)) as db, db:
        db.execute("UPDATE sources SET warnings='invalid json'")
    assert not index.status().get("error")
    index.refresh(rebuild=True)
    assert index.search("Unique")["total"] == 1


@pytest.mark.parametrize("fail_install", [False, True])
def test_corrupt_rebuild_retires_crash_wal_and_restores_files_on_failure(tmp_path, monkeypatch, fail_install):
    from pathlib import Path
    path = report(tmp_path)
    index = SearchIndex(tmp_path, "a")
    index.refresh()
    # A crashed writer leaves real committed WAL/SHM files behind.
    subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute("UPDATE docs SET body='STALE CONTENT' WHERE kind='report'")
db.commit()
os._exit(0)
""", str(index.path)], check=True)
    wal = Path(str(index.path) + "-wal")
    shm = Path(str(index.path) + "-shm")
    assert wal.exists() and shm.exists()
    index.path.write_bytes(b"corrupt database")
    assert index.status().get("error")
    path.write_text("Replacement report", encoding="utf-8")
    if fail_install:
        before = {p: p.read_bytes() for p in (index.path, wal, shm)}
        original_replace = Path.replace

        def fail_replacement(self, target):
            if self.name.startswith("rebuild-") and self.suffix == ".sqlite3":
                raise PermissionError("simulated replacement failure")
            return original_replace(self, target)

        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "replace", fail_replacement)
            with pytest.raises(PermissionError):
                index.refresh(rebuild=True)
        assert {p: p.read_bytes() for p in before} == before
    index.refresh(rebuild=True)
    assert index.search("Replacement")["total"] == 1
    with closing(index._read()) as reader:
        assert reader.execute("SELECT body FROM docs WHERE kind='report'").fetchone()[0] == "Replacement report"
