from dataclasses import asdict
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from arxiv_ra.config import AppConfig, DiscoveryConfig, load_config
from arxiv_ra.discovery import DiscoveryService
from arxiv_ra.feedback import FeedbackStore
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.llm import LLMClient
from arxiv_ra.models import Paper, VerifiedMetadata
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.recent_interest import (RecentInterest, build_recent_interest,
                                     direction_description, prefilter_candidates)
from arxiv_ra.utils import read_json
from arxiv_ra.web import create_app


def paper(aid="2609.00100", title="Robot manipulation with tactile feedback", **kwargs):
    now = datetime.now(timezone.utc)
    return Paper(aid, title, [], "Tactile feedback improves robot manipulation.", ["cs.RO"],
                 "cs.RO", now, now, f"https://arxiv.org/abs/{aid}", f"https://arxiv.org/pdf/{aid}", **kwargs)


def library_entries(count=6):
    return {f"2609.0000{i}": {"arxiv_id": f"2609.0000{i}", "saved_at": f"2026-09-{i+1:02}T00:00:00Z",
                              "paper": paper(f"2609.0000{i}", f"Tactile grasping experiment {i}").to_dict()}
            for i in range(count)}


def configured():
    config = AppConfig(profile_id="robotics", profile_name="Robotics")
    config.discovery.interest_description = "Robot manipulation with tactile sensing"
    config.discovery.positive_keywords = ["robot", "manipulation"]
    config.discovery.arxiv_query_terms = ["robot manipulation"]
    config.discovery.arxiv_categories = ["cs.RO"]
    return config


def focus_model():
    return SimpleNamespace(enabled=True, chat=Mock(return_value=json.dumps(
        {"focus": "机器人操作中的触觉反馈", "query_terms": ["tactile grasping", "tactile feedback"]})))


def test_latest_five_drive_focus_and_cache_without_mutating_direction(tmp_path):
    config = configured()
    original = asdict(config)
    entries = library_entries()
    llm = focus_model()
    result = build_recent_interest(config, entries, llm, tmp_path)
    assert [s["arxiv_id"] for s in result.samples] == [f"2609.0000{i}" for i in (5, 4, 3, 2, 1)]
    prompt = llm.chat.call_args.args[1]
    assert "2609.00000" not in prompt and "Tactile feedback improves" in prompt
    assert result.status == "ready" and result.query_terms == ["tactile grasping", "tactile feedback"]
    assert asdict(config) == original
    assert build_recent_interest(config, entries, llm, tmp_path).cached
    llm.chat.assert_called_once()
    entries.pop("2609.00005")
    refreshed = build_recent_interest(config, entries, llm, tmp_path)
    assert not refreshed.cached and refreshed.samples[0]["arxiv_id"] == "2609.00004"
    assert refreshed.samples[-1]["arxiv_id"] == "2609.00000"


def test_cache_changes_with_direction_model_and_sample_metadata(tmp_path):
    config, entries, llm = configured(), library_entries(1), focus_model()
    build_recent_interest(config, entries, llm, tmp_path)
    config.profile_id = "other"
    assert not build_recent_interest(config, entries, llm, tmp_path).cached
    config.discovery.interest_description = "Tactile robotics in factories"
    assert not build_recent_interest(config, entries, llm, tmp_path).cached
    config.llm.model = "other-model"
    assert not build_recent_interest(config, entries, llm, tmp_path).cached
    entries["2609.00000"]["paper"]["abstract"] = "Updated findings"
    assert not build_recent_interest(config, entries, llm, tmp_path).cached
    assert llm.chat.call_count == 5


@pytest.mark.parametrize("response", ["broken json", '{"focus":"x","query_terms":["AI"]}',
                                      '{"focus":"x","query_terms":["cat:cs.CV OR all:*"]}',
                                      '{"focus":"x","query_terms":[42]}', '[]'])
def test_invalid_focus_falls_back_and_does_not_poison_cache(tmp_path, response):
    llm = focus_model()
    llm.chat.return_value = response
    result = build_recent_interest(configured(), library_entries(1), llm, tmp_path)
    assert result.status == "failed" and result.query_terms == []
    assert not list(tmp_path.rglob("*.json"))


def test_disabled_empty_unavailable_and_failed_model_use_base_search(tmp_path):
    config, llm = configured(), focus_model()
    assert build_recent_interest(config, {}, llm, tmp_path).status == "no_samples"
    config.discovery.recent_library_enabled = False
    assert build_recent_interest(config, library_entries(), llm, tmp_path).samples == []
    config.discovery.recent_library_enabled = True
    llm.enabled = False
    assert build_recent_interest(config, library_entries(), llm, tmp_path).status == "model_unavailable"
    llm.chat.assert_not_called()
    llm.enabled = True
    llm.chat.side_effect = TimeoutError("model offline")
    assert build_recent_interest(config, library_entries(), llm, tmp_path).status == "failed"


def test_no_relevant_focus_is_valid_and_cached(tmp_path):
    llm = focus_model()
    llm.chat.return_value = '{"focus":"", "query_terms":[]}'
    assert build_recent_interest(configured(), library_entries(), llm, tmp_path).status == "no_focus"
    assert build_recent_interest(configured(), library_entries(), llm, tmp_path).cached
    llm.chat.assert_called_once()


def test_only_recent_five_contribute_local_preference_score(tmp_path):
    entries = library_entries()
    entries["2609.00000"]["paper"]["title"] = "Forgotten astronomy"
    old = paper(title="Forgotten astronomy")
    old.abstract = "A study of astronomy."
    current = paper(title="Tactile grasping")
    FeedbackStore(tmp_path, "robotics").apply([old, current], entries)
    assert old.feedback_score == 0
    assert current.feedback_score > 0


def test_collection_version_refresh_does_not_change_recent_order(tmp_path):
    library = PaperLibraryStore(tmp_path, "robotics")
    first = library.add({"paper": paper("2609.00001").to_dict()}, "Robotics")
    library.add({"paper": paper("2609.00002").to_dict()}, "Robotics")
    library.add({"paper": paper("2609.00001", version=2).to_dict()}, "Robotics")
    assert library.all()["2609.00001"]["saved_at"] == first["saved_at"]
    llm = focus_model()
    assert build_recent_interest(configured(), library.all(), llm, tmp_path).samples[0]["arxiv_id"] == "2609.00002"
    library.remove("2609.00001")
    library.add({"paper": paper("2609.00001").to_dict()}, "Robotics")
    assert build_recent_interest(configured(), library.all(), llm, tmp_path).samples[0]["arxiv_id"] == "2609.00001"


def test_supplemental_search_preserves_base_queries_and_tracks_both_routes(tmp_path):
    config = DiscoveryConfig(provider="hybrid", arxiv_categories=["cs.RO"], arxiv_query_terms=["robot manipulation"])
    before = asdict(config)
    shared, extra, semantic = paper(), paper("2609.00101"), paper("2609.00102")
    arxiv = SimpleNamespace(search=Mock(side_effect=[[shared], [shared, extra]]), get_many=Mock())
    alpha = SimpleNamespace(discover=Mock(side_effect=[[], [semantic, extra]]))
    result = DiscoveryService(config, arxiv, alpha, tmp_path).discover(
        {"2609.99999"}, recent_interest=RecentInterest("ready", focus="触觉", query_terms=["tactile grasping"]))
    assert arxiv.search.call_args_list[0].args[3] == ["robot manipulation"]
    assert arxiv.search.call_args_list[1].args[3] == ["tactile grasping"]
    assert all(call.args[:2] == (["cs.RO"], config.lookback_days) for call in arxiv.search.call_args_list)
    assert alpha.discover.call_args_list[0].kwargs["limit"] == 15
    assert alpha.discover.call_args_list[1].kwargs["limit"] == 5
    assert all(call.kwargs["excluded_ids"] == {"2609.99999"} for call in alpha.discover.call_args_list)
    by_id = {p.arxiv_id: p for p in result.papers}
    assert len(by_id) == 3
    assert by_id[shared.arxiv_id].discovery_routes == ["base", "recent"]
    assert by_id[extra.arxiv_id].discovery_sources == ["arxiv", "alphaxiv"]
    assert by_id[extra.arxiv_id].discovery_routes == ["recent"]
    assert Paper.from_dict(by_id[extra.arxiv_id].to_dict()).discovery_routes == ["recent"]
    assert asdict(config) == before


def test_focused_provider_failures_keep_base_results(tmp_path):
    arxiv = SimpleNamespace(search=Mock(side_effect=[[paper()], TimeoutError("offline")]))
    alpha = SimpleNamespace(discover=Mock(side_effect=[[], TimeoutError("offline")]))
    result = DiscoveryService(DiscoveryConfig(provider="hybrid"), arxiv, alpha, tmp_path).discover(
        recent_interest=RecentInterest("ready", query_terms=["tactile feedback"]))
    assert len(result.papers) == 1
    assert result.sources["arxiv_recent"]["status"] == result.sources["alphaxiv_recent"]["status"] == "failed"


def test_live_focused_arxiv_metadata_replaces_older_complete_alpha_cache(tmp_path):
    old = paper(version=1)
    current = paper(title="Updated official findings", version=3)
    arxiv = SimpleNamespace(search=Mock(side_effect=[[], [current]]))
    alpha = SimpleNamespace(discover=Mock(side_effect=[[old], []]))
    service = DiscoveryService(DiscoveryConfig(provider="hybrid"), arxiv, alpha, tmp_path)
    service.resolver.remember(old)
    result = service.discover(recent_interest=RecentInterest("ready", query_terms=["tactile feedback"]))
    assert len(result.papers) == 1
    assert result.papers[0].version == 3 and result.papers[0].title == "Updated official findings"
    assert result.papers[0].discovery_routes == ["base", "recent"]
    assert service.resolver.cached(current.arxiv_id).version == 3


def test_exhausted_arxiv_is_not_retried_for_focus(tmp_path):
    arxiv = SimpleNamespace(search=Mock(side_effect=TimeoutError("offline")))
    alpha = SimpleNamespace(discover=Mock(return_value=[paper()]))
    result = DiscoveryService(DiscoveryConfig(provider="hybrid"), arxiv, alpha, tmp_path).discover(
        recent_interest=RecentInterest("ready", query_terms=["tactile feedback"]))
    assert len(result.papers) == 1
    arxiv.search.assert_called_once()
    assert result.sources["arxiv_recent"]["status"] == "skipped"


def test_prefilter_keeps_base_coverage_and_room_for_focused_candidates():
    focused = [paper(f"2609.0000{i}", discovery_routes=["recent"]) for i in range(6)]
    base = [paper(f"2609.0010{i}", discovery_routes=["base"]) for i in range(6)]
    # Highest-scoring focused results cannot crowd out the entire base pool.
    assert prefilter_candidates(focused + base, 5) == focused[:1] + base[:4]
    assert prefilter_candidates(base + focused, 5) == base[:4] + focused[:1]
    assert prefilter_candidates(base[:1] + focused, 5) == base[:1] + focused[:4]
    assert prefilter_candidates(base + focused, 1) == base[:1]


def test_rerank_uses_selected_direction_without_hardcoded_topic():
    prompts = []
    llm = SimpleNamespace(enabled=True, chat=lambda system, prompt, **kw: prompts.append(prompt) or '{"papers":[]}')
    robot = configured()
    biology = AppConfig(profile_id="biology", profile_name="Biology")
    biology.discovery.interest_description = "Protein folding"
    LLMClient.rerank(llm, [paper()], direction_description(robot))
    LLMClient.rerank(llm, [paper()], direction_description(biology))
    assert "Robot manipulation with tactile sensing" in prompts[0]
    assert "Protein folding" in prompts[1] and "Robot manipulation with tactile sensing" not in prompts[1]
    assert all("Agent/LLM/VLM" not in text and "T2I/图像生成编辑" not in text for text in prompts)


def test_legacy_paper_snapshot_does_not_gain_empty_routes():
    snapshot = paper().to_dict()
    assert "discovery_routes" not in snapshot
    assert Paper.from_dict(snapshot).to_dict() == snapshot


@pytest.mark.parametrize("enabled", [True, False])
def test_daily_run_publishes_profile_scoped_guidance_and_uses_one_snapshot(tmp_path, monkeypatch, enabled):
    config = configured()
    config.output_dir = str(tmp_path / "run")
    config.discovery.provider = "arxiv"
    config.discovery.recent_library_enabled = enabled
    config.discovery.minimum_concept_groups = 1
    config.discovery.concept_groups = [["robot"]]
    config.obsidian.enabled = config.weekly.enabled = config.version_tracking.enabled = False
    library = PaperLibraryStore(tmp_path / "run", "robotics")
    library.add({"paper": paper("2609.00001", "Tactile grasping").to_dict()}, "Robotics")
    PaperLibraryStore(tmp_path / "run", "biology").add({"paper": paper("2609.00002", "Protein folding").to_dict()}, "Biology")
    prompts = []
    llm = focus_model()

    def rerank(papers, context):
        prompts.append(context)
        for candidate in papers:
            candidate.llm_score = 8

    llm.rerank = rerank
    unrelated = paper("2609.00300", "Off-topic astronomy")
    unrelated.abstract = "An astronomy study."
    blocked = paper("2609.00400")
    FeedbackStore(tmp_path / "run", "robotics").set(blocked.to_dict(), "not_relevant")
    calls = []

    def search(*args):
        calls.append(args)
        # Changes while retrieving must not alter this run's focus or feedback.
        library.remove("2609.00001")
        return [paper()] if len(calls) == 1 else [paper("2609.00200"), unrelated, blocked]

    clients = SimpleNamespace(llm=llm, arxiv=SimpleNamespace(search=search), alphaxiv=SimpleNamespace(),
                              verifier=SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title)))
    monkeypatch.setattr("arxiv_ra.pipeline.localize_abstracts", lambda *a: None)
    result = DailyPipeline(config, tmp_path, clients=clients).run(deliver=False)
    rows = read_json(result.with_name("recommendations-robotics.json"))
    assert len(calls) == (2 if enabled else 1)
    assert {r["paper"]["arxiv_id"] for r in rows} == ({"2609.00100", "2609.00200"} if enabled else {"2609.00100"})
    interest = rows[0]["paper"]["ranking_explanation"]["recent_interest"]
    assert interest["status"] == ("ready" if enabled else "disabled")
    assert "Protein folding" not in prompts[0]
    if enabled:
        assert "Tactile grasping" in prompts[0]
        assert interest["samples"][0]["arxiv_id"] == "2609.00001"
        assert "tactile grasping" in result.read_text(encoding="utf-8")
    else:
        llm.chat.assert_not_called()
        assert all(r["paper"]["feedback_score"] == 0 for r in rows)


def test_settings_switch_persists_in_selected_direction(tmp_path):
    from test_web import _full_settings_form

    config_path = tmp_path / "config.yaml"
    config_path.write_text("output_dir: run\ndiscovery:\n  interest_description: robotics\n", encoding="utf-8")
    app = create_app(config_path)
    with TestClient(app) as client:
        assert 'name="recent_library_enabled"' in client.get("/settings").text
        form = {**_full_settings_form(), "profile_id": "robotics", "recent_library_enabled": "true"}
        assert client.post("/settings/config", data=form).status_code == 200
        assert load_config(config_path).discovery.recent_library_enabled is True
        form.pop("recent_library_enabled")
        assert client.post("/settings/config", data=form).status_code == 200
        assert load_config(config_path).discovery.recent_library_enabled is False
        assert ProfileManager(tmp_path).get("robotics")["discovery"]["recent_library_enabled"] is False
