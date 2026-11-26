import threading
import time
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from paperloom.papers.backup import BackupService
from paperloom.profiles.profiles import ProfileManager
from paperloom.utils import write_json
from paperloom.web import create_app


@pytest.fixture
def workspace(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\n", encoding="utf-8")
    profiles = ProfileManager(tmp_path)
    profiles.save({"id": "alpha", "name": "Alpha"})
    profiles.save({"id": "beta", "name": "Beta"})
    profiles.activate("alpha")
    app = create_app(config)
    with TestClient(app) as client:
        yield client, app.state


def mutate(client, target, action, *, active="alpha"):
    return client.post(f"/profiles/{target}/{action}", data={"profile_id": active}, follow_redirects=False)


def test_delete_and_restore_preserve_settings_and_direction_records(workspace):
    client, state = workspace
    data = state.output_root
    reading = data / "reading-state-beta.json"
    overview = data / "direction-overviews" / "beta" / "state.json"
    write_json(reading, {"version": 1, "library": {}, "feedback": {}})
    write_json(overview, {"articles": {}})
    records = {path: path.read_bytes() for path in (reading, overview)}
    original = state.profiles.get("beta")
    assert mutate(client, "alpha", "delete").status_code == 409

    response = mutate(client, "beta", "delete")
    assert response.status_code == 303
    page = BeautifulSoup(client.get(response.headers["location"]).text, "html.parser")
    assert page.select_one('#saved-profiles [action="/profiles/beta/delete"]') is None
    assert page.select_one('#deleted-profiles[open] [action="/profiles/beta/restore"]')
    assert page.select_one('#saved-profiles [action="/profiles/alpha/delete"] button[disabled]')
    assert client.get("/profiles/beta/edit").status_code == 404
    assert client.get("/profiles/beta/overview?source=saved").status_code == 404
    assert mutate(client, "beta", "activate").status_code == 404
    assert state.profiles.unique_id("Beta") == "beta-2"
    assert mutate(client, "alpha", "delete").status_code == 409

    assert mutate(client, "beta", "restore").status_code == 303
    assert state.profiles.get("beta") == original
    assert state.profiles.active_id() == "alpha"
    assert state.profiles.deleted_ids() == set()
    assert {path: path.read_bytes() for path in records} == records


def test_restored_schedule_keeps_settings_and_history_but_stays_off(workspace):
    client, state = workspace
    scheduler = state.scheduler
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    scheduler.save_profile("beta", {"enabled": True, "time": "09:30", "timezone": "UTC",
                                    "weekdays": [1, 2], "send_email": True}, now=start)
    assert mutate(client, "beta", "delete").status_code == 303
    scheduler.set_enabled(True, now=start)
    history = {"beta:2020-01-01": {"profile_id": "beta", "date": "2020-01-01", "status": "succeeded"}}
    write_json(scheduler.state_file, {"version": 1, "occurrences": history})
    due = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)
    assert scheduler.tick(now=due) == []
    assert scheduler.view(now=due)["plans"] == []

    assert mutate(client, "beta", "restore").status_code == 303
    plan, = scheduler.view(now=due)["plans"]
    assert (plan["enabled"], plan["time"], plan["weekdays"], plan["send_email"]) == (False, "09:30", [1, 2], True)
    assert scheduler.state()["occurrences"] == history
    assert scheduler.tick(now=due) == []


def test_busy_profile_cannot_be_deleted_and_stale_jobs_cannot_start(workspace):
    client, state = workspace
    started = threading.Event()
    release = threading.Event()

    def work():
        started.set()
        release.wait(timeout=5)

    job = state.jobs.submit("digest", "Beta digest", work, profile_id="beta")
    try:
        assert started.wait(timeout=3)
        response = mutate(client, "beta", "delete")
        assert response.status_code == 409
        assert "任务" in response.json()["detail"]
        assert not state.profiles.is_deleted("beta")
        state.jobs.cancel(job.id)
        assert mutate(client, "beta", "delete").status_code == 409
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while state.jobs.active_for("digest", "beta"):
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert mutate(client, "beta", "delete").status_code == 303
    with pytest.raises(ValueError, match="研究方向已删除"):
        state.jobs.submit("digest", "Stale request", work, profile_id="beta")


def test_catalogs_hide_only_attempts_from_the_deleted_direction(workspace):
    client, state = workspace

    def seed(folder, aid, profile, version):
        report = state.output_root / "2026-11-23" / "reports" / folder / "report.html"
        write_json(report.with_name("metadata.json"), {
            "profile_id": profile, "paper": {"arxiv_id": aid, "version": version, "title": folder},
        })
        report.write_text("report", encoding="utf-8")
        report.with_suffix(".md").write_text("report", encoding="utf-8")
        return report

    seed("Alpha shared paper", "2411.00001", "alpha", 1)
    archived_shared = seed("Beta shared paper", "2411.00001", "beta", 2)
    archived_only = seed("Beta only paper", "2411.00002", "beta", 1)
    seed("Legacy paper", "2411.00003", "", 1)
    seed("Historical paper", "2411.00004", "unknown", 1)
    assert len(client.get("/api/reports").json()["reports"]) == 4
    created = client.post("/api/reading/sessions", json={
        "mode": "workspace", "report_ids": [archived_only.relative_to(state.output_root).as_posix()],
    })
    assert created.status_code == 200

    assert mutate(client, "beta", "delete").status_code == 303
    catalog = {row["arxiv_id"]: row for row in client.get("/api/reports").json()["reports"]}
    assert set(catalog) == {"2411.00001", "2411.00003", "2411.00004"}
    shared = catalog["2411.00001"]
    assert shared["version"] == 1
    assert shared["directions"] == [{"id": "alpha", "name": "Alpha"}]
    assert "Beta only paper" not in client.get("/reports").text
    assert "Beta shared paper" not in client.get("/").text
    reading = client.get("/api/reading/reports").json()["reports"]
    assert {row["paper"]["arxiv_id"] for row in reading} == set(catalog)
    assert all(row["paper"]["version"] == 1 for row in reading)
    session_id = created.json()["id"]
    session = client.get(f"/api/reading/sessions/{session_id}").json()
    assert session["references"][0]["missing"] is False

    form = next(form for form in BeautifulSoup(client.get("/reports").text, "html.parser").select(".report-delete-form")
                if form.select_one('[name="report_id"]')["value"] == shared["report_id"])
    assert client.post("/reports/delete-batch", data={"report_ids": [shared["report_id"]],
                       "delete_tokens": form.select_one('[name="delete_tokens"]')["value"]},
                       follow_redirects=False).status_code == 303
    assert archived_shared.is_file() and archived_only.is_file()
    assert mutate(client, "beta", "restore").status_code == 303
    assert "Beta only paper" in client.get("/reports").text
    assert "Beta shared paper" in client.get("/reports").text


def test_backup_restore_preserves_the_direction_lifecycle(workspace):
    client, state = workspace
    backups = BackupService(state.config_path)
    live = backups.create()
    plan = backups.preview(live, "example")
    backups.restore(live, "example", token=plan["token"])
    restored = ProfileManager(backups.target("example"))
    assert restored.get("beta")["name"] == "Beta"

    assert mutate(client, "beta", "delete").status_code == 303
    overview = state.output_root / "direction-overviews" / "beta" / "state.json"
    write_json(overview, {"articles": {}})
    archived = backups.create(reports=True)
    for mode in ("keep", "replace"):
        plan = backups.preview(archived, "example", mode)
        backups.restore(archived, "example", token=plan["token"], mode=mode)
        assert restored.is_deleted("beta") is (mode == "replace")
    assert [profile["id"] for profile in restored.list()] == ["alpha"]
    assert restored.active_id() == "alpha"
    assert (backups.target("example") / "run" / overview.relative_to(state.output_root)).is_file()

    plan = backups.preview(live, "example", "replace")
    backups.restore(live, "example", token=plan["token"], mode="replace")
    assert not restored.is_deleted("beta")
    assert restored.get("beta")["name"] == "Beta"
