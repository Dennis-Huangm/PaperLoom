"""Select report evidence by stored identity, never by a filename's sort order."""
from pathlib import Path
from typing import Any
from datetime import datetime

from .utils import read_json


def quality_rank(metadata: dict) -> int:
    return {"full": 2, "abstract": 0}.get(metadata.get("report_quality"), 1)


def profile_matches(metadata: dict, profile_id: str) -> bool:
    # Unscoped legacy artifacts retain their historical shared visibility.
    return metadata.get("profile_id") in (None, "", profile_id or "default")


def matching_report(output_root: Path, profile_id: str,
                    paper: dict[str, Any], *, before: datetime | None = None) -> Path | None:
    candidates = []
    for path in output_root.glob("????-??-??/reports/*/metadata.json"):
        try:
            payload = read_json(path, {})
            if not isinstance(payload, dict) or not profile_matches(payload, profile_id):
                continue
            if before is not None:
                raw_time = payload.get("generated_at") or path.parents[2].name
                generated = datetime.fromisoformat(raw_time)
                if generated.tzinfo is None:
                    generated = generated.replace(tzinfo=before.tzinfo)
                if generated > before:
                    continue
            stored = payload.get("paper") or {}
            if (stored.get("arxiv_id") != paper.get("arxiv_id")
                    or stored.get("version") != paper.get("version")):
                continue
            report = path.with_name("report.md")
            if report.is_file():
                candidates.append((bool(payload.get("profile_id")),
                                   quality_rank(payload),
                                   report.stat().st_mtime_ns, str(report), report))
        except (OSError, ValueError, TypeError):
            continue
    return max(candidates)[-1] if candidates else None
