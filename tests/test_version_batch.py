from types import SimpleNamespace
import threading

import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.task_runtime import TaskCancelled, TaskHooks, bind_task_hooks
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.version_batch import VersionSyncBatch, update_candidates
from arxiv_ra.version_sync import PaperVersionSync
from test_version_sync import Arxiv, paper, tracked_report


@pytest.fixture
def batch_setup(tmp_path):
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="test", profile_name="Test")
    service = VersionSyncBatch(config, tmp_path)
    arxiv = Arxiv()
    clients = SimpleNamespace(arxiv=arxiv, alphaxiv=None)
    candidates = [{"arxiv_id": f"2501.0000{i}", "title": f"Paper {i}", "local_version": 1,
                   "saved_version": 1, "latest_version": 3, "checked_at": "2026-09-23T12:00:00Z"}
                  for i in range(1, 4)]
    return service, clients, candidates


def plan(service, candidates, **options):
    return service.preview([item["arxiv_id"] for item in candidates], candidates, **options)


def test_update_candidates_include_missing_pdf_exclude_unknown_and_current():
    items = [{"arxiv_id": "new", "latest_version": 3, "local_version": 1},
             {"arxiv_id": "missing", "latest_version": 3, "saved_version": 3},
             {"arxiv_id": "saved-old", "latest_version": 3, "local_version": 3, "saved_version": 1},
             {"arxiv_id": "current", "latest_version": 3, "local_version": 3, "saved_version": 3},
             {"arxiv_id": "unknown", "saved_version": 1}]
    assert [item["arxiv_id"] for item in update_candidates(items)] == ["new", "missing", "saved-old"]


def test_preview_is_offline_pins_scope_and_deduplicates(batch_setup):
    service, clients, candidates = batch_setup
    state = service.preview([candidates[0]["arxiv_id"]] * 2, candidates, report=True)
    assert len(state["items"]) == 1
    assert state["items"][0]["target_version"] == 3
    assert state["options"] == {"report": True, "zotero": False, "obsidian": False}
    assert service.summary(state)["reports"] == 1
    assert not clients.arxiv.queries and not clients.arxiv.downloads
    assert not list(service.output_root.glob("papers/**/*.pdf"))
    assert service.read(state["id"])["status"] == "preview"


@pytest.mark.parametrize("selection", [[], ["2501.99999"], ["../evil"]])
def test_preview_rejects_empty_or_untracked_selection(batch_setup, selection):
    service, _, candidates = batch_setup
    with pytest.raises(ValueError):
        service.preview(selection, candidates)
    assert not service.recent()


def test_preview_rejects_oversized_batches_and_disabled_exports(batch_setup):
    service, _, candidates = batch_setup
    with pytest.raises(ValueError):
        service.preview([f"2501.{i:05}" for i in range(201)], candidates)
    with pytest.raises(ValueError, match="Obsidian"):
        plan(service, candidates, obsidian=True)


def test_batch_isolates_failures_and_retries_only_unfinished_papers(batch_setup, monkeypatch):
    service, clients, candidates = batch_setup
    state = plan(service, candidates)
    original = clients.arxiv.download_pdf
    failed = {"2501.00002"}

    def download(value, path):
        if value.arxiv_id in failed:
            raise OSError("one download offline")
        original(value, path)

    monkeypatch.setattr(clients.arxiv, "download_pdf", download)
    result = service.run(state["id"], clients=clients)
    assert result.is_file()
    state = service.read(state["id"])
    assert [item["status"] for item in state["items"]] == ["succeeded", "failed", "succeeded"]
    assert state["status"] == "partial"
    queries_before = list(clients.arxiv.queries)
    failed.clear()
    clients.arxiv.latest = 4
    restarted = VersionSyncBatch(service.config, service.project_root)
    restarted.run(state["id"], clients=clients)
    assert clients.arxiv.queries[len(queries_before):] == ["2501.00002v3"]
    assert len(clients.arxiv.downloads) == 3
    assert service.read(state["id"])["status"] == "succeeded"
    service.run(state["id"], clients=clients)
    assert len(clients.arxiv.downloads) == 3


def test_query_failure_does_not_abort_remaining_papers(batch_setup, monkeypatch):
    service, clients, candidates = batch_setup
    state = plan(service, candidates)
    original = clients.arxiv.get

    def get(aid):
        if aid == "2501.00001v3":
            raise OSError("metadata offline")
        return original(aid)

    monkeypatch.setattr(clients.arxiv, "get", get)
    service.run(state["id"], clients=clients)
    items = service.read(state["id"])["items"]
    assert [item["status"] for item in items] == ["failed", "succeeded", "succeeded"]
    assert "metadata offline" in items[0]["error"]


def test_cancel_then_restart_keeps_completed_and_pending_items(batch_setup, monkeypatch):
    service, clients, candidates = batch_setup
    state = plan(service, candidates)
    original = clients.arxiv.download_pdf

    def download(value, path):
        if value.arxiv_id == "2501.00002":
            raise TaskCancelled()
        original(value, path)

    monkeypatch.setattr(clients.arxiv, "download_pdf", download)
    with pytest.raises(TaskCancelled):
        service.run(state["id"], clients=clients)
    state = service.read(state["id"])
    assert state["status"] == "interrupted"
    assert [item["status"] for item in state["items"]] == ["succeeded", "interrupted", "pending"]
    monkeypatch.setattr(clients.arxiv, "download_pdf", original)
    VersionSyncBatch(service.config, service.project_root).run(state["id"], clients=clients)
    assert len(clients.arxiv.downloads) == 3
    assert service.read(state["id"])["status"] == "succeeded"


def test_process_loss_after_publication_reuses_completed_single_paper_steps(batch_setup):
    service, clients, candidates = batch_setup
    state = plan(service, candidates[:1])
    with PaperVersionSync(service.config, service.project_root, clients=clients) as sync:
        sync.sync("2501.00001", target_version=3)
    state["status"] = state["items"][0]["status"] = "running"
    write_json(service._path(state["id"]), state)
    service.run(state["id"], clients=clients)
    assert len(clients.arxiv.downloads) == 1
    assert service.read(state["id"])["status"] == "succeeded"


def test_batch_does_not_inherit_unselected_model_or_exports(batch_setup, monkeypatch):
    service, clients, candidates = batch_setup
    calls = []
    monkeypatch.setattr(PaperVersionSync, "_report", lambda *args: (calls.append("report") or {"status": "degraded"}))
    with PaperVersionSync(service.config, service.project_root, clients=clients) as sync:
        sync.sync("2501.00001", target_version=3, report=True)
    assert calls == ["report"]
    state = plan(service, candidates[:1])
    service.run(state["id"], clients=clients)
    assert calls == ["report"]
    item = service.read(state["id"])["items"][0]
    assert item["status"] == "succeeded"
    assert "report" not in item["steps"]


def test_preview_config_is_preserved_across_execution_and_restart(batch_setup, monkeypatch):
    service, clients, candidates = batch_setup
    service.config.llm.model = "model-at-preview"
    state = plan(service, candidates[:1], report=True)
    service.config.llm.model = "different-model"
    models = []
    monkeypatch.setattr(PaperVersionSync, "_report", lambda self, *args:
                        (models.append(self.config.llm.model) or {"status": "degraded"}))
    service.run(state["id"], clients=clients)
    VersionSyncBatch(service.config, service.project_root).run(state["id"], clients=clients)
    assert models == ["model-at-preview", "model-at-preview"]


def test_batch_progress_is_scaled_and_cancellation_hooks_are_forwarded(batch_setup):
    service, clients, candidates = batch_setup
    state = plan(service, candidates[:2])
    updates = []
    hooks = TaskHooks(progress=lambda text, percent: updates.append((text, percent)), warning=lambda *_: None,
                      is_cancelled=lambda: False)
    with bind_task_hooks(hooks):
        service.run(state["id"], clients=clients)
    assert all(percent < 50 for text, percent in updates if text.startswith("1/2"))
    assert updates[-1][1] == 100


def test_batches_are_profile_scoped_and_reject_path_traversal(batch_setup):
    service, _, candidates = batch_setup
    state = plan(service, candidates)
    service.config.profile_id = "other"
    other = VersionSyncBatch(service.config, service.project_root)
    with pytest.raises(ValueError):
        other.read(state["id"])
    with pytest.raises(ValueError):
        service.read("../batch")


def test_unfinished_batches_remain_accessible_beyond_recent_history_limit(batch_setup):
    service, _, candidates = batch_setup
    older = plan(service, candidates[:1])
    older["status"] = "interrupted"
    older["created_at"] = "2026-01-01T00:00:00+00:00"
    write_json(service._path(older["id"]), older)
    for _ in range(3):
        plan(service, candidates[:1])
    recent = service.recent(limit=2)
    assert len(recent) == 3
    assert any(state["id"] == older["id"] for state in recent)


def test_cancel_signal_before_first_paper_leaves_resumable_plan(batch_setup):
    service, clients, candidates = batch_setup
    state = plan(service, candidates)
    with bind_task_hooks(TaskHooks(progress=lambda *_: None, warning=lambda *_: None, is_cancelled=lambda: True)):
        with pytest.raises(TaskCancelled):
            service.run(state["id"], clients=clients)
    assert service.read(state["id"])["status"] == "interrupted"
    assert not clients.arxiv.queries
    assert all(item["status"] == "pending" for item in service.read(state["id"])["items"])


def test_batch_web_preview_and_duplicate_execution_are_safe(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from arxiv_ra.web import create_app

    config_path = tmp_path / "config.yaml"
    config_path.write_text("output_dir: run\n", encoding="utf-8")
    profiles = ProfileManager(tmp_path)
    profiles.save({"id": "test", "name": "Test"})
    profiles.activate("test")
    library = PaperLibraryStore(tmp_path / "run", "test")
    library.add({"paper": paper(1).to_dict()}, "Test")
    tracked_report(tmp_path / "run")
    write_json(tmp_path / "run/version-state-test.json", {"items": {"2501.00001":
               {"arxiv_id": "2501.00001", "title": "Test Paper", "latest_version": 3}}})
    entered, release = threading.Event(), threading.Event()

    def run(self, batch_id):
        entered.set()
        assert release.wait(5)
        return self._path(batch_id).with_name("index.html")

    monkeypatch.setattr(VersionSyncBatch, "run", run)
    with TestClient(create_app(config_path)) as client:
        assert "论文版本状态" in client.get("/versions").text
        invalid = client.post("/api/version-batches/preview", data={"arxiv_ids": "2501.99999"})
        assert invalid.status_code == 400
        response = client.post("/api/version-batches/preview", data={"arxiv_ids": "2501.00001"})
        assert response.status_code == 200
        preview = response.json()
        assert preview["summary"]["reports"] == 1
        assert "config" not in preview
        url = f"/api/jobs/version-batch/{preview['id']}"
        try:
            first = client.post(url)
            assert first.status_code == 202 and entered.wait(3)
            assert client.post(url).json()["id"] == first.json()["id"]
            assert "批量同步记录" in client.get("/versions").text
            profiles.save({"id": "other", "name": "Other"})
            profiles.activate("other")
            assert client.post(url).status_code == 404
        finally:
            release.set()
