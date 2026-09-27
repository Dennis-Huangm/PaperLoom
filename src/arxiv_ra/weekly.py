from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import uuid

from .config import AppConfig
from .report_store import matching_report
from .research_clients import ResearchClients
from .obsidian import ObsidianExporter
from .render import render_report
from .storage import read_recommendations
from .utils import read_json, write_json
from .task_runtime import task_checkpoint, task_progress, task_warning
from .activity import collect_activity, activity_markdown
from .reading_state import ReadingStateStore


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

    def generate(self, now: datetime | None = None, *, include_notes: bool = False) -> Path:
        task_progress("正在汇总本周推荐和阅读记录…", 8)
        now = now or datetime.now(ZoneInfo(self.config.timezone))
        zone = ZoneInfo(self.config.timezone)
        now = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
        self._period_end = now
        self._report_sources = []
        iso_year, iso_week, _ = now.isocalendar()
        week_id = f"{iso_year}-W{iso_week:02d}"
        destination = self.output_root / "weekly" / f"{week_id}-{self.config.profile_id or 'default'}-{uuid.uuid4().hex[:12]}"
        destination.mkdir(parents=True, exist_ok=True)
        state = ReadingStateStore(self.output_root, self.config.profile_id).snapshot()
        activity = collect_activity(self.output_root, self.config.profile_id, now, self.config.weekly.days, state)
        papers = self._with_activity(self._collect(now), activity)
        task_progress(f"已汇总 {len(papers)} 篇论文，正在读取阅读偏好…", 24)
        feedback, library = state["feedback"], state["library"]
        task_progress("正在归纳主题、方法和开放问题…", 38)
        markdown = self._generate_markdown(week_id, papers, feedback, library)
        markdown += "\n\n" + activity_markdown(activity, include_notes)
        task_progress("周报内容已生成，正在保存并渲染…", 78)
        report_path = destination / "report.md"
        report_path.write_text(markdown, encoding="utf-8")
        html_path = destination / "report.html"
        render_report(markdown, html_path, f"{self.config.profile_name} · {week_id} 研究周报")
        metadata_payload = {
            "week_id": week_id,
            "title": f"{self.config.profile_name} · {week_id} 研究活动周报",
            "profile_id": self.config.profile_id,
            "profile_name": self.config.profile_name,
            # A recovered historical period can be generated much later. Keep
            # ordering/export time separate from the frozen activity cutoff.
            "generated_at": datetime.now(zone).isoformat(),
            "paper_count": len(papers),
            "activity_counts": activity["counts"],
            "period_start": activity["start"], "period_end": activity["end"],
            "includes_personal_notes": include_notes,
            "papers": [
                {
                    "arxiv_id": (item.get("paper") or {}).get("arxiv_id"),
                    "title": (item.get("paper") or {}).get("title"),
                    "version": (item.get("paper") or {}).get("version"),
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
        # Only explicit opt-in retains private note text in the weekly artifact.
        public_activity = {**activity, "events": [
            {k: v for k, v in e.items() if include_notes or k not in {"notes", "tags"}}
            for e in activity["events"]]}
        write_json(destination / "activity.json", public_activity)
        write_json(destination / "sources.json", {"papers": papers, "report_excerpts": self._report_sources})
        if (
            self.config.obsidian.enabled
            and self.config.obsidian.auto_sync
            and self.config.obsidian.sync_weekly
        ):
            try:
                task_progress("正在同步研究周报到 Obsidian…", 92)
                ObsidianExporter(self.config, self.project_root, clients=self.clients).sync_weekly(
                    metadata_payload, report_path
                )
            except Exception as exc:
                task_warning(
                    "Obsidian 同步",
                    f"周报同步失败：{type(exc).__name__}: {exc}",
                )
                (destination / "obsidian-sync-error.txt").write_text(
                    f"Obsidian 周报同步失败：{type(exc).__name__}: {exc}",
                    encoding="utf-8",
                )
        task_checkpoint()
        return html_path

    def _with_activity(self, papers: list[dict], activity: dict) -> list[dict]:
        # Active research comes before passive recommendations when the input
        # limit is reached; the complete event list is still shown in the appendix.
        active = {}
        for event in activity["events"]:
            if event["kind"] not in {"saved", "restored", "reading", "notes", "report", "version", "library_version"}:
                continue
            paper = dict(event["paper"])
            if event["kind"] in {"reading", "notes"} and event.get("read_version"):
                if paper.get("version") != event["read_version"]:
                    # The saved v3 abstract cannot serve as v1 reading evidence.
                    paper = {k: v for k, v in paper.items() if k in {"arxiv_id", "title", "primary_category"}}
                paper["version"] = event["read_version"]
            for candidate in [*papers, *active.values()]:
                old = candidate.get("paper") or {}
                if old.get("arxiv_id") == paper["arxiv_id"] and old.get("version") == paper.get("version"):
                    paper = {**old, **paper}
            active[paper["arxiv_id"]] = {"paper": paper, "verified": {}, "activity_at": event["at"],
                                         "recommendation_date": "", "activity_kind": event["kind"]}
        result = sorted(active.values(), key=lambda x: x["activity_at"], reverse=True)
        result.extend(item for item in papers if item["paper"]["arxiv_id"] not in active)
        return result[:self.config.weekly.max_papers]

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
                    previous = by_id.get(arxiv_id)
                    if previous and (
                        previous["recommendation_date"],
                        int((previous.get("paper") or {}).get("version") or 0),
                    ) > (date_dir.name, int(paper.get("version") or 0)):
                        continue
                    by_id[arxiv_id] = copy
        papers = list(by_id.values())
        papers.sort(
            key=lambda item: float((item.get("paper") or {}).get("final_score") or 0),
            reverse=True,
        )
        return papers[: self.config.weekly.max_papers]

    def _report_excerpt(self, paper: dict[str, Any]) -> str:
        if not self.config.weekly.include_deep_reports:
            return ""
        path = matching_report(self.output_root, self.config.profile_id, paper, before=getattr(self, "_period_end", None))
        excerpt = path.read_text(encoding="utf-8")[:3500] if path else ""
        if path and hasattr(self, "_report_sources"):
            self._report_sources.append({"arxiv_id": paper.get("arxiv_id"), "version": paper.get("version"),
                "report_id": path.relative_to(self.output_root).as_posix(), "excerpt": excerpt})
        return excerpt

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
                "## 本周概览\n\n本期没有可用于论文综述的推荐或活动论文；研究活动见下方记录。\n"
            )
        if not self.clients.llm.enabled:
            return self._fallback_markdown(week_id, papers, feedback, library)
        blocks: list[str] = []
        for index, item in enumerate(papers, start=1):
            task_checkpoint()
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
            report_excerpt = self._report_excerpt(paper) if detailed else ""
            blocks.append(
                f"""[{index}] arXiv:{arxiv_id}
修订版：{paper.get('version') or '未知'}
标题：{paper.get('title', '')}
类别：{paper.get('primary_category', '')}
推荐日期：{item.get('recommendation_date', '')}
研究活动：{item.get('activity_kind', '')} {item.get('activity_at', '')}
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
1. 每个事实性判断都点名对应论文、arXiv ID 和给定修订版；
2. 区分论文明确结论与跨论文推断；
3. “本周必读”优先考虑用户加入文献库的论文，并降低已标记“不相关”的论文；
4. 使用 `[论文标题](https://arxiv.org/abs/IDv版本号)` 链接，未知版本不添加后缀；生成报告不等于用户读完，个人活动与论文事实分开；
5. 没有证据支持的分歧或趋势明确写“证据不足”。""",
            )
        except Exception as exc:
            task_warning(
                "LLM 周报生成",
                "模型调用失败，已回退到结构化汇总。"
                f"请检查 LLM Base URL、模型名和服务状态（{type(exc).__name__}: {exc}）。",
            )
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
            f"本期综合推荐与研究活动，共收录 {len(papers)} 篇论文，分布在 {len(categories)} 个主要类别。",
            "",
            "## 主题与趋势",
            "",
        ]
        for category, items in categories.items():
            lines.append(f"### {category}")
            for item in items:
                paper = item.get("paper") or {}
                lines.append(
                    self._paper_link(paper)
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
            lines.append(self._paper_link(paper))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _paper_link(paper):
        from .activity import literal
        from urllib.parse import quote
        aid = str(paper.get("arxiv_id") or "")
        key = aid + (f"v{paper['version']}" if paper.get("version") else "")
        return f"- [{literal(paper.get('title') or aid)}](https://arxiv.org/abs/{quote(key, safe='/')})（{literal(key)}）"
