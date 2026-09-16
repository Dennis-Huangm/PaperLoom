from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pymupdf as fitz

from .config import AppConfig
from .feedback import FeedbackStore
from .research_clients import ResearchClients
from .render import render_report
from .utils import read_json, write_json
from .zotero import ZoteroClient


ARXIV_ID_IN_TEXT = re.compile(r"(?:arxiv[:/\s]+)([a-z-]+/\d{7}|\d{4}\.\d{4,5})", re.I)


class VersionTracker:
    def __init__(self, config: AppConfig, project_root: Path, *, clients: ResearchClients | None = None) -> None:
        self.config = config
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

        def add(arxiv_id: str, title: str, source: str) -> None:
            arxiv_id = re.sub(r"v\d+$", "", arxiv_id.strip(), flags=re.I)
            if not arxiv_id:
                return
            item = sources.setdefault(arxiv_id, {"arxiv_id": arxiv_id, "title": title, "sources": []})
            if title and not item.get("title"):
                item["title"] = title
            if source not in item["sources"]:
                item["sources"].append(source)

        if self.config.version_tracking.include_reports:
            for path in self.output_root.glob("????-??-??/reports/*/metadata.json"):
                paper = (read_json(path, {}) or {}).get("paper") or {}
                add(str(paper.get("arxiv_id") or ""), str(paper.get("title") or ""), "本地报告")
        if self.config.version_tracking.include_feedback:
            for arxiv_id, entry in FeedbackStore(self.output_root, self.config.profile_id).all().items():
                add(arxiv_id, str((entry.get("paper") or {}).get("title") or ""), "阅读反馈")
        if self.config.version_tracking.include_zotero and self.config.zotero.enabled:
            try:
                zotero = ZoteroClient(self.config.zotero, timeout=3.0)
                if zotero.status().get("ready"):
                    for wrapper in zotero._get_items("/users/0/items/top", {"format": "json"}):
                        data = wrapper.get("data", wrapper)
                        text = " ".join(
                            str(data.get(field) or "")
                            for field in ("archiveID", "extra", "url")
                        )
                        match = ARXIV_ID_IN_TEXT.search(text)
                        archive_id = str(data.get("archiveID") or "")
                        arxiv_id = match.group(1) if match else archive_id if re.fullmatch(r"\d{4}\.\d{4,5}", archive_id) else ""
                        add(arxiv_id, str(data.get("title") or ""), "Zotero")
            except Exception:
                pass
        return dict(list(sources.items())[: self.config.version_tracking.max_tracked])

    def check(self, now: datetime | None = None) -> Path:
        now = now or datetime.now(ZoneInfo(self.config.timezone))
        sources = self.tracked_sources()
        payload = read_json(self.state_path, {}) or {}
        state = payload.get("items", {}) if isinstance(payload.get("items", {}), dict) else {}
        current = {paper.arxiv_id: paper for paper in self.clients.arxiv.get_many(list(sources))}
        new_events: list[dict[str, Any]] = []
        for arxiv_id, source in sources.items():
            paper = current.get(arxiv_id)
            if not paper:
                continue
            previous = state.get(arxiv_id) or {}
            old_version = int(previous.get("latest_version") or 0)
            item = {
                **previous,
                "arxiv_id": arxiv_id,
                "title": paper.title or source.get("title", ""),
                "latest_version": paper.version,
                "updated": paper.updated.isoformat(),
                "abs_url": paper.abs_url,
                "sources": source.get("sources", []),
                "checked_at": now.isoformat(),
                "events": list(previous.get("events") or []),
            }
            if old_version and paper.version > old_version:
                event = self._version_event(paper, old_version, now)
                item["events"].append(event)
                new_events.append(event)
            state[arxiv_id] = item
        write_json(
            self.state_path,
            {"version": 1, "checked_at": now.isoformat(), "items": state},
        )
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
        except Exception:
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
                report = Path(event["report_path"])
                relative = report.relative_to(folder).as_posix()
                lines.append(f"- v{event['from_version']} → v{event['to_version']}：[查看差异报告]({relative})")
        else:
            lines.append("本次没有发现新版本。")
        lines.extend(["", "## 正在追踪", ""])
        for item in sorted(state.values(), key=lambda value: value.get("title", "").casefold()):
            lines.append(
                f"- [{item.get('title', item.get('arxiv_id'))}]({item.get('abs_url', '')}) · arXiv:{item.get('arxiv_id')} · v{item.get('latest_version')} · {', '.join(item.get('sources') or [])}"
            )
        markdown = "\n".join(lines) + "\n"
        name = f"index-{self.config.profile_id or 'default'}"
        (folder / f"{name}.md").write_text(markdown, encoding="utf-8")
        html_path = folder / f"{name}.html"
        render_report(markdown, html_path, f"{self.config.profile_name} · arXiv 版本追踪")
        return html_path
