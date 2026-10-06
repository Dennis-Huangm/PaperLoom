"""Durable, revision-pinned synchronization of one paper and its optional exports."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf

from .config import AppConfig
from .library import PaperLibraryStore
from .models import Paper
from .model_budget import current_model_budget
from .obsidian import ObsidianExporter
from .paper_data import PaperResolver, matches, versioned, with_sources
from .reading_state import _locked
from .task_result import render_task_result
from .research_clients import ResearchClients
from .task_runtime import TaskCancelled, task_checkpoint, task_progress, task_warning
from .utils import read_json, write_json
from .zotero import ZoteroClient


STEP_LABELS = {"download": "元数据与 PDF", "library": "文献库", "report": "阅读报告",
               "zotero": "Zotero", "obsidian": "Obsidian"}
STATUS_LABELS = {"succeeded": "已完成", "failed": "失败，可重试", "degraded": "摘要级回退，可重试",
                 "running": "处理中", "interrupted": "已中断，可重试", "skipped": "无需更新",
                 "partial": "部分完成，可重试"}
BASE_ARXIV_ID_RE = re.compile(r"(?:[a-z-]+(?:\.[a-z]{2})?/\d{7}|\d{4}\.\d{4,5})", re.I)


class PaperVersionSync:
    def __init__(self, config: AppConfig, project_root: Path, *, clients=None):
        self.config, self.project_root = config, project_root
        output = Path(config.output_dir)
        self.output_root = (output if output.is_absolute() else project_root / output).resolve()
        self.clients = clients if clients is not None else ResearchClients(config)
        self._owns_clients = clients is None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._owns_clients:
            self.clients.close()

    def sync(self, arxiv_id: str, *, target_version: int | None = None, retry: bool = False,
             report: bool = False, zotero: bool = False, obsidian: bool = False,
             exact_options: bool = False, existing_zotero: dict | None = None) -> Path:
        if not BASE_ARXIV_ID_RE.fullmatch(arxiv_id):
            raise ValueError("同步需要不带版本后缀的 arXiv ID")
        if target_version is not None and target_version < 1:
            raise ValueError("目标修订版必须大于零")
        if retry and target_version is None:
            raise ValueError("重试必须指定原目标版本")
        self.root = self.output_root / "papers" / (self.config.profile_id or "default") / arxiv_id.replace("/", "-")
        self.state_path = self.root / "sync.json"
        with _locked(self.root / "sync.lock"):
            self.state = read_json(self.state_path, {}) or {}
            self.state.update(arxiv_id=arxiv_id, profile_id=self.config.profile_id)
            self.state.setdefault("operations", {})
            task_progress("正在确认目标修订版…", 5)
            try:
                # A latest query is authoritative: never fall back to stale caches.
                if target_version is None:
                    paper = self.clients.arxiv.get(arxiv_id)
                    if not matches(paper, arxiv_id) or not paper.version:
                        raise ValueError("arXiv 没有返回可靠的最新版本")
                    self.state["latest_version"] = paper.version
                    self.state["latest_checked_at"] = self._now()
                    target_version = paper.version
                else:
                    cached = read_json(self.root / f"v{target_version}" / "metadata.json", {}) or {}
                    paper = Paper.from_dict(cached["paper"]) if cached.get("paper") else self.clients.arxiv.get(f"{arxiv_id}v{target_version}")
                if not matches(paper, f"{arxiv_id}v{target_version}") or paper.metadata_status != "complete":
                    raise ValueError("论文元数据与目标版本不匹配或不完整")
                saved = PaperLibraryStore(self.output_root, self.config.profile_id).all().get(arxiv_id)
                if saved:
                    snapshot = Paper.from_dict(saved['paper'])
                    paper = with_sources(paper, [*paper.discovery_sources, *snapshot.discovery_sources],
                                         snapshot.conference_publications)
                paper = versioned(paper)
            except Exception as exc:
                self.state["latest_error"] = f"{type(exc).__name__}: {exc}"
                self._save()
                self._render()
                raise RuntimeError(f"未能确认目标版本，未更新本地论文：{exc}") from exc
            self.state["latest_error"] = ""
            self.state["title"] = paper.title
            self.state["last_target"] = target_version
            key = str(target_version)
            if retry and key not in self.state["operations"]:
                raise ValueError("没有该版本的同步记录")
            self.operation = self.state["operations"].setdefault(key, {"steps": {}, "options": {}})
            if not retry:
                self.operation["existing_zotero"] = existing_zotero
                if exact_options:
                    self.operation["options"] = {}
                for name, enabled in (("report", report), ("zotero", zotero), ("obsidian", obsidian)):
                    self.operation["options"][name] = self.operation["options"].get(name, False) or enabled
            self.operation.update(status="running", updated_at=self._now())
            self._save()
            folder = self.root / f"v{target_version}"
            try:
                task_progress(f"正在同步 v{target_version} 的 PDF 和元数据…", 18)
                if not self._step("download", lambda: self._download(paper, folder),
                                  lambda step: self._local_valid(folder, paper)):
                    self.operation["status"] = "failed"
                    self._save()
                    return self._render()
                # Saved papers may have been removed/re-added since the last run.
                self._step("library", lambda: self._update_library(paper, folder), lambda step: False)
                options = self.operation["options"]
                if options.get("report"):
                    task_progress("正在生成当前修订版的阅读报告…", 35)
                    self._step("report", lambda: self._report(paper, folder),
                               lambda step: self._report_path(step) is not None)
                report_step = self.operation["steps"].get("report") or {}
                report_path = self._report_path(report_step)
                signature = hashlib.sha256((folder / "paper.pdf").read_bytes() +
                            (report_path.read_bytes() if report_path else b"")).hexdigest()
                if options.get("zotero"):
                    zotero_signature = self._export_signature(signature, self.config.zotero)
                    task_progress("正在同步 Zotero…", 85)
                    self._step("zotero", lambda: self._zotero(paper, folder, report_path, zotero_signature),
                               lambda step: False)
                if options.get("obsidian"):
                    obsidian_signature = self._export_signature(signature, self.config.obsidian)
                    task_progress("正在同步 Obsidian…", 92)
                    self._step("obsidian", lambda: self._obsidian(paper, folder, report_path, obsidian_signature),
                               lambda step: False)
                active_steps = {"download", "library"} | {name for name, enabled in options.items() if enabled}
                self.operation["status"] = ("partial" if any(s["status"] in {"failed", "degraded", "interrupted"}
                    for name, s in self.operation["steps"].items() if name in active_steps) else "succeeded")
                self._save()
                return self._render()
            except TaskCancelled:
                self.operation["status"] = "interrupted"
                for step in self.operation["steps"].values():
                    if step["status"] == "running":
                        step["status"] = "interrupted"
                self._save()
                self._render()
                raise

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _export_signature(content_signature, settings):
        return hashlib.sha256((content_signature + json.dumps(asdict(settings), sort_keys=True)).encode()).hexdigest()

    def _save(self):
        write_json(self.state_path, self.state)

    def _step(self, name, action, valid=lambda step: True):
        task_checkpoint()
        old = self.operation["steps"].get(name, {})
        if old.get("status") == "succeeded" and valid(old):
            return True
        self.operation["steps"][name] = {"status": "running", "updated_at": self._now()}
        self._save()
        try:
            budget = current_model_budget()
            blocked = budget.blocked if budget else 0
            result = {"status": "succeeded", **(action() or {}), "updated_at": self._now()}
            if budget and budget.blocked > blocked:
                result.update(status="degraded", error=f"本轮模型请求已达到 {budget.limit} 次上限，可手动继续")
        except Exception as exc:
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "updated_at": self._now()}
        self.operation["steps"][name] = result
        self._save()
        if result["status"] in {"failed", "degraded"}:
            task_warning(STEP_LABELS[name], result.get("error") or "仅生成摘要级结果，修复模型或解析器后可重试")
        return result["status"] != "failed"

    @staticmethod
    def _local_valid(folder, paper):
        try:
            metadata = read_json(folder / "metadata.json", {}) or {}
            pdf = folder / "paper.pdf"
            if (metadata.get("paper", {}).get("arxiv_id") != paper.arxiv_id
                    or metadata.get("paper", {}).get("version") != paper.version):
                return False
            if metadata.get("pdf_sha256"):
                return hashlib.sha256(pdf.read_bytes()).hexdigest() == metadata["pdf_sha256"]
            # Backward compatibility for files downloaded before checksums.
            with pymupdf.open(pdf) as document:
                return document.page_count > 0
        except (OSError, ValueError, RuntimeError):
            return False

    def _download(self, paper, folder):
        if self._local_valid(folder, paper):
            return {}
        with TemporaryDirectory(prefix=".download-", dir=self.root) as temporary:
            pdf = Path(temporary) / "paper.pdf"
            self.clients.arxiv.download_pdf(paper, pdf)
            with pymupdf.open(pdf) as document:
                if not document.page_count:
                    raise ValueError("下载的 PDF 没有有效页面")
            folder.mkdir(parents=True, exist_ok=True)
            pdf.replace(folder / "paper.pdf")
            write_json(folder / "metadata.json", {"profile_id": self.config.profile_id,
                       "paper": paper.to_dict(), "downloaded_at": self._now(),
                       "pdf_sha256": hashlib.sha256((folder / "paper.pdf").read_bytes()).hexdigest()})
        PaperResolver(self.config.discovery, self.clients.arxiv, None, self.output_root).remember(paper)
        return {}

    def _update_library(self, paper, folder):
        library = PaperLibraryStore(self.output_root, self.config.profile_id)
        previous = library.all().get(paper.arxiv_id)
        if not previous or int((previous.get("paper") or {}).get("version") or 0) > paper.version:
            return {"status": "skipped"}
        before = folder / "library-before.json"
        if not before.exists():
            write_json(before, previous)
        if not library.refresh(previous, {"paper": paper.to_dict(), "verified": {}}):
            raise RuntimeError("收藏已被修改，本次未覆盖；可重试")
        return {}

    def _report(self, paper, folder):
        from .pipeline import DailyPipeline

        config = copy.deepcopy(self.config)
        config.obsidian.auto_sync = False  # Exports have their own durable stages.
        with DailyPipeline(config, self.project_root, clients=self.clients) as pipeline:
            path = pipeline.report_arxiv_id(f"{paper.arxiv_id}v{paper.version}",
                                            snapshot=paper, local_pdf=folder / "paper.pdf")
        metadata = read_json(path.with_name("metadata.json"), {})
        return {"path": path.relative_to(self.output_root).as_posix(),
                "status": "succeeded" if metadata.get("report_quality") == "full" else "degraded"}

    def _report_path(self, step):
        if not step.get("path"):
            return None
        path = (self.output_root / step["path"]).resolve()
        if path.is_relative_to(self.output_root) and path.is_file() and path.with_suffix(".html").is_file():
            return path
        return None

    def _collect(self, paper, folder, report_path, signature, target):
        from .collaboration import PaperCollection
        metadata = read_json(report_path.with_name("metadata.json"), {}) if report_path else {}
        operation = PaperCollection(self.config, self.project_root, clients=self.clients).collect(
            {"paper": paper.to_dict(), "verified": metadata.get("verified") or {}},
            zotero=target == "zotero", obsidian=target == "obsidian", report_path=report_path,
            pdf_path=folder / "paper.pdf", zotero_factory=ZoteroClient)
        step = operation['targets'][target]
        if step['status'] != 'succeeded':
            raise ValueError(step.get('error') or '收录未完成，请查看收录回执')
        result = {"signature": signature, "operation_id": operation['operation_id']}
        if target == 'zotero':
            result['item_key'] = step['item_key']
        else:
            result['path'] = str(Path(self.config.obsidian.vault_path) / step['path'])
        return result

    def _zotero(self, paper, folder, report_path, signature):
        if not self.config.zotero.attach_pdf:
            raise ValueError("请先在 Zotero 设置中启用 PDF 附件同步")
        existing = self.operation.get("existing_zotero")
        if existing is not None:
            keys = existing.get("keys", [])
            if len(keys) != 1 or not existing.get("library"):
                raise ValueError("Zotero 关联未明确，请重新检查来源后预览")
            client = ZoteroClient(self.config.zotero)
            try:
                if client.library_identity() != existing["library"]:
                    raise ValueError("Zotero 文献库已切换，请重新预览")
                wrapper = client.get_item(keys[0])
                if not wrapper or paper.arxiv_id not in client._arxiv_ids(wrapper.get("data", wrapper)):
                    raise ValueError("Zotero 原条目已移除或关联改变，请重新核实")
                receipt = self.operation.setdefault("zotero_receipt", {})
                result = client.save_paper(paper.to_dict(), {}, self.config.profile_name,
                    pdf_path=folder / "paper.pdf", collection_key="__root__", selected_key=keys[0],
                    receipt=receipt, checkpoint=self._save)
                return {**result.to_dict(), "signature": signature, "version": paper.version,
                        "library": existing["library"], "verified_at": self._now()}
            finally:
                client.client.close()
        return self._collect(paper, folder, report_path, signature, 'zotero')

    def _obsidian(self, paper, folder, report_path, signature):
        return self._collect(paper, folder, report_path, signature, 'obsidian')

    def _render(self):
        lines = [f"# {self.state.get('title') or self.state['arxiv_id']} · 版本同步", ""]
        from urllib.parse import urlencode
        lines += ["[查看收录回执](/collection?" + urlencode({'arxiv_id': self.state['arxiv_id']}) + ")", ""]
        if self.state.get("latest_error"):
            lines += ["未确认最新版本：" + self.state["latest_error"], ""]
        for version, operation in sorted(self.state["operations"].items(), key=lambda item: int(item[0]), reverse=True):
            lines += [f"## v{version} · {STATUS_LABELS.get(operation.get('status'), '待处理')}", ""]
            active = {"download", "library"} | {name for name, enabled in operation.get("options", {}).items() if enabled}
            for name, step in operation["steps"].items():
                if name not in active:
                    continue
                lines.append(f"- {STEP_LABELS[name]}：{STATUS_LABELS.get(step['status'], step['status'])}" +
                             (f" — {step['error']}" if step.get("error") else ""))
            if (self.root / f"v{version}/paper.pdf").is_file():
                lines += ["", f"[打开 PDF](v{version}/paper.pdf)"]
            report = self._report_path(operation["steps"].get("report", {}))
            if report:
                from os.path import relpath
                lines += ["", f"[打开报告]({Path(relpath(report.with_suffix('.html'), self.root)).as_posix()})"]
        destination = self.root / "index.html"
        render_task_result("\n".join(lines), destination, "论文版本同步", 'sync')
        return destination
