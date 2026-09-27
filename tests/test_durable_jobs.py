import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from arxiv_ra.config import AppConfig
from arxiv_ra.job_requests import capture_request, recovery_runner
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.task_runtime import task_checkpoint_data, task_progress, task_warning
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.web import create_app
from arxiv_ra.web_jobs import JobManager


def wait_job(manager, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if manager.get(job.id).status not in {"queued", "running", "cancelling"}:
            return manager.get(job.id)
        time.sleep(.01)
    pytest.fail("job did not finish")


def stop(manager):
    manager.close()
    manager.executor.shutdown(wait=True)


def request(root, kind="report", **parameters):
    return capture_request(kind, AppConfig(output_dir=str(root), profile_id="alpha"), root, **parameters)


def test_process_loss_preserves_running_and_queued_requests_without_auto_replay(tmp_path):
    # Exit without finally/shutdown, after real OS locks and atomic writes.
    code = '''
import os, sys, threading
from pathlib import Path
from arxiv_ra.config import AppConfig
from arxiv_ra.web_jobs import JobManager
from arxiv_ra.job_requests import capture_request
from arxiv_ra.task_runtime import task_checkpoint_data, task_progress, task_warning
from arxiv_ra.utils import write_json
root = Path(sys.argv[1])
manager = JobManager(root, 1)
gate = threading.Event()
def work():
    task_checkpoint_data("paper", {"arxiv_id": "2407.05600", "version": 2, "title": "Pinned"})
    task_progress("PDF parsed", 40)
    task_warning("parser", "partial extraction")
    gate.wait(10)
    os._exit(77)
recipe = capture_request("report", AppConfig(profile_id="alpha"), root, arxiv_id="2407.05600", snapshot=None)
first = manager.submit("report", "first", work, request=recipe, profile_id="alpha")
second = manager.submit("report", "second", lambda: root / "SHOULD_NOT_RUN", request=recipe, profile_id="alpha")
write_json(root / "ids.json", [first.id, second.id])
gate.set()
manager.executor.shutdown(wait=True)
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], timeout=20, capture_output=True)
    assert result.returncode == 77, result.stderr.decode(errors="replace")
    ids = read_json(tmp_path / "ids.json")
    manager = JobManager(tmp_path)
    try:
        assert all(manager.get(aid).status == "interrupted" for aid in ids)
        first = manager.get(ids[0])
        assert "PDF parsed" in first.detail and first.warnings[0]["message"] == "partial extraction"
        assert manager.checkpoints[first.id]["paper"]["version"] == 2
        assert not manager.pending and manager.active_count == 0
        assert all(manager.get(aid).recoverable for aid in ids)
    finally:
        stop(manager)


def test_exclusive_owner_released_only_after_shutdown_workers_finish(tmp_path):
    manager = JobManager(tmp_path)
    release, started = threading.Event(), threading.Event()
    def work():
        started.set()
        release.wait(5)
        return tmp_path / "result.html"
    manager.submit("weekly", "holding", work)
    assert started.wait(2)
    manager.close()
    try:
        with pytest.raises(RuntimeError, match="已有 GUI"):
            JobManager(tmp_path)
    finally:
        release.set()
        manager.executor.shutdown(wait=True)
    another = JobManager(tmp_path)
    stop(another)


def test_failed_write_never_dispatches_task(tmp_path, monkeypatch):
    manager = JobManager(tmp_path)
    ran = []
    def fail(_):
        raise OSError("disk full")
    monkeypatch.setattr(manager.store, "save", fail)
    try:
        with pytest.raises(OSError):
            manager.submit("report", "test", lambda: ran.append(True))
        assert not ran and not manager.jobs and not manager.pending
    finally:
        stop(manager)


def test_restart_retains_completion_and_preserves_malformed_files(tmp_path):
    manager = JobManager(tmp_path)
    result = tmp_path / "report.html"
    result.write_text("report")
    def work():
        task_warning("test", "kept warning")
        return result
    job = manager.submit("report", "test", work)
    assert wait_job(manager, job).status == "succeeded_with_warnings"
    stop(manager)
    broken = tmp_path / ".jobs/broken.json"
    broken.write_text("{broken")
    manager = JobManager(tmp_path)
    try:
        saved = manager.get(job.id)
        assert saved.result_url == "/artifacts/report.html" and saved.warnings
        assert saved.status == "succeeded_with_warnings"
        assert manager.load_errors and broken.read_text() == "{broken"
    finally:
        stop(manager)


def test_retry_uses_pinned_paper_and_saved_config_without_duplicate_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "PRIVATE_RUNTIME_SECRET")
    manager = JobManager(tmp_path)
    recipe = request(tmp_path, arxiv_id="2407.05600", snapshot=None)
    def interrupted():
        task_checkpoint_data("paper", {"arxiv_id": "2407.05600", "version": 2, "title": "Pinned", "metadata_status": "complete"})
        raise RuntimeError("connection lost")
    old = manager.submit("report", "old", interrupted, request=recipe, profile_id="alpha")
    assert wait_job(manager, old).status == "failed"
    assert "PRIVATE_RUNTIME_SECRET" not in (tmp_path / f".jobs/{old.id}.json").read_text(encoding="utf-8")
    recipe["config"]["profile_id"] = "changed-after-submit"
    stop(manager)
    manager = JobManager(tmp_path)
    calls = []
    class Pipeline:
        def __init__(self, config, root):
            calls.append(config.profile_id)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def report_arxiv_id(self, aid, snapshot):
            calls.append((aid, snapshot.version))
            return tmp_path / "report.html"
    monkeypatch.setattr("arxiv_ra.pipeline.DailyPipeline", Pipeline)
    try:
        with pytest.raises(ValueError, match="研究方向"):
            manager.retry(old.id, "beta")
        new = manager.retry(old.id, "alpha")
        assert wait_job(manager, new).status == "succeeded"
        assert new.retry_of == old.id and new.id != old.id
        assert manager.retry(old.id, "alpha").id == new.id
        assert calls == ["alpha", ("2407.05600", 2)]
        assert manager.get(old.id).status == "failed"
    finally:
        stop(manager)
    # Simulate loss after child publication but before linking the parent record.
    original_path = tmp_path / f".jobs/{old.id}.json"
    original = read_json(original_path)
    original["job"]["retry_job_id"] = ""
    write_json(original_path, original)
    manager = JobManager(tmp_path)
    try:
        assert manager.get(old.id).retry_job_id == new.id
        assert manager.retry(old.id, "alpha").id == new.id
    finally:
        stop(manager)


@pytest.mark.parametrize("kind", ["digest", "weekly", "compare", "version-batch"])
def test_recovery_execution_contracts(tmp_path, monkeypatch, kind):
    calls = []
    class Runner:
        def __init__(self, config, root):
            assert config.profile_id == "alpha"
            assert config.output_dir == str(tmp_path.resolve())
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            return tmp_path / "result.html"
        generate = run
    for module, name in [("pipeline", "DailyPipeline"), ("weekly", "WeeklySynthesizer"),
                         ("comparison", "ComparisonService"), ("version_batch", "VersionSyncBatch")]:
        monkeypatch.setattr(f"arxiv_ra.{module}.{name}", Runner)
    params = {"digest": {"force": True, "send_email": True},
              "weekly": {"now": "2026-09-24T10:00:00+08:00", "include_notes": True},
              "compare": {"snapshot": {"profile_id": "alpha", "sources": ["frozen"]}},
              "version-batch": {"batch_id": "batch"}}[kind]
    recovery_runner(request(tmp_path, kind, **params), tmp_path, {})()
    args, kwargs = calls[0]
    if kind == "digest": assert kwargs == {"force": True, "demo": False, "deliver": False}
    if kind == "weekly": assert kwargs["now"].isoformat() == params["now"] and kwargs["include_notes"]
    if kind == "compare": assert args[0] == params["snapshot"]
    if kind == "version-batch": assert args == ("batch",)


def test_api_restart_recovery_profile_guard_and_private_ledger(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("output_dir: run\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
    app = create_app(cfg)
    def failed(self, snapshot):
        raise RuntimeError("fixture failure")
    monkeypatch.setattr("arxiv_ra.comparison.ComparisonService.generate", failed)
    for aid in ["2407.05600", "2407.05601"]:
        PaperLibraryStore(tmp_path / "run", "alpha").add({"paper": {
            "arxiv_id": aid, "version": 2, "title": "Frozen paper", "abstract": "Public source"}}, "Alpha")
    with TestClient(app) as client:
        payload = client.post("/api/jobs/compare", data={"papers": ["2407.05600v2", "2407.05601v2"]}).json()
        old = app.state.jobs.get(payload["id"])
        assert wait_job(app.state.jobs, old).status == "failed"
    app.state.jobs.executor.shutdown(wait=True)
    captured = []
    def succeeded(self, snapshot):
        captured.append(snapshot)
        return tmp_path / "run/result.html"
    monkeypatch.setattr("arxiv_ra.comparison.ComparisonService.generate", succeeded)
    app = create_app(cfg)
    with TestClient(app) as client:
        page = client.get("/generate").text
        assert f'data-retry-job="{old.id}"' in page
        assert client.get(f"/api/jobs/{old.id}").json()["status"] == "failed"
        assert client.get(f"/artifacts/.jobs/{old.id}.json").status_code == 404
        assert client.get(f"/artifacts/.JOBS/{old.id}.json").status_code == 404
        assert client.post(f"/api/jobs/{old.id}/retry", headers={"X-Paperloom-Profile": "beta"}).status_code == 409
        response = client.post(f"/api/jobs/{old.id}/retry")
        assert response.status_code == 202
        new = app.state.jobs.get(response.json()["id"])
        assert wait_job(app.state.jobs, new).status == "succeeded"
        assert captured[0]["sources"][0]["paper"]["version"] == 2
        assert "config" not in response.json() and "request" not in response.json()


def test_history_pages_retain_older_recoverable_failures_on_first_page(tmp_path):
    manager = JobManager(tmp_path)
    def fail(): raise RuntimeError("offline")
    old = manager.submit("report", "old failure", fail, profile_id="alpha",
                         request=request(tmp_path, arxiv_id="2407.05600", snapshot=None))
    wait_job(manager, old)
    try:
        for index in range(27):
            job = manager.submit("report", str(index), lambda: tmp_path / "result.html")
            wait_job(manager, job)
        first = manager.history(1)
        assert first["pages"] == 2 and old.id in {item["id"] for item in first["items"]}
        second = manager.history(2)
        assert len(second["items"]) == 3 and second["page"] == 2
        assert manager.history(-1)["page"] == 1
    finally:
        stop(manager)


def test_pinned_recovery_does_not_coalesce_with_new_active_latest_request(tmp_path, monkeypatch):
    manager = JobManager(tmp_path, max_parallel=2)
    release, started = threading.Event(), threading.Event()
    recipe = request(tmp_path, arxiv_id="2407.05600", snapshot=None)
    def fail():
        task_checkpoint_data("paper", {"arxiv_id": "2407.05600", "version": 2, "metadata_status": "complete"})
        raise RuntimeError("lost")
    old = manager.submit("report", "latest", fail, request=recipe, profile_id="alpha", identity="latest-request")
    wait_job(manager, old)
    def query_latest():
        started.set()
        release.wait(5)
        return tmp_path / "newest.html"
    latest = manager.submit("report", "latest", query_latest, request=recipe, profile_id="alpha", identity="latest-request")
    assert started.wait(2)
    seen = []
    def fake_recovery(request, root, checkpoint):
        seen.append(checkpoint["paper"]["version"])
        return lambda: root / "v2.html"
    monkeypatch.setattr("arxiv_ra.web_jobs.recovery_runner", fake_recovery)
    try:
        retry = manager.retry(old.id, "alpha")
        assert retry.id != latest.id and wait_job(manager, retry).status == "succeeded"
        assert seen == [2]
        assert retry.result_url == "/artifacts/v2.html"
        assert manager.retry(old.id, "alpha").id == retry.id
    finally:
        release.set()
        stop(manager)
