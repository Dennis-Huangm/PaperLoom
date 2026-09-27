from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from arxiv_ra.config import AppConfig
from arxiv_ra.feedback import FeedbackStore
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.models import Paper
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.ranker import rank_papers, finish_explanation
from arxiv_ra.reading_state import ReadingConflict
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.web import create_app


def paper(aid="2407.05600", title="Medical Image Segmentation"):
    now = datetime.now(timezone.utc)
    return Paper(aid, title, [], "A medical segmentation benchmark.", ["cs.AI"], "cs.AI",
                 now, now, f"https://arxiv.org/abs/{aid}", f"https://arxiv.org/pdf/{aid}")


def test_paper_only_does_not_teach_or_block_related_papers(tmp_path):
    store = FeedbackStore(tmp_path, "a")
    store.set(paper().to_dict(), "not_relevant")
    other = paper("2407.05601")
    other.feedback_score = -2
    store.apply([other])
    assert store.blocks(paper()) and not store.blocks(other)
    assert other.feedback_score == 0 and store.learned_terms() == {}


def test_scoring_and_blocking_can_share_one_preference_snapshot(tmp_path):
    store = FeedbackStore(tmp_path, "a")
    snapshot = store.state.snapshot()
    store.set(paper().to_dict(), "not_relevant", scope="topic", terms=["medical"])
    candidate = paper("2407.05601")
    store.apply([candidate], snapshot["library"], feedback_entries=snapshot["feedback"])
    assert candidate.feedback_score == 0
    assert store.block_reasons(candidate, snapshot["feedback"]) == []
    assert store.blocks(candidate)


def test_explicit_topics_have_word_boundaries_and_legacy_rules_remain(tmp_path):
    store = FeedbackStore(tmp_path, "a")
    store.set(paper().to_dict(), "not_relevant", scope="topic", terms=["medical segmentation"])
    blocked = paper("2407.05601")
    assert store.block_reasons(blocked)[0]["terms"] == ["medical segmentation"]
    blocked.abstract = "A biomedical segmentation benchmark"
    blocked.title = "Benchmark"
    assert not store.blocks(blocked)
    write_json(tmp_path / "feedback-old.json", {"2407.05600": {"arxiv_id": "2407.05600", "verdict": "not_relevant", "terms": ["medical image"]}})
    assert FeedbackStore(tmp_path, "old").blocks(paper("2407.05601"))


@pytest.mark.parametrize("terms", [[], ["x"], ["a" * 81], [str(n) + "term" for n in range(11)]])
def test_invalid_topic_rules_are_not_saved(tmp_path, terms):
    store = FeedbackStore(tmp_path, "a")
    with pytest.raises(ValueError):
        store.set(paper().to_dict(), "not_relevant", scope="topic", terms=terms)
    assert store.all() == {}


def test_undo_restores_collection_notes_and_rejects_stale_token(tmp_path):
    library = PaperLibraryStore(tmp_path, "a")
    library.add({"paper": paper().to_dict()}, "A")
    store = FeedbackStore(tmp_path, "a")
    store.state.update_reading(paper().arxiv_id, status="read", notes="My notes", tags=["method"],
                               read_version=1, expected_updated_at="")
    first = store.set(paper().to_dict(), "not_relevant")
    second = store.set(paper().to_dict(), "not_relevant", scope="topic", terms=["medical"])
    with pytest.raises(ReadingConflict):
        store.state.undo_feedback(paper().arxiv_id, first["updated_at"])
    assert not library.all()
    assert store.state.undo_feedback(paper().arxiv_id, second["updated_at"])
    assert library.all()[paper().arxiv_id]["paper"]["version"] == 1
    assert store.state.snapshot()["reading"][paper().arxiv_id]["notes"] == "My notes"
    third = store.set(paper().to_dict(), "not_relevant")
    newer = paper(); newer.version = 3
    library.add({"paper": newer.to_dict()}, "A")
    with pytest.raises(ReadingConflict):
        store.state.undo_feedback(paper().arxiv_id, third["updated_at"])
    assert library.all()[paper().arxiv_id]["paper"]["version"] == 3


def test_ranking_explanation_matches_actual_score_and_roundtrips():
    config = AppConfig()
    config.discovery.positive_keywords = ["medical"]
    config.discovery.negative_keywords = ["benchmark"]
    value = paper()
    rank_papers([value], config.discovery, config.ranking)
    value.llm_score = 8
    finish_explanation(value)
    explanation = value.ranking_explanation
    assert round(sum(explanation["components"].values()), 4) == value.lexical_score
    assert explanation["final_score"] == value.final_score
    assert explanation["positive_keywords"] == ["medical"]
    assert Paper.from_dict(value.to_dict()).ranking_explanation == explanation


def test_pipeline_audit_preserves_filtered_papers_and_independent_runs(tmp_path, monkeypatch):
    config = AppConfig(output_dir=str(tmp_path), profile_id="a")
    config.discovery.minimum_concept_groups = 0
    config.discovery.min_score = -100
    config.discovery.prefilter_count = 1
    config.ranking.llm_rerank = False
    pipeline = DailyPipeline(config, tmp_path, clients=SimpleNamespace(llm=SimpleNamespace(enabled=False)))
    excluded = paper()
    FeedbackStore(tmp_path, "a").set(excluded.to_dict(), "not_relevant")
    values = [excluded, paper("2407.05601"), paper("2407.05602")]
    monkeypatch.setattr(pipeline, "_discover_papers", lambda *a, **kw: values)
    directory = tmp_path / "2026-09-24"; directory.mkdir()
    candidates = pipeline._rank_candidates(directory, False)
    selected, _, _ = pipeline._select_candidates(candidates, False)
    pipeline._save_selection_audit(directory, candidates, selected)
    pipeline._save_selection_audit(directory, candidates, selected)
    files = list(directory.glob("selection-a-*.json"))
    assert len(files) == 2
    audit = read_json(files[0])
    assert len(audit["items"]) == 3
    assert sum(bool(i.get("selected")) for i in audit["items"]) == 1
    assert {r["reason"] for i in audit["items"] for r in i["reasons"]} == {"已排除此论文", "超出预筛数量"}


def test_feedback_ui_api_scope_undo_and_readonly_history(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
    app = create_app(config)
    output = tmp_path / "run"
    write_json(output / "2026-09-24/recommendations-alpha.json", [{"paper": paper().to_dict(), "verified": {}}])
    monkeypatch.setattr("arxiv_ra.web.localize_abstracts", lambda *a, **kw: None)
    with TestClient(app) as client:
        data = {"arxiv_id": paper().arxiv_id, "profile_id": "alpha", "verdict": "not_relevant"}
        assert client.post("/api/feedback", data={**data, "scope": "topic"}).status_code == 400
        feedback = client.post("/api/feedback", data=data).json()["feedback"]
        assert feedback["scope"] == "paper"
        assert "仅排除此论文" in client.get("/feedback").text
        assert "反馈与筛选记录" in client.get("/").text
        undo = {"arxiv_id": paper().arxiv_id, "profile_id": "alpha", "expected_updated_at": feedback["updated_at"]}
        assert client.post("/api/feedback/undo", data={**undo, "expected_updated_at": "stale"}).status_code == 409
        assert client.post("/api/feedback/undo", data=undo).status_code == 200
        assert client.post("/api/feedback/undo", data=undo).status_code == 409
        assert client.get("/feedback?audit=../../config.yaml").status_code == 404
        ProfileManager(tmp_path).save({"id": "beta", "name": "Beta"})
        ProfileManager(tmp_path).activate("beta")
        assert client.post("/api/feedback/undo", data=undo).status_code == 409
