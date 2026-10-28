"""Current material versions and source membership, independent of presentation."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..papers.feedback import FeedbackStore
from ..papers.library import PaperLibraryStore
from ..utils import read_json


def _safe_json(path: Path, default: Any) -> Any:
    """Treat one damaged artifact as missing instead of breaking the catalog."""
    try:
        return read_json(path, default)
    except (OSError, json.JSONDecodeError):
        return default


def tracking_snapshot(output_root: Path, profile_id: str) -> dict[str, Any]:
    suffix = profile_id or "default"
    payload = _safe_json(output_root / f"version-state-{suffix}.json", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    from .version_scope import local_sources
    by_id = local_sources(output_root, profile_id)
    for aid, previous in (payload.get("items") or {}).items():
        if "Zotero" in previous.get("sources", []):
            item = by_id.setdefault(aid, {"arxiv_id": aid, "title": previous.get("title") or aid, "sources": []})
            item["sources"].append("Zotero")
        if aid in by_id:
            current = by_id[aid]
            by_id[aid] = {**{k: v for k, v in previous.items() if k not in
                            {"sources", "report_version", "local_version", "saved_version"}}, **current}

    def entry(aid):
        item = by_id[aid]
        item.setdefault("checked_at", "")
        return item

    for aid, saved in PaperLibraryStore(output_root, profile_id).all().items():
        if aid in by_id:
            by_id[aid]["saved_version"] = (saved.get("paper") or {}).get("version")
    for state_path in (output_root / "papers" / suffix).glob("*/sync.json"):
        sync = _safe_json(state_path, {}) or {}
        if sync.get("arxiv_id") not in by_id:
            continue
        item = entry(sync["arxiv_id"])
        item["latest_version"] = max(int(item.get("latest_version") or 0), int(sync.get("latest_version") or 0)) or None
        item["checked_at"] = max(item.get("checked_at") or "", sync.get("latest_checked_at") or "")
        item["sync_error"] = sync.get("latest_error", "")
        item["sync_report_path"] = state_path.with_name("index.html")
        item["sync_target"] = sync.get("last_target")
        operation = (sync.get("operations") or {}).get(str(sync.get("last_target")), {})
        item["sync_status"] = operation.get("status", "")
        item["retry_available"] = operation.get("status") in {"failed", "partial", "running", "interrupted"}
        active_steps = {"download", "library"} | {name for name, enabled in operation.get("options", {}).items() if enabled}
        item["sync_steps"] = [{"name": name, "status": step.get("status", ""), "error": step.get("error", "")}
                              for name, step in operation.get("steps", {}).items() if name in active_steps]
        for completed in (sync.get("operations") or {}).values():
            step = (completed.get("steps") or {}).get("zotero") or {}
            if (step.get("status") == "succeeded" and step.get("version")
                    and step.get("item_key") in item.get("zotero_keys", [])
                    and step.get("library") == item.get("zotero_library") and not item.get("zotero_stale")):
                try:
                    newer = (datetime.fromisoformat(step["verified_at"]) >
                             datetime.fromisoformat(item.get("zotero_checked_at") or payload.get("checked_at") or "1970-01-01T00:00:00+00:00"))
                except (ValueError, KeyError, TypeError):
                    newer = False
                if newer:
                    item["zotero_version"] = step["version"]
                    item["zotero_checked_at"] = step["verified_at"]
                    item["zotero_reason"] = ""
        for path in state_path.parent.glob("v*/metadata.json"):
            paper = (_safe_json(path, {}) or {}).get("paper") or {}
            if path.with_name("paper.pdf").is_file():
                item["local_version"] = max(int(item.get("local_version") or 0), int(paper.get("version") or 0)) or None
    dismissed = FeedbackStore(output_root, profile_id).all()
    items = [entry(aid) for aid in list(by_id) if aid not in dismissed]
    return {"checked_at": payload.get("checked_at", ""), "items": items,
            "coverage": payload.get("coverage") or {}, "source_status": payload.get("source_status") or {}}
