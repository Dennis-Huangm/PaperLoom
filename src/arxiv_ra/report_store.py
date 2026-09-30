"""Select report evidence by stored identity, never by a filename's sort order."""
import re
import shutil
import json
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from datetime import datetime

from .utils import read_json
from .paper_data import base_id
from .reading_state import _locked


def quality_rank(metadata: dict) -> int:
    ranks: dict[Any, int] = {"full": 2, "abstract": 0}
    return ranks.get(metadata.get("report_quality"), 1)


def profile_matches(metadata: dict, profile_id: str) -> bool:
    # Unscoped legacy artifacts retain their historical shared visibility.
    return metadata.get("profile_id") in (None, "", profile_id or "default")


@dataclass(frozen=True)
class StoredReport:
    """One metadata read and its sibling materials, scoped to the current operation.

    Paths are not a guarantee that a material still exists when it is consumed.
    """
    metadata_path: Path
    metadata: dict[str, Any]

    @property
    def date(self) -> str:
        return self.metadata_path.parents[2].name

    @property
    def paper(self) -> dict[str, Any]:
        return self.metadata.get("paper") or {}

    @property
    def html_path(self) -> Path:
        return self.metadata_path.with_name("report.html")

    @property
    def markdown_path(self) -> Path:
        return self.metadata_path.with_name("report.md")

    @property
    def pdf_path(self) -> Path:
        return self.metadata_path.with_name("paper.pdf")

    @property
    def report_id(self) -> str:
        return self.html_path.relative_to(self.metadata_path.parents[3]).as_posix()

    def source_item(self) -> dict[str, Any]:
        return {**self.metadata, "_source_date": self.date}


def _report_records(output_root: Path, *, catalog: bool = False) -> Iterator[StoredReport]:
    """Read metadata once, preserving the two existing malformed-data policies."""
    for path in output_root.glob("????-??-??/reports/*/metadata.json"):
        try:
            payload = read_json(path, {})
        except (OSError, ValueError) as exc:
            if not catalog:
                continue
            if not isinstance(exc, (OSError, json.JSONDecodeError)):
                raise
            payload = {}
        if catalog:
            # Legacy catalogs show empty/unreadable metadata as a placeholder.
            payload = payload or {}
        if isinstance(payload, dict):
            yield StoredReport(path, payload)


def html_reports(output_root: Path) -> Iterator[StoredReport]:
    """Discover catalog-visible reports without choosing a preferred one."""
    return (record for record in _report_records(output_root, catalog=True)
            if record.html_path.exists())


def explicit_report(output_root: Path, arxiv_id: str, report_id: str) -> StoredReport | None:
    """Resolve an explicitly selected HTML report; never choose a substitute."""
    return next((report for report in html_reports(output_root)
                 if report.report_id == report_id
                 and report.paper.get("arxiv_id", "") == arxiv_id), None)


def preferred_report_index(reports: list[dict], profile_id: str) -> dict[tuple, dict]:
    """Choose direction-local HTML reports, retaining catalog order for ties."""
    selected: dict[tuple, dict] = {}
    for report in reports:
        if not profile_matches(report, profile_id):
            continue
        key = (report["arxiv_id"], report.get("version"))
        previous = selected.get(key)
        rank = lambda item: (bool(item.get("profile_id")), quality_rank(item))
        if previous is None or rank(report) > rank(previous):
            selected[key] = report
    return selected


def matching_report(output_root: Path, profile_id: str,
                    paper: dict[str, Any], *, before: datetime | None = None) -> Path | None:
    candidates = []
    for record in _report_records(output_root):
        try:
            payload = record.metadata
            if not profile_matches(payload, profile_id):
                continue
            if before is not None:
                raw_time = payload.get("generated_at") or record.date
                generated = datetime.fromisoformat(raw_time)
                if generated.tzinfo is None:
                    generated = generated.replace(tzinfo=before.tzinfo)
                if generated > before:
                    continue
            stored = record.paper
            if (stored.get("arxiv_id") != paper.get("arxiv_id")
                    or stored.get("version") != paper.get("version")):
                continue
            report = record.markdown_path
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
