"""Versioned research intent; model suggestions never replace user constraints."""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import asdict

from .config import DiscoveryConfig, RankingConfig
from .arxiv_categories import ARXIV_CATEGORIES

KINDS = {"topic": "主题线索", "prefer": "提高优先级", "demote": "降低优先级",
         "required": "必要条件", "exclude": "明确排除条件"}
HARD_KINDS = {"required", "exclude"}
BROAD_TERMS = {"ai", "llm", "llms", "deep learning", "artificial intelligence", "machine learning"}



def strings(value, *, limit=40, label="关键词") -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{label}必须是列表，最多 {limit} 项")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 200:
            raise ValueError(f"{label}每项必须是 1–200 字的文本")
        item = " ".join(item.split())
        if item.casefold() not in {s.casefold() for s in result}:
            result.append(item)
    return result


def categories(value) -> list[str]:
    values = strings(value, limit=12, label="arXiv 类别")
    for item in values:
        if item not in ARXIV_CATEGORIES:
            raise ValueError(f"无效的 arXiv 类别：{item}")
    if not values:
        raise ValueError("至少需要一个 arXiv 类别")
    return values


def condition(kind, text, *, origin="user", aliases=None, evidence="用户输入", confirmed=True):
    return {"id": hashlib.sha256(f"{kind}:{text.casefold()}".encode()).hexdigest()[:16],
            "kind": kind, "text": text, "aliases": strings([text] if aliases is None else aliases, limit=16),
            "origin": origin, "evidence": evidence, "confirmed": confirmed}


def validate_conditions(items):
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("条件最多 100 项")
    result, ids = [], set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str) or item["kind"] not in KINDS:
            raise ValueError("条件类型无效")
        text = strings([item.get("text")])[0]
        cid = item.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{16}", cid) or cid in ids:
            raise ValueError("条件标识无效或重复")
        if not isinstance(item.get("origin"), str) or item["origin"] not in {"user", "model", "legacy"} or not isinstance(item.get("confirmed"), bool):
            raise ValueError("条件来源或确认状态无效")
        ids.add(cid)
        aliases = strings(item.get("aliases", [text]), limit=16)
        if not aliases:
            aliases = [text]
        result.append({**item, "text": text, "aliases": aliases})
    return result


def refresh_draft(draft):
    """Compile the editable intent into the same discovery contract all runners use."""
    plan = draft["plan"]
    plan["conditions"] = validate_conditions(plan["conditions"])
    categories(draft["discovery"]["arxiv_categories"])
    confirmed = [c for c in plan["conditions"] if c["confirmed"]]
    topic = [a for c in confirmed if c["kind"] == "topic" for a in c["aliases"]]
    if not plan.get("branches") and topic:
        terms = [t for t in topic if t.casefold() not in BROAD_TERMS]
        if terms:
            plan["branches"] = [{"id": "core", "label": "核心主题", "groups": [terms[:16]]}]
    plan["branches"] = validate_branches(plan.get("branches", []), topic)
    d = draft["discovery"]
    d.update(search_plan=copy.deepcopy(plan), minimum_concept_groups=0, concept_groups=[],
             arxiv_query_terms=topic[:16], positive_keywords=[a for c in confirmed if c["kind"] in {"topic", "prefer"} for a in c["aliases"]],
             negative_keywords=[a for c in confirmed if c["kind"] == "demote" for a in c["aliases"]])
    draft["status"] = "ready" if topic and plan.get("branches") else "needs_input"
    if draft.get("generation_error"):
        draft["status"] = "failed"
    draft["description"] = d["interest_description"]
    return draft


def validate_branches(branches, topic):
    if not isinstance(branches, list) or len(branches) > 3:
        raise ValueError("检索分支最多三条")
    result, seen = [], set()
    anchors = {t.casefold() for t in topic if t.casefold() not in BROAD_TERMS}
    for b in branches:
        if not isinstance(b, dict) or not isinstance(b.get("id"), str) or b["id"] not in {"core", "context", "adjacent"} or b["id"] in seen:
            raise ValueError("检索分支标识无效或重复")
        groups = b.get("groups")
        if not isinstance(groups, list) or not 1 <= len(groups) <= 4:
            raise ValueError("每条分支需要 1–4 个概念组")
        groups = [strings(g, limit=16) for g in groups]
        if any(not g or any(re.search(r'["\\\[\]():]', t) for t in g) for g in groups):
            raise ValueError("分支需要非空短语，不接受原始查询语法")
        if not any(set(t.casefold() for t in g) <= anchors for g in groups):
            raise ValueError("每条分支需要独立的核心主题锚点组")
        seen.add(b["id"])
        result.append({"id": b["id"], "label": {"core": "核心主题", "context": "主题与场景", "adjacent": "相邻方法"}[b["id"]], "groups": groups})
    if result and "core" not in seen:
        raise ValueError("检索方案缺少核心分支")
    return sorted(result, key=lambda b: ["core", "context", "adjacent"].index(b["id"]))


def branch_budgets(branches, budget):
    # Reserve half for core, then retain only branches with a positive quota.
    branches = branches[:max(0, min(budget, 1 + budget // 2))]
    if not branches:
        return []
    if len(branches) == 1:
        return [(branches[0], budget)]
    core = (budget + 1) // 2
    rest, extra = divmod(budget - core, len(branches) - 1)
    return [(branches[0], core)] + [(b, rest + (i < extra)) for i, b in enumerate(branches[1:])]


def _reconcile_branches(draft):
    topic = [a for c in draft["plan"]["conditions"] if c["kind"] == "topic" and c["confirmed"]
             for a in c["aliases"]]
    try:
        validate_branches(draft["plan"].get("branches", []), topic)
    except ValueError:
        draft["plan"]["branches"] = []
        draft["diagnostics"].append("主题线索已改变，原查询分支失去锚点；已重新建立基础查询，请检查。")


def edit_draft(draft, changes):
    """Apply user edits without losing provenance or resurrecting deleted suggestions."""
    draft = copy.deepcopy(draft)
    old = {c["id"]: c for c in draft["plan"]["conditions"]}
    items = validate_conditions(changes.get("conditions", draft["plan"]["conditions"]))
    suppressed = set(draft.get("suppressed", []))
    for item in items:
        previous = old.get(item["id"])
        if previous is None or any(item.get(k) != previous.get(k) for k in ("text", "kind", "aliases", "confirmed")):
            item.update(origin="user", evidence="用户编辑")
            # Preserve the original suggestion's identity after renaming it.
            if previous:
                suppressed.add(condition(previous["kind"], previous["text"])["id"])
        else:
            item.update(origin=previous["origin"], evidence=previous["evidence"])
    removed = [c for c in old.values() if c["id"] not in {i["id"] for i in items}]
    suppressed.update(c["id"] for c in removed)
    suppressed.update(condition(c["kind"], c["text"])["id"] for c in removed)
    draft["suppressed"] = sorted(suppressed)
    draft["plan"]["conditions"] = items
    if "name" in changes:
        value = changes["name"]
        if not isinstance(value, str) or not 0 < len(value.strip()) <= 80:
            raise ValueError("方向名称必须是 1–80 字的文本")
        value = " ".join(value.split())
        if value != draft["name"]:
            draft["name"] = value
            draft["name_user_edited"] = True
    if "description" in changes:
        if not isinstance(changes["description"], str) or not 0 < len(changes["description"].strip()) <= 4000:
            raise ValueError("研究主题必须是 1–4000 字")
        draft["discovery"]["interest_description"] = changes["description"].strip()
    if "categories" in changes:
        draft["discovery"]["arxiv_categories"] = categories(changes["categories"])
    scope_keys = ("mode", "conferences", "conference_year_from", "conference_year_to")
    if any(key in changes for key in scope_keys):
        from .conference_scope import validate_scope
        scope = {key: changes.get(key, draft["discovery"].get(key, "latest" if key == "mode" else [] if key == "conferences" else None)) for key in scope_keys}
        scope["conferences"] = validate_scope(scope["mode"], scope["conferences"], scope["conference_year_from"], scope["conference_year_to"])
        draft["discovery"].update(scope)
    for key, upper in (("max_candidates", 5000), ("lookback_days", 365)):
        if key in changes:
            if type(changes[key]) is not int or not 1 <= changes[key] <= upper:
                raise ValueError(f"{key} 必须在 1–{upper} 之间")
            draft["discovery"][key] = changes[key]
    if "branches" in changes:
        topic = [a for c in items if c["kind"] == "topic" and c["confirmed"] for a in c["aliases"]]
        branches = validate_branches(changes["branches"], topic)
        if branches != draft["plan"].get("branches"):
            draft["plan"].update(branches=branches, branches_user_edited=True)
    draft["generation_error"] = ""
    _reconcile_branches(draft)
    return refresh_draft(draft)


def merge_regenerated_draft(draft, fresh):
    preserved = [c for c in draft["plan"]["conditions"] if c["origin"] != "model"]
    blocked = set(draft.get("suppressed", [])) | {c["id"] for c in preserved}
    blocked.update(condition(c["kind"], c["text"])["id"] for c in preserved)
    fresh["plan"]["conditions"] = preserved + [c for c in fresh["plan"]["conditions"] if c["id"] not in blocked]
    fresh.update(source=draft["source"], suppressed=draft.get("suppressed", []),
                 preview=draft.get("preview"), reference_check=draft.get("reference_check"),
                 ranking=copy.deepcopy(draft["ranking"]))
    if draft.get("name_user_edited"):
        fresh["name"] = draft["name"]
        fresh["name_user_edited"] = True
    fresh["discovery"]["interest_description"] = draft["description"]
    fresh["discovery"]["arxiv_categories"] = draft["discovery"]["arxiv_categories"]
    for key in ("max_candidates", "lookback_days", "mode", "conferences", "conference_year_from", "conference_year_to"):
        if key in draft["discovery"]:
            fresh["discovery"][key] = copy.deepcopy(draft["discovery"][key])
    if draft["plan"].get("branches_user_edited"):
        fresh["plan"].update(branches=copy.deepcopy(draft["plan"]["branches"]), branches_user_edited=True)
    _reconcile_branches(fresh)
    return refresh_draft(fresh)


def new_draft(config, profile_id, name, keywords, negative, references, description, generated,
              *, required=(), excluded=(), error="", count=None):
    conditions = [condition(kind, text) for kind, values in
                  (("topic", keywords), ("demote", negative), ("required", required), ("exclude", excluded))
                  for text in strings(list(values))]
    diagnostics = []
    for ref in references:
        if not ref["title"] or not ref["abstract"]:
            reason = ref.get("error") or "缺少标题或摘要"
            diagnostics.append(f"参考资料不可用或不完整：{ref['arxiv_id']}（{reason}）")
    try:
        if not isinstance(generated, dict):
            raise ValueError("模型必须返回 JSON 对象")
        suggestions = generated.get("conditions", [])
        if not isinstance(suggestions, list) or len(suggestions) > 60:
            raise ValueError("模型条件必须是列表且最多 60 项")
        valid_basis = {r["arxiv_id"] for r in references if r["title"] and r["abstract"]} | {"input"}
        for item in suggestions:
            if not isinstance(item, dict) or not isinstance(item.get("kind"), str) or item["kind"] not in KINDS:
                raise ValueError("模型条件类型无效")
            basis = item.get("basis")
            if isinstance(basis, str):
                # The prompt labels references as arXiv:<id>; accept that label,
                # but never strip revisions or infer an unprovided reference.
                basis = re.sub(r"^arxiv:\s*", "", basis.strip(), flags=re.I)
            if not isinstance(basis, str) or basis not in valid_basis:
                raise ValueError("模型引用了未提供或未解析的依据")
            evidence = strings([item.get("evidence")])[0]
            text = strings([item.get("text")])[0]
            candidate = condition(item["kind"], text, origin="model", aliases=item.get("aliases"),
                                  evidence=f"{basis}：{evidence}", confirmed=item["kind"] not in HARD_KINDS)
            if candidate["id"] not in {c["id"] for c in conditions}:
                conditions.append(candidate)
        for key in ("positive_keywords", "negative_keywords", "arxiv_query_terms"):
            values = strings(generated.get(key, []), limit=16 if key == "arxiv_query_terms" else 40)
            if key != "arxiv_query_terms":
                kind = "topic" if key == "positive_keywords" else "demote"
                for text in values:
                    if not any(c["kind"] == kind and c["text"].casefold() == text.casefold() for c in conditions):
                        conditions.append(condition(kind, text, origin="model", evidence="研究主题归纳"))
        # Query suggestions also provide usable topic terms for reference-only inputs.
        for text in strings(generated.get("arxiv_query_terms", []), limit=16):
            if not any(c["kind"] == "topic" and text.casefold() in [a.casefold() for a in c["aliases"]] for c in conditions):
                conditions.append(condition("topic", text, origin="model", evidence="检索建议"))
        selected_categories = categories(generated["arxiv_categories"]) if "arxiv_categories" in generated else categories(
            list(dict.fromkeys(c for ref in references for c in ref["categories"])) or DiscoveryConfig().arxiv_categories)
        interest = generated.get("interest_description", description or name)
        if not isinstance(interest, str) or not interest.strip() or len(interest) > 4000:
            raise ValueError("研究主题必须是 1–4000 字的文本")
    except ValueError as exc:
        error = str(exc)
        selected_categories, interest = DiscoveryConfig().arxiv_categories, description or name
    discovery = asdict(DiscoveryConfig())
    discovery.update(interest_description=interest, arxiv_categories=selected_categories,
                     seed_papers=[r["arxiv_id"] for r in references])
    if count is not None:
        discovery["recommendation_count"] = count
        discovery["prefilter_count"] = max(discovery["prefilter_count"], count)
    if error:
        # A partially validated model response is not a usable plan. Keep user
        # input and the first failure; its dependent branches cannot be checked.
        conditions = [c for c in conditions if c["origin"] == "user"]
    suggested_name = generated.get("profile_name") if isinstance(generated, dict) and not error else None
    if isinstance(suggested_name, str):
        suggested_name = " ".join(suggested_name.split())
    if not isinstance(suggested_name, str) or not 0 < len(suggested_name) <= 80:
        suggested_name = name
    draft = {"id": profile_id, "name": suggested_name, "revision": 1, "generation_error": error,
             "diagnostics": diagnostics, "references": references,
             "source": {"name_hint": name, "keywords": keywords, "negative_keywords": negative,
                        "reference_papers": [{"arxiv_id": r["arxiv_id"], "title": r["title"]} for r in references]},
             "plan": {"version": 2, "conditions": conditions, "branches": generated.get("branches", []) if isinstance(generated, dict) and not error else []},
             "discovery": discovery, "ranking": asdict(RankingConfig())}
    if error:
        diagnostics.append(error)
    try:
        return refresh_draft(draft)
    except ValueError as exc:
        draft["generation_error"] = str(exc)
        draft["diagnostics"].append(str(exc))
        draft["plan"]["branches"] = []
        # Invalid generated conditions cannot make a failed draft executable.
        draft["plan"]["conditions"] = [c for c in conditions if c["origin"] == "user"]
        return refresh_draft(draft)
