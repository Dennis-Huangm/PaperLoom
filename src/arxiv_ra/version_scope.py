"""Source membership and material versions shared by checks, views and plans."""
from pathlib import Path

from .feedback import FeedbackStore
from .paper_data import base_id
from .report_store import html_reports, profile_matches


SCOPES = {"all": "全部", "reports": "本地报告", "zotero": "Zotero"}
MATERIALS = {"report": "本地报告", "zotero": "Zotero"}
STATUS_LABELS = {"outdated": "待更新", "unknown": "待核实", "current": "已是最新", "absent": "未收录"}


def validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError("无效的追踪范围")
    return scope


def in_scope(item: dict, scope: str) -> bool:
    validate_scope(scope)
    return bool(set(item.get("sources", [])) & ({"本地报告", "Zotero"} if scope == "all" else {SCOPES[scope]}))


def local_sources(root: Path, profile_id: str) -> dict[str, dict]:
    sources: dict[str, dict] = {}
    dismissed = FeedbackStore(root, profile_id).all()
    for record in html_reports(root):
        aid = base_id(str(record.paper.get("arxiv_id") or ""))
        if not aid or aid in dismissed or not profile_matches(record.metadata, profile_id):
            continue
        item = sources.setdefault(aid, {"arxiv_id": aid, "title": record.paper.get("title") or aid,
                                       "sources": ["本地报告"]})
        version = int(record.paper.get("version") or 0)
        item["report_version"] = max(item.get("report_version", 0), version)
        if record.pdf_path.is_file():
            item["local_version"] = max(item.get("local_version", 0), version)
    return sources


def material_status(item: dict, target: str) -> str:
    if MATERIALS[target] not in item.get("sources", []):
        return "absent"
    if target == "zotero" and item.get("zotero_stale"):
        return "unknown"
    latest, version = int(item.get("latest_version") or 0), int(item.get(target + "_version") or 0)
    if not latest or not version or item.get("check_error"):
        return "unknown"
    return "outdated" if version < latest else "current"


def scoped_targets(item: dict, scope: str = "all") -> list[str]:
    validate_scope(scope)
    return [target for target, label in MATERIALS.items()
            if label in item.get("sources", []) and (scope == "all" or SCOPES[scope] == label)]


def update_options(item: dict, scope: str = "all") -> dict[str, bool]:
    targets = scoped_targets(item, scope)
    return {"report": "report" in targets and material_status(item, "report") == "outdated",
            "zotero": "zotero" in targets and material_status(item, "zotero") == "outdated",
            "obsidian": False}


def can_supplement(item: dict, scope: str = "all") -> bool:
    return (scope in {"all", "zotero"} and material_status(item, "zotero") == "unknown"
            and bool(item.get("latest_version")) and not item.get("zotero_stale")
            and not item.get("check_error") and len(item.get("zotero_keys", [])) == 1
            and bool(item.get("zotero_library")))


def describe(item: dict, scope: str = "all") -> dict:
    statuses = {target: material_status(item, target) for target in MATERIALS}
    relevant = [statuses[target] for target in scoped_targets(item, scope)]
    return {**item, "material_status": statuses, "supplementable": can_supplement(item, scope),
            "outdated": "outdated" in relevant, "unknown": "unknown" in relevant}
