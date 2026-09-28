import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from arxiv_ra.research_clients import ResearchClients
from arxiv_ra.web import create_app
from test_profile_plan_discovery import paper


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: original\n  arxiv_categories: [cs.CV]\n", encoding="utf-8")
    model = SimpleNamespace(enabled=True, chat=lambda *a, **kw: json.dumps({
        "interest_description": "Robot world models", "arxiv_categories": ["cs.RO"],
        "arxiv_query_terms": ["world model"], "positive_keywords": [],
        "negative_keywords": [], "concept_groups": [["robot"], ["video"]],
        "minimum_concept_groups": 2,
    }))
    monkeypatch.setattr(ResearchClients, "llm", property(lambda self: model))
    monkeypatch.setattr(ResearchClients, "alphaxiv", property(lambda self: SimpleNamespace(enabled=False)))
    app = create_app(config)
    with TestClient(app) as client:
        yield client, model, tmp_path


def test_generate_draft_preserves_user_intent_without_activating(workspace):
    client, _, root = workspace
    active = (root / "profiles" / "active.txt").read_text()
    response = client.post("/api/profile-drafts", json={
        "name": "Robots", "keywords": ["world model", "robot"],
        "negative_keywords": ["survey"], "required": ["用于机器人控制"],
    })
    assert response.status_code == 201
    draft = response.json()
    assert draft["status"] == "ready"
    assert draft["revision"] == 1
    assert {(c["kind"], c["text"]) for c in draft["plan"]["conditions"] if c["origin"] == "user"} == {
        ("topic", "world model"), ("topic", "robot"), ("demote", "survey"), ("required", "用于机器人控制")}
    assert draft["discovery"]["minimum_concept_groups"] == 0
    assert (root / "profiles" / "active.txt").read_text() == active
    assert not (root / "profiles" / f'{draft["id"]}.yaml').exists()
    page = client.get(f'/profiles/drafts/{draft["id"]}')
    assert page.status_code == 200
    assert "必要条件" in page.text and "survey" in page.text


def test_edit_activate_and_reject_stale_revision(workspace):
    client, _, root = workspace
    draft = client.post("/api/profile-drafts", json={"name": "Editable", "keywords": ["world model"]}).json()
    conditions = draft["plan"]["conditions"]
    conditions[0]["aliases"] = ["world model", "world models"]
    saved = client.post(f'/api/profile-drafts/{draft["id"]}/edit', json={
        "revision": 1, "conditions": conditions}).json()
    assert saved["revision"] == 2
    stale = client.post(f'/api/profile-drafts/{draft["id"]}/activate', json={"revision": 1})
    assert stale.status_code == 409
    response = client.post(f'/api/profile-drafts/{draft["id"]}/activate', json={"revision": 2})
    assert response.status_code == 200
    assert (root / "profiles" / "active.txt").read_text().strip() == draft["id"]
    # Same revision is safe to retry and does not duplicate the formal profile.
    assert client.post(f'/api/profile-drafts/{draft["id"]}/activate', json={"revision": 2}).status_code == 200


def test_regeneration_keeps_edits_and_deleted_suggestions(workspace):
    client, model, _ = workspace
    model.chat = lambda *a, **kw: json.dumps({"positive_keywords": ["video prediction"], "conditions": [
        {"kind": "required", "text": "simulation", "basis": "input", "evidence": "可能的场景"}]})
    draft = client.post("/api/profile-drafts", json={"name": "Keep edits", "keywords": ["world model"]}).json()
    assert any(c["kind"] == "required" and not c["confirmed"] for c in draft["plan"]["conditions"])
    keep = [c for c in draft["plan"]["conditions"] if c["origin"] == "user"]
    keep[0]["aliases"].append("world models")
    saved = client.post(f'/api/profile-drafts/{draft["id"]}/edit', json={"revision": 1, "conditions": keep}).json()
    regenerated = client.post(f'/api/profile-drafts/{draft["id"]}/regenerate', json={"revision": saved["revision"]})
    assert regenerated.status_code == 200
    assert regenerated.json()["plan"]["conditions"] == saved["plan"]["conditions"]


@pytest.mark.parametrize("reply", ['not json', '{"positive_keywords":[{}]}', '{"arxiv_categories":["cs.FAKE"]}',
    '{"conditions":[{"kind":"topic","text":"robot","basis":"9999.12345","evidence":"unknown"}]}'])
def test_invalid_generation_is_visible_and_cannot_activate(workspace, reply):
    client, model, _ = workspace
    model.chat = lambda *a, **kw: reply
    response = client.post("/api/profile-drafts", json={"name": "Invalid", "keywords": ["robot"]})
    assert response.status_code == 201
    draft = response.json()
    assert draft["status"] == "failed"
    assert draft["diagnostics"]
    assert client.post(f'/api/profile-drafts/{draft["id"]}/activate', json={"revision": 1}).status_code == 400


def test_preview_shares_constraints_and_has_no_recommendation_side_effects(workspace, monkeypatch):
    client, model, root = workspace
    draft = client.post("/api/profile-drafts", json={"name": "Preview", "keywords": ["world model"],
        "excluded": ["medical imaging"]}).json()
    cid = next(c["id"] for c in draft["plan"]["conditions"] if c["kind"] == "exclude")
    samples = [paper(abstract="We study robot control, unlike medical imaging."),
               paper("2609.00002", "Medical imaging", "We study medical imaging.")]
    monkeypatch.setattr(ResearchClients, "arxiv", property(lambda self: SimpleNamespace(
        search=lambda *a, **kw: samples, get_many=lambda ids: [])))
    model.chat = lambda *a, **kw: json.dumps({"papers": [
        {"id": samples[0].arxiv_id, "score": 9, "conditions": [{"id": cid, "verdict": "not_satisfied", "quote": "We study robot control", "reason": "研究对象是机器人"}]},
        {"id": samples[1].arxiv_id, "score": 8, "conditions": [{"id": cid, "verdict": "satisfied", "quote": "We study medical imaging", "reason": "研究对象是医学影像"}]}]})
    active = (root / "profiles" / "active.txt").read_text()
    response = client.post(f'/api/profile-drafts/{draft["id"]}/preview', json={"revision": 1})
    assert response.status_code == 202
    for _ in range(100):
        preview = client.get(f'/api/profile-drafts/{draft["id"]}').json().get("preview")
        if preview:
            break
        time.sleep(.02)
    assert preview["status"] == "ok"
    assert [p["arxiv_id"] for p in preview["selected"]] == ["2609.00001"]
    assert preview["rejected"][0]["paper"]["arxiv_id"] == "2609.00002"
    assert (root / "profiles" / "active.txt").read_text() == active
    assert not list((root / "run").rglob("recommendations*.json"))
    assert not list((root / "run").glob("state-*.json"))
    assert not list((root / "run").glob("reading-state-*.json"))
    # The same external responses must give the same hard-constraint outcome in a real daily run.
    from arxiv_ra.config import load_config
    from arxiv_ra.pipeline import DailyPipeline
    from arxiv_ra.models import VerifiedMetadata
    monkeypatch.setattr(ResearchClients, "verifier", property(lambda self: SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title))))
    monkeypatch.setattr("arxiv_ra.abstracts.LLMClient", lambda cfg: SimpleNamespace(enabled=False))
    client.post(f'/api/profile-drafts/{draft["id"]}/activate', json={"revision": 1})
    with DailyPipeline(load_config(root / "config.yaml"), root) as pipeline:
        pipeline.run(deliver=False)
    recommendations = next((root / "run").glob(f'*/recommendations-{draft["id"]}.json'))
    assert [item["paper"]["arxiv_id"] for item in json.loads(recommendations.read_text(encoding="utf-8"))] == ["2609.00001"]


def test_copy_legacy_and_save_settings_preserve_versioned_intent(workspace):
    from arxiv_ra.config import load_config
    from test_web import _full_settings_form
    client, _, root = workspace
    original = (root / "profiles" / "original.yaml").read_bytes()
    copied = client.post("/profiles/original/copy", follow_redirects=False)
    assert copied.status_code == 303
    draft_id = copied.headers["location"].rsplit("/", 1)[-1]
    assert (root / "profiles" / "original.yaml").read_bytes() == original
    # A legacy category-only profile remains a draft until a topic is supplied.
    draft = client.get(f"/api/profile-drafts/{draft_id}").json()
    assert draft["status"] == "needs_input"
    created = client.post("/api/profile-drafts", json={"name": "New", "keywords": ["world model"], "required": ["robot control"]}).json()
    client.post(f'/api/profile-drafts/{created["id"]}/activate', json={"revision": 1})
    before = load_config(root / "config.yaml").discovery.search_plan
    response = client.post("/settings/config", data=_full_settings_form(), follow_redirects=False)
    assert response.status_code == 303
    config = load_config(root / "config.yaml")
    assert config.discovery.search_plan == before
    assert config.discovery.minimum_concept_groups == 0
    # Retrying an already completed activation cannot undo later settings changes.
    recommendation_count = config.discovery.recommendation_count
    assert client.post(f'/api/profile-drafts/{created["id"]}/activate', json={"revision": 1}).status_code == 200
    assert load_config(root / "config.yaml").discovery.recommendation_count == recommendation_count


def test_reference_only_without_model_stays_incomplete(workspace, monkeypatch):
    client, model, _ = workspace
    model.enabled = False
    monkeypatch.setattr(ResearchClients, "arxiv", property(lambda self: SimpleNamespace(get=lambda aid: paper(aid))))
    draft = client.post("/api/profile-drafts", json={"name": "References", "reference_ids": ["2401.00001"]}).json()
    assert draft["status"] == "needs_input"
    assert draft["references"][0]["title"]
    assert client.post(f'/api/profile-drafts/{draft["id"]}/activate', json={"revision": 1}).status_code == 400


def test_unknown_hard_condition_and_controlled_word_variants(workspace, monkeypatch):
    client, model, _ = workspace
    draft = client.post("/api/profile-drafts", json={"name": "Unknown", "keywords": ["world-model"],
        "required": ["real robot experiments"]}).json()
    model.enabled = False
    monkeypatch.setattr(ResearchClients, "arxiv", property(lambda self: SimpleNamespace(search=lambda *a, **kw: [paper()])))
    client.post(f'/api/profile-drafts/{draft["id"]}/preview', json={"revision": 1})
    for _ in range(100):
        preview = client.get(f'/api/profile-drafts/{draft["id"]}').json().get("preview")
        if preview:
            break
        time.sleep(.02)
    assert preview["status"] == "uncertain"
    assert not preview["selected"]
    excluded = preview["rejected"][0]
    assert excluded["reasons"][0]["condition"]["verdict"] == "unknown"
    assert "world-model" in excluded["paper"]["ranking_explanation"]["positive_keywords"]


def test_reference_check_ignores_date_and_old_results_are_marked_stale(workspace, monkeypatch):
    client, model, _ = workspace
    monkeypatch.setattr(ResearchClients, "arxiv", property(lambda self: SimpleNamespace(get=lambda aid: paper(aid))))
    draft = client.post("/api/profile-drafts", json={"name": "Classic", "keywords": ["world model"],
        "reference_ids": ["1401.00001"], "required": ["cs.RO"]}).json()
    model.enabled = False
    assert client.post(f'/api/profile-drafts/{draft["id"]}/references', json={"revision": 1}).status_code == 202
    for _ in range(100):
        result = client.get(f'/api/profile-drafts/{draft["id"]}').json().get("reference_check")
        if result:
            break
        time.sleep(.02)
    assert result["counts"]["retrieved"] == 1
    assert result["status"] == "uncertain"
    assert not result["selected"]
    assert result["reference_verdicts"][0]["verdict"] == "unknown"
    checks = result["rejected"][0]["paper"]["ranking_explanation"]["conditions"]
    assert checks[0]["field"] == "categories"
    edited = client.post(f'/api/profile-drafts/{draft["id"]}/edit', json={"revision": 1, "description": "Updated world models"})
    assert edited.status_code == 200
    assert "结果已过期" in client.get(f'/profiles/drafts/{draft["id"]}').text
    assert client.post(f'/api/profile-drafts/{draft["id"]}/preview', json={"revision": 1}).status_code == 409


def test_new_direction_uses_application_ranking_defaults(workspace):
    from test_web import _full_settings_form
    from arxiv_ra.config import RankingConfig
    from dataclasses import asdict
    client, _, _ = workspace
    settings = _full_settings_form()
    settings.update(llm_min_score="9.9", keyword_weight="99")
    assert client.post("/settings/config", data=settings, follow_redirects=False).status_code == 303
    draft = client.post("/api/profile-drafts", json={"name": "Independent", "keywords": ["robot"]}).json()
    assert draft["ranking"] == asdict(RankingConfig())


def test_preference_edit_and_regeneration_keep_valid_query_branches(workspace):
    client, model, _ = workspace
    reply = {"positive_keywords": ["world model"], "branches": [
        {"id": "core", "groups": [["world model"]]},
        {"id": "context", "groups": [["world model"], ["robot control"]]}]}
    model.chat = lambda *a, **kw: json.dumps(reply)
    draft = client.post("/api/profile-drafts", json={"name": "Branches", "keywords": ["world model"],
        "negative_keywords": ["survey"]}).json()
    conditions = draft["plan"]["conditions"]
    next(c for c in conditions if c["kind"] == "demote")["text"] = "review articles"
    edited = client.post(f'/api/profile-drafts/{draft["id"]}/edit', json={"revision": 1, "conditions": conditions}).json()
    assert len(edited["plan"]["branches"]) == 2
    regenerated = client.post(f'/api/profile-drafts/{draft["id"]}/regenerate', json={"revision": 2}).json()
    assert len(regenerated["plan"]["branches"]) == 2


def test_failed_reference_only_does_not_invent_a_theme(workspace, monkeypatch):
    client, model, _ = workspace
    def unavailable(aid):
        raise RuntimeError("offline")
    monkeypatch.setattr(ResearchClients, "arxiv", property(lambda self: SimpleNamespace(get=unavailable)))
    model.chat = lambda *a, **kw: json.dumps({"positive_keywords": ["invented theme"]})
    draft = client.post("/api/profile-drafts", json={"name": "References only", "reference_ids": ["2401.00001"]}).json()
    assert draft["status"] == "needs_input"
    assert draft["diagnostics"]
    assert not draft["plan"]["conditions"]


def test_create_request_can_be_retried_without_duplicate_drafts(workspace):
    client, _, root = workspace
    data = {"name": "Retry", "keywords": ["robot control"], "request_id": "test-retry-1"}
    first = client.post("/api/profile-drafts", json=data).json()
    second = client.post("/api/profile-drafts", json=data).json()
    assert first["id"] == second["id"]
    assert len(list((root / "profiles" / "drafts").glob("*.yaml"))) == 1


def test_advanced_query_edit_is_validated_and_survives_regeneration(workspace):
    client, _, _ = workspace
    draft = client.post("/api/profile-drafts", json={"name": "Advanced", "keywords": ["world model"]}).json()
    url = f'/api/profile-drafts/{draft["id"]}'
    bad = client.post(url + "/edit", json={"revision": 1, "branches": [{"id": "core", "groups": [["AI"]]}]})
    assert bad.status_code == 400
    branches = [{"id": "core", "groups": [["world model"]]},
                {"id": "context", "groups": [["world model"], ["robot control"]]}]
    edited = client.post(url + "/edit", json={"revision": 1, "branches": branches, "max_candidates": 20, "lookback_days": 14}).json()
    assert edited["plan"]["branches_user_edited"]
    regenerated = client.post(url + "/regenerate", json={"revision": 2}).json()
    assert regenerated["plan"]["branches"] == edited["plan"]["branches"]
    assert regenerated["discovery"]["max_candidates"] == 20
    assert regenerated["discovery"]["lookback_days"] == 14
