"""HTTP boundary for research drafts; formal profiles remain independently usable."""
from __future__ import annotations

import copy
import re
from dataclasses import asdict
from typing import Any
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .profiles import ProfileGenerator, DraftConflict
from .profile_plan import KINDS, strings, refresh_draft, validate_conditions, condition, categories, new_draft
from .profile_preview import preview_profile


def register_profile_routes(app, profiles, current_config, templates, context, jobs, project_root):
    def get_draft(draft_id):
        try:
            return profiles.draft(draft_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, "检索草稿不存在") from exc

    async def generate(data):
        name = data.get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("方向名称必须是 1–80 字的文本")
        keywords = strings(data.get("keywords", []))
        negative = strings(data.get("negative_keywords", []), limit=30)
        references = strings(data.get("reference_ids", []), limit=12)
        if any(not re.fullmatch(r"(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", r, re.I) for r in references):
            raise ValueError("参考论文 arXiv ID 无效")
        count = data.get("recommendation_count", 5)
        if type(count) is not int or not 1 <= count <= 50:
            raise ValueError("每日推荐数应为 1–50")
        description = data.get("description", "")
        if not isinstance(description, str) or len(description) > 4000:
            raise ValueError("描述最多 4000 字")
        if not keywords and not references and not description.strip():
            raise ValueError("请至少提供主题线索、描述或参考论文")
        with ProfileGenerator(current_config()) as generator:
            draft = await run_in_threadpool(generator.generate, profiles.unique_id(name), name,
                keywords, negative, references, description, count,
                required=strings(data.get("required", [])), excluded=strings(data.get("excluded", [])))
        return profiles.save_draft(draft)

    @app.post("/api/profile-drafts")
    async def create_draft(request: Request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请输入草稿对象")
            draft = await generate(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(draft, status_code=201)

    @app.get("/api/profile-drafts/{draft_id}")
    def read_draft(draft_id: str):
        return get_draft(draft_id)

    @app.post("/profiles/{profile_id}/copy")
    def copy_profile(profile_id: str):
        try:
            old = profiles.get(profile_id)
            name = f"{old['name']} 副本"
            draft_id = profiles.unique_id(name)
            d = old.get("discovery", {})
            if d.get("search_plan", {}).get("version") == 2:
                draft = copy.deepcopy(old)
                draft.update(id=draft_id, name=name, revision=1)
                draft.pop("preview", None)
                draft.pop("reference_check", None)
            else:
                references = [{**r, "abstract": "", "categories": []}
                              for r in old.get("source", {}).get("reference_papers", [])]
                draft = new_draft(current_config(), draft_id, name, d.get("positive_keywords", []),
                    d.get("negative_keywords", []), references, old.get("description", ""),
                    {"arxiv_categories": d.get("arxiv_categories") or ["cs.AI", "cs.LG"]})
                for c in draft["plan"]["conditions"]:
                    c.update(origin="legacy", evidence="历史配置，来源未记录")
                draft["diagnostics"].append(f"从旧档案复制；原概念组门槛为 {d.get('minimum_concept_groups', 0)}，新草稿将其作为主题线索，请检查差异。原档案保持不变。")
                for group in d.get("concept_groups", []):
                    if group:
                        c = condition("topic", group[0], aliases=group, origin="legacy", evidence="历史概念组")
                        if c["id"] not in {x["id"] for x in draft["plan"]["conditions"]}:
                            draft["plan"]["conditions"].append(c)
                refresh_draft(draft)
            profiles.save_draft(draft)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/profiles/drafts/{draft_id}", status_code=303)

    @app.get("/profiles/drafts/{draft_id}")
    def draft_page(request: Request, draft_id: str):
        return templates.TemplateResponse(request, "profile_draft.html", context(request, "profiles",
            draft=get_draft(draft_id), condition_kinds=KINDS))

    async def mutation_data(request, draft):
        data: dict[str, Any]
        if "application/json" in request.headers.get("content-type", ""):
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请输入草稿对象")
            return data
        form = await request.form()
        data = {"revision": int(str(form.get("revision", "0")))}
        if "edit" in request.url.path:
            items = []
            for c in draft["plan"]["conditions"]:
                prefix = c["id"]
                if form.get(f"delete-{prefix}"):
                    continue
                items.append({**c, "text": str(form.get(f"text-{prefix}", c["text"])),
                              "kind": str(form.get(f"kind-{prefix}", c["kind"])),
                              "aliases": [a.strip() for a in str(form.get(f"aliases-{prefix}", "")).splitlines() if a.strip()],
                              "confirmed": f"confirmed-{prefix}" in form})
            if str(form.get("new_text", "")).strip():
                kind = str(form.get("new_kind", "topic"))
                items.append(condition(kind, str(form["new_text"]).strip()))
            data.update(conditions=items, description=str(form.get("description", "")),
                        categories=[s.strip() for s in str(form.get("categories", "")).splitlines() if s.strip()])
        return data

    def edit(draft, data):
        if type(data.get("revision")) is not int or draft["revision"] != data["revision"]:
            raise DraftConflict("草稿已更新，请刷新后保存")
        old = {c["id"]: c for c in draft["plan"]["conditions"]}
        items = validate_conditions(data.get("conditions", draft["plan"]["conditions"]))
        for item in items:
            previous = old.get(item["id"])
            if previous is None or any(item.get(k) != previous.get(k) for k in ("text", "kind", "aliases", "confirmed")):
                item.update(origin="user", evidence="用户编辑")
            else:
                item.update(origin=previous["origin"], evidence=previous["evidence"])
        suppressed = set(draft.get("suppressed", []))
        suppressed.update(c["id"] for c in old.values() if c["id"] not in {i["id"] for i in items})
        draft["suppressed"] = sorted(suppressed)
        draft["plan"]["conditions"] = items
        if items != list(old.values()):
            draft["plan"]["branches"] = []
        if "description" in data:
            if not isinstance(data["description"], str) or not 0 < len(data["description"].strip()) <= 4000:
                raise ValueError("研究主题必须是 1–4000 字")
            draft["discovery"]["interest_description"] = data["description"].strip()
        if "categories" in data:
            draft["discovery"]["arxiv_categories"] = categories(data["categories"])
        draft["generation_error"] = ""
        refresh_draft(draft)
        return profiles.save_draft(draft, expected_revision=data["revision"])

    @app.post("/api/profile-drafts/{draft_id}/{action}")
    @app.post("/profiles/drafts/{draft_id}/{action}")
    async def mutate_draft(request: Request, draft_id: str, action: str):
        draft = get_draft(draft_id)
        try:
            data = await mutation_data(request, draft)
            if type(data.get("revision")) is not int or data["revision"] != draft["revision"]:
                raise DraftConflict("草稿已更新，请刷新后重试")
            if action == "edit":
                result = edit(draft, data)
            elif action == "activate":
                result = profiles.activate_draft(draft_id, data["revision"])
            elif action == "regenerate":
                with ProfileGenerator(current_config()) as generator:
                    user_conditions = [c for c in draft["plan"]["conditions"] if c["origin"] != "model"]
                    intent = draft["description"] + "\n已确认条件：" + "; ".join(f"{KINDS[c['kind']]}：{c['text']}" for c in user_conditions)
                    fresh = await run_in_threadpool(generator.generate, draft_id, draft["name"],
                        [], [], [r["arxiv_id"] for r in draft["references"]], intent,
                        draft["discovery"]["recommendation_count"])
                preserved = [c for c in draft["plan"]["conditions"] if c["origin"] != "model"]
                blocked = set(draft.get("suppressed", [])) | {c["id"] for c in preserved}
                fresh["plan"]["conditions"] = preserved + [c for c in fresh["plan"]["conditions"] if c["id"] not in blocked]
                fresh.update(source=draft["source"], suppressed=draft.get("suppressed", []),
                             preview=draft.get("preview"), reference_check=draft.get("reference_check"))
                fresh["discovery"]["interest_description"] = draft["description"]
                fresh["discovery"]["arxiv_categories"] = draft["discovery"]["arxiv_categories"]
                fresh["plan"]["branches"] = []
                result = profiles.save_draft(refresh_draft(fresh), expected_revision=data["revision"])
            elif action in {"preview", "references"}:
                if draft["status"] != "ready":
                    raise ValueError("请先补全草稿再检查")
                config = copy.deepcopy(current_config())
                def run_preview():
                    preview = preview_profile(draft, config, references_only=action == "references")
                    profiles.save_preview(draft_id, preview, references_only=action == "references")
                job = jobs.submit("profile-preview", f"{draft['name']} · {'参考检查' if action == 'references' else '试搜'}",
                    run_preview, identity=f"profile-preview:{draft_id}:{draft['revision']}:{action}", profile_id=config.profile_id)
                return JSONResponse(asdict(job), status_code=202)
            else:
                raise HTTPException(404, "未知草稿操作")
        except DraftConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if request.url.path.startswith("/api/"):
            return result
        return RedirectResponse("/profiles" if action == "activate" else f"/profiles/drafts/{draft_id}", status_code=303)

    return generate
