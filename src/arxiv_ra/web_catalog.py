from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import AppConfig
from .storage import read_recommendations
from .utils import read_json
from .library import PaperLibraryStore
from .feedback import FeedbackStore
from .report_store import profile_matches, quality_rank
from .version_sync import STEP_LABELS, STATUS_LABELS


DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe_json(path: Path, default: Any) -> Any:
    """Treat one damaged artifact as missing instead of breaking the catalog."""
    try:
        return read_json(path, default)
    except (OSError, json.JSONDecodeError):
        return default


def output_root_for(config: AppConfig, project_root: Path) -> Path:
    output = Path(config.output_dir)
    return output if output.is_absolute() else project_root / output


def artifact_url(path: Path, output_root: Path) -> str | None:
    """Return a browser URL only for files contained by the artifact root."""
    try:
        relative = path.resolve().relative_to(output_root.resolve()).as_posix()
    except ValueError:
        return None
    return f"/artifacts/{quote(relative, safe='/')}"


def result_artifact_url(path: Path, output_root: Path) -> str | None:
    """Prefer the rendered HTML sibling for Markdown task artifacts."""
    if path.suffix.casefold() in {".md", ".markdown"}:
        html_path = path.with_suffix(".html")
        if html_path.is_file():
            path = html_path
    return artifact_url(path, output_root)


def _dated_directories(output_root: Path, *, newest_first: bool = True) -> list[Path]:
    if not output_root.exists():
        return []
    return sorted(
        (
            path
            for path in output_root.iterdir()
            if path.is_dir() and DATE_DIR_RE.fullmatch(path.name)
        ),
        key=lambda path: path.name,
        reverse=newest_first,
    )


def recommendations_for_date(
    output_root: Path, date_label: str, profile_id: str = ""
) -> list[dict[str, Any]]:
    if not DATE_DIR_RE.fullmatch(date_label):
        return []
    return read_recommendations(output_root, date_label, profile_id)


def latest_recommendations(
    output_root: Path, profile_id: str = ""
) -> tuple[str | None, list[dict[str, Any]]]:
    for date_dir in _dated_directories(output_root):
        recommendations = recommendations_for_date(output_root, date_dir.name, profile_id)
        if recommendations:
            return date_dir.name, recommendations
    return None, []


def recommendation_history(
    output_root: Path, profile_id: str = ""
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for date_dir in _dated_directories(output_root):
        recommendations = recommendations_for_date(output_root, date_dir.name, profile_id)
        if recommendations:
            history.append({"date": date_dir.name, "count": len(recommendations)})
    return history


def report_library(output_root: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    if not output_root.exists():
        return reports
    for metadata_path in output_root.glob("????-??-??/reports/*/metadata.json"):
        payload = _safe_json(metadata_path, {}) or {}
        if not isinstance(payload, dict):
            continue
        paper = payload.get("paper") or {}
        verified = payload.get("verified") or {}
        report_path = metadata_path.parent / "report.html"
        if not report_path.exists():
            continue
        reports.append(
            {
                "date": metadata_path.parents[2].name,
                "folder": metadata_path.parent.name,
                "arxiv_id": paper.get("arxiv_id", ""),
                "version": paper.get("version"),
                "profile_id": payload.get("profile_id", ""),
                "report_quality": payload.get("report_quality", "unknown"),
                "evidence": payload.get("evidence") or {},
                "model": payload.get("model", ""),
                "errors": payload.get("errors") or [],
                "parsed_pages": payload.get("parsed_pages"),
                "generated_at": payload.get("generated_at", ""),
                "title": paper.get("title", metadata_path.parent.name),
                "authors": [
                    item.get("name", "") for item in (paper.get("authors") or [])
                ],
                "venue": verified.get("venue") or "会议/期刊未核实",
                "venue_status": verified.get("venue_status", "unverified"),
                "report_url": artifact_url(report_path, output_root),
                "report_id": report_path.relative_to(output_root).as_posix(),
                "method_figure_count": len(
                    list(metadata_path.parent.glob("method-figure-*"))
                ),
                "updated_at": datetime.fromtimestamp(
                    report_path.stat().st_mtime
                ).isoformat(timespec="minutes"),
                "metadata_path": metadata_path,
                "report_path": report_path,
                "pdf_path": metadata_path.parent / "paper.pdf",
            }
        )
    reports.sort(key=lambda item: (item["date"], item["generated_at"] or item["updated_at"]), reverse=True)
    return reports


def preferred_report_index(reports: list[dict], profile_id: str) -> dict[tuple, dict]:
    selected = {}
    for report in reports:
        if not profile_matches(report, profile_id):
            continue
        key = (report["arxiv_id"], report.get("version"))
        previous = selected.get(key)
        rank = lambda item: (bool(item.get("profile_id")), quality_rank(item))
        if previous is None or rank(report) > rank(previous):
            selected[key] = report
    return selected


def weekly_library(output_root: Path, profile_id: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for metadata_path in output_root.glob("weekly/*/metadata.json"):
        payload = _safe_json(metadata_path, {}) or {}
        if not isinstance(payload, dict):
            continue
        if profile_id and payload.get("profile_id") not in {"", profile_id}:
            continue
        report_path = metadata_path.parent / "report.html"
        if not report_path.exists():
            continue
        report_url = artifact_url(report_path, output_root)
        if report_url:
            report_url = f"{report_url}?v={int(report_path.stat().st_mtime)}"
        items.append(
            {
                "week_id": payload.get("week_id") or metadata_path.parent.name,
                "profile_name": payload.get("profile_name") or "默认方向",
                "paper_count": payload.get("paper_count", 0),
                "activity_counts": payload.get("activity_counts") or {},
                "includes_personal_notes": payload.get("includes_personal_notes", False),
                "generated_at": payload.get("generated_at", ""),
                "report_url": report_url,
            }
        )
    items.sort(key=lambda item: (item["week_id"], item["generated_at"]), reverse=True)
    return items


def version_tracking_data(output_root: Path, profile_id: str) -> dict[str, Any]:
    suffix = profile_id or "default"
    payload = _safe_json(output_root / f"version-state-{suffix}.json", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    by_id = {aid: dict(item) for aid, item in (payload.get("items") or {}).items()
             if item.get("tracked", True)}
    for item in by_id.values():
        # These describe files and current reading state, not the last check.
        for field in ("saved_version", "report_version", "local_version"):
            item.pop(field, None)

    def entry(aid, title=""):
        item = by_id.setdefault(aid, {"arxiv_id": aid, "title": title or aid, "sources": []})
        item.setdefault("checked_at", "")
        item.setdefault("sources", [])
        return item

    for aid, saved in PaperLibraryStore(output_root, profile_id).all().items():
        paper = saved.get("paper") or {}
        item = entry(aid, paper.get("title", ""))
        item["saved_version"] = paper.get("version")
        if "文献库" not in item["sources"]:
            item["sources"].append("文献库")
    for report in report_library(output_root):
        if not profile_matches(report, profile_id):
            continue
        item = entry(report["arxiv_id"], report["title"])
        if "本地报告" not in item["sources"]:
            item["sources"].append("本地报告")
        item["report_version"] = max(int(item.get("report_version") or 0), int(report.get("version") or 0)) or None
        if report["pdf_path"].is_file():
            item["local_version"] = max(int(item.get("local_version") or 0), int(report.get("version") or 0)) or None
    for state_path in (output_root / "papers" / suffix).glob("*/sync.json"):
        sync = _safe_json(state_path, {}) or {}
        if not sync.get("arxiv_id"):
            continue
        item = entry(sync["arxiv_id"], sync.get("title", ""))
        item["latest_version"] = max(int(item.get("latest_version") or 0), int(sync.get("latest_version") or 0)) or None
        item["checked_at"] = max(item.get("checked_at") or "", sync.get("latest_checked_at") or "")
        item["sync_error"] = sync.get("latest_error", "")
        item["sync_url"] = artifact_url(state_path.with_name("index.html"), output_root)
        item["sync_target"] = sync.get("last_target")
        operation = (sync.get("operations") or {}).get(str(sync.get("last_target")), {})
        item["sync_status"] = STATUS_LABELS.get(operation.get("status"), "尚未同步")
        item["retry_available"] = operation.get("status") in {"failed", "partial", "running", "interrupted"}
        active_steps = {"download", "library"} | {name for name, enabled in operation.get("options", {}).items() if enabled}
        item["sync_steps"] = [{"label": STEP_LABELS.get(name, name),
                               "status": STATUS_LABELS.get(step.get("status"), "待处理"), "error": step.get("error", "")}
                              for name, step in operation.get("steps", {}).items() if name in active_steps]
        for path in state_path.parent.glob("v*/metadata.json"):
            paper = (_safe_json(path, {}) or {}).get("paper") or {}
            if path.with_name("paper.pdf").is_file():
                item["local_version"] = max(int(item.get("local_version") or 0), int(paper.get("version") or 0)) or None
                if "已同步文件" not in item["sources"]:
                    item["sources"].append("已同步文件")
    dismissed = FeedbackStore(output_root, profile_id).all()
    items = [entry(aid) for aid in list(by_id) if aid not in dismissed]
    events: list[dict[str, Any]] = []
    for item in items:
        for event in item.get("events") or []:
            raw_report_path = str(event.get("report_path") or "")
            report_path = Path(raw_report_path) if raw_report_path else None
            events.append(
                {
                    **event,
                    "arxiv_id": item.get("arxiv_id"),
                    "title": item.get("title"),
                    "report_url": (
                        artifact_url(report_path, output_root)
                        if report_path and report_path.is_file()
                        else None
                    ),
                }
            )
    events.sort(key=lambda item: item.get("detected_at", ""), reverse=True)
    items.sort(key=lambda item: item.get("title", "").casefold())
    return {"checked_at": payload.get("checked_at", ""), "items": items, "events": events,
            "coverage": payload.get("coverage") or {}}


def citation_library(output_root: Path, profile_id: str | None = None) -> list[dict[str, Any]]:
    from .graph_store import active_graph_path, read_graph
    items = []
    for folder in (output_root / "citations").glob("*"):
        if not folder.is_dir():
            continue
        try:
            graph_path = active_graph_path(folder)
            graph = read_graph(graph_path)
        except (OSError, ValueError, TypeError):
            continue
        if profile_id is not None and graph.get('profile_id', '') != profile_id:
            # Old folders encoded their direction in their name.
            if graph.get('schema_version') == 2 or not folder.name.endswith('-' + (profile_id or 'default')):
                continue
        report_path = graph_path.with_name('index.html')
        if not report_path.is_file():
            continue
        items.append({'arxiv_id': graph.get('arxiv_id') or folder.name,
                      'title': graph['seed'].get('title') or folder.name,
                      'profile_name': graph.get('profile_name') or '历史方向',
                      'reference_count': len(graph.get('references') or []),
                      'citation_count': len(graph.get('citations') or []),
                      'similar_count': len(graph.get('similar') or []),
                      'node_count': len(graph['nodes']), 'status': graph.get('status', 'legacy'),
                      'report_url': artifact_url(report_path, output_root),
                      'updated_at': graph.get('generated_at') or datetime.fromtimestamp(report_path.stat().st_mtime).isoformat(timespec='minutes')})
    items.sort(key=lambda item: item['updated_at'], reverse=True)
    return items
