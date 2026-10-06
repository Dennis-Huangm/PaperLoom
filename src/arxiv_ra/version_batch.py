"""Durable manual batches, with immutable targets and opt-in export scope."""
from __future__ import annotations

import copy
import re
import uuid
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig
from .model_budget import model_request_budget
from .reading_state import _locked
from .task_result import render_task_result
from .task_runtime import TaskCancelled, task_checkpoint, task_progress, task_subtask, task_warning
from .utils import read_json, write_json
from .version_scope import can_supplement, in_scope, update_options, validate_scope
from .version_sync import BASE_ARXIV_ID_RE, PaperVersionSync, STATUS_LABELS, STEP_LABELS


BATCH_LABELS = {**STATUS_LABELS, "preview": "待执行", "pending": "待处理"}
MAX_BATCH_SIZE = 200


def update_candidates(items: list[dict], scope: str | None = None) -> list[dict]:
    """No network on page reads; unknown remote revisions require a check first."""
    if scope is not None:
        return [item for item in items if in_scope(item, scope) and any(update_options(item, scope).values())]
    return [item for item in items if int(item.get("latest_version") or 0) > 0
            and (int(item.get("local_version") or 0) < int(item["latest_version"])
                 or (item.get("saved_version") and int(item["saved_version"]) < int(item["latest_version"])))]


class VersionSyncBatch:
    def __init__(self, config: AppConfig, project_root: Path):
        self.config = copy.deepcopy(config)
        self.project_root = project_root.resolve()
        output = Path(config.output_dir)
        self.output_root = (output if output.is_absolute() else self.project_root / output).resolve()
        self.root = self.output_root / "version-batches" / (config.profile_id or "default")

    def _path(self, batch_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
            raise ValueError("无效的批次编号")
        return self.root / batch_id / "batch.json"

    def read(self, batch_id: str) -> dict:
        state = read_json(self._path(batch_id), None)
        if not state or state.get("profile_id") != self.config.profile_id:
            raise ValueError("当前方向不存在该批次")
        return state

    def preview(self, selected: list[str], candidates: list[dict], *, report=False, zotero=False, obsidian=False,
                origin="manual", scope: str | None = None, supplement: bool = False) -> dict:
        selected = list(dict.fromkeys(selected))
        if not selected or len(selected) > MAX_BATCH_SIZE:
            raise ValueError(f"请选择 1–{MAX_BATCH_SIZE} 篇论文")
        if scope is not None:
            validate_scope(scope)
        eligible = update_candidates(candidates, scope)
        if supplement:
            if scope not in {"all", "zotero"} or origin != "manual":
                raise ValueError("补充附件只适用于 Zotero 待核实论文")
            eligible = [item for item in candidates if can_supplement(item, scope)]
        available = {item["arxiv_id"]: item for item in eligible}
        if any(not BASE_ARXIV_ID_RE.fullmatch(aid) or aid not in available for aid in selected):
            raise ValueError("所选论文不在当前更新清单中，请刷新页面或先检查新版本")
        per_item = {}
        if scope is not None:
            for aid in selected:
                options = update_options(available[aid], scope)
                if supplement:
                    options = {"report": False, "zotero": True, "obsidian": False}
                if origin == "automatic":
                    options = {"report": options["report"] and report,
                               "zotero": options["zotero"] and zotero, "obsidian": False}
                    options = {name: enabled and name not in available[aid].get("attempted_targets", [])
                               for name, enabled in options.items()}
                if not any(options.values()):
                    raise ValueError("所选论文没有可更新的材料")
                per_item[aid] = options
            report = any(value["report"] for value in per_item.values())
            zotero = any(value["zotero"] for value in per_item.values())
            obsidian = False
        if zotero and (not self.config.zotero.enabled or not self.config.zotero.attach_pdf):
            raise ValueError("请先启用 Zotero 和 PDF 附件同步")
        if obsidian and (not self.config.obsidian.enabled or not self.config.obsidian.vault_path):
            raise ValueError("请先配置并启用 Obsidian")
        if obsidian and report and not self.config.obsidian.sync_reports:
            raise ValueError("请先启用 Obsidian 报告同步")
        now = self._now()
        state = {"version": 1, "id": uuid.uuid4().hex, "profile_id": self.config.profile_id,
                 "profile_name": self.config.profile_name, "created_at": now, "updated_at": now,
                 "status": "preview", "origin": origin,
                 "options": {"report": bool(report), "zotero": bool(zotero), "obsidian": bool(obsidian)},
                 "config": asdict(self.config), "items": [], "scope": scope, "supplement": supplement}
        for aid in selected:
            item = available[aid]
            state["items"].append({"arxiv_id": aid, "title": item.get("title") or aid,
                                   "local_version": item.get("local_version"), "saved_version": item.get("saved_version"),
                                   "target_version": int(item["latest_version"]), "checked_at": item.get("checked_at") or "",
                                   "status": "pending", "steps": {},
                                   **({"options": per_item[aid],
                                       "report_version": item.get("report_version"),
                                       "zotero_version": item.get("zotero_version"),
                                       "existing_zotero": {"keys": item.get("zotero_keys", []),
                                                           "library": item.get("zotero_library", "")}}
                                      if scope is not None else {})})
        self._save(state)
        return state

    def recent(self, limit=20) -> list[dict]:
        result = []
        for path in self.root.glob("*/batch.json"):
            try:
                state = self.read(path.parent.name)
                result.append(state)
            except (ValueError, OSError):
                continue
        ordered = sorted(result, key=lambda state: state["created_at"], reverse=True)
        latest_automatic = next((state["id"] for state in ordered if state.get("origin") == "automatic"), None)
        return [state for index, state in enumerate(ordered)
                if limit is None or index < limit or state["status"] not in {"succeeded", "preview"}
                or state["id"] == latest_automatic
                or state.get("origin") == "automatic" and state["status"] == "preview"]

    @staticmethod
    def summary(state: dict) -> dict:
        items = state["items"]
        succeeded = sum(item["status"] == "succeeded" for item in items)
        return {"total": len(items), "succeeded": succeeded,
                "failed": sum(item["status"] in {"failed", "partial", "interrupted"} for item in items),
                "remaining": len(items) - succeeded,
                "downloads": len(items),
                "missing_pdf": sum(int(item.get("local_version") or 0) < item["target_version"] for item in items),
                "reports": sum(bool(item.get("options", state["options"])["report"]) for item in items),
                "zotero": sum(bool(item.get("options", state["options"])["zotero"]) for item in items),
                "note_summaries": len(items) if state["options"]["obsidian"] else 0}

    def _snapshot_config(self, state: dict) -> AppConfig:
        config = AppConfig()
        for field in fields(config):
            if field.name in state["config"]:
                value = copy.deepcopy(state["config"][field.name])
                previous = getattr(config, field.name)
                setattr(config, field.name, type(previous)(**value) if is_dataclass(previous) else value)
        if config.profile_id != self.config.profile_id:
            raise ValueError("批次研究方向不匹配")
        # Paths remain within the output root even if the config file changes later.
        config.output_dir = str(self.output_root)
        return config

    def run(self, batch_id: str, *, clients=None) -> Path:
        path = self._path(batch_id)
        with _locked(path.with_suffix(".lock")):
            state = self.read(batch_id)
            if state["status"] == "succeeded":
                return self._render(state)
            state["status"] = "running"
            self._save(state)
            self._render(state)
            current = None
            try:
                config = self._snapshot_config(state)
                limit = config.version_sync.max_model_calls if state.get("origin") == "automatic" else None
                if limit is not None:
                    config.version_sync.validate()
                state["model_requests_this_run"] = 0

                def record_request(used):
                    state["model_requests_this_run"] = used
                    state["model_requests_total"] = state.get("model_requests_total", 0) + 1
                    self._save(state)

                with model_request_budget(limit, record_request), PaperVersionSync(config, self.project_root, clients=clients) as sync:
                    total = len(state["items"])
                    for index, item in enumerate(state["items"]):
                        current = item
                        if item["status"] == "succeeded":
                            continue
                        task_checkpoint()
                        item.update(status="running", error="", current_stage="准备同步")
                        self._save(state)

                        def progress(detail, percent):
                            item.update(current_stage=detail, progress=percent)
                            self._save(state)

                        label = f"{index + 1}/{total} · {item['arxiv_id']} v{item['target_version']}"
                        try:
                            with task_subtask(label, index * 100 / total, (index + 1) * 100 / total, progress):
                                result = sync.sync(item["arxiv_id"], target_version=item["target_version"],
                                                   exact_options=True, **item.get("options", state["options"]),
                                                   **({"existing_zotero": item["existing_zotero"]} if "existing_zotero" in item else {}))
                            active = {"download", "library"} | {name for name, enabled in item.get("options", state["options"]).items() if enabled}
                            item.update(status=sync.operation["status"], result=result.relative_to(self.output_root).as_posix(),
                                        steps={name: copy.deepcopy(step) for name, step in sync.operation["steps"].items() if name in active},
                                        progress=100, current_stage="本篇处理结束")
                        except Exception as exc:
                            item.update(status="failed", error=f"{type(exc).__name__}: {exc}", current_stage="同步失败")
                            self._save(state)
                            task_warning(label, item["error"])
                        self._save(state)
                        self._render(state)
                        task_progress(f"已处理 {index + 1}/{total} 篇", round((index + 1) * 100 / total))
                state["status"] = "succeeded" if all(item["status"] == "succeeded" for item in state["items"]) else "partial"
            except TaskCancelled:
                if current and current["status"] == "running":
                    current.update(status="interrupted", current_stage="已中断，可继续")
                state["status"] = "interrupted"
                self._save(state)
                self._render(state)
                raise
            self._save(state)
            return self._render(state)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _save(self, state):
        state["updated_at"] = self._now()
        write_json(self._path(state["id"]), state)

    def _render(self, state):
        summary = self.summary(state)
        lines = [f"# {state['profile_name']} · 批量版本同步", "",
                 f"{BATCH_LABELS[state['status']]} · 已完成 {summary['succeeded']}/{summary['total']} 篇", "",
                 "返回应用的追踪页可继续未完成论文；重试沿用原目标版本和所选步骤。", ""]
        for item in state["items"]:
            lines += [f"## {item['title']} · v{item['target_version']}", "",
                      f"arXiv:{item['arxiv_id']} · {BATCH_LABELS[item['status']]}", ""]
            if item.get("error"):
                lines += [item["error"], ""]
            for name, step in item["steps"].items():
                lines.append(f"- {STEP_LABELS[name]}：{BATCH_LABELS.get(step['status'], step['status'])}" +
                             (f" — {step['error']}" if step.get("error") else ""))
            if item.get("result"):
                lines += ["", f"[查看论文同步记录](../../../{item['result']})", ""]
        destination = self._path(state["id"]).with_name("index.html")
        render_task_result("\n".join(lines), destination, "批量版本同步", 'batch')
        return destination
