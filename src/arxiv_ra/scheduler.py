"""Explicit per-profile daily slots dispatched through the durable GUI queue."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import logging
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import load_config
from .job_requests import capture_request
from .pipeline import DailyPipeline
from .reading_state import _locked
from .utils import read_json, write_json


GRACE_MINUTES = 120
WEEKDAYS = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}
STATUS_LABELS = {"reserved": "已登记", "queued": "已排队", "running": "运行中", "cancelling": "取消中",
                 "cancelled": "已取消", "interrupted": "已中断", "failed": "失败", "missed": "错过时段",
                 "skipped": "跳过", "succeeded": "成功", "succeeded_with_warnings": "完成并有提示"}


def utc_now():
    return datetime.now(timezone.utc)


def checked_now(now):
    now = now or utc_now()
    if now.tzinfo is None:
        raise ValueError("调度时间必须带时区")
    return now.astimezone(timezone.utc)


def validate(spec):
    if not isinstance(spec, dict) or type(spec.get("enabled")) is not bool or type(spec.get("send_email")) is not bool:
        raise ValueError("无效的调度开关")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(spec.get("time", ""))):
        raise ValueError("运行时间必须为 HH:MM")
    try:
        ZoneInfo(spec["timezone"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("无效的 IANA 时区") from exc
    days = spec.get("weekdays")
    if not isinstance(days, list) or not days or any(type(day) is not int or day not in WEEKDAYS for day in days):
        raise ValueError("至少选择一个有效星期")
    return {"enabled": spec["enabled"], "send_email": spec["send_email"], "time": spec["time"],
            "timezone": spec["timezone"], "weekdays": sorted(set(days))}


def slot_for(day, spec):
    hour, minute = map(int, spec["time"].split(":"))
    zone = ZoneInfo(spec["timezone"])
    slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
    # Spring-forward gaps have no matching local wall time. Fall-back selects
    # the first occurrence; profile/local-date identity prevents a second run.
    if slot.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != slot.replace(tzinfo=None):
        return None
    return slot.astimezone(timezone.utc)


class ProfileScheduler:
    def __init__(self, config_path: Path, output_root: Path, jobs):
        self.config_path = config_path.resolve()
        self.project = self.config_path.parent
        self.output = output_root.resolve()
        self.config_file = self.output / "schedule-config.json"
        self.state_file = self.output / "schedule-state.json"
        self.lock_file = self.output / "schedule.lock"
        self.jobs = jobs
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = ""

    def settings(self):
        value = read_json(self.config_file, {"version": 1, "enabled": False, "enabled_at": "", "profiles": {}})
        if (not isinstance(value, dict) or value.get("version") != 1
                or type(value.get("enabled")) is not bool or not isinstance(value.get("profiles"), dict)):
            raise ValueError("调度配置损坏或版本不受支持，原文件已保留")
        return value

    def state(self):
        value = read_json(self.state_file, {"version": 1, "occurrences": {}})
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("occurrences"), dict):
            raise ValueError("调度台账损坏或版本不受支持，已停止自动调度；原文件已保留")
        return value

    def _save_state(self, state):
        if read_json(self.state_file, None) != state:
            write_json(self.state_file, state)

    def sync_state(self):
        with _locked(self.lock_file):
            state = self.state()
            self._reconcile(state)
            self._save_state(state)

    def restore_paused(self):
        marker = read_json(self.project / ".paperloom-restore.json", None)
        return marker is not None and not marker.get("scheduling_confirmed", False)

    def save_profile(self, profile_id, spec, *, now=None):
        load_config(self.config_path, profile_id=profile_id)
        spec = {**validate(spec), "effective_from": checked_now(now).isoformat()}
        with _locked(self.lock_file):
            settings = self.settings()
            settings["profiles"][profile_id] = spec
            write_json(self.config_file, settings)

    def set_enabled(self, enabled: bool, *, now=None):
        if type(enabled) is not bool:
            raise ValueError("无效的总开关")
        with _locked(self.lock_file):
            settings = self.settings()
            settings["enabled"] = enabled
            settings["enabled_at"] = checked_now(now).isoformat()
            write_json(self.config_file, settings)
            marker_path = self.project / ".paperloom-restore.json"
            if enabled and marker_path.is_file():
                marker = read_json(marker_path)
                write_json(marker_path, {**marker, "scheduling_confirmed": True})

    def _reconcile(self, state):
        if self.jobs is None:
            return
        for entry in state["occurrences"].values():
            identity = entry.get("identity")
            if not identity:
                continue
            job = self.jobs.find_identity(identity)
            if job:
                entry.update(job_id=job.id, status=job.status, detail=job.detail, result_url=job.result_url)
            elif entry["status"] in {"reserved", "queued", "running", "cancelling"}:
                entry.update(status="interrupted", detail="登记后未能确认任务已提交；不自动重跑，请从任务入口手动执行。")

    def tick(self, *, now=None):
        now = checked_now(now)
        submitted = []
        with _locked(self.lock_file):
            settings = self.settings()
            state = self.state()
            self._reconcile(state)
            if not settings["enabled"] or self.restore_paused():
                if self.state_file.exists():
                    self._save_state(state)
                return submitted
            for profile_id, saved in settings["profiles"].items():
                entry = None
                key = None
                if self.stop_event.is_set():
                    break
                # One invalid/deleted profile must not block the other directions.
                try:
                    spec = validate(saved)
                    state.setdefault("errors", {}).pop(profile_id, None)
                    if not spec["enabled"]:
                        continue
                    local = now.astimezone(ZoneInfo(spec["timezone"]))
                    if local.weekday() not in spec["weekdays"]:
                        continue
                    key = f"{profile_id}:{local.date().isoformat()}"
                    if key in state["occurrences"]:
                        continue
                    slot = slot_for(local.date(), spec)
                    effective = max(datetime.fromisoformat(saved["effective_from"]), datetime.fromisoformat(settings["enabled_at"]))
                    if slot is None:
                        if local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc) < effective:
                            continue
                        state["occurrences"][key] = {"profile_id": profile_id, "date": str(local.date()), "status": "skipped",
                            "scheduled_at": f"{local.date()} {spec['time']} {spec['timezone']}", "detail": "夏令时跳时，此本地时刻不存在。"}
                        continue
                    if slot < effective or now < slot:
                        continue
                    identity = f"schedule:{key}"
                    entry = {"profile_id": profile_id, "date": str(local.date()), "scheduled_at": slot.isoformat(),
                             "identity": identity, "status": "reserved", "detail": "已登记待提交"}
                    existing = self.jobs.find_identity(identity)
                    if existing:
                        entry.update(job_id=existing.id, status=existing.status, detail=existing.detail,
                                     result_url=existing.result_url)
                        state["occurrences"][key] = entry
                        continue
                    if now - slot > timedelta(minutes=GRACE_MINUTES):
                        entry.update(status="missed", detail="已超过两小时补触发窗口，不补跑过去的时段。")
                        state["occurrences"][key] = entry
                        continue
                    # Delay within the grace window if a manual/automatic digest
                    # of the same profile is still active.
                    if self.jobs.active_for("digest", profile_id):
                        continue
                    config = load_config(self.config_path, profile_id=profile_id)
                    output = Path(config.output_dir)
                    if (output if output.is_absolute() else self.project / output).resolve() != self.output:
                        raise ValueError("输出目录已更改，请重启 GUI 或调度进程后再执行")
                    config.timezone = spec["timezone"]
                    config.output_dir = str(self.output)
                    # Persist intent before dispatch. A crash here consumes the
                    # slot, rather than risking an ambiguous email replay.
                    state["occurrences"][key] = entry
                    self._save_state(state)
                    deliver = spec["send_email"] and config.delivery.email_enabled
                    def run(config=config, deliver=deliver):
                        with DailyPipeline(config, self.project) as pipeline:
                            return pipeline.run(force=False, demo=False, deliver=deliver)
                    job = self.jobs.submit("digest", f"定时 · {config.profile_name} · {local.date()} {spec['time']} {spec['timezone']}",
                        run, identity=identity, profile_id=profile_id,
                        request=capture_request("digest", config, self.project, force=False, send_email=deliver))
                    entry.update(job_id=job.id, status=job.status, detail=job.detail)
                    self._save_state(state)
                    submitted.append(job.id)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    # Error rows are separate from day identities if a timezone
                    # is invalid, and remain visible until the plan is fixed.
                    state.setdefault("errors", {})[profile_id] = str(exc)
                    if entry is not None and state["occurrences"].get(key) is entry and entry["status"] == "reserved":
                        entry.update(status="failed", detail=str(exc))
                    continue
            self._save_state(state)
        return submitted

    def view(self, *, now=None):
        now = checked_now(now)
        with _locked(self.lock_file):
            settings, state = self.settings(), self.state()
            self._reconcile(state)
        rows = []
        for profile_id, saved in settings["profiles"].items():
            row = {"profile_id": profile_id, **saved, "next_run": ""}
            try:
                spec = validate(saved)
                row["name"] = load_config(self.config_path, profile_id=profile_id).profile_name
                effective = max(datetime.fromisoformat(saved["effective_from"]),
                                datetime.fromisoformat(settings["enabled_at"]) if settings["enabled_at"] else now)
                if spec["enabled"] and settings["enabled"] and not self.restore_paused():
                    local = now.astimezone(ZoneInfo(spec["timezone"]))
                    for offset in range(9):
                        day = local.date() + timedelta(days=offset)
                        slot = slot_for(day, spec)
                        if (day.weekday() in spec["weekdays"] and slot is not None and slot >= effective
                                and slot + timedelta(minutes=GRACE_MINUTES) >= now
                                and f"{profile_id}:{day}" not in state["occurrences"]):
                            row["next_run"] = slot.astimezone(ZoneInfo(spec["timezone"])).isoformat(timespec="minutes")
                            break
            except (ValueError, KeyError, TypeError) as exc:
                row["error"] = str(exc)
            rows.append(row)
        history = sorted(state["occurrences"].values(), key=lambda item: (item["date"], item.get("scheduled_at", "")), reverse=True)[:100]
        return {"enabled": settings["enabled"], "restored_paused": self.restore_paused(), "plans": rows,
                "history": history, "errors": state.get("errors", {}), "last_error": self.last_error}

    def start(self):
        def poll():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                    self.last_error = ""
                except Exception as exc:
                    if self.last_error != str(exc):
                        logging.getLogger(__name__).error("自动调度暂时停止：%s", exc)
                    self.last_error = str(exc)
                self.stop_event.wait(30)
        self.thread = threading.Thread(target=poll, name="paperloom-scheduler", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
