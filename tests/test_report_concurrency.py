"""Concurrency acceptance through report generation and the real offline SDK."""
import json
import re
from threading import Barrier, Event, Lock
import time

import httpx
import pytest
from openai import APIStatusError, OpenAI

from arxiv_ra.config import AppConfig, LLMConfig
from arxiv_ra.llm import LLMClient
from arxiv_ra.models import FigureCandidate, Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.report import ReportGenerator
from arxiv_ra.report_checkpoint import ReportCheckpoint, bind_report_checkpoint
from arxiv_ra.model_budget import ModelBudgetExceeded, model_request_budget
from arxiv_ra.task_runtime import TaskCancelled, TaskHooks, bind_task_hooks


@pytest.fixture
def report_case(tmp_path):
    clients = []

    def make(respond, *, figures=0, chunks=2):
        config = AppConfig(output_dir=str(tmp_path / "run"))
        config.llm.max_chunk_chars = 4000
        paper = Paper.from_dict({"arxiv_id": "2501.12345", "title": "Concurrent paper"})
        parsed = ParsedPaper("".join(chr(65 + i) * 4000 for i in range(chunks)), ["source page"])
        images = []
        for index in range(figures):
            path = tmp_path / f"figure-{index}.png"
            path.write_bytes(b"offline-image")
            images.append(FigureCandidate(path=path, page=1, caption=f"Figure {index + 1}: Method"))

        def transport(request):
            body = json.loads(request.content)
            user = body["messages"][1]["content"]
            if isinstance(user, list):
                key = "figure"
            else:
                match = re.search(r"这是论文第 (\d+)/(\d+) 个片段", user)
                key = (f"chunk-{match[1]}" if match else "table" if
                       body["messages"][0]["content"].startswith("你是论文表格编辑") else "report")
            content = respond(key, user)
            if isinstance(content, httpx.Response):
                return content
            return httpx.Response(200, json={"id": "offline", "object": "chat.completion",
                "created": 0, "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}]})

        llm = LLMClient.__new__(LLMClient)
        llm.enabled, llm.config = True, config.llm
        llm.client = OpenAI(api_key="offline", base_url="https://offline.test/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        clients.append(llm.client)
        checkpoint = ReportCheckpoint(config.output_dir, config, paper)
        generator = ReportGenerator(llm, config.llm)

        def generate():
            with bind_report_checkpoint(checkpoint):
                return generator.generate(paper, VerifiedMetadata(), parsed, images)

        return config, checkpoint, images, generate

    yield make
    for client in clients:
        client.close()


def test_independent_figure_and_chunk_requests_overlap(report_case):
    chunk_started = Event()
    overlap = []
    synthesis = []

    def respond(key, user):
        if key == "figure":
            overlap.append(chunk_started.wait(1))
            return "method figure explanation"
        if key.startswith("chunk-"):
            chunk_started.set()
            return f"evidence-{key}"
        synthesis.append(user)
        return "# Concurrent paper\n\n## 核心方法\ncomplete report"

    _, checkpoint, _, generate = report_case(respond, figures=1)
    assert "complete report" in generate()
    assert overlap == [True]
    assert synthesis[0].index("evidence-chunk-1") < synthesis[0].index("evidence-chunk-2")
    assert "method figure explanation" in synthesis[0]
    assert checkpoint.state["chunks_done"] == 2


def test_parallel_model_budget_callbacks_remain_ordered_and_nonoverlapping(report_case):
    callback_active = 0
    overlap = []
    counts = []
    guard = Lock()

    def record(used):
        nonlocal callback_active
        with guard:
            callback_active += 1
            overlap.append(callback_active)
        time.sleep(0.04)
        counts.append(used)
        with guard:
            callback_active -= 1

    _, _, _, generate = report_case(lambda key, user: f"evidence-{key}", chunks=4)
    with model_request_budget(8, record) as budget:
        generate()
    assert max(overlap) == 1
    assert counts == [1, 2, 3, 4, 5] and budget.used == 5


def test_out_of_order_completion_is_bounded_and_synthesized_in_source_order(report_case):
    second_finished = Event()
    guard = Lock()
    active, peak = 0, 0
    finished, synthesis = [], []

    def respond(key, user):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        if key == "chunk-1":
            assert second_finished.wait(2)
        time.sleep(0.02)
        with guard:
            finished.append(key)
            active -= 1
        if key == "chunk-2":
            second_finished.set()
        if key == "report":
            synthesis.append(user)
        return f"evidence-{key}"

    _, checkpoint, _, generate = report_case(respond, chunks=5)
    generate()
    assert peak == 2 and finished.index("chunk-2") < finished.index("chunk-1")
    assert [synthesis[0].index(f"evidence-chunk-{i}") for i in range(1, 6)] == sorted(
        synthesis[0].index(f"evidence-chunk-{i}") for i in range(1, 6))
    assert checkpoint.state["chunks_done"] == 5
    records = [json.loads(line) for line in (checkpoint.root / "model-requests.jsonl").read_text().splitlines()]
    assert len(records) == 12
    assert {row["stage"] for row in records} == {"report", *(f"chunk-{i}" for i in range(1, 6))}


def test_cancel_drains_inflight_responses_and_resumes_only_remaining_chunks(report_case):
    cancelled = Event()
    barrier = Barrier(2)
    calls = []
    recovering = False

    def respond(key, user):
        calls.append(key)
        if not recovering:
            barrier.wait(2)
            if key == "chunk-2":
                cancelled.set()
            else:
                assert cancelled.wait(2)
        return f"evidence-{key}"

    _, checkpoint, _, generate = report_case(respond, chunks=4)
    hooks = TaskHooks(lambda *a: None, lambda *a: None, cancelled.is_set)
    with bind_task_hooks(hooks), pytest.raises(TaskCancelled):
        generate()
    assert set(calls) == {"chunk-1", "chunk-2"}
    assert all((checkpoint.root / f"chunk-{i}.json").is_file() for i in (1, 2))
    calls.clear()
    recovering = True
    generate()
    assert set(calls) == {"chunk-3", "chunk-4", "report"}


def test_failure_stops_new_work_but_preserves_other_inflight_success(report_case):
    second_started = Event()
    calls = []
    recovering = False

    def respond(key, user):
        calls.append(key)
        if not recovering:
            if key == "chunk-1":
                assert second_started.wait(2)
                return httpx.Response(400, json={"error": {"message": "invalid request"}})
            second_started.set()
            time.sleep(0.05)
        return f"evidence-{key}"

    _, checkpoint, _, generate = report_case(respond, chunks=4)
    with pytest.raises(APIStatusError):
        generate()
    assert set(calls) == {"chunk-1", "chunk-2"}
    assert (checkpoint.root / "chunk-2.json").is_file()
    assert not (checkpoint.root / "chunk-1.json").exists()
    assert checkpoint.state["chunks_done"] == 1
    calls.clear()
    recovering = True
    generate()
    assert set(calls) == {"chunk-1", "chunk-3", "chunk-4", "report"}


def test_changing_concurrency_reuses_completed_responses_and_recomputes_corruption(report_case):
    calls = []

    def respond(key, user):
        calls.append(key)
        return f"evidence-{key}"

    config, checkpoint, _, generate = report_case(respond)
    first = generate()
    calls.clear()
    config.llm.analysis_workers = 1
    assert generate() == first and not calls
    (checkpoint.root / "chunk-2.json").write_text('{"text":"tampered"}')
    assert generate() == first and calls == ["chunk-2"]


def test_parallel_requests_share_one_task_budget(report_case):
    calls = []

    def respond(key, user):
        calls.append(key)
        time.sleep(0.02)
        return f"evidence-{key}"

    _, _, _, generate = report_case(respond, chunks=4)
    with model_request_budget(1) as budget, pytest.raises(ModelBudgetExceeded):
        generate()
    assert len(calls) == budget.used == 1 and budget.blocked >= 1


def test_cancellation_during_budget_persistence_does_not_send_reserved_request(report_case):
    cancelled = Event()
    calls = []

    def respond(key, user):
        calls.append(key)
        return f"evidence-{key}"

    _, _, _, generate = report_case(respond)
    hooks = TaskHooks(lambda *a: None, lambda *a: None, cancelled.is_set)
    with bind_task_hooks(hooks), model_request_budget(8, lambda used: cancelled.set()), pytest.raises(TaskCancelled):
        generate()
    assert not calls


def test_multiple_figures_keep_distinct_checkpoints_and_source_order(report_case):
    calls, synthesis = [], []

    def respond(key, user):
        calls.append(key)
        if key == "figure":
            number = int(re.search(r"Figure (\d+)", user[0]["text"])[1])
            time.sleep(0.02 if number == 1 else 0.005)
            return f"explanation-{number}"
        if key == "report":
            synthesis.append(user)
        return f"evidence-{key}"

    _, checkpoint, figures, generate = report_case(respond, figures=3)
    first = generate()
    assert [figure.explanation for figure in figures] == ["explanation-1", "explanation-2", "explanation-3"]
    assert synthesis[0].index("explanation-1") < synthesis[0].index("explanation-2") < synthesis[0].index("explanation-3")
    assert all(json.loads((checkpoint.root / f"figure-{i}.json").read_text())["text"] == f"explanation-{i+1}"
               for i in range(3))
    for figure in figures:
        figure.explanation = ""
    calls.clear()
    assert generate() == first and not calls


def test_table_organization_and_synthesis_wait_for_all_analysis_and_run_once(report_case):
    completed = set()
    calls = []
    plan = {"tables": [{"number": 1, "caption": "Scores", "variants": [{
        "schema": "e1", "condition": "", "note": "Higher is better.", "rows": [["e1-r1", "e2-r1"]]}]}]}

    def respond(key, user):
        calls.append(key)
        if key in {"figure", "chunk-1", "chunk-2"}:
            time.sleep(0.01)
            completed.add(key)
            return "figure explanation" if key == "figure" else "### Table 1: Scores\n| Model | Score |\n|---|---|\n| A | 91.2 |"
        assert completed == {"figure", "chunk-1", "chunk-2"}
        if key == "table":
            return json.dumps(plan)
        assert calls.count("table") == 1
        return "# Concurrent paper\n\n## 关键结果\n[[表格:table-1]]"

    _, _, _, generate = report_case(respond, figures=1)
    assert "91.2" in generate()
    assert calls.count("table") == calls.count("report") == 1 and len(calls) == 5


def test_serial_setting_sends_only_one_analysis_request_at_a_time(report_case):
    active = 0
    peak = 0

    def respond(key, user):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.005)
        active -= 1
        return f"evidence-{key}"

    config, _, _, generate = report_case(respond, figures=2, chunks=4)
    config.llm.analysis_workers = 1
    generate()
    assert peak == 1


@pytest.mark.parametrize("workers", [0, 5, 1.5, True])
def test_invalid_analysis_concurrency_is_rejected(workers):
    with pytest.raises(ValueError, match="并发数"):
        LLMConfig(analysis_workers=workers)
