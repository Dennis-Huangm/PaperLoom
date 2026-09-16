from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import AppConfig
from .feedback import FeedbackStore
from .library import PaperLibraryStore
from .research_clients import ResearchClients
from .obsidian import ObsidianExporter
from .render import render_report
from .storage import read_recommendations
from .utils import read_json, write_json


class WeeklySynthesizer:
    def __init__(self, config: AppConfig, project_root: Path, *, clients: ResearchClients | None = None) -> None:
        self.config = config
        self.project_root = project_root
        output = Path(config.output_dir)
        self.output_root = output if output.is_absolute() else project_root / output
        self.clients = clients if clients is not None else ResearchClients(config)
        self._owns_clients = clients is None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._owns_clients:
            self.clients.close()

    def generate(self, now: datetime | None = None) -> Path:
        now = now or datetime.now(ZoneInfo(self.config.timezone))
        iso_year, iso_week, _ = now.isocalendar()
        week_id = f"{iso_year}-W{iso_week:02d}"
        destination = self.output_root / "weekly" / f"{week_id}-{self.config.profile_id or 'default'}"
        destination.mkdir(parents=True, exist_ok=True)
        papers = self._collect(now)
        feedback = FeedbackStore(self.output_root, self.config.profile_id).all()
        library = PaperLibraryStore(self.output_root, self.config.profile_id).all()
        markdown = self._generate_markdown(week_id, papers, feedback, library)
        report_path = destination / "report.md"
        report_path.write_text(markdown, encoding="utf-8")
        html_path = destination / "report.html"
        render_report(markdown, html_path, f"{self.config.profile_name} · {week_id} 研究周报")
        metadata_payload = {
            "week_id": week_id,
            "profile_id": self.config.profile_id,
            "profile_name": self.config.profile_name,
            "generated_at": now.isoformat(),
            "paper_count": len(papers),
            "papers": [
                {
                    "arxiv_id": (item.get("paper") or {}).get("arxiv_id"),
                    "title": (item.get("paper") or {}).get("title"),
                    "preference": (
                        "relevant"
                        if (item.get("paper") or {}).get("arxiv_id", "") in library
                        else (feedback.get((item.get("paper") or {}).get("arxiv_id", "")) or {}).get("verdict")
                    ),
                }
                for item in papers
            ],
        }
        write_json(destination / "metadata.json", metadata_payload)
        if (
            self.config.obsidian.enabled
            and self.config.obsidian.auto_sync
            and self.config.obsidian.sync_weekly
        ):
            try:
                ObsidianExporter(self.config, self.project_root, clients=self.clients).sync_weekly(
                    metadata_payload, report_path
                )
            except Exception as exc:
                (destination / "obsidian-sync-error.txt").write_text(
                    f"Obsidian 周报同步失败：{type(exc).__name__}: {exc}",
                    encoding="utf-8",
                )
        return html_path

    def _collect(self, now: datetime) -> list[dict[str, Any]]:
        start = now.date() - timedelta(days=max(1, self.config.weekly.days) - 1)
        by_id: dict[str, dict[str, Any]] = {}
        for date_dir in sorted(self.output_root.glob("????-??-??")):
            try:
                date_value = datetime.strptime(date_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not start <= date_value <= now.date():
                continue
            for item in read_recommendations(
                self.output_root, date_dir.name, self.config.profile_id
            ):
                paper = item.get("paper") or {}
                arxiv_id = str(paper.get("arxiv_id") or "")
                if arxiv_id:
                    copy = dict(item)
                    copy["recommendation_date"] = date_dir.name
                    by_id[arxiv_id] = copy
        papers = list(by_id.values())
        papers.sort(
            key=lambda item: float((item.get("paper") or {}).get("final_score") or 0),
            reverse=True,
        )
        return papers[: self.config.weekly.max_papers]

    def _report_excerpt(self, arxiv_id: str) -> str:
        if not self.config.weekly.include_deep_reports:
            return ""
        paths = sorted(self.output_root.glob(f"????-??-??/reports/{arxiv_id.replace('/', '-')}-*/report.md"))
        if not paths:
            return ""
        return paths[-1].read_text(encoding="utf-8")[:3500]

    def _generate_markdown(
        self,
        week_id: str,
        papers: list[dict[str, Any]],
        feedback: dict[str, dict[str, Any]],
        library: dict[str, dict[str, Any]],
    ) -> str:
        if not papers:
            return (
                f"# {self.config.profile_name} · {week_id} 研究周报\n\n"
                "## 本周概览\n\n本周没有新的推荐论文。\n"
            )
        if not self.clients.llm.enabled:
            return self._fallback_markdown(week_id, papers, feedback, library)
        blocks: list[str] = []
        for index, item in enumerate(papers, start=1):
            paper = item.get("paper") or {}
            verified = item.get("verified") or {}
            arxiv_id = str(paper.get("arxiv_id") or "")
            feedback_item = feedback.get(arxiv_id) or {}
            preference = (
                "已加入文献库（相关）"
                if arxiv_id in library
                else feedback_item.get("label", "未标记")
            )
            detailed = index <= 8
            report_excerpt = self._report_excerpt(arxiv_id) if detailed else ""
            blocks.append(
                f"""[{index}] arXiv:{arxiv_id}
标题：{paper.get('title', '')}
类别：{paper.get('primary_category', '')}
推荐日期：{item.get('recommendation_date', '')}
推荐分数：{paper.get('final_score', '')}
推荐偏好：{preference}
会议/期刊：{verified.get('venue') or '未核实'}
推荐理由：{paper.get('recommendation_reason', '')}
摘要：{str(paper.get('abstract') or '')[:1200] if detailed else '见标题与推荐理由'}
深读报告摘录：{report_excerpt or '无'}"""
            )
        evidence = "\n\n".join(blocks)
        try:
            return self.clients.llm.chat(
                "你是严谨的 AI 科研周报编辑。只依据给定论文证据归纳，不得发明趋势、实验结论或论文关系。",
                f"""研究方向：{self.config.discovery.interest_description}
周次：{week_id}

论文证据：
{evidence}

请生成中文 Markdown 周报，严格使用以下结构：
# {self.config.profile_name} · {week_id} 研究周报
## 本周概览
## 主题与趋势
## 方法簇
## 结论分歧与证据
## 开放问题
## 本周必读

要求：
1. 每个事实性判断都点名对应论文或 arXiv ID；
2. 区分论文明确结论与跨论文推断；
3. “本周必读”优先考虑用户加入文献库的论文，并降低已标记“不相关”的论文；
4. 使用 `[论文标题](https://arxiv.org/abs/ID)` 链接；
5. 没有证据支持的分歧或趋势明确写“证据不足”。""",
            )
        except Exception as exc:
            fallback = self._fallback_markdown(week_id, papers, feedback, library)
            return fallback + f"\n> LLM 周报生成失败，已回退到结构化汇总：{type(exc).__name__}\n"

    def _fallback_markdown(
        self,
        week_id: str,
        papers: list[dict[str, Any]],
        feedback: dict[str, dict[str, Any]],
        library: dict[str, dict[str, Any]],
    ) -> str:
        categories: dict[str, list[dict[str, Any]]] = {}
        for item in papers:
            paper = item.get("paper") or {}
            categories.setdefault(str(paper.get("primary_category") or "未分类"), []).append(item)
        lines = [
            f"# {self.config.profile_name} · {week_id} 研究周报",
            "",
            "## 本周概览",
            "",
            f"本周共收录 {len(papers)} 篇推荐论文，分布在 {len(categories)} 个主要类别。",
            "",
            "## 主题与趋势",
            "",
        ]
        for category, items in categories.items():
            lines.append(f"### {category}")
            for item in items:
                paper = item.get("paper") or {}
                lines.append(
                    f"- [{paper.get('title', '')}]({paper.get('abs_url', '')})（arXiv:{paper.get('arxiv_id', '')}）"
                )
        lines.extend(["", "## 方法簇", "", "未启用 LLM，暂按 arXiv 类别分组。", "", "## 结论分歧与证据", "", "证据不足。", "", "## 开放问题", "", "需要结合完整阅读报告进一步归纳。", "", "## 本周必读", ""])
        prioritized = sorted(
            papers,
            key=lambda item: (
                2
                if str((item.get("paper") or {}).get("arxiv_id") or "") in library
                else -2
                if str(
                    (
                        feedback.get(
                            str((item.get("paper") or {}).get("arxiv_id") or "")
                        )
                        or {}
                    ).get("verdict")
                    or ""
                )
                == "not_relevant"
                else 0,
                float((item.get("paper") or {}).get("final_score") or 0),
            ),
            reverse=True,
        )[:5]
        for item in prioritized:
            paper = item.get("paper") or {}
            lines.append(f"- [{paper.get('title', '')}]({paper.get('abs_url', '')})")
        return "\n".join(lines) + "\n"
