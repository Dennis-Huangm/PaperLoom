from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pymupdf as fitz

from .config import AppConfig
from .feedback import FeedbackStore
from .library import PaperLibraryStore
from .reading_state import _locked
from .report_store import profile_matches
from .research_clients import ResearchClients
from .render import render_report
from .utils import read_json, write_json
from .zotero import ZoteroClient
from .task_runtime import task_progress, task_warning, task_subtask


class VersionTracker:
    def __init__(self, config: AppConfig, project_root: Path, *, clients: ResearchClients | None = None) -> None:
        self.config = config
        self.project_root = project_root
        self.auto_sync_result = None
        output = Path(config.output_dir)
        self.output_root = output if output.is_absolute() else project_root / output
        suffix = config.profile_id or "default"
        self.state_path = self.output_root / f"version-state-{suffix}.json"
        self.clients = clients if clients is not None else ResearchClients(config)
        self._owns_clients = clients is None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._owns_clients:
            self.clients.close()

    def tracked_sources(self) -> dict[str, dict[str, Any]]:
        sources: dict[str, dict[str, Any]] = {}

        dismissed = FeedbackStore(self.output_root, self.config.profile_id).all()

        def add(arxiv_id: str, title: str, source: str, **versions) -> None:
            arxiv_id = re.sub(r"v\d+$", "", arxiv_id.strip(), flags=re.I)
            if not arxiv_id or arxiv_id in dismissed:
                return
            item = sources.setdefault(arxiv_id, {"arxiv_id": arxiv_id, "title": title, "sources": []})
            if title and not item.get("title"):
                item["title"] = title
            if source not in item["sources"]:
                item["sources"].append(source)
            for key, version in versions.items():
                if version:
                    item[key] = max(int(item.get(key) or 0), int(version))

        if self.config.version_tracking.include_library:
            for arxiv_id, entry in PaperLibraryStore(self.output_root, self.config.profile_id).all().items():
                paper = entry.get("paper") or {}
                add(arxiv_id, str(paper.get("title") or ""), "文献库",
                    saved_version=paper.get("version"))

        if self.config.version_tracking.include_reports:
            for path in self.output_root.glob("????-??-??/reports/*/metadata.json"):
                payload = read_json(path, {}) or {}
                if not profile_matches(payload, self.config.profile_id):
                    continue
                paper = payload.get("paper") or {}
                add(str(paper.get("arxiv_id") or ""), str(paper.get("title") or ""), "本地报告",
                    report_version=paper.get("version") if path.with_name("report.html").exists() else None,
                    local_version=paper.get("version") if path.with_name("paper.pdf").exists() else None)
        for path in (self.output_root / "papers" / (self.config.profile_id or "default")).glob("*/v*/metadata.json"):
            paper = (read_json(path, {}) or {}).get("paper") or {}
            if path.with_name("paper.pdf").is_file():
                add(str(paper.get("arxiv_id") or ""), str(paper.get("title") or ""), "已同步文件",
                    local_version=paper.get("version"))
        if self.config.version_tracking.include_zotero and self.config.zotero.enabled:
            try:
                zotero = ZoteroClient(self.config.zotero, timeout=3.0)
                try:
                    for paper in zotero.list_trackable_papers():
                        add(paper["arxiv_id"], paper["title"], "Zotero")
                finally:
                    zotero.client.close()
            except Exception as exc:
                task_warning(
                    "Zotero 版本来源",
                    f"无法读取 Zotero 条目，本次仅追踪本地来源：{type(exc).__name__}: {exc}",
                )
        return sources

    def check(self, now: datetime | None = None) -> Path:
        with _locked(self.state_path.with_suffix(".lock")):
            return self._check(now)

    def _check(self, now: datetime | None = None) -> Path:
        task_progress("正在汇总需要追踪的论文…", 8)
        now = now or datetime.now(ZoneInfo(self.config.timezone))
        sources = self.tracked_sources()
        payload = read_json(self.state_path, {}) or {}
        state = payload.get("items", {}) if isinstance(payload.get("items", {}), dict) else {}
        sequence = int(payload.get("check_number") or 0) + 1
        for aid, item in state.items():
            item["tracked"] = aid in sources
        for aid, source in sources.items():
            state[aid] = {**state.get(aid, {}), **source, "tracked": True}
        batch = sorted(sources, key=lambda aid: (int(state[aid].get("attempt_number") or 0), aid))[
            :max(1, self.config.version_tracking.max_tracked)]

        def persist():
            write_json(self.state_path, {"version": 1, "checked_at": now.isoformat(),
                       "check_number": sequence, "coverage": {"total": len(sources), "checked": len(batch),
                       "pending": len(sources) - len(batch)}, "items": state})

        persist()
        task_progress(f"正在查询本轮 {len(batch)}/{len(sources)} 篇论文的最新版本…", 24)
        failures = {}
        try:
            current = {paper.arxiv_id: paper for paper in self.clients.arxiv.get_many(batch)} if batch else {}
        except Exception:
            current = {}
            for aid in batch:
                try:
                    current.update({paper.arxiv_id: paper for paper in self.clients.arxiv.get_many([aid])})
                except Exception as exc:
                    failures[aid] = f"{type(exc).__name__}: {exc}"
        new_events: list[dict[str, Any]] = []
        total = len(batch)
        for index, arxiv_id in enumerate(batch, start=1):
            source = sources[arxiv_id]
            task_progress(
                f"正在比对版本（{index}/{total}）：arXiv:{arxiv_id}",
                45 + round(35 * (index - 1) / max(total, 1)),
            )
            paper = current.get(arxiv_id)
            previous = state[arxiv_id]
            previous.update(attempt_number=sequence, attempted_at=now.isoformat())
            if not paper or not paper.version:
                previous["check_error"] = failures.get(arxiv_id, "arXiv 未返回可靠版本号")
                persist()
                task_warning("版本查询", f"{arxiv_id}：{previous['check_error']}")
                continue
            old_version = int(previous.get("latest_version") or 0)
            item = {
                **previous,
                "arxiv_id": arxiv_id,
                "title": paper.title or source.get("title", ""),
                "latest_version": max(old_version, paper.version),
                "updated": paper.updated.isoformat() if paper.updated else "",
                "abs_url": paper.abs_url,
                "sources": source.get("sources", []),
                "checked_at": now.isoformat(),
                "events": list(previous.get("events") or []),
                "check_error": "",
            }
            if old_version and paper.version > old_version:
                event = {"detected_at": now.isoformat(), "from_version": old_version,
                         "to_version": paper.version, "status": "pending"}
                item["events"].append(event)
                new_events.append(event)
            state[arxiv_id] = item
            persist()  # Detection survives any PDF/LLM failure or cancellation.
            for event in item["events"]:
                if event.get("to_version") != paper.version or event.get("status") not in {"pending", "failed"}:
                    continue
                try:
                    detected_at = event["detected_at"]
                    event.update(self._version_event(paper, event["from_version"], now),
                                 status="succeeded", error="")
                    event["detected_at"] = detected_at
                except Exception as exc:
                    event.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                    task_warning("版本差异", f"{arxiv_id} 新版已记录，差异分析失败：{exc}")
                persist()
        task_progress(f"版本比对完成，发现 {len(new_events)} 个更新，正在生成结果…", 90)
        # Keep the tracker lock until the automatic plan is persisted/executed,
        # so concurrent CLI, daily and web checks cannot duplicate a round.
        from .version_auto import AutomaticVersionSync
        with task_subtask("自动版本同步", 91, 99):
            self.auto_sync_result = AutomaticVersionSync(self.config, self.project_root).run(state, clients=self.clients)
        return self._render_overview(state, new_events, now)

    def _version_event(self, paper, old_version: int, now: datetime) -> dict[str, Any]:
        folder = self.output_root / "versions" / (self.config.profile_id or "default") / paper.arxiv_id.replace("/", "-") / f"v{old_version}-to-v{paper.version}"
        folder.mkdir(parents=True, exist_ok=True)
        report_path = folder / "report.html"
        if self.config.version_tracking.analyze_pdf_diff:
            markdown = self._analyze_diff(paper.arxiv_id, paper.title, old_version, paper.version, folder)
        else:
            markdown = self._fallback_diff(paper.title, paper.arxiv_id, old_version, paper.version)
        (folder / "report.md").write_text(markdown, encoding="utf-8")
        render_report(markdown, report_path, f"{paper.title} · v{old_version} → v{paper.version}")
        return {
            "detected_at": now.isoformat(),
            "from_version": old_version,
            "to_version": paper.version,
            "report_path": str(report_path),
        }

    def _analyze_diff(
        self, arxiv_id: str, title: str, old_version: int, new_version: int, folder: Path
    ) -> str:
        old_pdf = folder / f"{arxiv_id.replace('/', '-') }v{old_version}.pdf"
        new_pdf = folder / f"{arxiv_id.replace('/', '-') }v{new_version}.pdf"
        self.clients.arxiv.download_version(arxiv_id, old_version, old_pdf)
        self.clients.arxiv.download_version(arxiv_id, new_version, new_pdf)
        old_text = self._section_sample(old_pdf)
        new_text = self._section_sample(new_pdf)
        if not self.clients.llm.enabled:
            return self._fallback_diff(title, arxiv_id, old_version, new_version, old_text, new_text)
        try:
            return self.clients.llm.chat(
                "你是严谨的论文版本差异分析助手。只报告给定两个版本文本能够支持的变化，不得猜测作者动机。",
                f"""论文：{title}（arXiv:{arxiv_id}）
旧版本：v{old_version}
新版本：v{new_version}

旧版关键章节：
{old_text[:18000]}

新版关键章节：
{new_text[:18000]}

请输出中文 Markdown：
# {title} · v{old_version} → v{new_version}
## 变化摘要
## 方法变化
## 实验与结果变化
## 结论与局限变化
## 对阅读与引用的影响

每项注明“文本明确变化”或“分析推断”；证据不足时明确说明。""",
            )
        except Exception as exc:
            task_warning(
                "LLM 版本差异分析",
                "模型调用失败，已回退到文本长度与版本信息摘要。"
                f"请检查 LLM Base URL、模型名和服务状态（{type(exc).__name__}: {exc}）。",
            )
            return self._fallback_diff(title, arxiv_id, old_version, new_version, old_text, new_text)

    @staticmethod
    def _section_sample(path: Path) -> str:
        with fitz.open(path) as document:
            text = "\n".join(page.get_text("text") for page in document)
        headings = re.compile(r"(?im)^\s*(abstract|introduction|method(?:ology)?|approach|experiment(?:s|al setup)?|results?|limitations?|conclusion)\s*$")
        matches = list(headings.finditer(text))
        if not matches:
            return (text[:12000] + "\n...\n" + text[-5000:])[:18000]
        sections: list[str] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections.append(text[match.start() : end][:3500])
        return "\n\n".join(sections)[:18000]

    @staticmethod
    def _fallback_diff(
        title: str,
        arxiv_id: str,
        old_version: int,
        new_version: int,
        old_text: str = "",
        new_text: str = "",
    ) -> str:
        return f"""# {title} · v{old_version} → v{new_version}

## 变化摘要

检测到 arXiv:{arxiv_id} 从 v{old_version} 更新到 v{new_version}。

## 方法变化

未启用或未完成 LLM 差异分析，需要人工核对。

## 实验与结果变化

旧版抽取字符数：{len(old_text)}；新版抽取字符数：{len(new_text)}。

## 结论与局限变化

证据不足。

## 对阅读与引用的影响

建议引用前确认最新版本，并检查作者是否新增实验或修改结论。
"""

    def _render_overview(
        self, state: dict[str, Any], events: list[dict[str, Any]], now: datetime
    ) -> Path:
        folder = self.output_root / "versions"
        folder.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {self.config.profile_name} · arXiv 版本追踪",
            "",
            f"最近检查：{now:%Y-%m-%d %H:%M}；追踪 {len(state)} 篇论文。",
            "",
            "## 本次发现",
            "",
        ]
        if events:
            for event in events:
                if event.get("report_path"):
                    report = Path(event["report_path"])
                    relative = report.relative_to(folder).as_posix()
                    lines.append(f"- v{event['from_version']} → v{event['to_version']}：[查看差异报告]({relative})")
                else:
                    lines.append(f"- v{event['from_version']} → v{event['to_version']}：新版已记录，差异分析待重试")
        else:
            lines.append("本次没有发现新版本。")
        lines.extend(["", "## 正在追踪", ""])
        if self.auto_sync_result:
            result = self.auto_sync_result
            lines += [f"自动同步：本轮选择 {result['selected']} 篇，因数量上限留待后续 {result['deferred']} 篇，"
                      f"已有自动尝试记录 {result['held']} 篇（需要时在批量同步记录中手动继续）。", ""]
            if result.get("batch_id"):
                lines += [f"[查看本轮自动同步结果](../version-batches/{self.config.profile_id or 'default'}/{result['batch_id']}/index.html)", ""]
            if result.get("error"):
                lines += [f"自动同步失败：{result['error']}", ""]
        for item in sorted(state.values(), key=lambda value: value.get("title", "").casefold()):
            if not item.get("tracked", True):
                continue
            lines.append(
                f"- [{item.get('title', item.get('arxiv_id'))}]({item.get('abs_url', '')}) · arXiv:{item.get('arxiv_id')} · v{item.get('latest_version')} · {', '.join(item.get('sources') or [])}"
            )
        markdown = "\n".join(lines) + "\n"
        name = f"index-{self.config.profile_id or 'default'}"
        (folder / f"{name}.md").write_text(markdown, encoding="utf-8")
        html_path = folder / f"{name}.html"
        render_report(markdown, html_path, f"{self.config.profile_name} · arXiv 版本追踪")
        return html_path
