from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arxiv_ra.config import AppConfig, WeeklyConfig
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.weekly import WeeklySynthesizer


def test_weekly_fallback_collects_recommendations_and_feedback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    output = tmp_path / "run"
    date_dir = output / "2026-08-17"
    write_json(
        date_dir / "recommendations.json",
        [
            {
                "profile_id": "agentict2i",
                "paper": {
                    "arxiv_id": "2407.05600",
                    "title": "GenArtist",
                    "abstract": "Agentic image generation.",
                    "primary_category": "cs.CV",
                    "abs_url": "https://arxiv.org/abs/2407.05600",
                    "final_score": 9.5,
                    "recommendation_reason": "Relevant",
                },
                "verified": {"venue": "NeurIPS"},
            },
            {
                "profile_id": "another-profile",
                "paper": {
                    "arxiv_id": "2501.00001",
                    "title": "Other Profile Paper",
                    "primary_category": "cs.RO",
                    "abs_url": "https://arxiv.org/abs/2501.00001",
                    "final_score": 10,
                },
                "verified": {},
            },
        ],
    )
    PaperLibraryStore(output, "agentict2i").add(
        {
            "paper": {
                "arxiv_id": "2407.05600",
                "title": "GenArtist",
                "abstract": "",
                "primary_category": "cs.CV",
                "abs_url": "https://arxiv.org/abs/2407.05600",
            }
        },
        "AgenticT2I",
    )
    config = AppConfig(
        output_dir=str(output),
        profile_id="agentict2i",
        profile_name="AgenticT2I",
        weekly=WeeklyConfig(days=7),
    )
    now = datetime(2026, 8, 19, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

    report = WeeklySynthesizer(config, tmp_path).generate(now)

    markdown = report.with_suffix(".md").read_text(encoding="utf-8")
    metadata = read_json(report.parent / "metadata.json", {})
    assert report.exists()
    assert "GenArtist" in markdown
    assert "Other Profile Paper" not in markdown
    assert "本周必读" in markdown
    assert metadata["paper_count"] == 1
    assert metadata["papers"][0]["preference"] == "relevant"
