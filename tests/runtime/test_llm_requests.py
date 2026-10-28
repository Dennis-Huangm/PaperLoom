"""Exercise the request policy through the real SDK and an offline HTTP transport."""
import json
import time

import httpx
import pytest
from openai import APIStatusError, APITimeoutError, OpenAI

from paperloom.config import AppConfig, LLMConfig
from paperloom.runtime.model_access.llm import LLMClient
from paperloom.papers.models import Paper, ParsedPaper, VerifiedMetadata
from paperloom.reports.report import ReportGenerator
from paperloom.reports.report_checkpoint import ReportCheckpoint, bind_report_checkpoint
from paperloom.runtime.model_access.model_budget import ModelBudgetExceeded, model_request_budget
from paperloom.runtime.jobs.task_runtime import TaskCancelled, TaskHooks, bind_task_hooks


def completion():
    return httpx.Response(200, json={
        "id": "offline", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "complete report"}}],
    })


@pytest.fixture
def offline_llm(monkeypatch):
    requests, sleeps, clients = [], [], []
    monkeypatch.setenv("LLM_API_KEY", "offline-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://offline.test/v1")
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def make(respond, config=None):
        def transport(request):
            requests.append(request)
            return respond(request)

        def factory(**kwargs):
            client = OpenAI(**kwargs, http_client=httpx.Client(
                transport=httpx.MockTransport(transport)))
            clients.append(client)
            return client

        monkeypatch.setattr("paperloom.runtime.model_access.llm.OpenAI", factory)
        return LLMClient(config or LLMConfig()), requests, sleeps

    yield make
    for client in clients:
        client.close()


def test_read_timeout_has_two_attempts_without_changing_generation_parameters(offline_llm):
    def respond(request):
        raise httpx.ReadTimeout("simulated", request=request)

    llm, requests, _ = offline_llm(respond)
    with pytest.raises(APITimeoutError):
        llm.chat("system", "paper", json_mode=True)
    assert len(requests) == 2
    assert json.loads(requests[0].content) == json.loads(requests[1].content)


@pytest.mark.parametrize("parameter", ["temperature", "response_format"])
def test_only_explicitly_unsupported_parameter_is_removed(offline_llm, parameter):
    def respond(request):
        if parameter in json.loads(request.content):
            return httpx.Response(400, json={"error": {
                "message": f"Unsupported parameter: {parameter}",
                "param": parameter, "code": "unsupported_parameter"}})
        return completion()

    llm, requests, _ = offline_llm(respond)
    assert llm.chat("system", "paper", json_mode=True) == "complete report"
    assert len(requests) == 2
    first, second = [json.loads(request.content) for request in requests]
    first.pop(parameter)
    assert first == second


def test_long_generation_keeps_explicit_read_timeout(offline_llm):
    llm, requests, _ = offline_llm(lambda request: completion(), LLMConfig(request_timeout_seconds=720))
    assert llm.chat("system", "paper") == "complete report"
    assert requests[0].extensions["timeout"] == {
        "connect": 5.0, "read": 720.0, "write": 60.0, "pool": 5.0}


@pytest.mark.parametrize("retry_after", ["3", "120"])
def test_rate_limit_obeys_retry_after_without_unbounded_wait(offline_llm, retry_after):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(429, headers={"retry-after": retry_after},
                                  json={"error": {"message": "busy"}})
        return completion()

    llm, requests, sleeps = offline_llm(respond)
    if retry_after == "3":
        assert llm.chat("system", "paper") == "complete report"
        assert len(requests) == 2 and sum(sleeps) == 3
    else:
        with pytest.raises(APIStatusError):
            llm.chat("system", "paper")
        assert len(requests) == 1 and not sleeps


def test_report_records_stage_attempt_and_elapsed_without_prompt_or_credentials(offline_llm, tmp_path):
    llm, requests, _ = offline_llm(lambda request: completion())
    paper = Paper.from_dict({"arxiv_id": "2501.12345", "title": "Offline paper"})
    config = AppConfig(output_dir=str(tmp_path / "run"))
    checkpoint = ReportCheckpoint(config.output_dir, config, paper)
    with bind_report_checkpoint(checkpoint):
        ReportGenerator(llm, config.llm).generate(
            paper, VerifiedMetadata(), ParsedPaper("private-paper-text", ["private-paper-text"]), [])
    raw = (checkpoint.root / "model-requests.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    events = [record for record in records if record["event"] == "finished"]
    assert [record["event"] for record in records] == ["started", "finished", "started", "finished"]
    assert len(events) == len(requests) == 2
    assert [event["stage"] for event in events] == ["chunk-1", "report"]
    assert all(event["outcome"] == "success" and event["elapsed_seconds"] >= 0 for event in events)
    assert "private-paper-text" not in raw and "offline-test-key" not in raw and "offline.test" not in raw


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_error_does_not_retry_or_strip_parameters(offline_llm, status):
    llm, requests, sleeps = offline_llm(lambda request: httpx.Response(status,
        json={"error": {"message": "temperature must be between 0 and 2", "param": "temperature"}}))
    with pytest.raises(APIStatusError):
        llm.chat("system", "paper", json_mode=True)
    assert len(requests) == 1 and not sleeps


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503])
def test_transient_failure_can_recover_without_parameter_changes(offline_llm, status):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(status, json={"error": {"message": "busy"}}) if count == 1 else completion()

    llm, requests, _ = offline_llm(respond)
    assert llm.chat("system", "paper", json_mode=True) == "complete report"
    assert len(requests) == 2 and requests[0].content == requests[1].content


def test_budget_includes_parameter_compatibility_retry(offline_llm):
    llm, requests, _ = offline_llm(lambda request: httpx.Response(400, json={
        "error": {"message": "Unsupported parameter: temperature", "param": "temperature"}}))
    with model_request_budget(1) as budget, pytest.raises(ModelBudgetExceeded):
        llm.chat("system", "paper")
    assert len(requests) == budget.used == budget.blocked == 1


@pytest.mark.parametrize("status", [400, 429, 500])
def test_optional_table_organization_never_retries(offline_llm, status):
    llm, requests, sleeps = offline_llm(lambda request: httpx.Response(status,
        json={"error": {"message": "Unsupported parameter: temperature", "param": "temperature"}}))
    with pytest.raises(APIStatusError):
        llm.chat("system", "paper", retry_on_error=False)
    assert len(requests) == 1 and not sleeps


def test_vision_uses_the_same_two_attempt_policy(offline_llm, tmp_path):
    def respond(request):
        raise httpx.ReadTimeout("simulated", request=request)

    llm, requests, _ = offline_llm(respond)
    image = tmp_path / "figure.png"
    image.write_bytes(b"offline-image")
    with pytest.raises(APITimeoutError):
        llm.describe_figure(image, "paper", "abstract", "caption")
    assert len(requests) == 2 and requests[0].content == requests[1].content


def test_cancellation_during_backoff_prevents_second_request(offline_llm, monkeypatch):
    llm, requests, _ = offline_llm(lambda request: httpx.Response(503, json={"error": {"message": "busy"}}))
    cancelled = False

    def sleep(delay):
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(time, "sleep", sleep)
    hooks = TaskHooks(lambda *args: None, lambda *args: None, lambda: cancelled)
    with bind_task_hooks(hooks), pytest.raises(TaskCancelled):
        llm.chat("system", "paper")
    assert len(requests) == 1


def test_completed_response_survives_inflight_cancellation(offline_llm, tmp_path):
    cancelled = False

    def respond(request):
        nonlocal cancelled
        cancelled = True
        return completion()

    llm, requests, _ = offline_llm(respond)
    paper = Paper.from_dict({"arxiv_id": "2501.12345", "title": "Offline paper"})
    config = AppConfig(output_dir=str(tmp_path / "run"))
    checkpoint = ReportCheckpoint(config.output_dir, config, paper)
    hooks = TaskHooks(lambda *args: None, lambda *args: None, lambda: cancelled)
    with bind_task_hooks(hooks), bind_report_checkpoint(checkpoint), pytest.raises(TaskCancelled):
        ReportGenerator(llm, config.llm).generate(
            paper, VerifiedMetadata(), ParsedPaper("source", ["source"]), [])
    assert len(requests) == 1
    assert json.loads((checkpoint.root / "chunk-1.json").read_text())["text"] == "complete report"


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_request_timeout_is_rejected(seconds):
    with pytest.raises(ValueError, match="超时"):
        LLMConfig(request_timeout_seconds=seconds)


def test_quota_exhaustion_is_not_a_transient_rate_limit(offline_llm):
    llm, requests, sleeps = offline_llm(lambda request: httpx.Response(429, json={
        "error": {"code": "insufficient_quota", "message": "quota exhausted"}}))
    with pytest.raises(APIStatusError):
        llm.chat("system", "paper")
    assert len(requests) == 1 and not sleeps


def test_compatibility_and_transient_failures_share_two_attempts(offline_llm):
    statuses = [400, 503]

    def respond(request):
        return httpx.Response(statuses.pop(0), json={"error": {
            "message": "Unsupported parameter: temperature", "param": "temperature"}})

    llm, requests, _ = offline_llm(respond)
    with pytest.raises(APIStatusError) as exc:
        llm.chat("system", "paper")
    assert exc.value.status_code == 503 and len(requests) == 2


@pytest.mark.parametrize("header, value", [
    ("retry-after-ms", "1500"),
    ("retry-after", "Thu, 01 Jan 2026 00:00:02 GMT"),
])
def test_server_cooldown_accepts_milliseconds_and_http_date(offline_llm, monkeypatch, header, value):
    monkeypatch.setattr(time, "time", lambda: 1767225600.0)
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(429, headers={header: value}, json={"error": {"message": "busy"}}) if count == 1 else completion()

    llm, requests, sleeps = offline_llm(respond)
    assert llm.chat("system", "paper") == "complete report"
    assert len(requests) == 2 and sum(sleeps) == (1.5 if header == "retry-after-ms" else 2.0)


def test_server_can_disable_transient_retries(offline_llm):
    llm, requests, sleeps = offline_llm(lambda request: httpx.Response(503,
        headers={"x-should-retry": "false"}, json={"error": {"message": "busy"}}))
    with pytest.raises(APIStatusError):
        llm.chat("system", "paper")
    assert len(requests) == 1 and not sleeps


def test_cancelled_task_does_not_send_or_consume_budget(offline_llm):
    llm, requests, _ = offline_llm(lambda request: completion())
    hooks = TaskHooks(lambda *args: None, lambda *args: None, lambda: True)
    with bind_task_hooks(hooks), model_request_budget(1) as budget, pytest.raises(TaskCancelled):
        llm.chat("system", "paper")
    assert not requests and budget.used == 0


def test_diagnostic_write_failure_does_not_discard_model_response(offline_llm, tmp_path):
    llm, requests, _ = offline_llm(lambda request: completion())
    paper = Paper.from_dict({"arxiv_id": "2501.12345", "title": "Offline paper"})
    config = AppConfig(output_dir=str(tmp_path / "run"))
    checkpoint = ReportCheckpoint(config.output_dir, config, paper)
    (checkpoint.root / "model-requests.jsonl").mkdir()
    with bind_report_checkpoint(checkpoint):
        result = ReportGenerator(llm, config.llm).generate(
            paper, VerifiedMetadata(), ParsedPaper("source", ["source"]), [])
    assert "complete report" in result and len(requests) == 2
    assert (checkpoint.root / "report.json").is_file()
