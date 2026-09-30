from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from .utils import read_json, write_json
from .reading_state import _locked


def clear_recommendations(output_root: Path, date_label: str, profile_id: str = "") -> None:
    """Clear a profile's daily archive, preserving reading and processed state."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_label):
        raise ValueError("日期格式必须为 YYYY-MM-DD")
    date.fromisoformat(date_label)
    if profile_id and not re.fullmatch(r"[\w-]+", profile_id):
        raise ValueError("无效的研究方向")
    root = output_root.resolve()
    folder = root / date_label
    canonical = recommendation_data_path(root, date_label, profile_id)
    legacy = folder / "recommendations.json"
    digest = folder / (f"index-{profile_id}.html" if profile_id else "index.html")
    batch = folder / "batches" / (profile_id or "default")
    paths = [canonical, legacy, digest, folder / "index.html", batch,
             folder / f"no-new-{profile_id or 'default'}.html"]
    if any(path.resolve() != path for path in paths):
        raise ValueError("推荐记录路径不能包含符号链接或目录联接")
    with _locked(output_root / f"publication-{profile_id or 'default'}.lock"):
        payload = read_json(legacy, [])
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ValueError("推荐历史格式错误，已保留原文件")
        # Keep an empty canonical file to prevent legacy fallback resurrecting this day.
        # Untagged legacy rows may also serve other profiles; the tombstone hides
        # them only for the cleared profile without destroying shared history.
        remaining = [item for item in payload if item.get("profile_id") != profile_id] if profile_id else []
        write_json(canonical, [])
        if remaining != payload or not profile_id:
            write_json(legacy, remaining)
            (folder / "index.html").unlink(missing_ok=True)
        digest.unlink(missing_ok=True)
        (folder / f"no-new-{profile_id or 'default'}.html").unlink(missing_ok=True)
        if batch.exists():
            shutil.rmtree(batch)


def recommendation_data_path(
    output_root: Path, date_label: str, profile_id: str = ""
) -> Path:
    """Return the canonical recommendation file for one research profile."""
    filename = f"recommendations-{profile_id}.json" if profile_id else "recommendations.json"
    return output_root / date_label / filename


def read_recommendations(
    output_root: Path, date_label: str, profile_id: str = ""
) -> list[dict[str, Any]]:
    """Read one profile's daily result with legacy-file compatibility."""
    profile_path = recommendation_data_path(output_root, date_label, profile_id)
    legacy_path = output_root / date_label / "recommendations.json"
    source_path = profile_path if profile_path.is_file() else legacy_path
    try:
        payload = read_json(source_path, None)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    if not profile_id:
        return payload
    matching = [
        item for item in payload if str(item.get("profile_id") or "") == profile_id
    ]
    if matching:
        return matching
    if payload and all(not item.get("profile_id") for item in payload):
        return payload
    return []
