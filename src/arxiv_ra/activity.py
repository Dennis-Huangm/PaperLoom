"""Local research activity, bounded by timezone-aware event time, not file mtime."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import html
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .reading_state import READING_STATUSES, ReadingStateStore
from .utils import read_json


LABELS = {"saved": "新增收藏", "removed": "移出收藏", "restored": "恢复收藏",
          "reading": "阅读进度", "notes": "个人笔记更新", "report": "生成阅读报告",
          "version": "发现修订更新", "library_version": "收藏版本更新"}


def literal(value: object) -> str:
    """Render local/paper text without turning it into Markdown instructions."""
    # Entities remain literal during Markdown/math tokenization, including
    # dollars and pre-existing LaTeX delimiters from copied personal notes.
    return re.sub(r"[\\`*_{}\[\]()#$+.!|>-]",
                  lambda match: f"&#{ord(match[0])};" if match[0] in "\\[]()$" else "\\" + match[0],
                  html.escape(str(value)))


def timestamp(value, zone) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=zone) if result.tzinfo is None else result.astimezone(zone)
    except (ValueError, TypeError):
        return None


def safe_json(path: Path, default=None):
    try:
        return read_json(path, default)
    except (OSError, ValueError):
        return default


def collect_activity(output: Path, profile_id: str, now: datetime, days: int,
                     state: dict | None = None) -> dict:
    zone = now.tzinfo or ZoneInfo("UTC")
    now = now.replace(tzinfo=zone) if now.tzinfo is None else now
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(1, days) - 1)
    state = state if state is not None else ReadingStateStore(output, profile_id).snapshot()
    events = []

    def add(event):
        at = timestamp(event.get("at"), zone)
        if at and start <= at <= now and (event.get("paper") or {}).get("arxiv_id"):
            events.append({**event, "at": at.isoformat()})

    for event in state.get("activity") or []:
        add(event)
    # Legacy records can prove only their surviving timestamps. Do not invent
    # removed collections or old note text from the current state.
    began = timestamp(state.get("activity_started_at"), zone)
    def legacy(event):
        at = timestamp(event.get("at"), zone)
        if at and (began is None or at < began):
            add({**event, "legacy": True})

    for aid, entry in state["library"].items():
        paper = {"arxiv_id": aid, **(entry.get("paper") or {})}
        legacy({"kind": "saved", "at": entry.get("saved_at"), "paper": paper})
    for aid, record in (state.get("reading") or {}).items():
        paper = ((state["library"].get(aid) or {}).get("paper") or
                 (state["feedback"].get(aid) or {}).get("paper") or {})
        paper = {**paper, "arxiv_id": aid}
        for event in record.get("history") or []:
            legacy({"kind": "reading", "paper": paper, **event})
        if record.get("notes") or record.get("tags"):
            legacy({"kind": "notes", "at": record.get("updated_at"), "paper": paper,
                    "notes": record.get("notes", ""), "tags": record.get("tags", []),
                    "read_version": record.get("read_version")})
    for path in output.glob("????-??-??/reports/*/metadata.json"):
        payload = safe_json(path, {}) or {}
        if not isinstance(payload, dict) or payload.get("profile_id") != (profile_id or "default"):
            continue
        if not path.with_name("report.md").is_file():
            continue
        add({"kind": "report", "at": payload.get("generated_at"), "paper": payload.get("paper") or {},
             "quality": payload.get("report_quality", "unknown"),
             "report_id": path.with_name("report.html").relative_to(output).as_posix()})
    tracking = safe_json(output / f"version-state-{profile_id or 'default'}.json", {}) or {}
    for aid, item in (tracking.get("items") or {}).items():
        for event in item.get("events") or []:
            add({"kind": "version", "at": event.get("detected_at"),
                 "paper": {"arxiv_id": aid, "title": item.get("title", aid), "version": event.get("to_version")},
                 "from_version": event.get("from_version"), "analysis_status": event.get("status", "unknown")})
    events.sort(key=lambda e: e["at"])
    counts = dict(Counter(event["kind"] for event in events))
    counts["completed"] = sum(e["kind"] == "reading" and e.get("status") == "read" for e in events)
    return {"start": start.isoformat(), "end": now.isoformat(), "events": events, "counts": counts,
            "history_note": "升级前只汇总仍可从时间戳确认的活动，历史取消收藏等未记录行为无法还原。"}


def activity_markdown(activity: dict, include_notes: bool = False) -> str:
    lines = ["## 本期研究活动", f"统计区间：{activity['start'][:10]} 至 {activity['end'][:19].replace('T', ' ')}。",
             activity["history_note"], "已读次数统计状态变更，不代表模型替你完成了阅读。"]
    counts = activity["counts"]
    lines.append(" · ".join(f"{label} {counts.get(kind, 0)}" for kind, label in
                 [("saved", "新增收藏"), ("completed", "标记已读"), ("report", "生成报告"),
                  ("version", "发现新版"), ("library_version", "收藏更新"), ("notes", "笔记更新")]))
    notes = {}
    for event in activity["events"]:
        paper = event["paper"]
        aid = paper["arxiv_id"]
        detail = LABELS.get(event["kind"], event["kind"])
        if event["kind"] == "reading":
            detail += "：" + READING_STATUSES.get(event.get("status"), "未知")
        if event["kind"] in {"reading", "notes"}:
            version = event.get("read_version")
        else:
            version = paper.get("version")
        if event["kind"] in {"version", "library_version"}:
            detail += f"（v{event.get('from_version') or '?'} → v{version or '?'}）"
        if event["kind"] == "report":
            detail += "（" + {"full": "全文分析", "abstract": "摘要级回退"}.get(event.get("quality"), "质量未记录") + "）"
        lines.append(f"- {event['at'][:19].replace('T', ' ')} · {literal(detail)} · {literal(paper.get('title') or aid)}"
                     f"（{literal(aid)}{f' v{version}' if version else ' · 版本未记录'}）")
        if event["kind"] == "notes":
            notes[aid] = event
    if not activity["events"]:
        lines.append("本期没有可确认的研究活动。")
    if include_notes:
        lines.extend(["## 本期个人笔记与结论", "以下是个人记录的原样摘录，在模型生成后附入，未发送给模型，也不作为论文事实核验结果。"])
        for aid, event in notes.items():
            version = event.get("read_version")
            revision = f"v{version}" if version else "阅读版本未记录"
            lines.append(f"### {literal(event['paper'].get('title') or aid)} · {literal(aid)} · {revision}")
            lines.append("\n".join("> " + literal(line) for line in (event.get("notes") or "本期最后一次更新已清空笔记。").splitlines()))
            if event.get("tags"):
                lines.append("个人标签：" + literal("、".join(event["tags"])))
        if not notes:
            lines.append("本期没有个人笔记更新。")
    return "\n\n".join(lines) + "\n"
