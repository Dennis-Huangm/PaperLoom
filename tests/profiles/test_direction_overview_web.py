import time
import threading
import json
import re
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from paperloom.runtime.research_clients import ResearchClients
from paperloom.web import create_app
from tests.profiles.test_direction_overview import direction, LiteratureAPI, OverviewModel
from types import SimpleNamespace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: SVG editing\n", encoding="utf-8")
    monkeypatch.setattr(ResearchClients, "llm", property(lambda self: OverviewModel()))
    monkeypatch.setattr(ResearchClients, "arxiv", property(lambda self: LiteratureAPI()))
    monkeypatch.setattr(ResearchClients, "conferences", property(lambda self: SimpleNamespace(
        discover=lambda *a, **kw: ([], {}))))
    app = create_app(config)
    app.state.profiles.save(direction())
    with TestClient(app) as client:
        yield client, app


def wait_overview(client, profile_id, source="saved"):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(f"/api/direction-overviews/{profile_id}?source={source}")
        assert response.status_code == 200
        value = response.json()
        if value["status"] not in {"queued", "running", "cancelling"}:
            return value
        time.sleep(.02)
    raise AssertionError("overview task did not finish")


@pytest.mark.parametrize('edited', [False, True])
@pytest.mark.parametrize('when', ['snapshot', 'response'])
def test_completion_between_article_and_job_reads_returns_published_article(tmp_path, monkeypatch, edited, when):
    from fastapi import FastAPI
    from paperloom.web import web_direction_overview
    from paperloom.profiles.direction_overview import OverviewStore
    from paperloom.runtime.jobs.manager import BackgroundJob

    before = {'id': 'svg', 'name': 'SVG', 'description': 'SVG editing'}
    current = {**before, 'description': 'Only SVG optimization'} if edited else before
    store = OverviewStore(tmp_path)
    run = store.start(before)
    store.attach_job(before, 'job')
    job = BackgroundJob('job', 'direction-overview', 'Overview', 'running', '', '')
    original_read = store.read
    completed = False

    def complete():
        nonlocal completed
        if not completed:
            completed = True
            store.publish(run, '# Saved article\n\nOriginal topic.', [], [])
            job.status = 'succeeded'

    def complete_after_snapshot(profile):
        snapshot = original_read(profile)
        if when == 'snapshot':
            complete()
        return snapshot

    original_asdict = web_direction_overview.asdict
    def complete_before_response(job):
        complete()
        return original_asdict(job)

    monkeypatch.setattr(store, 'read', complete_after_snapshot)
    monkeypatch.setattr(web_direction_overview, 'OverviewStore', lambda _: store)
    if when == 'response':
        monkeypatch.setattr(web_direction_overview, 'asdict', complete_before_response)
    app = FastAPI()
    web_direction_overview.register_overview_routes(
        app, SimpleNamespace(get=lambda _: current), lambda: None, None, None,
        SimpleNamespace(output_root=tmp_path, get=lambda _: job), tmp_path)
    with TestClient(app) as client:
        result = client.get('/api/direction-overviews/svg').json()
        if when == 'response':
            assert result['status'] == 'running'
            assert result['job']['status'] == 'running'
            result = client.get('/api/direction-overviews/svg').json()
    assert result['article']['id'] == run['id']
    assert 'Original topic.' in result['markdown']
    assert result['stale'] is edited


def test_existing_direction_can_generate_read_and_download_overview(workspace):
    client, app = workspace
    original = deepcopy(app.state.profiles.get("svg"))
    response = client.post("/api/direction-overviews/svg?source=saved")
    assert response.status_code == 202
    result = wait_overview(client, "svg")
    assert result["status"] == "ready"
    page = client.get("/profiles/svg/overview?source=saved")
    assert page.status_code == 200
    assert "SVG 编辑涉及控制点优化" in page.text
    assert "2401.00001v2" in page.text
    download = client.get("/profiles/svg/overview.md?source=saved")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    assert "## 参考文献" in download.text
    assert "/profiles/svg/overview?source=saved" in client.get("/profiles").text
    assert "方向小综述" in client.get("/profiles/svg/edit").text
    assert app.state.profiles.get("svg") == original


def test_new_draft_starts_overview_and_can_be_activated_before_completion(workspace, monkeypatch):
    client, app = workspace
    entered, release = threading.Event(), threading.Event()

    class Model(OverviewModel):
        def chat(self, system, prompt, **kwargs):
            if "profile_name" in prompt:
                return json.dumps({"profile_name": "SVG 编辑", "interest_description": "SVG editing",
                                   "arxiv_query_terms": ["SVG"], "arxiv_categories": ["cs.CV"]})
            if kwargs.get("json_mode"):
                entered.set()
                assert release.wait(5)
            return super().chat(system, prompt, **kwargs)

    monkeypatch.setattr(ResearchClients, "llm", property(lambda self: Model()))
    draft = client.post("/api/profile-drafts", json={"name": "SVG", "description": "SVG editing",
        "keywords": ["SVG"], "reference_ids": ["2401.00001v2"]}).json()
    try:
        assert entered.wait(2), "creating a direction must automatically start its overview"
        response = client.post(f"/api/profile-drafts/{draft['id']}/activate", json={"revision": draft["revision"]})
        assert response.status_code == 200
        assert "方向小综述" in client.get(f"/profiles/drafts/{draft['id']}").text
    finally:
        release.set()
    result = wait_overview(client, draft["id"])
    assert result["status"] == "ready"
    assert "SVG 编辑涉及控制点优化" in client.get(f"/profiles/{draft['id']}/overview").text


def test_editing_reference_papers_marks_existing_article_outdated(workspace):
    client, _ = workspace
    client.post("/api/direction-overviews/svg")
    assert wait_overview(client, "svg")["status"] == "ready"
    edit = client.get("/profiles/svg/edit")
    revision = re.search(r'name="profile_revision" value="([a-f0-9]{64})"', edit.text)[1]
    response = client.post("/profiles/svg/edit", data={
        "profile_revision": revision, "name": "SVG 生成", "description": "研究 SVG 生成与编辑",
        "keywords": "SVG", "negative_keywords": "", "categories": "cs.CV", "lookback_days": "2",
        "recommendation_count": "5", "max_candidates": "300", "reference_ids": "2501.00002v2",
    }, follow_redirects=False)
    assert response.status_code == 303
    result = client.get("/api/direction-overviews/svg").json()
    assert result["stale"] is True
    assert "SVG 编辑涉及控制点优化" in result["markdown"]


def test_topic_edit_during_generation_keeps_progress_for_original_snapshot(workspace, monkeypatch):
    client, _ = workspace
    entered, release = threading.Event(), threading.Event()

    class Model(OverviewModel):
        def chat(self, *args, **kwargs):
            if kwargs.get("json_mode"):
                entered.set()
                assert release.wait(5)
            return super().chat(*args, **kwargs)

    monkeypatch.setattr(ResearchClients, "llm", property(lambda self: Model()))
    client.post("/api/direction-overviews/svg")
    try:
        assert entered.wait(2)
        edit = client.get("/profiles/svg/edit")
        revision = re.search(r'name="profile_revision" value="([a-f0-9]{64})"', edit.text)[1]
        response = client.post("/profiles/svg/edit", data={
            "profile_revision": revision, "name": "SVG 生成", "description": "仅关注 SVG 编辑",
            "keywords": "SVG", "negative_keywords": "", "categories": "cs.CV", "lookback_days": "2",
            "recommendation_count": "5", "max_candidates": "300",
        }, follow_redirects=False)
        assert response.status_code == 303
        pending = client.get("/api/direction-overviews/svg").json()
        assert pending["status"] == "running"
        assert pending["generation_outdated"] is True
    finally:
        release.set()
    result = wait_overview(client, "svg")
    assert result["stale"] is True
    assert "修改前的主题" in client.get("/profiles/svg/overview").text or "主题已更新" in client.get("/profiles/svg/overview").text


def test_failed_overview_can_be_retried_from_task_page_without_losing_old_article(workspace, monkeypatch):
    client, _ = workspace
    client.post("/profiles/svg/activate")
    client.post("/api/direction-overviews/svg")
    assert wait_overview(client, "svg")["status"] == "ready"


    class Model(OverviewModel):
        fail = True

        def chat(self, *args, **kwargs):
            if self.fail and not kwargs.get("json_mode"):
                raise RuntimeError("模型暂时不可用")
            return super().chat(*args, **kwargs)

    model = Model()
    monkeypatch.setattr(ResearchClients, "llm", property(lambda self: model))
    job = client.post("/api/direction-overviews/svg").json()
    failed = wait_overview(client, "svg")
    assert failed["status"] == "failed"
    assert "SVG 编辑涉及控制点优化" in failed["markdown"]
    assert client.get(f"/api/jobs/{job['id']}").json()["recoverable"] is True
    model.fail = False
    response = client.post(f"/api/jobs/{job['id']}/retry")
    assert response.status_code == 202
    assert wait_overview(client, "svg")["status"] == "ready"


def test_unavailable_overview_storage_does_not_block_creating_or_opening_draft(workspace):
    client, app = workspace
    (app.state.output_root / "direction-overviews").write_text("blocked", encoding="utf-8")
    response = client.post("/api/profile-drafts", json={"name": "SVG", "description": "SVG editing", "keywords": ["SVG"]})
    assert response.status_code == 201
    draft = response.json()
    page = client.get(f"/profiles/drafts/{draft['id']}")
    assert page.status_code == 200
    assert "无法读取小综述" in page.text
