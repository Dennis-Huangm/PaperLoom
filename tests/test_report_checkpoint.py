from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import sys
import time

import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.job_requests import capture_request
from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata, FigureCandidate
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.report import ReportGenerator
from arxiv_ra.report_checkpoint import ReportCheckpoint, bind_report_checkpoint, CheckpointWriteError
from arxiv_ra.task_runtime import TaskHooks, TaskCancelled, bind_task_hooks
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.web_jobs import JobManager
from fastapi.testclient import TestClient
from arxiv_ra.web import create_app
from arxiv_ra.task_runtime import task_checkpoint_data, task_warning


def paper():
    return Paper.from_dict({"arxiv_id": "2407.05600", "title": "Checkpoint Paper", "version": 2,
                            "abstract": "Original abstract", "metadata_status": "complete"})


def setup(tmp_path):
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="alpha")
    config.llm.max_chunk_chars = 4000
    config.obsidian.enabled = False
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value="# Checkpoint Paper\n\n## 核心方法\nEvidence"),
                          client=SimpleNamespace(base_url="https://fixture.invalid/v1"))
    clients = SimpleNamespace(
        llm=llm, reporter=ReportGenerator(llm, config.llm), alphaxiv=None,
        verifier=SimpleNamespace(verify=Mock(return_value=VerifiedMetadata(title=paper().title))),
        arxiv=SimpleNamespace(download_pdf=Mock(side_effect=lambda p, target: target.write_bytes(b"fixture PDF v2"))),
        parser=SimpleNamespace(parse=Mock(return_value=ParsedPaper("A" * 9000, ["A" * 9000]))),
        arxiv_html=SimpleNamespace(fetch=Mock(return_value=[])))
    return config, clients


def wait_job(manager, identifier):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = manager.get(identifier)
        if job.status not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(.01)
    pytest.fail("task did not finish")


def stop(manager):
    manager.close()
    manager.executor.shutdown(wait=True)


@pytest.mark.parametrize("stage", ["download", "parse", "empty_text"])
def test_pdf_failure_is_failed_even_when_abstract_report_is_saved(tmp_path, monkeypatch, stage):
    config, clients = setup(tmp_path)
    monkeypatch.setattr("arxiv_ra.pipeline.PaperResolver.resolve", lambda *a, **k: deepcopy(paper()))
    if stage == "download":
        clients.arxiv.download_pdf.side_effect = RuntimeError("PDF download failed")
    elif stage == "parse":
        clients.parser.parse.side_effect = ValueError("PDF parse failed")
    else:
        clients.parser.parse.return_value = ParsedPaper("", [])
    root = Path(config.output_dir)
    manager = JobManager(root)
    recipe = capture_request("report", config, tmp_path, arxiv_id=paper().arxiv_id)
    try:
        job = manager.submit("report", "fixture",
                             lambda: DailyPipeline(config, tmp_path, clients=clients).report_arxiv_id(paper().arxiv_id),
                             request=recipe, profile_id="alpha")
        failed = wait_job(manager, job.id)
        assert failed.status == "failed"
        assert failed.progress is None and "PDF" in failed.detail
        assert failed.result_url and failed.recoverable
        assert not failed.report_progress["parsed"]
        assert failed.report_progress["resumable"]
        assert next(root.glob("*/reports/*/report.md")).is_file()
        # Old warning-success records are corrected when loading the queue.
        record_path = root / ".jobs" / f"{job.id}.json"
        record = read_json(record_path)
        record["job"].update(status="succeeded_with_warnings", progress=100, detail="任务已完成")
    finally:
        stop(manager)
    write_json(record_path, record)
    manager = JobManager(root)
    try:
        restored = manager.get(job.id)
        assert restored.status == "failed" and restored.progress is None
        assert read_json(record_path)["job"]["status"] == "failed"
    finally:
        stop(manager)


def test_fallback_after_restart_resumes_remaining_chunks_and_preserves_old_report(tmp_path, monkeypatch):
    config, clients = setup(tmp_path)
    monkeypatch.setattr("arxiv_ra.pipeline.PaperResolver.resolve", lambda *a, **k: deepcopy(paper()))
    monkeypatch.setattr("arxiv_ra.pipeline.DailyPipeline", lambda cfg, root: DailyPipeline(cfg, root, clients=clients))
    clients.llm.chat.side_effect = ["first chunk evidence", TimeoutError("offline fixture")]
    root = Path(config.output_dir)
    manager = JobManager(root)
    request = capture_request("report", config, tmp_path, arxiv_id=paper().arxiv_id, snapshot=paper().to_dict())
    first = manager.submit("report", "fixture", lambda: DailyPipeline(config, tmp_path, clients=clients).report_arxiv_id(paper().arxiv_id),
                           profile_id="alpha", request=request)
    first = wait_job(manager, first.id)
    assert first.status == "succeeded_with_warnings"
    assert first.report_progress["chunks_done"] == 1 and first.report_progress["resumable"]
    old_report = next(root.glob("*/reports/*/report.md"))
    old_content = old_report.read_bytes()
    identifier = first.id
    stop(manager)

    clients.llm.chat.reset_mock(side_effect=True)
    manager = JobManager(root)
    try:
        child = manager.retry(identifier, "alpha")
        recovered = wait_job(manager, child.id)
        assert recovered.status == "succeeded"
        assert clients.llm.chat.call_count == 3  # two remaining chunks plus synthesis
        assert clients.arxiv.download_pdf.call_count == clients.parser.parse.call_count == 1
        assert recovered.report_progress == {"parsed": True, "chunks_done": 3, "chunks_total": 3,
                                             "report_ready": True, "resumable": False}
        assert old_report.read_bytes() == old_content
        assert len(list(root.glob("*/reports/*/report.md"))) == 2
        assert manager.retry(identifier, "alpha").id == child.id
        with pytest.raises(ValueError):
            manager.retry(child.id, "alpha")
    finally:
        stop(manager)


def prepared(tmp_path):
    config, clients = setup(tmp_path)
    checkpoint = ReportCheckpoint(config.output_dir, config, paper())
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"source PDF")
    parsed = clients.parser.parse.return_value
    checkpoint.save_parsed(parsed, pdf)
    return config, clients, checkpoint, parsed


def generate(clients, checkpoint, parsed):
    with bind_report_checkpoint(checkpoint):
        return clients.reporter.generate(paper(), VerifiedMetadata(title=paper().title), parsed, [])


def test_final_integration_failure_reuses_all_chunks_and_completed_report(tmp_path):
    config, clients, checkpoint, parsed = prepared(tmp_path)
    clients.llm.chat.side_effect = ["chunk1", "chunk2", "chunk3", RuntimeError("synthesis failure")]
    with pytest.raises(RuntimeError):
        generate(clients, checkpoint, parsed)
    assert checkpoint.state["chunks_done"] == 3 and not checkpoint.state["report_ready"]
    clients.llm.chat.reset_mock(side_effect=True)
    restored = ReportCheckpoint(config.output_dir, config, paper(), {"id": checkpoint.root.name})
    target = tmp_path / "new"
    target.mkdir()
    parsed = restored.restore_parsed(target)
    first = generate(clients, restored, parsed)
    assert clients.llm.chat.call_count == 1
    assert generate(clients, restored, parsed) == first
    assert clients.llm.chat.call_count == 1


@pytest.mark.parametrize("change", ["model", "endpoint", "temperature", "chunks", "prompt", "source", "parser"])
def test_incompatible_processing_inputs_are_not_reused(tmp_path, monkeypatch, change):
    config, clients, checkpoint, parsed = prepared(tmp_path)
    generate(clients, checkpoint, parsed)
    clients.llm.chat.reset_mock()
    if change == "model":
        config.llm.model = "different-model"
    elif change == "endpoint":
        clients.llm.client.base_url = "https://other.invalid/v1"
    elif change == "temperature":
        config.llm.temperature = .8
    elif change == "chunks":
        config.llm.max_chunk_chars = 5000
    elif change == "prompt":
        monkeypatch.setattr("arxiv_ra.report.CHUNK_SYSTEM", "Changed extraction instructions")
    elif change == "source":
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"different source PDF")
        checkpoint.save_parsed(parsed, pdf)
    else:
        checkpoint.pdf_config["max_pages"] += 1
        checkpoint.save_parsed(parsed, tmp_path / "paper.pdf")
    generate(clients, checkpoint, parsed)
    # A changed extraction prompt regenerates chunks; identical resulting notes
    # may still permit synthesis reuse because its exact input has not changed.
    assert clients.llm.chat.call_count >= (2 if change == "chunks" else 3)


def test_source_integrity_missing_cache_and_revision_change(tmp_path):
    config, clients, checkpoint, parsed = prepared(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    checkpoint.root.joinpath("paper.pdf").write_bytes(b"tampered")
    assert checkpoint.restore_parsed(target) is None
    checkpoint.save_parsed(parsed, tmp_path / "paper.pdf")
    raw = read_json(checkpoint.root / "parsed.json")
    raw["parsed"]["text"] = "altered extracted text"
    write_json(checkpoint.root / "parsed.json", raw)
    assert checkpoint.restore_parsed(target) is None
    changed = paper()
    changed.version = 3
    replacement = ReportCheckpoint(config.output_dir, config, changed, {"id": checkpoint.root.name})
    assert replacement.root != checkpoint.root and checkpoint.root.exists()
    missing = ReportCheckpoint(config.output_dir, config, paper(), {"id": "a" * 32})
    assert missing.root.name != "a" * 32
    traversal = ReportCheckpoint(config.output_dir, config, paper(), {"id": "../outside"})
    assert traversal.root.parent == checkpoint.root.parent


def test_corrupt_chunk_regenerates_only_that_step_and_ignores_empty_response(tmp_path):
    config, clients, checkpoint, parsed = prepared(tmp_path)
    generate(clients, checkpoint, parsed)
    checkpoint.root.joinpath("chunk-2.json").write_text("{invalid", encoding="utf-8")
    clients.llm.chat.reset_mock()
    generate(clients, checkpoint, parsed)
    assert clients.llm.chat.call_count == 1
    checkpoint.root.joinpath("chunk-2.json").write_text("{}", encoding="utf-8")
    clients.llm.chat.return_value = ""
    with pytest.raises(ValueError, match="空内容"):
        generate(clients, checkpoint, parsed)
    assert read_json(checkpoint.root / "chunk-2.json") == {}


def test_cancel_inflight_keeps_completed_chunk_for_next_attempt(tmp_path):
    config, clients, checkpoint, parsed = prepared(tmp_path)
    cancelled = False
    def response(*args):
        nonlocal cancelled
        cancelled = True
        return "completed before cancellation boundary"
    clients.llm.chat.side_effect = response
    with bind_task_hooks(TaskHooks(lambda *a: None, lambda *a: None, lambda: cancelled)):
        with pytest.raises(TaskCancelled):
            generate(clients, checkpoint, parsed)
    assert read_json(checkpoint.root / "chunk-1.json")["text"] == "completed before cancellation boundary"
    clients.llm.chat.reset_mock(side_effect=True)
    generate(clients, checkpoint, parsed)
    assert clients.llm.chat.call_count == 3


def test_failed_checkpoint_write_stops_without_marking_chunk_complete(tmp_path, monkeypatch):
    config, clients, checkpoint, parsed = prepared(tmp_path)
    import arxiv_ra.report_checkpoint as module
    original = module.write_json
    def fail(path, value):
        if path.name == "chunk-1.json":
            raise OSError("disk full")
        original(path, value)
    monkeypatch.setattr(module, "write_json", fail)
    with pytest.raises(CheckpointWriteError):
        generate(clients, checkpoint, parsed)
    assert clients.llm.chat.call_count == 1
    assert checkpoint.state["chunks_done"] == 0


def test_checkpoint_survives_abrupt_process_exit(tmp_path):
    code = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from arxiv_ra.config import AppConfig
from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.report import ReportGenerator
from arxiv_ra.report_checkpoint import ReportCheckpoint, bind_report_checkpoint
root = Path(sys.argv[1])
config = AppConfig(output_dir=str(root), profile_id="alpha")
config.llm.max_chunk_chars = 4000
p = Paper.from_dict({"arxiv_id": "2407.05600", "title": "Checkpoint Paper", "version": 2, "abstract": "Original abstract", "metadata_status": "complete"})
cp = ReportCheckpoint(root, config, p)
(root / "identifier").write_text(cp.root.name)
calls = 0
def chat(*args):
 global calls
 calls += 1
 if calls == 2: os._exit(77)
 return "saved evidence"
llm = SimpleNamespace(enabled=True, chat=chat, client=None)
with bind_report_checkpoint(cp):
 ReportGenerator(llm, config.llm).generate(p, VerifiedMetadata(), ParsedPaper("A" * 9000, ["A" * 9000]), [])
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, timeout=15)
    assert result.returncode == 77, result.stderr.decode(errors="replace")
    config, clients = setup(tmp_path)
    config.output_dir = str(tmp_path)
    clients.llm.client = None
    restored = ReportCheckpoint(tmp_path, config, paper(), {"id": (tmp_path / "identifier").read_text()})
    generate(clients, restored, clients.parser.parse.return_value)
    assert clients.llm.chat.call_count == 3


def test_figure_explanations_reused_with_verified_image(tmp_path):
    config, clients, checkpoint, parsed = prepared(tmp_path)
    figure_path = tmp_path / "figure.png"
    figure_path.write_bytes(b"fixture image")
    clients.llm.describe_figure = Mock(return_value="Verified diagram explanation")
    def run():
        with bind_report_checkpoint(checkpoint):
            clients.reporter.generate(paper(), VerifiedMetadata(), parsed, [FigureCandidate(figure_path, 1, "Figure 1")])
    run()
    run()
    assert clients.llm.describe_figure.call_count == 1
    figure_path.write_bytes(b"changed image")
    run()
    assert clients.llm.describe_figure.call_count == 2


def test_report_recovery_ui_api_progress_and_private_cache(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("output_dir: run\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
    app = create_app(path)
    def fallback():
        task_checkpoint_data("report_work", {"id": "a" * 32})
        task_checkpoint_data("report_progress", {"parsed": True, "chunks_done": 2, "chunks_total": 3,
                                                  "report_ready": False, "resumable": True})
        task_warning("LLM", "fixture fallback")
        return tmp_path / "run/fallback.html"
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="alpha")
    recipe = capture_request("report", config, tmp_path, arxiv_id=paper().arxiv_id)
    with TestClient(app) as client:
        job = app.state.jobs.submit("report", "fixture", fallback, request=recipe, profile_id="alpha")
        wait_job(app.state.jobs, job.id)
        page = client.get("/generate").text
        assert "继续全文分析" in page and "分片 2/3" in page
        assert f'data-retry-job="{job.id}"' in page and "打开结果" in page
        private = tmp_path / "run/.jobs/report-work" / ("a" * 32) / "chunk-1.json"
        write_json(private, {"text": "private evidence"})
        assert client.get(f"/artifacts/.jobs/report-work/{'a' * 32}/chunk-1.json").status_code == 404
        payload = client.get(f"/api/jobs/{job.id}").json()
        assert payload["report_progress"]["chunks_done"] == 2
        assert "report_work" not in payload and "checkpoint" not in payload
        assert client.post(f"/api/jobs/{job.id}/retry", headers={"X-Paperloom-Profile": "beta"}).status_code == 409
        monkeypatch.setattr("arxiv_ra.web_jobs.recovery_runner", lambda *a: lambda: tmp_path / "run/full.html")
        response = client.post(f"/api/jobs/{job.id}/retry")
        assert response.status_code == 202
        wait_job(app.state.jobs, response.json()["id"])
        assert client.post(f"/api/jobs/{job.id}/retry").json()["id"] == response.json()["id"]


def test_local_pdf_change_and_checkpoint_path_cannot_escape_storage(tmp_path):
    config, clients, checkpoint, parsed = prepared(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    new_pdf = tmp_path / "new.pdf"
    new_pdf.write_bytes(b"new local revision")
    assert checkpoint.restore_parsed(target, new_pdf) is None
    record = read_json(checkpoint.root / "parsed.json")
    record["files"]["../outside.txt"] = "anything"
    write_json(checkpoint.root / "parsed.json", record)
    assert checkpoint.restore_parsed(target) is None
    assert not target.parent.joinpath("outside.txt").exists()
