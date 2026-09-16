from dataclasses import replace
from datetime import datetime, timezone
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arxiv_ra.config import DiscoveryConfig
from arxiv_ra.discovery import DiscoveryService
from arxiv_ra.models import Author, Paper
from arxiv_ra.paper_data import PaperResolver, local_paper_item
from arxiv_ra.utils import write_json
from arxiv_ra.config import AppConfig
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.research_clients import ResearchClients
from arxiv_ra.models import VerifiedMetadata


def paper(version=2):
    now = datetime.now(timezone.utc)
    return Paper("2609.00001", f"Revision {version}", [Author("A")], "full abstract",
                 ["cs.AI"], "cs.AI", now, now, "https://arxiv.org/abs/2609.00001",
                 "https://arxiv.org/pdf/2609.00001", version=version)


def test_historical_snapshot_is_pinned_but_direct_id_refreshes(tmp_path):
    arxiv = SimpleNamespace(get=Mock(return_value=paper(5)))
    resolver = PaperResolver(None, arxiv, None, tmp_path)
    resolver.remember(paper(4))
    historical = resolver.resolve("2609.00001", paper(2), intent="snapshot")
    assert historical.version == 2
    assert historical.pdf_url.endswith("v2") and historical.abs_url.endswith("v2")
    arxiv.get.assert_not_called()
    latest = resolver.resolve("2609.00001", paper(2), intent="latest")
    assert latest.version == 5 and latest.pdf_url.endswith("v5")
    arxiv.get.assert_called_once_with("2609.00001")


def test_exact_version_cannot_fall_back_to_wrong_cache_snapshot_or_provider(tmp_path):
    arxiv = SimpleNamespace(get=Mock(return_value=paper(4)))
    alpha = SimpleNamespace(enabled=True, lookup=Mock(return_value=paper(4)))
    resolver = PaperResolver(DiscoveryConfig(provider="hybrid"), arxiv, alpha, tmp_path)
    resolver.remember(paper(4))
    with pytest.raises(RuntimeError, match="版本"):
        resolver.resolve("2609.00001v2", paper(4), intent="latest")
    resolver.remember(paper(2), "2609.00001v2")
    assert resolver.resolve("2609.00001v2", intent="latest").version == 2
    assert resolver.cached("2609.00001").version == 4


def test_stale_complete_cache_survives_failed_or_empty_hydration(tmp_path):
    for outcome in (TimeoutError("offline"), []):
        arxiv = SimpleNamespace(search=Mock(return_value=[]), get_many=Mock())
        if isinstance(outcome, Exception):
            arxiv.get_many.side_effect = outcome
        else:
            arxiv.get_many.return_value = outcome
        preview = replace(paper(), metadata_status="partial", metadata_source="alphaxiv", version=None)
        alpha = SimpleNamespace(enabled=True, discover=Mock(return_value=[preview]))
        discovery = DiscoveryService(DiscoveryConfig(provider="hybrid"), arxiv, alpha, tmp_path)
        discovery.resolver.remember(paper())
        path = discovery.resolver._path("2609.00001")
        os.utime(path, (time.time() - 90000,) * 2)
        result = discovery.discover()
        assert result.papers[0].metadata_status == "complete"
        assert result.papers[0].discovery_sources == ["alphaxiv"]
        assert result.papers[0].resolution_note
        assert result.hydration["cached_fallback"] == 1
        arxiv.get_many.assert_called_once()


def test_offline_latest_chooses_best_complete_version_and_marks_it(tmp_path):
    resolver = PaperResolver(None, SimpleNamespace(get=Mock(side_effect=TimeoutError())), None, tmp_path)
    resolver.remember(paper(4))
    selected = resolver.resolve("2609.00001", paper(2), intent="latest")
    assert selected.version == 4 and "尚未确认" in selected.resolution_note


def test_history_lookup_keeps_exact_day_and_profile(tmp_path):
    for day, version in (("2026-09-14", 2), ("2026-09-15", 4)):
        write_json(tmp_path / day / "recommendations-a.json", [{"profile_id": "a", "paper": paper(version).to_dict()}])
    selected = local_paper_item(tmp_path, "a", "2609.00001", source_date="2026-09-14", origin="recommendation")
    assert selected["paper"]["version"] == 2
    assert local_paper_item(tmp_path, "b", "2609.00001", source_date="2026-09-14", origin="recommendation") is None
    assert local_paper_item(tmp_path, "a", "2609.00001v4", source_date="2026-09-14", origin="recommendation") is None


def test_latest_local_fallback_compares_reports_with_recommendations(tmp_path):
    write_json(tmp_path / "2026-09-15" / "recommendations-a.json",
               [{"profile_id": "a", "paper": paper(2).to_dict()}])
    for revision in (9, 10):
        write_json(tmp_path / "2026-09-14" / "reports" / f"2609.00001-v{revision}-a-test" / "metadata.json",
                   {"profile_id": "a", "paper": paper(revision).to_dict()})
    item = local_paper_item(tmp_path, "a", "2609.00001", latest=True)
    assert item["paper"]["version"] == 10
    assert local_paper_item(tmp_path, "a", "2609.00001")["paper"]["version"] == 2


def test_report_pipeline_uses_same_revision_for_metadata_pdf_and_figures(tmp_path):
    config = AppConfig(output_dir=str(tmp_path), profile_id="a")
    arxiv = SimpleNamespace(get=Mock(side_effect=AssertionError("snapshot already complete")), download_pdf=Mock())
    figures = SimpleNamespace(fetch=Mock(return_value=[]))
    parser = SimpleNamespace(parse=Mock(return_value=SimpleNamespace(figures=[], parser="test")))
    reporter = SimpleNamespace(generate=Mock(return_value="# Test\n\nReport"))
    clients = ResearchClients(config, arxiv=arxiv, alphaxiv=SimpleNamespace(enabled=False),
                              arxiv_html=figures, parser=parser, reporter=reporter,
                              verifier=SimpleNamespace(verify=Mock(return_value=VerifiedMetadata())))
    with DailyPipeline(config, tmp_path, clients=clients) as pipeline:
        first = pipeline.report_arxiv_id("2609.00001", snapshot=paper(2))
        second = pipeline.report_arxiv_id("2609.00001", snapshot=paper(4))
    assert first != second and first.exists() and second.exists()
    assert [call.args[0].pdf_url for call in arxiv.download_pdf.call_args_list] == [
        "https://arxiv.org/pdf/2609.00001v2", "https://arxiv.org/pdf/2609.00001v4"]
    assert [call.args[0] for call in figures.fetch.call_args_list] == ["2609.00001v2", "2609.00001v4"]
    arxiv.get.assert_not_called()
