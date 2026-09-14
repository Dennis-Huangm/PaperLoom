from datetime import datetime, timezone
from pathlib import Path

import yaml

from arxiv_ra.config import AppConfig, load_config
from arxiv_ra.models import Author, Paper
from arxiv_ra.profiles import ProfileGenerator, ProfileManager


def _config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "timezone": "Asia/Shanghai",
                "output_dir": "run",
                "discovery": {
                    "interest_description": "AgenticT2I",
                    "arxiv_categories": ["cs.CV"],
                    "positive_keywords": ["agent", "T2I"],
                    "recommendation_count": 10,
                },
                "ranking": {"keyword_weight": 3.0},
                "delivery": {"password_env": "SMTP_PASSWORD"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_default_profile_migrates_only_search_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _config(config_path)
    manager = ProfileManager(tmp_path)

    profile_id = manager.ensure_default(config_path)
    profile = manager.get(profile_id)

    assert profile_id == "agentict2i"
    assert profile["discovery"]["recommendation_count"] == 10
    assert "delivery" not in profile
    assert manager.active_id() == "agentict2i"


def test_active_profile_overrides_search_but_not_global_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _config(config_path)
    manager = ProfileManager(tmp_path)
    manager.ensure_default(config_path)
    manager.save(
        {
            "id": "robotics",
            "name": "Robotics",
            "discovery": {
                "interest_description": "Robot learning",
                "arxiv_categories": ["cs.RO"],
                "recommendation_count": 4,
            },
            "ranking": {"keyword_weight": 9.0},
        }
    )
    manager.activate("robotics")

    config = load_config(config_path)

    assert config.profile_id == "robotics"
    assert config.profile_name == "Robotics"
    assert config.discovery.arxiv_categories == ["cs.RO"]
    assert config.ranking.keyword_weight == 9.0
    assert config.delivery.password_env == "SMTP_PASSWORD"


def test_profile_generator_uses_reference_metadata_without_llm(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _config(config_path)
    config = load_config(config_path)
    generator = ProfileGenerator(config)
    generator.llm.enabled = False
    paper = Paper(
        arxiv_id="2401.00001",
        title="World Models for Robots",
        authors=[Author("Researcher")],
        abstract="A world model for embodied agents.",
        categories=["cs.RO", "cs.LG"],
        primary_category="cs.RO",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/abs/2401.00001",
        pdf_url="https://arxiv.org/pdf/2401.00001",
    )
    monkeypatch.setattr(generator.arxiv, "get", lambda _: paper)

    profile = generator.generate(
        "world-models",
        "World Models",
        ["world model", "embodied agent"],
        [],
        ["2401.00001"],
        recommendation_count=7,
    )

    assert profile["discovery"]["arxiv_categories"] == ["cs.RO", "cs.LG"]
    assert profile["discovery"]["seed_papers"] == ["2401.00001"]
    assert profile["source"]["reference_papers"][0]["title"] == "World Models for Robots"
    assert profile["discovery"]["recommendation_count"] == 7


def test_profile_generator_falls_back_to_alphaxiv_for_reference(monkeypatch) -> None:
    config = AppConfig()
    config.discovery.alphaxiv_fallback_enabled = True
    generator = ProfileGenerator(config)
    generator.llm.enabled = False
    paper = Paper(
        arxiv_id="2609.00006",
        title="Fallback Reference",
        authors=[Author("Researcher")],
        abstract="A visual agent reference.",
        categories=["cs.CV"],
        primary_category="cs.CV",
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        abs_url="https://arxiv.org/abs/2609.00006",
        pdf_url="https://arxiv.org/pdf/2609.00006",
    )
    generator.arxiv = type("Arxiv", (), {"get": lambda *_args: (_ for _ in ()).throw(RuntimeError("429"))})()
    generator.alphaxiv = type(
        "AlphaXiv",
        (),
        {"enabled": True, "lookup": lambda *_args, **_kwargs: paper},
    )()

    profile = generator.generate(
        "fallback",
        "Fallback",
        ["visual agent"],
        [],
        [paper.arxiv_id],
    )

    assert profile["source"]["reference_papers"] == [
        {"arxiv_id": paper.arxiv_id, "title": paper.title}
    ]


def test_profile_generator_preserves_reference_id_when_all_sources_fail() -> None:
    config = AppConfig()
    generator = ProfileGenerator(config)
    generator.llm.enabled = False
    generator.arxiv = type("Arxiv", (), {"get": lambda *_args: (_ for _ in ()).throw(RuntimeError("429"))})()
    generator.alphaxiv = type("AlphaXiv", (), {"enabled": False})()

    profile = generator.generate(
        "offline-reference",
        "Offline Reference",
        ["visual agent"],
        [],
        ["2609.00007"],
    )

    assert profile["discovery"]["seed_papers"] == ["2609.00007"]
    assert profile["source"]["reference_papers"][0]["title"] == ""
