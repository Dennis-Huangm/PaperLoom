from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..runtime.artifacts import artifact_url, result_artifact_url
from ..config import AppConfig
from ..papers.storage import read_recommendations
from ..utils import read_json
from ..papers.library import PaperLibraryStore
from ..papers.feedback import FeedbackStore
from ..papers.paper_data import base_id
from ..reports.report_store import preferred_report_index, profile_matches, quality_rank, html_reports
from ..versions.version_sync import STEP_LABELS, STATUS_LABELS
from ..versions.version_catalog import tracking_snapshot


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
    for record in html_reports(output_root):
        metadata_path = record.metadata_path
        payload = record.metadata
        paper = record.paper
        verified = payload.get("verified") or {}
        report_path = record.html_path
        reports.append(
            {
                "date": record.date,
                "folder": metadata_path.parent.name,
                "arxiv_id": paper.get("arxiv_id", ""),
                "version": paper.get("version"),
                "profile_id": payload.get("profile_id", ""),
                "profile_name": payload.get("profile_name", ""),
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
                "report_id": record.report_id,
                "method_figure_count": len(
                    list(metadata_path.parent.glob("method-figure-*"))
                ),
                "updated_at": datetime.fromtimestamp(
                    report_path.stat().st_mtime
                ).isoformat(timespec="minutes"),
                "metadata_path": metadata_path,
                "report_path": report_path,
                "pdf_path": record.pdf_path,
            }
        )
    reports.sort(key=lambda item: (item["date"], item["generated_at"] or item["updated_at"]), reverse=True)
    return reports


def grouped_report_library(reports: list[dict[str, Any]],
                           profile_names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Show one best available report for each arXiv ID with all known directions."""
    names = profile_names or {}
    by_id: dict[str, list[dict[str, Any]]] = {}
    for report in reports:
        arxiv_id = base_id(str(report.get("arxiv_id") or ""))
        by_id.setdefault(arxiv_id or report["report_id"], []).append(report)

    grouped = []
    for arxiv_id, attempts in by_id.items():
        def rank(item: dict[str, Any]) -> tuple[int, int, int]:
            try:
                version = int(item.get("version") or 0)
            except (TypeError, ValueError):
                version = 0
            return (version, quality_rank(item), item["report_path"].stat().st_mtime_ns)

        chosen = max(attempts, key=rank).copy()
        directions: dict[str, str] = {}
        for attempt in attempts:
            profile_id = str(attempt.get("profile_id") or "")
            direction_id = profile_id or "__legacy__"
            directions.setdefault(direction_id,
                                  names.get(profile_id) or attempt.get("profile_name")
                                  or (profile_id if profile_id else "历史报告"))
        chosen["arxiv_id"] = base_id(str(chosen.get("arxiv_id") or ""))
        chosen["directions"] = [
            {"id": direction_id, "name": direction_name}
            for direction_id, direction_name in sorted(directions.items(), key=lambda item: item[1].casefold())
        ]
        chosen["report_count"] = len(attempts)
        grouped.append(chosen)
    grouped.sort(key=lambda item: (item["date"], item["generated_at"] or item["updated_at"]), reverse=True)
    return grouped


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
    """Add browser links and labels to the shared material snapshot."""
    tracking = tracking_snapshot(output_root, profile_id)
    items = tracking["items"]
    for item in items:
        if "sync_report_path" in item:
            item["sync_url"] = artifact_url(item.pop("sync_report_path"), output_root)
            item["sync_status"] = STATUS_LABELS.get(item["sync_status"], "尚未同步")
            item["sync_steps"] = [
                {"label": STEP_LABELS.get(step["name"], step["name"]),
                 "status": STATUS_LABELS.get(step["status"], "待处理"), "error": step["error"]}
                for step in item["sync_steps"]]
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
    return {**tracking, "events": events}


def citation_library(output_root: Path, profile_id: str | None = None) -> list[dict[str, Any]]:
    from ..related_work.graph_store import active_graph_path, read_graph
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
