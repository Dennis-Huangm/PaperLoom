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
                "title": paper.get("title", metadata_path.parent.name),
                "authors": [
                    item.get("name", "") for item in (paper.get("authors") or [])
                ],
                "venue": verified.get("venue") or "会议/期刊未核实",
                "venue_status": verified.get("venue_status", "unverified"),
                "report_url": artifact_url(report_path, output_root),
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
    reports.sort(key=lambda item: (item["date"], item["updated_at"]), reverse=True)
    return reports


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
                "generated_at": payload.get("generated_at", ""),
                "report_url": report_url,
            }
        )
    items.sort(key=lambda item: item["week_id"], reverse=True)
    return items


def version_tracking_data(output_root: Path, profile_id: str) -> dict[str, Any]:
    suffix = profile_id or "default"
    payload = _safe_json(output_root / f"version-state-{suffix}.json", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    items = list((payload.get("items") or {}).values())
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
    return {"checked_at": payload.get("checked_at", ""), "items": items, "events": events}


def citation_library(output_root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for graph_path in output_root.glob("citations/*/graph.json"):
        graph = _safe_json(graph_path, {}) or {}
        if not isinstance(graph, dict):
            continue
        report_path = graph_path.parent / "index.html"
        if not report_path.exists():
            continue
        report_url = artifact_url(report_path, output_root)
        if report_url:
            report_url = f"{report_url}?v={int(report_path.stat().st_mtime)}"
        items.append(
            {
                "arxiv_id": graph.get("arxiv_id") or graph_path.parent.name,
                "title": (graph.get("seed") or {}).get("title")
                or graph_path.parent.name,
                "reference_count": len(graph.get("references") or []),
                "citation_count": len(graph.get("citations") or []),
                "similar_count": len(graph.get("similar") or []),
                "report_url": report_url,
                "updated_at": datetime.fromtimestamp(
                    report_path.stat().st_mtime
                ).isoformat(timespec="minutes"),
            }
        )
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items
