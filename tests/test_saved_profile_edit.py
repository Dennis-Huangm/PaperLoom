import re

from fastapi.testclient import TestClient

from arxiv_ra.config import load_config
from arxiv_ra.profile_plan import new_draft
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.web import create_app


def _client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "output_dir: run\ndiscovery:\n  interest_description: Original\n  arxiv_categories: [cs.CV]\n",
        encoding="utf-8",
    )
    app = create_app(config_path)
    return TestClient(app), ProfileManager(tmp_path), config_path


def _revision(page):
    return re.search(r'name="profile_revision" value="([a-f0-9]{64})"', page).group(1)


def test_edit_saved_versioned_direction_updates_in_place(tmp_path):
    client, profiles, config_path = _client(tmp_path)
    original_active = profiles.active_id()
    draft = new_draft(load_config(config_path), "svg", "SVG research", ["svg"], [], [],
                      "SVG editing", {"arxiv_categories": ["cs.CV"]})
    profiles.save(draft)
    condition = draft["plan"]["conditions"][0]

    with client:
        page = client.get("/profiles/svg/edit")
        assert page.status_code == 200
        assert 'name="name" value="SVG research"' in page.text
        assert 'href="/profiles/svg/edit"' in client.get("/profiles").text
        response = client.post("/profiles/svg/edit", data={
            "profile_id": original_active,
            "profile_revision": _revision(page.text),
            "name": "SVG 生成与编辑",
            "description": "智能体 SVG 编辑与评测",
            "categories": "cs.CV",
            "lookback_days": "30",
            "recommendation_count": "7",
            "max_candidates": "250",
            f"text-{condition['id']}": "vector graphics",
            f"kind-{condition['id']}": "topic",
            f"aliases-{condition['id']}": "vector graphics",
            f"confirmed-{condition['id']}": "on",
        }, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/profiles?updated=svg")

    saved = profiles.get("svg")
    assert saved["name"] == "SVG 生成与编辑"
    assert saved["description"] == "智能体 SVG 编辑与评测"
    assert saved["discovery"]["positive_keywords"] == ["vector graphics"]
    assert saved["discovery"]["recommendation_count"] == 7
    assert saved["discovery"]["lookback_days"] == 30
    assert profiles.active_id() == original_active
    assert len(profiles.list()) == 2


def test_edit_saved_legacy_direction_preserves_other_settings_and_rejects_stale_page(tmp_path):
    client, profiles, _ = _client(tmp_path)
    original_active = profiles.active_id()
    profiles.save({
        "id": "legacy", "name": "Old name", "description": "Old description",
        "discovery": {"interest_description": "Old description", "arxiv_categories": ["cs.CV"],
                      "positive_keywords": ["svg"], "negative_keywords": ["survey"],
                      "arxiv_query_terms": ["vector graphics"], "lookback_days": 15,
                      "max_candidates": 300, "prefilter_count": 20, "recommendation_count": 5},
        "ranking": {"keyword_weight": 9.0}, "version_sync": {"enabled": False},
    })
    with client:
        page = client.get("/profiles/legacy/edit")
        assert page.status_code == 200
        revision = _revision(page.text)
        form = {"profile_id": original_active, "profile_revision": revision,
                "name": "矢量图形研究", "description": "聚焦可编辑矢量图形",
                "keywords": "svg\nvector editing", "negative_keywords": "survey",
                "categories": "cs.CV", "lookback_days": "20",
                "recommendation_count": "6", "max_candidates": "300"}
        assert client.post("/profiles/legacy/edit", data=form, follow_redirects=False).status_code == 303
        stale = client.post("/profiles/legacy/edit", data=form)
        assert stale.status_code == 409

    saved = profiles.get("legacy")
    assert saved["name"] == "矢量图形研究"
    assert saved["discovery"]["positive_keywords"] == ["svg", "vector editing"]
    assert saved["discovery"]["arxiv_query_terms"] == ["vector graphics"]
    assert saved["ranking"] == {"keyword_weight": 9.0}
    assert saved["version_sync"] == {"enabled": False}
    assert profiles.active_id() == original_active


def test_active_direction_rename_is_visible_without_switching_or_changing_failed_edit(tmp_path):
    client, profiles, config_path = _client(tmp_path)
    active = profiles.active_id()
    with client:
        page = client.get(f"/profiles/{active}/edit")
        assert page.status_code == 200
        original = profiles.get(active)
        bad = client.post(f"/profiles/{active}/edit", data={
            "profile_id": active, "profile_revision": _revision(page.text),
            "name": "", "description": "Updated", "categories": "cs.CV",
            "keywords": "robot", "negative_keywords": "",
            "lookback_days": "20", "recommendation_count": "5", "max_candidates": "300",
        })
        assert bad.status_code == 400
        assert profiles.get(active) == original
        good = client.post(f"/profiles/{active}/edit", data={
            "profile_id": active, "profile_revision": _revision(page.text),
            "name": "新的研究方向", "description": "Updated", "categories": "cs.CV",
            "keywords": "robot", "negative_keywords": "",
            "lookback_days": "20", "recommendation_count": "5", "max_candidates": "300",
        }, follow_redirects=False)
        assert good.status_code == 303
        assert "新的研究方向" in client.get("/profiles").text
    config = load_config(config_path)
    assert config.profile_id == active
    assert config.profile_name == "新的研究方向"
