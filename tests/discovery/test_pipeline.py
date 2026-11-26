from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import json

import httpx
import pytest

from paperloom.config import AppConfig
from paperloom.papers.models import Author, FigureCandidate, Paper, ReportArtifact, VerifiedMetadata
from paperloom.discovery.pipeline import DailyPipeline
from paperloom.utils import read_json, write_json


def test_report_artifact_keeps_all_experiment_images_beyond_eight(tmp_path: Path) -> None:
    import fitz

    pdf = tmp_path / "input.pdf"
    with fitz.open() as doc:
        for number in range(1, 12):
            page = doc.new_page()
            if number == 2:
                page.insert_text((70, 65), "3 Experiments")
            page.draw_rect(fitz.Rect(70, 100, 400, 220))
            page.insert_text((70, 245), f"Figure {number}: Result comparison.")
        doc.save(pdf)
    paper = Paper(
        arxiv_id="2501.00001", title="Figure coverage", authors=[Author("Researcher")],
        abstract="Experiment results", categories=["cs.AI"], primary_category="cs.AI",
        published=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated=datetime(2025, 1, 1, tzinfo=timezone.utc),
        abs_url="https://arxiv.org/abs/2501.00001v1", pdf_url="", version=1,
    )
    config = AppConfig(output_dir=str(tmp_path / "run"))
    config.pdf.use_docling_if_available = False
    config.llm.api_key_env = "PAPERLOOM_TEST_NO_KEY"
    config.llm.base_url_env = "PAPERLOOM_TEST_NO_URL"
    pipeline = DailyPipeline(config, tmp_path)
    pipeline.clients.verifier = SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title))
    def html_fetch(_arxiv_id, output_dir):
        path = output_dir / "html-figure-02.png"
        with fitz.open(pdf) as doc:
            doc[1].get_pixmap().save(path)
        return [FigureCandidate(path=path, page=0, caption="Figure 2: Result comparison.",
                                kind="html_figure", role="method", section="Related work")]

    pipeline.clients.arxiv_html = SimpleNamespace(fetch=html_fetch)
    result = pipeline.report_arxiv_id("2501.00001v1", snapshot=paper, local_pdf=pdf)
    report = result.read_text(encoding="utf-8")
    assert report.count("![论文实验图") == 10
    assert report.count("![论文方法图") == 1
    assert "Figure 11" in report
    inventory = read_json(result.parent / "experiment-figures.json", [])
    assert len(inventory) == 10
    assert all((result.parent / item["filename"]).is_file() for item in inventory)
    assert inventory[0]["source"] == "html_figure"
    assert inventory[0]["page"] == 2
    assert inventory[0]["section"] == "3 Experiments"


def test_report_generation_does_not_overwrite_daily_digest(tmp_path: Path) -> None:
    paper = Paper(
        arxiv_id="2501.00001",
        title="Test Paper",
        authors=[Author("Researcher")],
        abstract="Abstract",
        categories=["cs.AI"],
        primary_category="cs.AI",
        published=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated=datetime(2025, 1, 1, tzinfo=timezone.utc),
        abs_url="https://arxiv.org/abs/2501.00001",
        pdf_url="https://arxiv.org/pdf/2501.00001",
    )
    date_label = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    run_dir = tmp_path / date_label
    run_dir.mkdir(parents=True)
    digest = run_dir / "index.html"
    digest.write_text("daily recommendations", encoding="utf-8")
    report_path = run_dir / "reports" / "test" / "report.md"
    artifact = ReportArtifact(paper, report_path, None, VerifiedMetadata(), "report")

    pipeline = DailyPipeline(AppConfig(output_dir=str(tmp_path)), tmp_path)
    pipeline.config = SimpleNamespace(timezone="Asia/Shanghai")
    pipeline.output_root = tmp_path
    pipeline.clients.arxiv = SimpleNamespace(get=lambda _arxiv_id: paper)
    pipeline._process_paper = lambda *_args, **_kwargs: artifact

    assert pipeline.report_arxiv_id(paper.arxiv_id) == report_path
    assert digest.read_text(encoding="utf-8") == "daily recommendations"


def test_report_generation_uses_local_snapshot_when_arxiv_api_fails(tmp_path: Path) -> None:
    paper = Paper(
        arxiv_id="2609.00005",
        title="Snapshot Report",
        authors=[Author("Researcher")],
        abstract="A saved recommendation.",
        categories=["cs.CV"],
        primary_category="cs.CV",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/abs/2609.00005",
        pdf_url="https://arxiv.org/pdf/2609.00005",
    )
    output = tmp_path / "run" / "2026-09-14"
    output.mkdir(parents=True)
    write_json(
        output / "recommendations-test.json",
        [{"profile_id": "test", "paper": paper.to_dict(), "verified": {}}],
    )
    artifact = ReportArtifact(paper, output / "reports" / "snapshot" / "report.md", None, VerifiedMetadata(), "report")
    pipeline = DailyPipeline(AppConfig(output_dir=str(tmp_path)), tmp_path)
    pipeline.config = SimpleNamespace(timezone="Asia/Shanghai", profile_id="test")
    pipeline.output_root = tmp_path / "run"
    pipeline.clients.arxiv = SimpleNamespace(
        get=lambda _arxiv_id: (_ for _ in ()).throw(
            AssertionError("local snapshot should avoid the arXiv API")
        )
    )
    pipeline._process_paper = lambda received, *_args, **_kwargs: (assert_same(received, paper), artifact)[1]

    assert pipeline.report_arxiv_id(paper.arxiv_id) == artifact.report_path


def assert_same(left: Paper, right: Paper) -> None:
    assert left.arxiv_id == right.arxiv_id


def test_daily_explores_a_missing_route_even_with_a_full_list(tmp_path, monkeypatch):
    from dataclasses import replace

    now = datetime.now(timezone.utc)
    base = Paper("2601.00001", "Vision", [], "Video synthesis", ["cs.AI"], "cs.AI",
                 now, now, "", "")
    recent = replace(base, arxiv_id="2601.00002", title="Latent planning", abstract="latent planning")
    old = replace(recent, arxiv_id="1811.04551", title="Latent planning classic",
                  published=datetime(2018, 11, 1, tzinfo=timezone.utc))

    def plan(*args, **kwargs):
        return json.dumps({"actions": [{"tool": "topic", "query": "latent planning",
                                        "reason": "寻找规划方向的近期工作"}]})

    config = AppConfig(output_dir=str(tmp_path), profile_id="daily")
    config.discovery.positive_keywords = ["latent planning"]
    config.discovery.arxiv_categories = ["cs.AI"]
    config.discovery.minimum_concept_groups = 0
    config.discovery.recommendation_count = 1
    config.discovery.min_score = -1
    config.ranking.llm_rerank = False
    config.weekly.enabled = config.version_tracking.enabled = False
    monkeypatch.setattr("paperloom.discovery.pipeline.localize_abstracts", lambda *a: None)
    with DailyPipeline(config, tmp_path) as pipeline:
        pipeline.clients.arxiv = SimpleNamespace(search=lambda *a, **kw: [base],
            search_literature=lambda *a, **kw: [old, recent])
        pipeline.clients.llm = SimpleNamespace(enabled=True, chat=plan)
        pipeline.clients.verifier = SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title))
        pipeline.run(force=True, deliver=False)
    audit = read_json(next(tmp_path.glob("*/selection-daily-*.json")), {})
    assert [r["paper"]["arxiv_id"] for r in audit["items"] if r["selected"]] == [recent.arxiv_id]
    assert old.arxiv_id not in {r["paper"]["arxiv_id"] for r in audit["items"]}
    assert audit["search_log"][0]["query"] == "latent planning"
    published = read_json(next(tmp_path.glob("*/recommendations-daily.json")), [])[0]["paper"]
    assert "latent planning" in published["recommendation_reason"] and "latent planning" in published["recommendation_detail"]


@pytest.mark.parametrize("venue, eligible", [("acl", True), ("icml", False)])
def test_daily_requires_conference_evidence_after_a_web_lead(tmp_path, monkeypatch, venue, eligible):
    paper = Paper("1912.01603", "Grounded Planning", [], "planning with language", ["cs.AI"],
                  "cs.AI", datetime(2019, 12, 1, tzinfo=timezone.utc), None, "", "")
    paper.conference_publications = [{"conference": venue, "year": 2024, "paper_type": "long",
        "evidence_url": "https://example.org/proceedings", "association_evidence": "title and authors"}]
    def discover(scope, *args):
        return ([paper] if scope.arxiv_query_terms == ["Grounded Planning"] else []), {"directory": {"status": "ok"}}

    def plan(system, prompt, **kwargs):
        if "fake result 2601.09999" not in prompt:
            return json.dumps({"actions": [{"tool": "web", "query": "planning project", "reason": "定位论文原题"}]})
        if "Grounded Planning" not in prompt:
            return '{"actions":[]}'
        return json.dumps({"actions": [{"tool": "conference", "query": "Grounded Planning",
            "venue": venue, "year": 2024, "reason": "核对会议目录"}]})

    def web(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("GET", url), text=(
            "<rss><channel><item><title>Grounded Planning</title><description>fake result 2601.09999</description>"
            "</item></channel></rss>"))
    monkeypatch.setattr(httpx, "get", web)
    monkeypatch.setattr("paperloom.discovery.pipeline.localize_abstracts", lambda *a: None)
    config = AppConfig(output_dir=str(tmp_path), profile_id="daily")
    config.discovery.mode = "conference"
    config.discovery.conferences = ["acl"]
    config.discovery.conference_year_from = config.discovery.conference_year_to = 2024
    config.discovery.min_score = -1
    config.discovery.minimum_concept_groups = 0
    config.ranking.llm_rerank = config.weekly.enabled = config.version_tracking.enabled = False
    with DailyPipeline(config, tmp_path) as pipeline:
        pipeline.clients.llm = SimpleNamespace(enabled=True, chat=plan)
        pipeline.clients.conferences = SimpleNamespace(discover=discover)
        pipeline.clients.verifier = SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title))
        pipeline.run(deliver=False)
    audit = read_json(next(tmp_path.glob("*/selection-daily-*.json")), {})
    assert [r["paper"]["arxiv_id"] for r in audit["items"] if r["selected"]] == (["1912.01603"] if eligible else [])
    assert [r["tool"] for r in audit["search_log"]] == ["web", "conference"]


def test_report_generation_uses_alphaxiv_for_unknown_local_id(tmp_path: Path) -> None:
    paper = Paper(
        arxiv_id="2609.00008",
        title="Resolved by alphaXiv",
        authors=[],
        abstract="Resolved paper.",
        categories=[],
        primary_category="",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/abs/2609.00008",
        pdf_url="https://arxiv.org/pdf/2609.00008",
    )
    artifact = ReportArtifact(paper, tmp_path / "report.md", None, VerifiedMetadata(), "report")
    pipeline = DailyPipeline(AppConfig(output_dir=str(tmp_path)), tmp_path)
    pipeline.config = SimpleNamespace(
        timezone="Asia/Shanghai",
        profile_id="test",
        discovery=SimpleNamespace(provider="auto", alphaxiv_fallback_enabled=True),
    )
    pipeline.output_root = tmp_path / "run"
    pipeline.clients.arxiv = SimpleNamespace(
        get=lambda _arxiv_id: (_ for _ in ()).throw(RuntimeError("429"))
    )
    pipeline.clients.alphaxiv = SimpleNamespace(enabled=True, lookup=lambda _arxiv_id: paper)
    pipeline._process_paper = lambda *_args, **_kwargs: artifact

    assert pipeline.report_arxiv_id(paper.arxiv_id) == artifact.report_path


def test_daily_pipeline_publishes_only_after_localization(
    tmp_path: Path, monkeypatch
) -> None:
    paper = Paper(
        arxiv_id="2609.00001",
        title="Atomic Recommendation Publish",
        authors=[Author("Researcher")],
        abstract="Abstract",
        categories=["cs.AI"],
        primary_category="cs.AI",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/abs/2609.00001",
        pdf_url="https://arxiv.org/pdf/2609.00001",
    )
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="test")
    config.discovery.min_score = -1
    config.discovery.minimum_concept_groups = 0
    config.discovery.recommendation_count = 1
    config.ranking.llm_rerank = False
    config.weekly.enabled = False
    config.version_tracking.enabled = False
    pipeline = DailyPipeline(config, tmp_path)
    pipeline.clients.arxiv = SimpleNamespace(search=lambda *_args, **_kwargs: [paper])
    pipeline.clients.verifier = SimpleNamespace(
        verify=lambda item: VerifiedMetadata(
            title=item.title, authors=item.authors, sources=["arXiv only"]
        )
    )
    monkeypatch.setattr("paperloom.discovery.pipeline.rank_papers", lambda papers, *_args: papers)
    monkeypatch.setattr("paperloom.discovery.pipeline.matched_concept_groups", lambda *_args: 0)

    date_label = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    recommendation_path = tmp_path / "run" / date_label / "recommendations.json"
    write_json(recommendation_path, [{"paper": {"title": "Previous complete result"}}])

    def fake_localize(_config, _output_root, payload) -> None:
        current = read_json(recommendation_path, [])
        assert current[0]["paper"]["title"] == "Previous complete result"
        payload[0]["paper"]["abstract_zh"] = "已完成的中文摘要。"
        payload[0]["paper"]["recommendation_detail"] = "已完成的推荐依据。"

    monkeypatch.setattr("paperloom.discovery.pipeline.localize_abstracts", fake_localize)

    pipeline.run(force=True, demo=False, deliver=False)

    published = read_json(recommendation_path, [])
    assert published[0]["paper"]["title"] == paper.title
    assert published[0]["paper"]["abstract_zh"] == "已完成的中文摘要。"
    assert published[0]["paper"]["source_label"] not in published[0]["paper"]["recommendation_reason"]
    assert (
        tmp_path
        / "run"
        / date_label
        / "recommendations-test.json"
    ).is_file()


def test_delivery_failure_does_not_commit_processed_state(
    tmp_path: Path, monkeypatch
) -> None:
    paper = Paper(
        arxiv_id="2609.00002",
        title="Retry Delivery Safely",
        authors=[Author("Researcher")],
        abstract="Abstract",
        categories=["cs.AI"],
        primary_category="cs.AI",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/abs/2609.00002",
        pdf_url="https://arxiv.org/pdf/2609.00002",
    )
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="test")
    config.discovery.min_score = -1
    config.discovery.minimum_concept_groups = 0
    config.discovery.recommendation_count = 1
    config.ranking.llm_rerank = False
    config.delivery.email_enabled = True
    config.weekly.enabled = False
    config.version_tracking.enabled = False
    pipeline = DailyPipeline(config, tmp_path)
    pipeline.clients.arxiv = SimpleNamespace(search=lambda *_args, **_kwargs: [paper])
    pipeline.clients.verifier = SimpleNamespace(
        verify=lambda item: VerifiedMetadata(title=item.title, authors=item.authors)
    )
    monkeypatch.setattr("paperloom.discovery.pipeline.rank_papers", lambda papers, *_args: papers)
    monkeypatch.setattr("paperloom.discovery.pipeline.matched_concept_groups", lambda *_args: 0)
    monkeypatch.setattr("paperloom.discovery.pipeline.localize_abstracts", lambda *_args: None)

    def fail_delivery(*_args, **_kwargs):
        raise ConnectionError("SMTP unavailable")

    monkeypatch.setattr("paperloom.discovery.pipeline.send_digest", fail_delivery)

    try:
        pipeline.run(force=False, demo=False, deliver=True)
    except ConnectionError:
        pass
    else:
        raise AssertionError("delivery failure should propagate")

    assert not (tmp_path / "run" / "state-test.json").exists()


def test_daily_pipeline_uses_alphaxiv_only_after_arxiv_failure(
    tmp_path: Path, monkeypatch
) -> None:
    paper = Paper(
        arxiv_id="2609.00003",
        title="Fallback Paper",
        authors=[Author("Researcher")],
        abstract="Agentic image generation.",
        categories=["cs.CV"],
        primary_category="cs.CV",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/abs/2609.00003",
        pdf_url="https://arxiv.org/pdf/2609.00003",
    )
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="test")
    config.discovery.min_score = -1
    config.discovery.minimum_concept_groups = 2
    config.discovery.recommendation_count = 1
    config.discovery.alphaxiv_fallback_enabled = True
    config.discovery.arxiv_categories = ["cs.CV"]
    config.ranking.llm_rerank = False
    config.weekly.enabled = False
    config.version_tracking.enabled = False
    pipeline = DailyPipeline(config, tmp_path)
    pipeline.clients.arxiv = SimpleNamespace(
        search=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("arXiv API 当前限流")
        )
    )
    pipeline.clients.alphaxiv = SimpleNamespace(
        discover=lambda **_kwargs: [paper]
    )
    pipeline.clients.verifier = SimpleNamespace(
        verify=lambda item: VerifiedMetadata(title=item.title, authors=item.authors)
    )
    monkeypatch.setattr("paperloom.discovery.pipeline.rank_papers", lambda papers, *_args: papers)
    monkeypatch.setattr("paperloom.discovery.pipeline.matched_concept_groups", lambda *_args: 1)
    monkeypatch.setattr("paperloom.discovery.pipeline.localize_abstracts", lambda *_args: None)

    pipeline.run(force=True, demo=False, deliver=False)

    date_label = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    manifest = next((tmp_path / "run" / date_label).glob("discovery-test-*.json"))
    assert read_json(manifest)["sources"]["alphaxiv"]["status"] == "ok"
    assert not (tmp_path / "run" / date_label / "discovery-source.txt").exists()


def test_fallback_does_not_publish_empty_success_when_all_candidates_are_processed(
    tmp_path: Path, monkeypatch
) -> None:
    paper = Paper(
        arxiv_id="2609.00004",
        title="Already Recommended",
        authors=[Author("Researcher")],
        abstract="Agentic image generation.",
        categories=["cs.CV"],
        primary_category="cs.CV",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/abs/2609.00004",
        pdf_url="https://arxiv.org/pdf/2609.00004",
    )
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="test")
    config.discovery.minimum_concept_groups = 0
    config.ranking.llm_rerank = False
    config.weekly.enabled = False
    config.version_tracking.enabled = False
    pipeline = DailyPipeline(config, tmp_path)
    pipeline.clients.arxiv = SimpleNamespace(
        search=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("429"))
    )
    pipeline.clients.alphaxiv = SimpleNamespace(discover=lambda **_kwargs: [paper])
    monkeypatch.setattr("paperloom.discovery.pipeline.rank_papers", lambda papers, *_args: papers)
    monkeypatch.setattr("paperloom.discovery.pipeline.matched_concept_groups", lambda *_args: 0)
    write_json(tmp_path / "run" / "state-test.json", {"processed": [paper.arxiv_id]})

    with __import__("pytest").raises(RuntimeError, match="已阻止发布重复的空日报"):
        pipeline.run(force=False, demo=False, deliver=False)

    date_label = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    assert not (tmp_path / "run" / date_label / "recommendations-test.json").exists()
