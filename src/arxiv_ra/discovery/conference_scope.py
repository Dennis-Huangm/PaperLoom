"""User-facing conference identities and scope validation."""
from datetime import datetime, timezone

CONFERENCES = {"icml": "ICML", "neurips": "NeurIPS", "cvpr": "CVPR", "iclr": "ICLR", "acl": "ACL"}
MODES = {"latest": "最新 arXiv", "conference": "会议论文", "mixed": "混合推荐"}


def validate_scope(mode, conferences, year_from, year_to):
    if mode not in MODES:
        raise ValueError("无效的论文发现模式")
    if not isinstance(conferences, list) or any(not isinstance(c, str) for c in conferences):
        raise ValueError("会议必须是名称列表")
    names = list(dict.fromkeys("neurips" if c.strip().lower() == "nips" else c.strip().lower() for c in conferences))
    if any(c not in CONFERENCES for c in names):
        raise ValueError("会议仅支持 ICML、NeurIPS/NIPS、CVPR、ICLR、ACL")
    if mode != "latest" and not names:
        raise ValueError("请至少选择一个会议")
    if mode != "latest" or year_from is not None or year_to is not None:
        maximum = datetime.now(timezone.utc).year
        if any(type(y) is not int or not 1987 <= y <= maximum for y in (year_from, year_to)):
            raise ValueError(f"会议年份必须为 1987–{maximum} 的整数")
        if year_from > year_to:
            raise ValueError("起始会议年份不能晚于结束年份")
    return names


def scope_from_form(form):
    def year(name):
        value = str(form.get(name, "")).strip()
        return int(value) if value else None
    names = form.getlist("conferences") if hasattr(form, "getlist") else form.get("conferences", [])
    if isinstance(names, str):
        names = [c.strip() for c in names.split(",") if c.strip()]
    scope = dict(mode=str(form.get("mode", "latest")), conferences=names,
                 conference_year_from=year("conference_year_from"), conference_year_to=year("conference_year_to"))
    scope["conferences"] = validate_scope(scope["mode"], names, scope["conference_year_from"], scope["conference_year_to"])
    return scope
