"""HTTP boundary for research drafts; formal profiles remain independently usable."""
from __future__ import annotations

import copy
import re
from dataclasses import asdict
from datetime import datetime
from typing import Any
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .profiles import DraftConflict
from .profile_plan import KINDS, categories, strings, refresh_draft, condition, new_draft, edit_draft
from .profile_drafts import ProfileDraftWorkflow, DRAFT_ACTIONS


def register_profile_routes(app, profiles, current_config, templates, context, jobs, project_root):
    def get_draft(draft_id):
        try:
            return profiles.draft(draft_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, "检索草稿不存在") from exc

    def get_saved(profile_id):
        try:
            return profiles.get(profile_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, "研究方向不存在") from exc

    @app.post("/profiles/deleted-drafts/clear")
    def clear_deleted_drafts():
        try:
            profiles.clear_deleted_drafts()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, "清空已删除草稿失败，请检查文件权限后重试") from exc
        return RedirectResponse("/profiles#deleted-drafts", status_code=303)

    @app.get("/profiles/{profile_id}/edit")
    def edit_saved_page(request: Request, profile_id: str):
        profile = get_saved(profile_id)
        return templates.TemplateResponse(request, "profile_edit.html", context(request, "profiles",
            profile=profile, profile_revision=profiles.saved_revision(profile_id), condition_kinds=KINDS,
            versioned=(profile.get("plan") or {}).get("version") == 2))

    @app.post("/profiles/{profile_id}/edit")
    async def edit_saved_profile(request: Request, profile_id: str):
        original = get_saved(profile_id)
        form = await request.form()
        revision = str(form.get("profile_revision", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise HTTPException(400, "研究方向版本无效")
        try:
            name = " ".join(str(form.get("name", "")).split())
            description = str(form.get("description", "")).strip()
            if not 0 < len(name) <= 80 or not 0 < len(description) <= 4000:
                raise ValueError("方向名称或研究主题长度无效")
            selected_categories = categories([s.strip() for s in str(form.get("categories", "")).splitlines() if s.strip()])
            numbers = {}
            for key, maximum in (("lookback_days", 365), ("recommendation_count", 50), ("max_candidates", 5000)):
                value = int(str(form.get(key, "")))
                if not 1 <= value <= maximum:
                    raise ValueError(f"{key} 必须在 1–{maximum} 之间")
                numbers[key] = value
            if (original.get("plan") or {}).get("version") == 2:
                changes = await mutation_data(request, original, "edit")
                changes.update(name=name, description=description, categories=selected_categories,
                               lookback_days=numbers["lookback_days"], max_candidates=numbers["max_candidates"])
                updated = edit_draft(original, changes)
                if updated["status"] != "ready":
                    raise ValueError("请保留至少一个可用的主题条件和检索分支")
            else:
                updated = copy.deepcopy(original)
                updated.update(name=name, description=description)
                updated.setdefault("discovery", {}).update(
                    interest_description=description, arxiv_categories=selected_categories,
                    positive_keywords=strings([s.strip() for s in str(form.get("keywords", "")).splitlines() if s.strip()]),
                    negative_keywords=strings([s.strip() for s in str(form.get("negative_keywords", "")).splitlines() if s.strip()]),
                )
            updated["discovery"].update(numbers)
            updated["discovery"]["prefilter_count"] = max(
                updated["discovery"].get("prefilter_count", 0), numbers["recommendation_count"])
            updated["updated_at"] = datetime.now().isoformat(timespec="seconds")
            profiles.replace_saved(profile_id, updated, revision)
        except DraftConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/profiles?updated={profile_id}#saved-profiles", status_code=303)

    async def generate(data):
        workflow = ProfileDraftWorkflow(profiles, current_config(), jobs)
        return await run_in_threadpool(workflow.create, data)

    @app.post("/api/profile-drafts")
    async def create_draft(request: Request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请输入草稿对象")
            draft = await generate(data)
        except DraftConflict as exc:
            raise HTTPException(409, str(exc)) from exc
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

    async def mutation_data(request, draft, action):
        data: dict[str, Any]
        if "application/json" in request.headers.get("content-type", ""):
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请输入草稿对象")
            return data
        form = await request.form()
        data = {"revision": int(str(form.get("revision", "0")))}
        if action == "preview":
            data["lookback_days"] = int(str(form.get("lookback_days", "90")))
        if action == "edit":
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
            data.update(conditions=items, name=str(form.get("name", "")), description=str(form.get("description", "")),
                        categories=[s.strip() for s in str(form.get("categories", "")).splitlines() if s.strip()])
            if "branch-core" in form:
                data["branches"] = [{"id": key, "groups": [[t.strip() for t in line.split(",") if t.strip()]
                    for line in str(form.get(f"branch-{key}", "")).splitlines() if line.strip()]}
                    for key in ("core", "context", "adjacent") if str(form.get(f"branch-{key}", "")).strip()]
            for key in ("max_candidates", "lookback_days"):
                if key in form:
                    data[key] = int(str(form[key]))
        return data

    @app.post("/api/profile-drafts/{draft_id}/{action}")
    @app.post("/profiles/drafts/{draft_id}/{action}")
    async def mutate_draft(request: Request, draft_id: str, action: str):
        if action not in DRAFT_ACTIONS:
            raise HTTPException(404, "未知草稿操作")
        draft = {} if action == "restore" else get_draft(draft_id)
        try:
            data = await mutation_data(request, draft, action)
            workflow = ProfileDraftWorkflow(profiles, current_config(), jobs)
            result = await run_in_threadpool(workflow.mutate, draft_id, action, data)
        except KeyError as exc:
            raise HTTPException(404, "已删除草稿不存在" if action == "restore" else "检索草稿不存在") from exc
        except DraftConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if action in {"preview", "references"}:
            return JSONResponse(asdict(result), status_code=202)
        if request.url.path.startswith("/api/"):
            return result
        if action == "delete":
            destination = "/profiles#deleted-drafts"
        elif action == "activate":
            destination = "/profiles"
        else:
            destination = f"/profiles/drafts/{draft_id}"
        return RedirectResponse(destination, status_code=303)

    return generate
