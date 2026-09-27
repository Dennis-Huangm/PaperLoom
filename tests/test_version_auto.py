from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest
import yaml
from fastapi.testclient import TestClient

from arxiv_ra.config import AppConfig, LLMConfig, VersionSyncConfig, load_config
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.llm import LLMClient
from arxiv_ra.model_budget import ModelBudgetExceeded, current_model_budget, model_request_budget
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.task_runtime import TaskCancelled
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.version_auto import AutomaticVersionSync
from arxiv_ra.version_batch import VersionSyncBatch
from arxiv_ra.version_sync import PaperVersionSync
from arxiv_ra.version_tracker import VersionTracker
from arxiv_ra.web import create_app
from test_version_sync import Arxiv, paper


@pytest.fixture
def automatic(tmp_path):
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="test")
    config.version_tracking.include_zotero = False
    config.version_tracking.analyze_pdf_diff = False
    clients = SimpleNamespace(arxiv=Arxiv(), alphaxiv=None)
    library = PaperLibraryStore(tmp_path / "run", "test")
    for index in range(1, 4):
        library.add({"paper": paper(1, f"2501.0000{index}").to_dict()}, "Test")
    tracker = VersionTracker(config, tmp_path, clients=clients)
    return config, clients, tracker, VersionSyncBatch(config, tmp_path)


def test_disabled_policy_only_checks_and_never_downloads(automatic):
    config, clients, tracker, batches = automatic
    tracker.check()
    assert not clients.arxiv.downloads
    assert not batches.recent()
    assert tracker.auto_sync_result is None


def test_automatic_rounds_cap_downloads_keep_history_and_do_not_repeat(automatic):
    config, clients, tracker, batches = automatic
    config.version_sync.enabled = True
    config.version_sync.max_papers = 2
    first = tracker.check()
    assert len(clients.arxiv.downloads) == 2
    assert tracker.auto_sync_result["deferred"] == 1
    assert "查看本轮自动同步结果" in first.read_text(encoding="utf-8")
    state = batches.recent()[0]
    assert state["origin"] == "automatic" and state["status"] == "succeeded"
    assert state["options"] == {"report": False, "zotero": False, "obsidian": False}
    assert all(item["target_version"] == 3 for item in state["items"])
    tracker.check()
    tracker.check()
    assert len(clients.arxiv.downloads) == 3
    assert len(batches.recent()) == 2
    clients.arxiv.latest = 4
    tracker.check()
    root = batches.output_root / "papers/test/2501.00001"
    assert (root / "v3/paper.pdf").is_file() and (root / "v4/paper.pdf").is_file()


def test_failed_attempt_is_not_automatically_retried_but_manual_recovery_works(automatic):
    config, clients, tracker, batches = automatic
    config.version_sync.enabled = True
    clients.arxiv.fail_download = True
    tracker.check()
    state = batches.recent()[0]
    assert state["status"] == "partial"
    clients.arxiv.fail_download = False
    tracker.check()
    assert len(clients.arxiv.downloads) == 3
    assert tracker.auto_sync_result["held"] == 3
    batches.run(state["id"], clients=clients)
    assert batches.read(state["id"])["status"] == "succeeded"
    # The latest round is idle now; a saved batch reference also reflects recovery.
    summary = AutomaticVersionSync(config, tracker.project_root)
    write_json(summary.state_path, {"batch_id": state["id"], "status": "partial"})
    assert summary.latest()["status"] == "succeeded"
    assert len(clients.arxiv.downloads) == 6


def test_lookup_failure_does_not_sync_stale_version(automatic):
    config, clients, tracker, batches = automatic
    tracker.check()
    config.version_sync.enabled = True
    clients.arxiv.fail_lookup = True
    tracker.check()
    assert not clients.arxiv.downloads and not batches.recent()
    assert all(item["check_error"] for item in read_json(tracker.state_path, {})["items"].values())


def test_cancelled_auto_batch_requires_explicit_continuation(automatic, monkeypatch):
    config, clients, tracker, batches = automatic
    config.version_sync.enabled = True
    original = clients.arxiv.download_pdf
    def cancel(*args):
        raise TaskCancelled()
    monkeypatch.setattr(clients.arxiv, "download_pdf", cancel)
    with pytest.raises(TaskCancelled):
        tracker.check()
    state = batches.recent()[0]
    assert state["status"] == "interrupted"
    assert AutomaticVersionSync(config, tracker.project_root).latest()["status"] == "interrupted"
    monkeypatch.setattr(clients.arxiv, "download_pdf", original)
    tracker.check()
    assert not clients.arxiv.downloads
    batches.run(state["id"], clients=clients)
    assert len(clients.arxiv.downloads) == 3


def test_process_loss_after_plan_prevents_duplicate_auto_batch(automatic, monkeypatch):
    config, clients, tracker, batches = automatic
    config.version_sync.enabled = True
    def crash(*args, **kwargs):
        raise SystemExit("process gone")
    with monkeypatch.context() as patch:
        patch.setattr(VersionSyncBatch, "run", crash)
        with pytest.raises(SystemExit):
            tracker.check()
    assert batches.recent()[0]["status"] == "preview"
    tracker.check()
    assert not clients.arxiv.downloads and len(batches.recent()) == 1


def test_invalid_automatic_config_keeps_version_discovery(automatic):
    config, clients, tracker, batches = automatic
    config.version_sync.enabled = True
    config.version_sync.max_papers = 0
    assert tracker.check().is_file()
    assert tracker.auto_sync_result["status"] == "failed"
    assert "max_papers" in tracker.auto_sync_result["error"]
    assert not clients.arxiv.downloads


def test_model_paper_limit_and_frozen_export_options(automatic, monkeypatch):
    config, clients, tracker, batches = automatic
    config.version_sync = VersionSyncConfig(enabled=True, report=True, max_model_papers=1)
    called = []
    monkeypatch.setattr(PaperVersionSync, "_report", lambda self, *args: called.append(self.config.llm.model) or {})
    tracker.check()
    assert len(clients.arxiv.downloads) == len(called) == 1
    assert tracker.auto_sync_result["deferred"] == 2
    assert batches.recent()[0]["config"]["version_sync"]["report"] is True


def fake_llm(responses=None):
    requests = []
    def respond(request):
        requests.append(request)
        if responses:
            return httpx.Response(responses.pop(0), json={"error": {"message": "try again"}})
        return httpx.Response(200, json={"id": "test", "object": "chat.completion",
            "created": 0, "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Test response"}}]})
    llm = LLMClient.__new__(LLMClient)
    llm.config = LLMConfig()
    llm.enabled = True
    llm.client = OpenAI(api_key="test", base_url="https://offline.test/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    return llm, requests


def test_model_budget_counts_vision_and_text_and_resets_outside_scope(tmp_path):
    llm, requests = fake_llm()
    try:
        with model_request_budget(2) as budget:
            llm.chat("s", "u")
            path = tmp_path / "figure.png"
            path.write_bytes(b"fake-image-for-mock")
            llm.describe_figure(path, "paper", "abstract", "caption")
            with pytest.raises(ModelBudgetExceeded):
                llm.chat("s", "u")
            assert budget.used == len(requests) == 2
        assert current_model_budget() is None
        llm.chat("s", "u")
        assert len(requests) == 3
    finally:
        llm.client.close()


def test_model_budget_includes_compatibility_retry_disables_sdk_retries():
    llm, requests = fake_llm([500, 500, 500])
    try:
        with model_request_budget(1):
            with pytest.raises(ModelBudgetExceeded):
                llm.chat("s", "u")
        assert len(requests) == 1
    finally:
        llm.client.close()


def test_budget_fallback_is_degraded_and_request_counts_are_durable(automatic, monkeypatch):
    config, clients, tracker, batches = automatic
    config.version_sync = VersionSyncConfig(enabled=True, report=True, max_model_papers=1, max_model_calls=1)
    llm, requests = fake_llm()
    def report(self, *args):
        llm.chat("s", "u")
        try:
            llm.chat("s", "u")
        except ModelBudgetExceeded:
            pass  # A lower-level report/figure fallback must not hide the limit.
        return {}
    monkeypatch.setattr(PaperVersionSync, "_report", report)
    try:
        tracker.check()
        state = batches.recent()[0]
        assert state["model_requests_this_run"] == state["model_requests_total"] == 1
        assert state["items"][0]["steps"]["report"]["status"] == "degraded"
        assert "上限" in state["items"][0]["steps"]["report"]["error"]
        assert state["items"][0]["steps"]["download"]["status"] == "succeeded"
        batches.run(state["id"], clients=clients)
        state = batches.read(state["id"])
        assert state["model_requests_this_run"] == 1 and state["model_requests_total"] == 2
        assert len(clients.arxiv.downloads) == 1 and len(requests) == 2
    finally:
        llm.client.close()


def test_policy_settings_are_direction_scoped_and_validate_targets(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("output_dir: run\nzotero:\n  enabled: false\nversion_sync:\n  enabled: true\n", encoding="utf-8")
    profiles = ProfileManager(tmp_path)
    for aid in ("one", "two"):
        profiles.save({"id": aid, "name": aid})
    profiles.activate("one")
    assert not load_config(config_path).version_sync.enabled
    with TestClient(create_app(config_path)) as client:
        values = {"profile_id": "one", "enabled": "true", "max_papers": 4, "max_model_calls": 7}
        response = client.post("/versions/policy", data=values)
        assert response.status_code == 200 and "自动同步策略已保存" in response.text
        assert load_config(config_path).version_sync.max_model_calls == 7
        assert load_config(config_path).version_sync.enabled
        assert client.post("/versions/policy", data={**values, "max_papers": 0}).status_code == 400
        assert client.post("/versions/policy", data={**values, "max_model_calls": 0}).status_code == 400
        assert client.post("/versions/policy", data={**values, "zotero": "true"}).status_code == 400
        assert client.post("/versions/policy", data={**values, "obsidian": "true"}).status_code == 400
        profiles.activate("two")
        assert not load_config(config_path).version_sync.enabled
        assert client.post("/versions/policy", data=values).status_code == 409
        profiles.activate("one")
        assert load_config(config_path).version_sync.max_papers == 4
        assert client.post("/versions/policy", data={"profile_id": "one"}).status_code == 200
        assert not load_config(config_path).version_sync.enabled


def test_default_profile_migrates_existing_policy_only_once(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"version_sync": asdict(VersionSyncConfig(enabled=True))}), encoding="utf-8")
    profiles = ProfileManager(tmp_path)
    profiles.ensure_default(path)
    assert load_config(path).version_sync.enabled
    profiles.save({"id": "new", "name": "New"})
    profiles.activate("new")
    assert not load_config(path).version_sync.enabled


def test_concurrent_checks_share_attempt_ledger(automatic):
    config, clients, tracker, batches = automatic
    config.version_sync.enabled = True
    other = VersionTracker(config, tracker.project_root, clients=clients)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.check) for service in (tracker, other)]
        for future in futures:
            assert future.result(timeout=10).is_file()
    assert len(clients.arxiv.downloads) == 3
    assert len(batches.recent()) == 1


def test_auto_sync_does_not_touch_another_direction(automatic):
    config, clients, tracker, batches = automatic
    config.version_sync.enabled = True
    other_library = PaperLibraryStore(batches.output_root, "other")
    other_library.add({"paper": paper(1).to_dict()}, "Other")
    tracker.check()
    assert other_library.all()["2501.00001"]["paper"]["version"] == 1
    assert not (batches.output_root / "papers/other").exists()
