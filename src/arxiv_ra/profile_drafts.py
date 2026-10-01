"""Research draft lifecycle shared by JSON and form adapters.

Generation runs outside storage locks. Completion commits against the revision
that was read before generation; ProfileManager owns the atomic conflict checks.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .profiles import ProfileGenerator, DraftConflict
from .profile_plan import KINDS, strings, edit_draft, merge_regenerated_draft
from .profile_preview import preview_profile


DRAFT_ACTIONS = {"edit", "delete", "restore", "activate", "regenerate", "preview", "references"}


class ProfileDraftWorkflow:
    def __init__(self, profiles, config, jobs):
        self.profiles = profiles
        self.config = copy.deepcopy(config)
        self.jobs = jobs

    def create(self, data):
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
        request_id = data.get("request_id", "")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,100}", request_id):
            raise ValueError("创建请求标识无效")
        required = strings(data.get("required", []))
        excluded = strings(data.get("excluded", []))
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        draft_id = "draft-" + hashlib.sha256(request_id.encode()).hexdigest()[:24] if request_id else self.profiles.unique_id(name)
        if request_id:
            try:
                existing = self.profiles.draft(draft_id)
            except KeyError:
                pass
            else:
                if existing.get("request_fingerprint") != fingerprint:
                    raise DraftConflict("同一次创建请求的内容已改变，请刷新后重新创建")
                return existing
        with ProfileGenerator(self.config) as generator:
            draft = generator.generate(draft_id, name, keywords, negative, references, description, count,
                                       required=required, excluded=excluded)
        draft.update(request_id=request_id, request_fingerprint=fingerprint)
        return self.profiles.save_draft(draft)

    def mutate(self, draft_id, action, data):
        if action not in DRAFT_ACTIONS:
            raise ValueError("未知草稿操作")
        revision = data.get("revision")
        if action == "restore":
            if type(revision) is not int:
                raise ValueError("草稿版本无效")
            return self.profiles.restore_draft(draft_id, revision)
        draft = self.profiles.draft(draft_id)
        if type(revision) is not int or revision != draft["revision"]:
            raise DraftConflict("草稿已更新，请刷新后重试")
        if action == "edit":
            return self.profiles.save_draft(edit_draft(draft, data), expected_revision=revision)
        if action == "delete":
            self.profiles.delete_draft(draft_id, revision)
            return {"deleted": True, "id": draft_id}
        if action == "activate":
            return self.profiles.activate_draft(draft_id, revision)
        if action == "regenerate":
            user_conditions = [c for c in draft["plan"]["conditions"] if c["origin"] != "model"]
            intent = draft["description"] + "\n已确认条件：" + "; ".join(
                f"{KINDS[c['kind']]}：{c['text']}" for c in user_conditions)
            with ProfileGenerator(self.config) as generator:
                fresh = generator.generate(draft_id, draft.get("source", {}).get("name_hint") or draft["name"],
                    [], [], [r["arxiv_id"] for r in draft["references"]], intent,
                    draft["discovery"]["recommendation_count"])
            return self.profiles.save_draft(merge_regenerated_draft(draft, fresh), expected_revision=revision)
        return self._preview(draft, action, data)

    def _preview(self, draft, action, data):
        if draft["status"] != "ready":
            raise ValueError("请先补全草稿再检查")
        days = data.get("lookback_days", 90)
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("试搜范围必须为 1–365 天")
        references_only = action == "references"
        def run():
            preview = preview_profile(draft, self.config, references_only=references_only, lookback_days=days)
            self.profiles.save_preview(draft["id"], preview, references_only=references_only)
        return self.jobs.submit("profile-preview",
            f"{draft['name']} · {'参考检查' if references_only else '试搜'}", run,
            identity=f"profile-preview:{draft['id']}:{draft['revision']}:{action}:{days}",
            profile_id=self.config.profile_id)
