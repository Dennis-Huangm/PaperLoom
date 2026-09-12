from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import read_json


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
