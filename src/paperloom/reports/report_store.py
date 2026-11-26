"""Select report evidence by stored identity, never by a filename's sort order."""
import re
import hashlib
import shutil
import json
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from datetime import datetime

from ..utils import read_json
from ..papers.paper_data import base_id
from ..papers.reading_state import _locked


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


def iter_report_records(output_root: Path, *, catalog: bool = False) -> Iterator[StoredReport]:
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


def report_records(output_root: Path) -> list[StoredReport]:
    """One operation's metadata in stable discovery order, including Markdown-only attempts."""
    return sorted(iter_report_records(output_root), key=lambda record: record.metadata_path)


def html_reports(output_root: Path) -> Iterator[StoredReport]:
    """Discover catalog-visible reports without choosing a preferred one."""
    return (record for record in iter_report_records(output_root, catalog=True)
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


def matching_record(records, profile_id: str, paper: dict[str, Any], *,
                    before: datetime | None = None) -> StoredReport | None:
    candidates = []
    for record in records:
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
                                   report.stat().st_mtime_ns, str(report), record))
        except (OSError, ValueError, TypeError):
            continue
    return max(candidates)[-1] if candidates else None


def matching_report(output_root: Path, profile_id: str,
                    paper: dict[str, Any], *, before: datetime | None = None) -> Path | None:
    match = matching_record(iter_report_records(output_root), profile_id, paper, before=before)
    return match.markdown_path if match else None


def catalog_lock(root: Path):
    return _locked(root / "report-catalog.lock")


def deletion_token(report_ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(report_ids), ensure_ascii=False).encode()).hexdigest()


def deletion_folders(root: Path, report_ids: list[str], arxiv_id: str, hidden: set[str]) -> list[Path]:
    """Resolve only validated, visible report attempts before any deletion starts."""
    root = root.resolve()
    folders = []
    for rid in report_ids:
        folder = (root / rid).parent
        relative = folder.resolve().relative_to(root)
        if (len(relative.parts) != 3 or relative.parts[1] != "reports"
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative.parts[0])
                or folder != folder.resolve()):
            raise ValueError("报告目录已变化")
        meta = read_json(folder / "metadata.json")
        if (not isinstance(meta, dict) or str(meta.get("profile_id") or "") in hidden
                or base_id(str((meta.get("paper") or {}).get("arxiv_id") or "")) != arxiv_id):
            raise ValueError("报告归属已变化")
        folders.append(folder)
    # Keep a visible entry for retries until every incomplete attempt has been removed.
    return sorted(folders, key=lambda folder: (folder / "report.html").is_file())


def _remove_attempt(folder: Path) -> None:
    # A failed recursive removal can erase the catalog entry before reaching a locked file.
    catalog_files = {path: path.read_bytes() for path in (folder / "metadata.json", folder / "report.html")
                     if path.is_file() and not path.is_symlink()}
    try:
        shutil.rmtree(folder)
    except OSError:
        if folder.is_dir():
            for path, content in catalog_files.items():
                if not path.exists():
                    with path.open("xb") as stream:
                        stream.write(content)
        raise


def delete_selected_reports(output_root, report_ids, tokens, *, expected_paper, hidden,
                            load_catalog, active_report, on_removed):
    """Validate the entire selection and remove exactly its confirmed directories in order."""
    with catalog_lock(output_root):
        catalog = {item["report_id"]: item for item in load_catalog()}
        plans = []
        for rid in dict.fromkeys(report_ids):
            item = catalog.get(rid)
            if item is None or item["delete_token"] not in tokens:
                raise ValueError("所选论文的报告已变化，请刷新后重新确认，本次未删除")
            aid = item["arxiv_id"]
            if expected_paper is not None and aid != expected_paper:
                raise ValueError("报告归属已变化，请刷新后重新确认")
            if aid and active_report(aid):
                raise ValueError(f"论文 {aid} 正在生成报告，请等待任务结束")
            try:
                folders = deletion_folders(output_root, item["attempt_ids"], aid, hidden)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise ValueError("报告目录或归属已变化，本次未删除") from exc
            plans.append((aid or rid, folders))

        completed = []
        for index, (label, folders) in enumerate(plans):
            for attempt, folder in enumerate(folders):
                try:
                    _remove_attempt(folder)
                    on_removed(folder / "report.html")
                except OSError as exc:
                    if folder.exists() or attempt + 1 < len(folders):
                        partial = label
                    else:
                        completed.append(label)
                        partial = "无"
                    remaining = [paper for paper, _ in plans[index + 1:]]
                    raise OSError(f"已删除：{'、'.join(completed) or '无'}。未删完：{partial}。"
                                  f"未处理：{'、'.join(remaining) or '无'}。错误：{exc}。请刷新后查看。") from exc
            completed.append(label)
