"""Apply a direction's opt-in policy after a version check.

The caller holds the tracker lock through planning and execution. Batch plans are
also the durable attempt ledger: a revision is never automatically retried after
failure, cancellation, or process loss. Recovery is explicit in batch history.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .task_runtime import TaskCancelled, task_checkpoint, task_warning
from .utils import read_json, write_json
from .version_scope import update_options
from .version_batch import VersionSyncBatch, update_candidates
from .version_sync import BASE_ARXIV_ID_RE
from .web_catalog import version_tracking_data


class AutomaticVersionSync:
    def __init__(self, config: AppConfig, project_root: Path):
        self.config = config
        self.batches = VersionSyncBatch(config, project_root)
        self.state_path = self.batches.root / "automatic.json"

    def latest(self) -> dict:
        result = read_json(self.state_path, {}) or {}
        if result.get("batch_id"):
            try:
                batch = self.batches.read(result["batch_id"])
                result.update(status=batch["status"], summary=self.batches.summary(batch))
            except (ValueError, OSError):
                pass
        return result

    def run(self, tracked: dict, *, clients=None) -> dict | None:
        policy = self.config.version_sync
        if not self.config.version_tracking.enabled or not policy.enabled:
            return None
        task_checkpoint()
        result: dict[str, Any] = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "running",
                  "policy": asdict(policy), "selected": 0, "deferred": 0, "held": 0}
        try:
            policy.validate()
            # Re-read actual files/reading state; tracker counters can be stale.
            items = version_tracking_data(self.batches.output_root, self.config.profile_id)["items"]
            candidates = []
            for item in items:
                aid = item["arxiv_id"]
                source = tracked.get(aid, {})
                if (BASE_ARXIV_ID_RE.fullmatch(aid) and source.get("tracked")
                        and not source.get("check_error") and source.get("latest_version")):
                    candidates.append({**item, "latest_version": source["latest_version"]})
            attempted = {(item["arxiv_id"], item["target_version"], target)
                         for batch in self.batches.recent(limit=None) if batch.get("origin") == "automatic"
                         for item in batch["items"]
                         for target, enabled in item.get("options", batch["options"]).items() if enabled}
            eligible = []
            for item in update_candidates(candidates, "all"):
                options = update_options(item)
                if not (options["report"] and policy.report or options["zotero"] and policy.zotero):
                    continue
                item["attempted_targets"] = [target for target in ("report", "zotero")
                    if (item["arxiv_id"], item["latest_version"], target) in attempted]
                if not any(enabled and getattr(policy, target) and target not in item["attempted_targets"]
                           for target, enabled in options.items()):
                    result["held"] += 1
                else:
                    eligible.append(item)
            # Oldest known update first, stable across page sorting and restarts.
            eligible.sort(key=lambda item: (item.get("updated") or "", item["arxiv_id"]))
            limit = policy.max_papers
            if policy.report:
                limit = min(limit, policy.max_model_papers)
            selected = eligible[:limit]
            result.update(selected=len(selected), deferred=len(eligible) - len(selected))
            if not selected:
                result["status"] = "idle"
            else:
                batch = self.batches.preview([item["arxiv_id"] for item in selected], selected,
                                             report=policy.report, zotero=policy.zotero,
                                             origin="automatic", scope="all")
                result["batch_id"] = batch["id"]
                write_json(self.state_path, result)
                self.batches.run(batch["id"], clients=clients)
                batch = self.batches.read(batch["id"])
                result.update(status=batch["status"], summary=self.batches.summary(batch))
        except TaskCancelled:
            result["status"] = "interrupted"
            write_json(self.state_path, result)
            raise
        except Exception as exc:
            result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            write_json(self.state_path, result)
            task_warning("自动版本同步", result["error"])
            return result
        write_json(self.state_path, result)
        return result
