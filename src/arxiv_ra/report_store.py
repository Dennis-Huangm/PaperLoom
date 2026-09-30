"""Select report evidence by stored identity, never by a filename's sort order."""
import re
import shutil
from pathlib import Path
from typing import Any
from datetime import datetime

from .utils import read_json
from .paper_data import base_id
from .reading_state import _locked


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


def delete_report_attempts(output_root: Path, arxiv_id: str) -> list[str]:
    """Delete only dated report directories whose metadata names this paper."""
    root = output_root.resolve()
    removed = []
    with _locked(root / "report-catalog.lock"):
        for metadata_path in root.glob("????-??-??/reports/*/metadata.json"):
            try:
                payload = read_json(metadata_path, {})
                paper = payload.get("paper") if isinstance(payload, dict) else None
                if base_id(str((paper or {}).get("arxiv_id") or "")) != arxiv_id:
                    continue
                folder = metadata_path.parent
                relative = folder.resolve().relative_to(root)
                if (len(relative.parts) != 3 or relative.parts[1] != "reports"
                        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative.parts[0])
                        or folder.is_symlink()):
                    continue
                report_id = (folder / "report.html").relative_to(root).as_posix()
            except (OSError, ValueError, TypeError, AttributeError):
                # A damaged or inaccessible artifact must never widen deletion
                # to another directory; leave it for explicit inspection.
                continue
            shutil.rmtree(folder)
            removed.append(report_id)
    return removed
