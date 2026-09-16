from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arxiv_ra.config import AppConfig, DiscoveryConfig
from arxiv_ra.discovery import DiscoveryService, PaperResolver
from arxiv_ra.models import Author, Paper
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.metadata import MetadataVerifier
from arxiv_ra.utils import read_json, write_json


def paper(aid="2609.00001", partial=False, **kw):
    now = datetime.now(timezone.utc)
    values = dict(arxiv_id=aid, title="agent study", authors=[Author("Researcher")],
                  abstract="agent research", categories=["cs.AI"], primary_category="cs.AI",
                  published=now, updated=now, abs_url=f"https://arxiv.org/abs/{aid}",
                  pdf_url=f"https://arxiv.org/pdf/{aid}", version=3)
    if partial:
        values.update(authors=[], categories=[], primary_category="", updated=None, version=None,
                      metadata_source="alphaxiv", metadata_status="partial", abstract_kind="preview",
                      discovery_sources=["alphaxiv"])
    values.update(kw)
    return Paper(**values)


def service(tmp_path, arxiv_results=(), alpha_results=(), mode="hybrid"):
    arxiv = SimpleNamespace(search=Mock(return_value=list(arxiv_results)), get_many=Mock(return_value=[]))
    alpha = SimpleNamespace(enabled=True, discover=Mock(return_value=list(alpha_results)))
    return DiscoveryService(DiscoveryConfig(provider=mode), arxiv, alpha, tmp_path)


def test_hybrid_merges_sources_and_hydrates_only_new_ids(tmp_path):
    official = paper(abstract="Full canonical abstract")
    extra = paper("2609.00002", partial=True)
    s = service(tmp_path, [official], [paper(partial=True), extra])
    hydrated = paper(extra.arxiv_id, abstract="Full extra abstract", version=4)
    s.arxiv.get_many.return_value = [hydrated]
    result = s.discover()
    s.alphaxiv.discover.assert_called_once()
    s.arxiv.get_many.assert_called_once_with([extra.arxiv_id])
    assert result.papers[0].abstract == official.abstract
    assert result.papers[0].discovery_sources == ["arxiv", "alphaxiv"]
    assert result.papers[1].version == 4 and result.papers[1].metadata_source == "arxiv"
    assert result.papers[1].discovery_sources == ["alphaxiv"]
    assert result.hydration["completed"] == 1


def test_hybrid_calls_alpha_when_arxiv_is_empty(tmp_path):
    s = service(tmp_path, [], [paper(partial=True)])
    s.arxiv.get_many.return_value = [paper()]
    assert len(s.discover().papers) == 1
    s.alphaxiv.discover.assert_called_once()


@pytest.mark.parametrize("mode, enabled", [("arxiv", True), ("auto", True), ("hybrid", False)])
def test_modes_preserve_legacy_behavior_and_disable_switch(tmp_path, mode, enabled):
    s = service(tmp_path, [paper()], mode=mode)
    s.config.alphaxiv_fallback_enabled = enabled
    assert len(s.discover().papers) == 1
    s.alphaxiv.discover.assert_not_called()


def test_alpha_failure_does_not_discard_arxiv_results(tmp_path):
    s = service(tmp_path, [paper()])
    s.alphaxiv.discover.side_effect = TimeoutError("semantic search timed out")
    result = s.discover()
    assert len(result.papers) == 1
    assert result.sources["alphaxiv"]["status"] == "failed"


def test_arxiv_failure_uses_complete_cache_and_does_not_retry_per_paper(tmp_path):
    first = service(tmp_path, [paper()])
    first.discover()
    s = service(tmp_path, [], [paper(partial=True), paper("2609.00002", partial=True)])
    s.arxiv.search.side_effect = RuntimeError("429")
    result = s.discover()
    assert [p.metadata_status for p in result.papers] == ["complete", "partial"]
    assert result.hydration["status"] == "deferred"
    s.arxiv.get_many.assert_not_called()


def test_both_sources_fail_with_diagnostic_result(tmp_path):
    s = service(tmp_path)
    s.arxiv.search.side_effect = RuntimeError("429")
    s.alphaxiv.discover.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError) as error:
        s.discover()
    assert error.value.discovery_result.sources["arxiv"]["status"] == "failed"


def test_unknown_dates_can_be_hydrated_but_never_pass_window_unverified(tmp_path):
    s = service(tmp_path, [], [paper(partial=True, published=None)])
    assert s.discover().papers == []
    s.arxiv.get_many.return_value = [paper()]
    assert len(s.discover().papers) == 1


def test_hydrated_dates_are_checked_again(tmp_path):
    s = service(tmp_path, [], [paper(partial=True)])
    s.arxiv.get_many.return_value = [paper(published=datetime.now(timezone.utc) - timedelta(days=30))]
    assert s.discover().rejected_dates == 1


def test_partial_cache_is_repaired_once_then_reused(tmp_path):
    arxiv = SimpleNamespace(get=Mock(return_value=paper()))
    resolver = PaperResolver(DiscoveryConfig(), arxiv, SimpleNamespace(enabled=False), tmp_path)
    snapshot = paper(partial=True)
    restored = resolver.resolve(snapshot.arxiv_id, snapshot)
    assert restored.metadata_status == "complete" and restored.discovery_sources == ["alphaxiv"]
    assert resolver.resolve(snapshot.arxiv_id, snapshot).version == 3
    arxiv.get.assert_called_once()


def test_partial_snapshot_is_usable_when_api_still_offline(tmp_path):
    snapshot = paper(partial=True, published=None)
    resolver = PaperResolver(DiscoveryConfig(), SimpleNamespace(get=Mock(side_effect=RuntimeError("offline"))),
                             SimpleNamespace(enabled=False), tmp_path)
    assert resolver.resolve(snapshot.arxiv_id, snapshot) is snapshot


def test_legacy_snapshot_does_not_invent_dates_or_claim_complete_metadata():
    snapshot = Paper.from_dict({"arxiv_id": "1706.03762", "title": "Old fallback",
                               "recommendation_reason": "由 alphaXiv 召回", "version": 1})
    assert snapshot.metadata_status == "partial" and snapshot.metadata_source == "alphaxiv"
    assert snapshot.published is None and snapshot.version is None


def test_failed_verification_keeps_actual_metadata_source():
    verifier = MetadataVerifier(AppConfig().metadata)
    verifier.client.close()
    verifier._openalex = lambda _: None
    verifier._semantic_scholar = lambda _: None
    assert verifier.verify(paper(partial=True)).sources == ["alphaXiv（待补全）"]


def pipeline(tmp_path, profile="test"):
    p = DailyPipeline(AppConfig(profile_id=profile, output_dir=str(tmp_path)), tmp_path)
    p.config.ranking.llm_rerank = False
    p.config.discovery.min_score = -1
    p.output_root = tmp_path
    p.clients.llm = SimpleNamespace(enabled=False)
    return p


def test_history_filtered_before_prefilter_and_force_reincludes_it(tmp_path):
    p = pipeline(tmp_path)
    p.config.discovery.prefilter_count = 2
    first = paper()
    papers = [replace(first, arxiv_id=f"2609.0000{i}") for i in (1, 2, 3)]
    p.clients.arxiv = SimpleNamespace(search=Mock(return_value=papers))
    p.clients.alphaxiv = SimpleNamespace()
    write_json(p._state_path(), {"processed": [x.arxiv_id for x in papers[:2]]})
    ranked = p._rank_candidates(tmp_path, False)
    assert [x.arxiv_id for x in ranked] == [papers[2].arxiv_id]
    assert p._select_candidates(ranked, False)[0]
    forced = p._rank_candidates(tmp_path, False, force=True)
    assert len(forced) == 2


def test_force_does_not_send_history_exclusions_to_alpha(tmp_path):
    p = pipeline(tmp_path)
    p.config.discovery.provider = "hybrid"
    p.clients.arxiv = SimpleNamespace(search=Mock(return_value=[]), get_many=Mock(return_value=[paper()]))
    p.clients.alphaxiv = SimpleNamespace(discover=Mock(return_value=[paper(partial=True)]))
    write_json(p._state_path(), {"processed": ["2609.00001"]})
    assert p._rank_candidates(tmp_path, False, force=True)
    assert p.clients.alphaxiv.discover.call_args.kwargs["excluded_ids"] == set()


def test_profile_runs_ignore_shared_marker_and_keep_separate_artifacts(tmp_path):
    a, b = pipeline(tmp_path, "a"), pipeline(tmp_path, "b")
    for p in (a, b):
        p.config.discovery.minimum_concept_groups = 2
        p.config.discovery.concept_groups = [["agent"], ["image"]]
        p.clients.alphaxiv = SimpleNamespace(discover=Mock(return_value=[paper(partial=True)]))
    a.clients.arxiv = SimpleNamespace(search=Mock(side_effect=RuntimeError("offline")))
    b.clients.arxiv = SimpleNamespace(search=Mock(return_value=[paper()]))
    (tmp_path / "discovery-source.txt").write_text("obsolete marker")
    assert a._rank_candidates(tmp_path, False)
    assert b._rank_candidates(tmp_path, False) == []
    assert (tmp_path / "candidates-a.json").exists() and (tmp_path / "candidates-b.json").exists()
    assert len(list(tmp_path.glob("discovery-*.json"))) == 2


def test_no_new_papers_keeps_previous_digest(tmp_path):
    p = pipeline(tmp_path)
    current = paper()
    p.clients.arxiv = SimpleNamespace(search=Mock(return_value=[current]))
    p.clients.alphaxiv = SimpleNamespace()
    from zoneinfo import ZoneInfo
    day = datetime.now(ZoneInfo(p.config.timezone)).date().isoformat()
    target = tmp_path / day / "recommendations-test.json"
    old = [{"profile_id": "test", "paper": current.to_dict()}]
    write_json(target, old)
    write_json(p._state_path(), {"processed": [current.arxiv_id]})
    result = p.run(deliver=False)
    assert result.name == "no-new-test.html" and read_json(target) == old


def test_stale_cache_is_refreshed_during_healthy_discovery(tmp_path):
    import os
    import time
    s = service(tmp_path, [], [paper(partial=True)])
    s.resolver.remember(paper(version=2))
    cache = s.resolver._path("2609.00001")
    os.utime(cache, (time.time() - 90000, time.time() - 90000))
    s.arxiv.get_many.return_value = [paper(version=4)]
    assert s.discover().papers[0].version == 4
    s.arxiv.get_many.assert_called_once()


def test_both_sources_failure_preserves_published_result(tmp_path):
    p = pipeline(tmp_path)
    p.config.discovery.provider = "hybrid"
    p.clients.arxiv = SimpleNamespace(search=Mock(side_effect=RuntimeError("offline")))
    p.clients.alphaxiv = SimpleNamespace(discover=Mock(side_effect=RuntimeError("offline")))
    from zoneinfo import ZoneInfo
    day = datetime.now(ZoneInfo(p.config.timezone)).date().isoformat()
    target = tmp_path / day / "recommendations-test.json"
    write_json(target, [{"profile_id": "test", "paper": paper().to_dict()}])
    previous = target.read_bytes()
    with pytest.raises(RuntimeError):
        p.run(deliver=False)
    assert target.read_bytes() == previous
    manifest = next((tmp_path / day).glob("discovery-test-*.json"))
    assert read_json(manifest)["sources"]["alphaxiv"]["status"] == "failed"


def test_partial_paper_report_and_render_support_unknown_dates(tmp_path):
    from arxiv_ra.models import VerifiedMetadata
    from arxiv_ra.report import ReportGenerator
    from arxiv_ra.render import render_recommendations
    p = paper(partial=True, published=None)
    metadata = VerifiedMetadata(sources=[p.metadata_label])
    generator = ReportGenerator(SimpleNamespace(enabled=False), AppConfig().llm)
    markdown = generator._extractive_report(p, metadata, None)
    assert "未核实" in markdown and "摘要预览（待补全）" in markdown
    html = render_recommendations([p], {p.arxiv_id: metadata}, tmp_path / "index.html", "2026-09-16")
    assert "摘要预览（待补全）" in html
