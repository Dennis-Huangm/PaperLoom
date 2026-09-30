import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from arxiv_ra.storage import clear_recommendations, read_recommendations
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.web import create_app
from arxiv_ra.web_catalog import recommendation_history
from arxiv_ra.models import Paper


def item(profile="alpha"):
    return {"profile_id": profile, "paper": {"arxiv_id": "2407.05600", "version": 1}}


def test_clear_day_preserves_other_days_profiles_and_personal_data(tmp_path):
    day = tmp_path / "2026-09-30"
    write_json(day / "recommendations-alpha.json", [item()])
    write_json(day / "recommendations-beta.json", [item("beta")])
    write_json(day / "recommendations.json", [item()])
    write_json(tmp_path / "2026-09-29/recommendations-alpha.json", [item()])
    preserved = [day / "reports/paper/metadata.json", tmp_path / "state-alpha.json",
                 tmp_path / "reading-alpha.json", day / "batches/beta/batch/recommendations.json"]
    for path in preserved:
        write_json(path, {"preserve": True})
    write_json(day / "batches/alpha/batch/recommendations.json", [item()])
    for name in ["index.html", "index-alpha.html", "no-new-alpha.html", "index-beta.html"]:
        (day / name).write_text("page")
    clear_recommendations(tmp_path, "2026-09-30", "alpha")
    assert read_recommendations(tmp_path, "2026-09-30", "alpha") == []
    assert read_recommendations(tmp_path, "2026-09-30", "beta") == [item("beta")]
    assert recommendation_history(tmp_path, "alpha") == [{"date": "2026-09-29", "count": 1}]
    assert all(read_json(path) == {"preserve": True} for path in preserved)
    assert not (day / "batches/alpha").exists()
    assert not (day / "index-alpha.html").exists()
    assert not (day / "index.html").exists()
    assert (day / "index-beta.html").exists()
    clear_recommendations(tmp_path, "2026-09-30", "alpha")


@pytest.mark.parametrize("legacy", [[item("beta")], [item(), item("beta")], [item("")]])
def test_legacy_fallback_cannot_restore_cleared_profile(tmp_path, legacy):
    path = tmp_path / "2026-09-30/recommendations.json"
    write_json(path, legacy)
    clear_recommendations(tmp_path, "2026-09-30", "alpha")
    assert read_recommendations(tmp_path, "2026-09-30", "alpha") == []
    expected = [row for row in legacy if row["profile_id"] != "alpha"]
    assert read_json(path) == expected
    assert read_recommendations(tmp_path, "2026-09-30", "beta") == expected


@pytest.mark.parametrize("day,profile", [("../outside", "alpha"), ("2026-02-30", "alpha"),
                                        ("2026-09-30", "../outside")])
def test_invalid_paths_rejected_before_writing(tmp_path, day, profile):
    with pytest.raises(ValueError):
        clear_recommendations(tmp_path, day, profile)
    assert list(tmp_path.iterdir()) == []


def test_clear_route_validates_profile_and_date_and_redirects(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
    app = create_app(config)
    monkeypatch.setattr("arxiv_ra.web.localize_abstracts", lambda *a, **kw: None)
    row = item()
    now = datetime.now(timezone.utc)
    row["paper"] = Paper("2407.05600", "Test", [], "Abstract", ["cs.AI"], "cs.AI",
                         now, now, "https://arxiv.org/abs/2407.05600",
                         "https://arxiv.org/pdf/2407.05600").to_dict()
    row["verified"] = {}
    path = tmp_path / "run/2026-09-30/recommendations-alpha.json"
    write_json(path, [row])
    with TestClient(app) as client:
        page = client.get("/?date=2026-09-30")
        assert page.status_code == 200
        assert 'action="/recommendations/clear"' in page.text
        assert 'name="date_label" value="2026-09-30"' in page.text
        data = {"profile_id": "alpha", "date_label": "2026-09-30"}
        assert client.post("/recommendations/clear", data={**data, "profile_id": "beta"}).status_code == 409
        assert client.post("/recommendations/clear", data={**data, "date_label": "2026-02-30"}).status_code == 400
        with monkeypatch.context() as patch:
            patch.setattr("arxiv_ra.web_jobs.JobManager.active_for", lambda *args: True)
            assert client.post("/recommendations/clear", data=data).status_code == 409
            assert read_json(path) == [row]
        response = client.post("/recommendations/clear", data=data, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert read_json(path) == []
        assert "还没有推荐结果" in client.get("/").text
