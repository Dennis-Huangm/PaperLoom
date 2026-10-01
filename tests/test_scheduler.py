from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from arxiv_ra.backup import BackupService
from arxiv_ra.cli import main
from arxiv_ra.config import load_config
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.scheduler import ProfileScheduler
from arxiv_ra.utils import write_json, read_json
from arxiv_ra.web import create_app
from arxiv_ra.web_jobs import JobManager


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def spec(time="08:00", zone="Asia/Shanghai", email=False, days=None):
    return {"enabled": True, "time": time, "timezone": zone, "send_email": email,
            "weekdays": list(range(7)) if days is None else days}


def setup(root):
    config = root / "config.yaml"
    config.write_text("output_dir: run\ndelivery:\n  email_enabled: true\ndiscovery:\n  interest_description: Global\n", encoding="utf-8")
    profiles = ProfileManager(root)
    for name in ("alpha", "beta"):
        profiles.save({"id": name, "name": name.title(), "discovery": {"interest_description": name},
                       "version_sync": {"enabled": name == "beta"}})
    profiles.activate("beta")
    return config, profiles


def drain(jobs):
    deadline = time.monotonic() + 5
    while jobs.active_count or jobs.pending:
        assert time.monotonic() < deadline
        time.sleep(.01)


@pytest.fixture
def env(tmp_path, monkeypatch):
    config, profiles = setup(tmp_path)
    calls = []
    class Pipeline:
        def __init__(self, config, root):
            self.config, self.root = config, root
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def run(self, **kwargs):
            calls.append((self.config, kwargs))
            path = Path(self.config.output_dir) / f"{self.config.profile_id}.html"
            path.write_text("fake digest")
            return path
    monkeypatch.setattr("arxiv_ra.pipeline.DailyPipeline", Pipeline)
    jobs = JobManager(tmp_path / "run", 2)
    scheduler = ProfileScheduler(config, tmp_path / "run", jobs)
    yield SimpleNamespace(root=tmp_path, config=config, profiles=profiles, calls=calls, jobs=jobs, scheduler=scheduler)
    scheduler.close()
    drain(jobs)
    jobs.close()


def enable(env, specifications=None, when="2026-09-24T00:00:00"):
    for profile, value in (specifications or {"alpha": spec()}).items():
        env.scheduler.save_profile(profile, value, now=at(when))
    env.scheduler.set_enabled(True, now=at(when))


def test_explicit_config_loading_does_not_mutate_active_profile(env):
    config = load_config(env.config, profile_id="alpha")
    assert config.profile_id == "alpha" and config.discovery.interest_description == "alpha"
    assert config.version_sync.enabled is False
    assert env.profiles.active_id() == "beta"
    with pytest.raises(ValueError):
        load_config(env.config, profile_id="missing")
    with pytest.raises(ValueError):
        load_config(env.config, profile_id="../beta")


def test_distinct_timezones_dispatch_pinned_profiles_and_email_scope(env):
    enable(env, {"alpha": spec(email=False), "beta": spec("01:00", "UTC", email=True)})
    assert len(env.scheduler.tick(now=at("2026-09-25T00:30:00"))) == 1
    drain(env.jobs)
    assert env.calls[0][0].profile_id == "alpha" and env.calls[0][1]["deliver"] is False
    env.profiles.activate("alpha")
    assert len(env.scheduler.tick(now=at("2026-09-25T01:30:00"))) == 1
    drain(env.jobs)
    assert env.calls[1][0].profile_id == "beta" and env.calls[1][0].timezone == "UTC"
    assert env.calls[1][1]["deliver"] is True and env.profiles.active_id() == "alpha"
    assert not env.scheduler.tick(now=at("2026-09-25T01:40:00"))
    env.scheduler.sync_state()
    assert all(row["status"] == "succeeded" for row in env.scheduler.view()["history"])


def test_overlapping_ticks_and_changed_plan_do_not_repeat_a_day(env):
    enable(env)
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: env.scheduler.tick(now=at("2026-09-25T00:30:00")), range(2)))
    drain(env.jobs)
    assert sum(map(len, results)) == 1 and len(env.calls) == 1
    env.scheduler.save_profile("alpha", spec("09:00"), now=at("2026-09-25T00:40:00"))
    assert not env.scheduler.tick(now=at("2026-09-25T01:05:00"))
    # Recover a lost linkage/state from the durable task's immutable identity.
    env.scheduler.state_file.unlink()
    assert not env.scheduler.tick(now=at("2026-09-25T01:10:00"))
    assert env.scheduler.state()["occurrences"]["alpha:2026-09-25"]["job_id"]


def test_late_enable_weekdays_and_missed_slots(env):
    enable(env, when="2026-09-25T00:30:00")
    assert not env.scheduler.tick(now=at("2026-09-25T00:31:00"))
    assert not env.scheduler.state()["occurrences"]
    assert env.scheduler.view(now=at("2026-09-25T00:31:00"))["plans"][0]["next_run"].startswith("2026-09-26")
    assert not env.scheduler.tick(now=at("2026-09-26T03:00:00"))
    assert env.scheduler.state()["occurrences"]["alpha:2026-09-26"]["status"] == "missed"
    env.scheduler.save_profile("alpha", spec(days=[0]), now=at("2026-09-26T04:00:00"))
    assert not env.scheduler.tick(now=at("2026-09-27T00:10:00"))
    assert env.scheduler.view(now=at("2026-09-27T00:10:00"))["plans"][0]["next_run"].startswith("2026-09-28")


def test_manual_digest_busy_delays_then_dispatches(env):
    enable(env)
    release = threading.Event()
    env.jobs.submit("digest", "manual fixture", lambda: (release.wait(5), None)[1], profile_id="alpha")
    try:
        assert not env.scheduler.tick(now=at("2026-09-25T00:15:00"))
        assert not env.scheduler.state()["occurrences"]
    finally:
        release.set()
    drain(env.jobs)
    assert len(env.scheduler.tick(now=at("2026-09-25T00:20:00"))) == 1


def test_reserved_interruption_is_not_replayed_and_job_link_is_repaired(env, monkeypatch):
    enable(env)
    write_json(env.scheduler.state_file, {"version": 1, "occurrences": {"alpha:2026-09-25": {
        "profile_id": "alpha", "date": "2026-09-25", "identity": "schedule:alpha:2026-09-25", "status": "reserved"}}})
    assert not env.scheduler.tick(now=at("2026-09-25T00:10:00"))
    assert env.scheduler.state()["occurrences"]["alpha:2026-09-25"]["status"] == "interrupted"
    save = env.scheduler._save_state
    def fail_link(state):
        if state["occurrences"].get("alpha:2026-09-26", {}).get("job_id"):
            raise OSError("fixture disk failure after dispatch")
        save(state)
    monkeypatch.setattr(env.scheduler, "_save_state", fail_link)
    with pytest.raises(OSError):
        env.scheduler.tick(now=at("2026-09-26T00:10:00"))
    drain(env.jobs)
    monkeypatch.setattr(env.scheduler, "_save_state", save)
    assert not env.scheduler.tick(now=at("2026-09-26T00:20:00"))
    assert len(env.calls) == 1 and env.scheduler.state()["occurrences"]["alpha:2026-09-26"]["status"] == "succeeded"


def test_failed_registration_never_starts_pipeline(env, monkeypatch):
    enable(env)
    monkeypatch.setattr(env.scheduler, "_save_state", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        env.scheduler.tick(now=at("2026-09-25T00:10:00"))
    assert not env.calls and not env.jobs.recent()


def test_dst_gap_skips_and_fold_runs_once(env):
    enable(env, {"alpha": spec("02:30", "America/New_York")}, when="2026-03-01T00:00:00")
    assert not env.scheduler.tick(now=at("2026-03-08T07:10:00"))
    assert env.scheduler.state()["occurrences"]["alpha:2026-03-08"]["status"] == "skipped"
    env.scheduler.save_profile("alpha", spec("01:30", "America/New_York"), now=at("2026-10-01T00:00:00"))
    assert len(env.scheduler.tick(now=at("2026-11-01T05:40:00"))) == 1
    drain(env.jobs)
    assert not env.scheduler.tick(now=at("2026-11-01T06:40:00"))
    assert len(env.calls) == 1


def test_invalid_profile_does_not_block_others_and_corrupt_state_stops_all(env):
    enable(env, {"alpha": spec(), "beta": spec()})
    (env.root / "profiles/alpha.yaml").unlink()
    assert len(env.scheduler.tick(now=at("2026-09-25T00:10:00"))) == 1
    drain(env.jobs)
    assert env.calls[0][0].profile_id == "beta" and "alpha" in env.scheduler.state()["errors"]
    env.scheduler.state_file.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        env.scheduler.tick(now=at("2026-09-26T00:10:00"))
    assert env.scheduler.state_file.read_text() == "broken" and len(env.calls) == 1


def test_backup_restoration_requires_explicit_schedule_reenable(env):
    enable(env)
    backup = BackupService(env.config)
    archive = backup.create()
    plan = backup.preview(archive, "copy")
    target = Path(backup.restore(archive, "copy", token=plan["token"])["target"])
    restored_jobs = JobManager(target / "run")
    restored = ProfileScheduler(target / "config.yaml", target / "run", restored_jobs)
    try:
        assert restored.restore_paused()
        assert not restored.tick(now=at("2026-09-25T00:15:00")) and not env.calls
        restored.set_enabled(True, now=at("2026-09-25T00:16:00"))
        assert not restored.restore_paused()
        assert not restored.tick(now=at("2026-09-25T00:17:00"))
        assert len(restored.tick(now=at("2026-09-26T00:01:00"))) == 1
        drain(restored_jobs)
    finally:
        restored_jobs.close()


@pytest.mark.parametrize("change", [{"time": "25:00"}, {"timezone": "Invalid/Zone"}, {"weekdays": []}, {"weekdays": [7]}, {"send_email": "true"}])
def test_invalid_plan_rejected(env, change):
    with pytest.raises(ValueError):
        env.scheduler.save_profile("alpha", {**spec(), **change})


def test_gui_routes_profile_boundary_and_private_state(tmp_path):
    config, profiles = setup(tmp_path)
    with TestClient(create_app(config)) as client:
        assert "多方向调度" in client.get("/profiles").text
        assert "自动调度总开关" in client.get("/schedules").text
        response = client.post("/schedules/profile/alpha", data={"profile_id": "beta", "enabled": "true",
            "time": "08:00", "timezone": "Asia/Shanghai", "weekdays": [0, 2, 4]})
        assert response.status_code == 200
        assert "Alpha" in response.text and profiles.active_id() == "beta"
        assert client.post("/schedules/enabled", data={"profile_id": "alpha", "enabled": "true"}).status_code == 409
        for name in ("schedule-config.json", "SCHEDULE-CONFIG.JSON", "schedule-state.json"):
            assert client.get("/artifacts/" + name).status_code == 404
        assert client.get("/schedules").headers["cache-control"] == "no-store"


def test_cli_once_respects_existing_queue_and_does_not_enable_plans(env, capsys):
    main(["--config", str(env.config), "schedule", "--once"])
    assert "未启动调度" in capsys.readouterr().out and not env.calls
    main(["--config", str(env.config), "schedule", "--status"])
    result = json.loads(capsys.readouterr().out)
    assert result["enabled"] is False


def test_standalone_cli_once_runs_due_profiles_and_persists_final_status(env, capsys, monkeypatch):
    enable(env)
    env.jobs.close()
    monkeypatch.setattr("arxiv_ra.scheduler.utc_now", lambda: at("2026-09-25T00:05:00"))
    main(["--config", str(env.config), "schedule", "--once"])
    result = json.loads(capsys.readouterr().out)
    assert len(result["submitted"]) == 1 and result["schedule"]["history"][0]["status"] == "succeeded"
    main(["--config", str(env.config), "schedule", "--once"])
    assert json.loads(capsys.readouterr().out)["submitted"] == [] and len(env.calls) == 1


def test_disabled_and_unchanged_ticks_do_not_rewrite_ledger(env):
    enable(env)
    env.scheduler.tick(now=at("2026-09-25T03:00:00"))
    before = env.scheduler.state_file.stat().st_mtime_ns
    env.scheduler.tick(now=at("2026-09-25T03:10:00"))
    assert env.scheduler.state_file.stat().st_mtime_ns == before
    env.scheduler.set_enabled(False, now=at("2026-09-25T03:15:00"))
    assert not env.scheduler.tick(now=at("2026-09-26T00:05:00"))
    assert env.scheduler.state_file.stat().st_mtime_ns == before
