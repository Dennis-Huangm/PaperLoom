from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from arxiv_ra.config import AppConfig
from arxiv_ra.models import Author, Paper, ReportArtifact, VerifiedMetadata
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.utils import read_json, write_json


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

    pipeline = DailyPipeline.__new__(DailyPipeline)
    pipeline.config = SimpleNamespace(timezone="Asia/Shanghai")
    pipeline.output_root = tmp_path
    pipeline.arxiv = SimpleNamespace(get=lambda _arxiv_id: paper)
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
    pipeline = DailyPipeline.__new__(DailyPipeline)
    pipeline.config = SimpleNamespace(timezone="Asia/Shanghai", profile_id="test")
    pipeline.output_root = tmp_path / "run"
    pipeline.arxiv = SimpleNamespace(
        get=lambda _arxiv_id: (_ for _ in ()).throw(
            AssertionError("local snapshot should avoid the arXiv API")
        )
    )
    pipeline._process_paper = lambda received, *_args, **_kwargs: (assert_same(received, paper), artifact)[1]

    assert pipeline.report_arxiv_id(paper.arxiv_id) == artifact.report_path


def assert_same(left: Paper, right: Paper) -> None:
    assert left.arxiv_id == right.arxiv_id


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
    pipeline = DailyPipeline.__new__(DailyPipeline)
    pipeline.config = SimpleNamespace(
        timezone="Asia/Shanghai",
        profile_id="test",
        discovery=SimpleNamespace(provider="auto", alphaxiv_fallback_enabled=True),
    )
    pipeline.output_root = tmp_path / "run"
    pipeline.arxiv = SimpleNamespace(
        get=lambda _arxiv_id: (_ for _ in ()).throw(RuntimeError("429"))
    )
    pipeline.alphaxiv = SimpleNamespace(enabled=True, lookup=lambda _arxiv_id: paper)
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
    pipeline.arxiv = SimpleNamespace(search=lambda *_args, **_kwargs: [paper])
    pipeline.verifier = SimpleNamespace(
        verify=lambda item: VerifiedMetadata(
            title=item.title, authors=item.authors, sources=["arXiv only"]
        )
    )
    monkeypatch.setattr("arxiv_ra.pipeline.rank_papers", lambda papers, *_args: papers)
    monkeypatch.setattr("arxiv_ra.pipeline.matched_concept_groups", lambda *_args: 0)

    date_label = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    recommendation_path = tmp_path / "run" / date_label / "recommendations.json"
    write_json(recommendation_path, [{"paper": {"title": "Previous complete result"}}])

    def fake_localize(_config, _output_root, payload) -> None:
        current = read_json(recommendation_path, [])
        assert current[0]["paper"]["title"] == "Previous complete result"
        payload[0]["paper"]["abstract_zh"] = "已完成的中文摘要。"
        payload[0]["paper"]["recommendation_detail"] = "已完成的推荐依据。"

    monkeypatch.setattr("arxiv_ra.pipeline.localize_abstracts", fake_localize)

    pipeline.run(force=True, demo=False, deliver=False)

    published = read_json(recommendation_path, [])
    assert published[0]["paper"]["title"] == paper.title
    assert published[0]["paper"]["abstract_zh"] == "已完成的中文摘要。"
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
    pipeline.arxiv = SimpleNamespace(search=lambda *_args, **_kwargs: [paper])
    pipeline.verifier = SimpleNamespace(
        verify=lambda item: VerifiedMetadata(title=item.title, authors=item.authors)
    )
    monkeypatch.setattr("arxiv_ra.pipeline.rank_papers", lambda papers, *_args: papers)
    monkeypatch.setattr("arxiv_ra.pipeline.matched_concept_groups", lambda *_args: 0)
    monkeypatch.setattr("arxiv_ra.pipeline.localize_abstracts", lambda *_args: None)

    def fail_delivery(*_args, **_kwargs):
        raise ConnectionError("SMTP unavailable")

    monkeypatch.setattr("arxiv_ra.pipeline.send_digest", fail_delivery)

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
    config.ranking.llm_rerank = False
    config.weekly.enabled = False
    config.version_tracking.enabled = False
    pipeline = DailyPipeline(config, tmp_path)
    pipeline.arxiv = SimpleNamespace(
        search=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("arXiv API 当前限流")
        )
    )
    pipeline.alphaxiv = SimpleNamespace(
        discover=lambda **_kwargs: [paper]
    )
    pipeline.verifier = SimpleNamespace(
        verify=lambda item: VerifiedMetadata(title=item.title, authors=item.authors)
    )
    monkeypatch.setattr("arxiv_ra.pipeline.rank_papers", lambda papers, *_args: papers)
    monkeypatch.setattr("arxiv_ra.pipeline.matched_concept_groups", lambda *_args: 1)
    monkeypatch.setattr("arxiv_ra.pipeline.localize_abstracts", lambda *_args: None)

    pipeline.run(force=True, demo=False, deliver=False)

    date_label = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    assert (tmp_path / "run" / date_label / "discovery-source.txt").read_text(
        encoding="utf-8"
    ).startswith("本次推荐使用 alphaXiv")


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
    pipeline.arxiv = SimpleNamespace(
        search=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("429"))
    )
    pipeline.alphaxiv = SimpleNamespace(discover=lambda **_kwargs: [paper])
    monkeypatch.setattr("arxiv_ra.pipeline.rank_papers", lambda papers, *_args: papers)
    monkeypatch.setattr("arxiv_ra.pipeline.matched_concept_groups", lambda *_args: 0)
    write_json(tmp_path / "run" / "state-test.json", {"processed": [paper.arxiv_id]})

    with __import__("pytest").raises(RuntimeError, match="已阻止发布重复的空日报"):
        pipeline.run(force=False, demo=False, deliver=False)

    date_label = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    assert not (tmp_path / "run" / date_label / "recommendations-test.json").exists()
