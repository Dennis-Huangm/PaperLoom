from pathlib import Path
import json
import threading
import time

import yaml
from fastapi.testclient import TestClient

from arxiv_ra.abstracts import localize_abstracts
from arxiv_ra.config import AppConfig
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.utils import read_json
from arxiv_ra.web import (
    JobManager,
    create_app,
    latest_recommendations,
    recommendation_history,
    recommendations_for_date,
    result_artifact_url,
    save_discovery_settings,
    update_dotenv,
)


def _write_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "timezone": "Asia/Shanghai",
                "output_dir": "run",
                "discovery": {
                    "interest_description": "AgenticT2I",
                    "recommendation_count": 10,
                    "lookback_days": 45,
                    "arxiv_categories": ["cs.CV"],
                    "positive_keywords": ["agent", "image generation"],
                    "negative_keywords": [],
                },
                "delivery": {
                    "email_enabled": True,
                    "email_address_env": "QQ_EMAIL",
                    "password_env": "SMTP_PASSWORD",
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _full_settings_form() -> dict[str, str]:
    return {
        "timezone": "Asia/Shanghai",
        "output_dir": "run",
        "jobs_max_parallel": "3",
        "interest_description": "Visual agents",
        "arxiv_categories": "cs.CV\ncs.AI",
        "arxiv_query_terms": "image generation\nimage editing",
        "positive_keywords": "agent\nimage editing",
        "negative_keywords": "survey",
        "seed_papers": "2407.05600",
        "concept_groups": "agent, llm\nimage generation, image editing",
        "lookback_days": "30",
        "max_candidates": "500",
        "prefilter_count": "50",
        "recommendation_count": "8",
        "min_score": "0.5",
        "minimum_concept_groups": "2",
        "category_weight": "1.0",
        "keyword_weight": "3.0",
        "negative_weight": "4.0",
        "recency_weight": "1.0",
        "llm_min_score": "6.5",
        "llm_rerank": "true",
        "llm_model": "test-model",
        "llm_api_key_env": "LLM_API_KEY",
        "llm_base_url_env": "LLM_BASE_URL",
        "llm_temperature": "0.1",
        "max_chunk_chars": "18000",
        "openalex_email": "researcher@example.com",
        "openalex_api_key_env": "OPENALEX_API_KEY",
        "semantic_scholar_api_key_env": "SEMANTIC_SCHOLAR_API_KEY",
        "semantic_scholar_min_interval": "1.1",
        "semantic_scholar_max_retries": "3",
        "verify_venue": "true",
        "pdf_parser": "pymupdf",
        "use_docling_if_available": "true",
        "max_pages": "60",
        "figure_dpi": "160",
        "email_enabled": "true",
        "smtp_host": "smtp.qq.com",
        "smtp_port": "465",
        "smtp_ssl": "true",
        "email_address_env": "QQ_EMAIL",
        "smtp_password_env": "SMTP_PASSWORD",
        "smtp_username": "",
        "from_address": "",
        "to_addresses": "",
        "zotero_enabled": "true",
        "zotero_mode": "local",
        "zotero_base_url": "http://127.0.0.1:23119/api",
        "zotero_api_key_env": "ZOTERO_LOCAL_API_KEY",
        "zotero_collection_name": "arXiv Research Assistant",
        "zotero_attach_pdf": "true",
        "zotero_pdf_attachment_mode": "imported_file",
        "zotero_attach_report": "true",
        "zotero_add_profile_tag": "true",
        "obsidian_vault_path": "",
        "obsidian_root_folder": "arXiv Research Assistant",
        "obsidian_home_folder": "Home",
        "obsidian_daily_folder": "Daily",
        "obsidian_papers_folder": "Papers",
        "obsidian_concepts_folder": "Topics",
        "obsidian_weekly_folder": "Reviews/Weekly",
        "obsidian_attachments_folder": "Attachments",
        "obsidian_profiles_folder": "System/Profiles",
        "obsidian_indexes_folder": "System/Indexes",
        "obsidian_auto_sync": "true",
        "obsidian_sync_daily": "true",
        "obsidian_sync_reports": "true",
        "obsidian_sync_weekly": "true",
        "obsidian_sync_feedback": "true",
        "obsidian_copy_figures": "true",
        "obsidian_generate_concepts": "true",
        "weekly_enabled": "true",
        "weekly_days": "7",
        "weekly_max_papers": "50",
        "weekly_include_deep_reports": "true",
        "weekly_auto_generate": "true",
        "weekly_weekday": "6",
        "version_tracking_enabled": "true",
        "version_tracking_auto_check": "true",
        "version_tracking_max_tracked": "50",
        "version_tracking_include_reports": "true",
        "version_tracking_include_feedback": "true",
        "version_tracking_include_zotero": "true",
        "version_tracking_analyze_pdf_diff": "true",
        "citations_enabled": "true",
        "citations_max_references": "15",
        "citations_max_citations": "15",
        "citations_max_similar": "15",
        "citations_min_interval": "1.1",
        "citations_max_retries": "3",
    }


def test_latest_recommendations_uses_newest_date(tmp_path: Path) -> None:
    older = tmp_path / "2026-08-16"
    newer = tmp_path / "2026-08-17"
    older.mkdir()
    newer.mkdir()
    (older / "recommendations.json").write_text("[]", encoding="utf-8")
    (newer / "recommendations.json").write_text('[{"paper":{"title":"Newest"}}]', encoding="utf-8")

    date_label, items = latest_recommendations(tmp_path)

    assert date_label == "2026-08-17"
    assert items[0]["paper"]["title"] == "Newest"


def test_latest_recommendations_skips_damaged_newer_artifact(tmp_path: Path) -> None:
    older = tmp_path / "2026-08-16"
    newer = tmp_path / "2026-08-17"
    older.mkdir()
    newer.mkdir()
    (older / "recommendations.json").write_text(
        '[{"paper":{"title":"Recoverable result"}}]', encoding="utf-8"
    )
    (newer / "recommendations.json").write_text("{incomplete", encoding="utf-8")

    date_label, items = latest_recommendations(tmp_path)

    assert date_label == "2026-08-16"
    assert items[0]["paper"]["title"] == "Recoverable result"


def test_latest_recommendations_are_scoped_to_active_profile(tmp_path: Path) -> None:
    older = tmp_path / "2026-08-21"
    newer = tmp_path / "2026-08-22"
    older.mkdir()
    newer.mkdir()
    (older / "recommendations.json").write_text(
        '[{"profile_id":"agentict2i","paper":{"title":"Agentic paper"}}]',
        encoding="utf-8",
    )
    (newer / "recommendations.json").write_text(
        '[{"profile_id":"robotics","paper":{"title":"Robotics paper"}}]',
        encoding="utf-8",
    )

    date_label, items = latest_recommendations(tmp_path, "agentict2i")

    assert date_label == "2026-08-21"
    assert items[0]["paper"]["title"] == "Agentic paper"


def test_recommendation_history_only_lists_dates_with_results_for_profile(
    tmp_path: Path,
) -> None:
    for date in ("2026-08-20", "2026-08-21", "2026-08-22"):
        (tmp_path / date).mkdir()
    (tmp_path / "2026-08-20" / "recommendations.json").write_text(
        '[{"profile_id":"agentict2i","paper":{"title":"Older"}}]',
        encoding="utf-8",
    )
    (tmp_path / "2026-08-21" / "recommendations.json").write_text(
        '[{"profile_id":"robotics","paper":{"title":"Other profile"}}]',
        encoding="utf-8",
    )
    (tmp_path / "2026-08-22" / "recommendations.json").write_text(
        "[]", encoding="utf-8"
    )

    history = recommendation_history(tmp_path, "agentict2i")
    selected = recommendations_for_date(tmp_path, "2026-08-20", "agentict2i")

    assert history == [{"date": "2026-08-20", "count": 1}]
    assert selected[0]["paper"]["title"] == "Older"


def test_same_day_recommendations_are_preserved_per_profile(tmp_path: Path) -> None:
    date_dir = tmp_path / "2026-08-22"
    date_dir.mkdir()
    (date_dir / "recommendations-agentict2i.json").write_text(
        '[{"profile_id":"agentict2i","paper":{"title":"Agentic paper"}}]',
        encoding="utf-8",
    )
    (date_dir / "recommendations-robotics.json").write_text(
        '[{"profile_id":"robotics","paper":{"title":"Robotics paper"}}]',
        encoding="utf-8",
    )
    # The compatibility alias may point at whichever profile ran most recently.
    (date_dir / "recommendations.json").write_text(
        '[{"profile_id":"robotics","paper":{"title":"Robotics paper"}}]',
        encoding="utf-8",
    )

    agentic = recommendations_for_date(tmp_path, "2026-08-22", "agentict2i")
    robotics = recommendations_for_date(tmp_path, "2026-08-22", "robotics")

    assert agentic[0]["paper"]["title"] == "Agentic paper"
    assert robotics[0]["paper"]["title"] == "Robotics paper"


def test_dashboard_can_switch_to_historical_recommendation_date(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)

    def item(arxiv_id: str, title: str) -> dict:
        return {
            "profile_id": "agentict2i",
            "paper": {
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": [{"name": "Researcher"}],
                "abstract": "English abstract.",
                "abstract_zh": f"{title} 的中文摘要。",
                "recommendation_detail": f"{title} 的推荐依据。",
                "primary_category": "cs.CV",
                "categories": ["cs.CV"],
                "published": "2026-08-01T00:00:00+00:00",
                "updated": "2026-08-01T00:00:00+00:00",
                "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
                "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
                "final_score": 9.0,
            },
            "verified": {},
        }

    for date, payload in (
        ("2026-08-20", [item("2608.00001", "Historical Paper")]),
        ("2026-08-22", [item("2608.00002", "Latest Paper")]),
    ):
        date_dir = tmp_path / "run" / date
        date_dir.mkdir(parents=True)
        (date_dir / "recommendations.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    app = create_app(config_path)

    with TestClient(app) as client:
        historical = client.get("/?date=2026-08-20")
        invalid = client.get("/?date=not-a-date")

    assert historical.status_code == 200
    assert "Historical Paper" in historical.text
    assert "Latest Paper" not in historical.text
    assert "2026-08-20" in historical.text
    assert "1 篇" in historical.text
    assert 'id="recommendation-calendar-trigger"' in historical.text
    assert 'id="recommendation-calendar-dialog"' in historical.text
    assert 'data-selected-date="2026-08-20"' in historical.text
    assert '"date": "2026-08-20"' in historical.text
    assert "当日推荐 1 篇" in historical.text
    assert invalid.status_code == 400


def test_job_result_prefers_rendered_html_over_markdown(tmp_path: Path) -> None:
    report_dir = tmp_path / "2026-08-22" / "reports" / "paper"
    report_dir.mkdir(parents=True)
    markdown = report_dir / "report.md"
    html = report_dir / "report.html"
    markdown.write_text("# Report", encoding="utf-8")
    html.write_text("<h1>Report</h1>", encoding="utf-8")

    assert result_artifact_url(markdown, tmp_path) == (
        "/artifacts/2026-08-22/reports/paper/report.html"
    )


def test_localized_abstracts_are_generated_once_and_cached(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class FakeLLM:
        enabled = True

        def __init__(self, _config) -> None:
            pass

        def chat(self, _system: str, user: str, json_mode: bool = False) -> str:
            calls.append(user)
            assert json_mode is True
            return '{"papers":[{"id":"2407.05600","abstract_zh":"该论文研究智能体图像生成，并通过统一策略协调推理、工具调用与图像输出。","recommendation_detail":"它将推理、工具调用与图像输出置于同一策略中，可直接用于分析 AgenticT2I 的统一决策机制。"}]}'

    monkeypatch.setattr("arxiv_ra.abstracts.LLMClient", FakeLLM)
    output_root = tmp_path / "run"
    output_root.mkdir()
    recommendations = [
        {
            "paper": {
                "arxiv_id": "2407.05600",
                "title": "GenArtist",
                "abstract": "This paper studies agentic image generation.",
            }
        }
    ]

    localize_abstracts(AppConfig(), output_root, recommendations)
    recommendations[0]["paper"].pop("abstract_zh")
    localize_abstracts(AppConfig(), output_root, recommendations)

    assert recommendations[0]["paper"]["abstract_zh"].startswith("该论文研究")
    assert "AgenticT2I" in recommendations[0]["paper"]["recommendation_detail"]
    assert len(calls) == 1


def test_non_generating_localization_is_fast_and_does_not_write_cache(
    tmp_path: Path, monkeypatch
) -> None:
    class ForbiddenLLM:
        def __init__(self, _config) -> None:
            raise AssertionError("page rendering must not initialize the LLM")

    monkeypatch.setattr("arxiv_ra.abstracts.LLMClient", ForbiddenLLM)
    output_root = tmp_path / "run"
    output_root.mkdir()
    recommendations = [
        {
            "paper": {
                "arxiv_id": "2609.00001",
                "title": "Fast Dashboard",
                "abstract": "A source abstract.",
                "recommendation_reason": "与当前方向的方法设计直接相关。",
            }
        }
    ]

    localize_abstracts(
        AppConfig(), output_root, recommendations, generate=False
    )

    assert recommendations[0]["paper"]["abstract_zh"]
    assert recommendations[0]["paper"]["recommendation_detail"]
    assert not (output_root / "abstract-translations.json").exists()


def test_localization_network_call_does_not_hold_cache_lock(
    tmp_path: Path, monkeypatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    reader_done = threading.Event()

    class BlockingLLM:
        enabled = True

        def __init__(self, _config) -> None:
            pass

        def chat(self, _system: str, _user: str, json_mode: bool = False) -> str:
            assert json_mode is True
            entered.set()
            release.wait(timeout=3)
            return '{"papers":[{"id":"2609.00002","abstract_zh":"阻塞任务的中文摘要。","recommendation_detail":"阻塞任务的推荐依据。"}]}'

    monkeypatch.setattr("arxiv_ra.abstracts.LLMClient", BlockingLLM)
    output_root = tmp_path / "run"
    output_root.mkdir()
    writer_payload = [
        {"paper": {"arxiv_id": "2609.00002", "title": "Writer", "abstract": "Abstract"}}
    ]
    reader_payload = [
        {
            "paper": {
                "arxiv_id": "2609.00003",
                "title": "Reader",
                "abstract": "Abstract",
                "recommendation_reason": "快速回退内容。",
            }
        }
    ]

    writer = threading.Thread(
        target=localize_abstracts,
        args=(AppConfig(), output_root, writer_payload),
    )
    reader = threading.Thread(
        target=lambda: (
            localize_abstracts(
                AppConfig(), output_root, reader_payload, generate=False
            ),
            reader_done.set(),
        )
    )
    writer.start()
    assert entered.wait(timeout=1)
    reader.start()
    try:
        assert reader_done.wait(timeout=1)
    finally:
        release.set()
        writer.join(timeout=3)
        reader.join(timeout=3)

    assert not writer.is_alive()
    assert not reader.is_alive()


def test_dashboard_marks_existing_report_and_uses_chinese_summary(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    date_dir = tmp_path / "run" / "2026-08-22"
    report_dir = date_dir / "reports" / "2407.05600-genartist"
    report_dir.mkdir(parents=True)
    (date_dir / "recommendations.json").write_text(
        '[{"paper":{"arxiv_id":"2407.05600","title":"GenArtist","authors":[],"abstract":"English abstract.",'
        '"primary_category":"cs.CV","categories":["cs.CV"],"published":"2024-07-08T00:00:00+00:00",'
        '"updated":"2024-07-08T00:00:00+00:00","abs_url":"https://arxiv.org/abs/2407.05600",'
        '"pdf_url":"https://arxiv.org/pdf/2407.05600","final_score":9.0},"verified":{}}]',
        encoding="utf-8",
    )
    (report_dir / "metadata.json").write_text(
        '{"paper":{"arxiv_id":"2407.05600","title":"GenArtist","authors":[]},"verified":{}}',
        encoding="utf-8",
    )
    (report_dir / "report.html").write_text("<html></html>", encoding="utf-8")
    (report_dir / "report.md").write_text(
        "# GenArtist\n\n## 一句话总结\n\n该论文统一协调推理、工具调用与图像生成。\n\n## 核心方法\n\n方法。\n",
        encoding="utf-8",
    )
    app = create_app(config_path)

    with TestClient(app) as client:
        response = client.get("/")
        reports_before = client.get("/reports")
        added = client.post("/api/library/add", data={"arxiv_id": "2407.05600"})
        reports_after = client.get("/reports")
        library = client.get("/library")

    assert response.status_code == 200
    assert "摘要精要" in response.text
    assert "该论文统一协调推理、工具调用与图像生成" in response.text
    assert "重新生成报告" in response.text
    assert "打开报告" in response.text
    assert "加入文献库" in reports_before.text
    assert added.status_code == 200 and added.json()["saved"] is True
    assert "已加入文献库（相关）" in reports_after.text
    assert "打开论文报告" in library.text


def test_dashboard_can_save_and_remove_paper_from_profile_library(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    date_dir = tmp_path / "run" / "2026-08-22"
    date_dir.mkdir(parents=True)
    (date_dir / "recommendations.json").write_text(
        '[{"profile_id":"agentict2i","paper":{"arxiv_id":"2407.05600","title":"GenArtist",'
        '"authors":[{"name":"Zhenyu Wang"}],"abstract":"","abstract_zh":"智能体图像生成摘要。",'
        '"recommendation_detail":"统一协调推理、工具调用和图像生成。","primary_category":"cs.CV",'
        '"categories":["cs.CV"],"published":"2024-07-08T00:00:00+00:00",'
        '"updated":"2024-07-08T00:00:00+00:00","abs_url":"https://arxiv.org/abs/2407.05600",'
        '"pdf_url":"https://arxiv.org/pdf/2407.05600","final_score":9.0},"verified":{}}]',
        encoding="utf-8",
    )
    app = create_app(config_path)

    with TestClient(app) as client:
        home = client.get("/")
        negative = client.post(
            "/api/feedback",
            data={"arxiv_id": "2407.05600", "verdict": "not_relevant"},
        )
        invalid_legacy = client.post(
            "/api/feedback",
            data={"arxiv_id": "2407.05600", "verdict": "must_read"},
        )
        saved = client.post("/api/library/toggle", data={"arxiv_id": "2407.05600"})
        library = client.get("/library")
        removed = client.post("/api/library/toggle", data={"arxiv_id": "2407.05600"})

    assert "加入文献库" in home.text
    assert "必读" not in home.text
    assert "稍后读" not in home.text
    assert negative.status_code == 200 and negative.json()["in_library"] is False
    assert invalid_legacy.status_code == 400
    assert saved.status_code == 200 and saved.json()["saved"] is True
    assert saved.json()["feedback"] is None
    assert "GenArtist" in library.text
    assert "智能体图像生成摘要" in library.text
    assert "生成论文报告" in library.text
    assert removed.status_code == 200 and removed.json()["saved"] is False


def test_report_only_save_localizes_library_card_from_report_sections(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    report_dir = tmp_path / "run" / "2026-08-24" / "reports" / "2607.19056-vector-bench"
    report_dir.mkdir(parents=True)
    (report_dir / "metadata.json").write_text(
        '{"paper":{"arxiv_id":"2607.19056","title":"Vector-Bench",'
        '"authors":[{"name":"Researcher"}],"abstract":"Long English abstract that should not be shown.",'
        '"primary_category":"cs.AI","categories":["cs.AI"],'
        '"published":"2026-07-01T00:00:00+00:00","updated":"2026-07-01T00:00:00+00:00",'
        '"abs_url":"https://arxiv.org/abs/2607.19056","pdf_url":"https://arxiv.org/pdf/2607.19056"},'
        '"verified":{}}',
        encoding="utf-8",
    )
    (report_dir / "report.html").write_text("<html></html>", encoding="utf-8")
    (report_dir / "report.md").write_text(
        "# Vector-Bench\n\n## 一句话总结\n\n该论文构建 SVG 精细编辑基准并评估局部修改能力。"
        "\n\n## 为什么值得阅读\n\n它能帮助分析智能体绘图系统是否只修改目标元素，同时保持其他内容不变。\n",
        encoding="utf-8",
    )
    app = create_app(config_path)

    with TestClient(app) as client:
        added = client.post("/api/library/add", data={"arxiv_id": "2607.19056"})
        library = client.get("/library")

    assert added.status_code == 200
    saved = read_json(tmp_path / "run" / "paper-library-agentict2i.json", {})["items"]["2607.19056"]
    assert "该论文构建 SVG 精细编辑基准" in library.text
    assert "它能帮助分析智能体绘图系统" in library.text
    assert "Long English abstract" not in library.text
    assert saved["paper"]["abstract_zh"].startswith("该论文构建")
    assert saved["paper"]["recommendation_detail"].startswith("它能帮助")


def test_library_page_repairs_existing_incomplete_report_entry(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    output_root = tmp_path / "run"
    report_dir = output_root / "2026-08-24" / "reports" / "2607.19056-vector-bench"
    report_dir.mkdir(parents=True)
    paper = {
        "arxiv_id": "2607.19056",
        "title": "Vector-Bench",
        "authors": [],
        "abstract": "English source abstract.",
        "primary_category": "cs.AI",
    }
    (report_dir / "metadata.json").write_text(
        json.dumps({"paper": paper, "verified": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (report_dir / "report.html").write_text("<html></html>", encoding="utf-8")
    (report_dir / "report.md").write_text(
        "# Vector-Bench\n\n## 一句话总结\n\n中文精炼总结。"
        "\n\n## 为什么值得阅读\n\n中文保存价值。\n",
        encoding="utf-8",
    )
    PaperLibraryStore(output_root, "agentict2i").add(
        {"paper": paper, "verified": {}}, "AgenticT2I"
    )
    app = create_app(config_path)

    with TestClient(app) as client:
        library = client.get("/library")

    repaired = PaperLibraryStore(output_root, "agentict2i").all()["2607.19056"]["paper"]
    assert "中文精炼总结" in library.text
    assert "中文保存价值" in library.text
    assert repaired["abstract_zh"] == "中文精炼总结。"
    assert repaired["recommendation_detail"] == "中文保存价值。"


def test_save_discovery_settings_preserves_delivery_secrets_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)

    save_discovery_settings(config_path, {"recommendation_count": 7})

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert payload["discovery"]["recommendation_count"] == 7
    assert payload["delivery"]["password_env"] == "SMTP_PASSWORD"
    assert "password" not in payload["delivery"]


def test_gui_pages_render_and_secrets_are_not_exposed(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    monkeypatch.setenv("SMTP_PASSWORD", "never-render-this-secret")
    app = create_app(config_path)

    with TestClient(app) as client:
        for path in ["/", "/reports", "/weekly", "/versions", "/citations", "/generate", "/profiles", "/settings", "/status"]:
            response = client.get(path)
            assert response.status_code == 200
            assert "never-render-this-secret" not in response.text
        assert client.get("/settings").headers["cache-control"] == "no-store"

        invalid = client.post("/api/jobs/report", data={"arxiv_id": "not-an-id"})
        assert invalid.status_code == 400


def test_redesigned_ui_uses_research_workspace_and_local_icon_library(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    app = create_app(config_path)

    with TestClient(app) as client:
        home = client.get("/")
        reports = client.get("/reports")
        settings = client.get("/settings")
        icon_css = client.get("/static/vendor/fontawesome/css/all.min.css")
        app_css = client.get("/static/app.css")
        app_icon = client.get("/static/app-icon.ico")

    assert "primary-nav" in home.text
    assert "vendor/fontawesome/css/all.min.css" in home.text
    assert "app-icon.ico" in home.text
    assert "20260911-calendar" in home.text
    assert "literature-workspace" not in home.text  # empty test data uses the intentional empty state
    assert "paper-grid" not in home.text
    assert "report-table" not in reports.text  # empty report library uses the intentional empty state
    assert "settings-shell" in settings.text
    assert "settings-jump" not in settings.text
    assert icon_css.status_code == 200
    assert app_css.status_code == 200
    assert "@media(min-width:2000px)" in app_css.text
    assert "max-width:2400px" in app_css.text
    assert "max-height:none" in app_css.text
    assert "Font Awesome 5 Free" in icon_css.text
    assert app_icon.status_code == 200
    assert app_icon.headers["content-type"] in {"image/vnd.microsoft.icon", "image/x-icon"}


def test_gui_updates_all_config_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    app = create_app(config_path)
    form = _full_settings_form()
    form["jobs_max_parallel"] = "4"

    with TestClient(app) as client:
        response = client.post(
            "/settings/config",
            data=form,
            follow_redirects=False,
        )

    assert response.status_code == 303
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    profile = yaml.safe_load((tmp_path / "profiles" / "agentict2i.yaml").read_text(encoding="utf-8"))
    assert profile["discovery"]["positive_keywords"] == ["agent", "image editing"]
    assert profile["discovery"]["recommendation_count"] == 8
    assert profile["discovery"]["concept_groups"] == [["agent", "llm"], ["image generation", "image editing"]]
    assert profile["ranking"]["llm_rerank"] is True
    assert payload["llm"]["model"] == "test-model"
    assert payload["metadata"]["verify_venue"] is True
    assert payload["metadata"]["semantic_scholar_min_interval"] == 1.1
    assert payload["metadata"]["semantic_scholar_max_retries"] == 3
    assert payload["pdf"]["max_pages"] == 60
    assert payload["delivery"]["smtp_host"] == "smtp.qq.com"
    assert payload["delivery"]["password_env"] == "SMTP_PASSWORD"
    assert payload["zotero"]["enabled"] is True
    assert payload["zotero"]["pdf_attachment_mode"] == "imported_file"
    assert payload["obsidian"]["enabled"] is False
    assert payload["obsidian"]["root_folder"] == "arXiv Research Assistant"
    assert payload["jobs"]["max_parallel"] == 4
    assert app.state.jobs.max_parallel == 4
    assert payload["weekly"]["enabled"] is True
    assert payload["weekly"]["weekday"] == 6
    assert payload["version_tracking"]["max_tracked"] == 50
    assert payload["citations"]["max_similar"] == 15


def test_job_manager_runs_in_parallel_and_merges_duplicate_targets(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, max_parallel=2)
    started = [threading.Event() for _ in range(3)]
    release = threading.Event()

    def task(index: int):
        def run() -> Path:
            started[index].set()
            release.wait(timeout=3)
            return tmp_path / f"result-{index}.html"

        return run

    first = manager.submit("report", "arXiv:1", task(0))
    duplicate = manager.submit("report", "arXiv:1", task(0))
    second = manager.submit("report", "arXiv:2", task(1))
    third = manager.submit("report", "arXiv:3", task(2))

    assert duplicate.id == first.id
    assert started[0].wait(1) and started[1].wait(1)
    assert started[2].wait(0.1) is False
    assert manager.get(third.id).status == "queued"

    manager.set_max_parallel(3)
    assert started[2].wait(1)
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if all(manager.get(job.id).status == "succeeded" for job in (first, second, third)):
            break
        time.sleep(0.02)
    assert all(manager.get(job.id).status == "succeeded" for job in (first, second, third))
    manager.executor.shutdown(wait=True)


def test_gui_can_switch_research_profiles(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    app = create_app(config_path)
    second = {
        "id": "world-models",
        "name": "World Models",
        "description": "Embodied world models",
        "source": {"keywords": ["world model"], "reference_papers": []},
        "discovery": {
            "interest_description": "Embodied world models",
            "arxiv_categories": ["cs.RO"],
            "positive_keywords": ["world model"],
            "recommendation_count": 6,
        },
        "ranking": {},
    }
    (tmp_path / "profiles" / "world-models.yaml").write_text(
        yaml.safe_dump(second, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with TestClient(app) as client:
        response = client.post("/profiles/world-models/activate", follow_redirects=False)
        page = client.get("/profiles")

    assert response.status_code == 303
    assert (tmp_path / "profiles" / "active.txt").read_text(encoding="utf-8").strip() == "world-models"
    assert "World Models · 当前方向" in page.text
    assert "cs.RO" in yaml.safe_dump(yaml.safe_load((tmp_path / "profiles" / "world-models.yaml").read_text(encoding="utf-8")))


def test_gui_creates_profile_from_keywords_and_references(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)

    def fake_generate(self, profile_id, name, keywords, negative_keywords, reference_ids, description, recommendation_count):
        return {
            "id": profile_id,
            "name": name,
            "description": description,
            "source": {
                "keywords": keywords,
                "negative_keywords": negative_keywords,
                "reference_papers": [{"arxiv_id": item, "title": "Reference"} for item in reference_ids],
            },
            "discovery": {
                "interest_description": description,
                "arxiv_categories": ["cs.RO"],
                "positive_keywords": keywords,
                "recommendation_count": recommendation_count,
            },
            "ranking": {},
        }

    monkeypatch.setattr("arxiv_ra.web.ProfileGenerator.generate", fake_generate)
    app = create_app(config_path)

    with TestClient(app) as client:
        response = client.post(
            "/profiles/create",
            data={
                "name": "Robot Agents",
                "description": "Robotic agent learning",
                "keywords": "robot agent\nworld model",
                "reference_ids": "2407.05600",
                "negative_keywords": "survey",
                "recommendation_count": "7",
                "activate": "true",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    saved = yaml.safe_load((tmp_path / "profiles" / "robot-agents.yaml").read_text(encoding="utf-8"))
    assert saved["source"]["keywords"] == ["robot agent", "world model"]
    assert saved["source"]["reference_papers"][0]["arxiv_id"] == "2407.05600"
    assert (tmp_path / "profiles" / "active.txt").read_text(encoding="utf-8").strip() == "robot-agents"


def test_gui_updates_credentials_without_rendering_existing_values(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_API_KEY=old-secret\nUNRELATED=keep\nSEMANTIC_SCHOLAR_API_KEY=remove-me\n", encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY", "old-secret")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "remove-me")
    app = create_app(config_path)

    with TestClient(app) as client:
        page = client.get("/settings")
        assert "old-secret" not in page.text
        assert "remove-me" not in page.text
        response = client.post(
            "/settings/credentials",
            data={
                "llm_api_key": "new-secret",
                "clear_semantic_scholar_api_key": "true",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    env_text = env_path.read_text(encoding="utf-8")
    assert "LLM_API_KEY=new-secret" in env_text
    assert "UNRELATED=keep" in env_text
    assert "remove-me" not in env_text
    assert "SEMANTIC_SCHOLAR_API_KEY=" not in env_text


def test_gui_rejects_cross_site_mutations(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    app = create_app(config_path)

    with TestClient(app) as client:
        response = client.post(
            "/settings/config",
            headers={"Origin": "https://malicious.example", "Sec-Fetch-Site": "cross-site"},
            data={
                "interest_description": "Changed",
                "recommendation_count": "1",
                "lookback_days": "1",
            },
        )

    assert response.status_code == 403
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert payload["discovery"]["interest_description"] == "AgenticT2I"


def test_zotero_save_reports_unavailable_before_downloading_pdf(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    date_dir = tmp_path / "run" / "2026-08-19"
    date_dir.mkdir(parents=True)
    (date_dir / "recommendations.json").write_text(
        '[{"paper":{"arxiv_id":"2407.05600","title":"Paper","authors":[],"abstract":"",'
        '"primary_category":"cs.CV","published":"2024-07-08T00:00:00+00:00",'
        '"abs_url":"https://arxiv.org/abs/2407.05600","pdf_url":"https://arxiv.org/pdf/2407.05600"},'
        '"verified":{}}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "arxiv_ra.web.ZoteroClient.status",
        lambda self: {"ready": False, "authorized": False},
    )
    app = create_app(config_path)

    with TestClient(app) as client:
        response = client.post("/api/zotero/save-paper", data={"arxiv_id": "2407.05600"})

    assert response.status_code == 503
    assert "Zotero 未运行" in response.json()["detail"]
    assert not (tmp_path / "run" / "zotero-cache").exists()


def test_zotero_collections_endpoint_returns_picker_tree(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    monkeypatch.setattr(
        "arxiv_ra.web.ZoteroClient.status",
        lambda self: {"ready": True, "authorized": True},
    )
    monkeypatch.setattr(
        "arxiv_ra.web.ZoteroClient.list_collections",
        lambda self: [
            {"key": "AIROOT01", "name": "AI", "parent_key": "", "label": "AI"},
            {"key": "AGENTS01", "name": "Agents", "parent_key": "AIROOT01", "label": "AI / Agents"},
        ],
    )
    app = create_app(config_path)

    with TestClient(app) as client:
        response = client.get("/api/zotero/collections")

    assert response.status_code == 200
    assert response.json()["default_name"] == "arXiv Research Assistant"
    assert response.json()["collections"][1]["label"] == "AI / Agents"


def test_update_dotenv_preserves_unrelated_entries_and_clear_is_explicit(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# comment\nKEEP=value\nSECRET=old\n", encoding="utf-8")
    monkeypatch.setenv("SECRET", "old")

    update_dotenv(env_path, {"NEW_KEY": "new-value"}, {"SECRET"})

    text = env_path.read_text(encoding="utf-8")
    assert "# comment" in text
    assert "KEEP=value" in text
    assert "NEW_KEY=new-value" in text
    assert "SECRET=" not in text
