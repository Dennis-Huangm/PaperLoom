from __future__ import annotations

import json
import hashlib
import functools
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import yaml

from .config import AppConfig
from .feedback import FeedbackStore
from .library import PaperLibraryStore
from .llm import LLMClient
from .storage import read_recommendations
from .utils import atomic_write_text, read_json, write_json


MANAGED_START = "<!-- ARXIV_RA_MANAGED_START -->"
MANAGED_END = "<!-- ARXIV_RA_MANAGED_END -->"
_OBSIDIAN_SYNC_LOCK = threading.RLock()


def _serialized_sync(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        with _OBSIDIAN_SYNC_LOCK:
            return method(*args, **kwargs)

    return wrapped


class ObsidianError(RuntimeError):
    pass


def discover_obsidian_vaults() -> list[dict[str, Any]]:
    config_path = Path.home() / "AppData" / "Roaming" / "obsidian" / "obsidian.json"
    if not config_path.exists():
        return []
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    vaults: list[dict[str, Any]] = []
    for vault_id, item in (payload.get("vaults") or {}).items():
        path = Path(str(item.get("path") or "")).expanduser()
        vaults.append(
            {
                "id": vault_id,
                "path": str(path),
                "name": path.name or str(path),
                "open": bool(item.get("open")),
                "exists": path.is_dir(),
                "timestamp": item.get("ts", 0),
            }
        )
    vaults.sort(key=lambda item: (not item["open"], not item["exists"], -int(item["timestamp"] or 0)))
    return vaults


class ObsidianExporter:
    def __init__(self, config: AppConfig, project_root: Path) -> None:
        self.config = config
        self.project_root = project_root.resolve()
        output = Path(config.output_dir)
        self.output_root = output if output.is_absolute() else self.project_root / output
        self.settings = config.obsidian
        self.llm = LLMClient(config.llm)

    def status(self) -> dict[str, Any]:
        if not self.settings.enabled:
            return {"ready": False, "detail": "尚未启用"}
        try:
            vault, root = self._paths(create=False)
        except ObsidianError as exc:
            return {"ready": False, "detail": str(exc)}
        return {
            "ready": True,
            "detail": str(root),
            "vault": str(vault),
            "root": str(root),
        }

    @_serialized_sync
    def sync_all(self) -> Path:
        self._require_enabled()
        feedback = FeedbackStore(self.output_root, self.config.profile_id).all()
        for date_dir in sorted(self.output_root.glob("????-??-??")):
            recommendations = read_recommendations(
                self.output_root, date_dir.name, self.config.profile_id
            )
            if recommendations and self.settings.sync_daily:
                self.sync_daily(date_dir.name, recommendations, feedback=feedback, rebuild=False)
        if self.settings.sync_reports:
            for metadata_path in sorted(self.output_root.glob("????-??-??/reports/*/metadata.json")):
                payload = read_json(metadata_path, {}) or {}
                report_path = metadata_path.parent / "report.md"
                if report_path.exists():
                    self.sync_report(
                        payload.get("paper") or {},
                        payload.get("verified") or {},
                        report_path,
                        feedback=feedback,
                        rebuild=False,
                    )
        if self.settings.sync_weekly:
            for metadata_path in sorted(self.output_root.glob("weekly/*/metadata.json")):
                report_path = metadata_path.parent / "report.md"
                if report_path.exists():
                    self.sync_weekly(
                        read_json(metadata_path, {}) or {}, report_path, rebuild=False
                    )
        self._sync_profile()
        return self.rebuild_indexes()

    @_serialized_sync
    def sync_daily(
        self,
        date_label: str,
        recommendations: list[dict[str, Any]],
        feedback: dict[str, dict[str, Any]] | None = None,
        rebuild: bool = True,
    ) -> Path:
        self._require_enabled()
        if not self.settings.sync_daily:
            return self._root(create=True) / "Home.md"
        feedback = feedback or FeedbackStore(self.output_root, self.config.profile_id).all()
        library = PaperLibraryStore(self.output_root, self.config.profile_id).all()
        recommendations = [
            item
            for item in recommendations
            if str(item.get("profile_id") or "") in {"", self.config.profile_id}
        ]
        paper_links: list[tuple[dict[str, Any], str, dict[str, Any] | None]] = []
        for item in recommendations:
            paper = item.get("paper") or {}
            arxiv_id = str(paper.get("arxiv_id") or "")
            preference = (
                {"label": "已加入文献库（相关）", "verdict": "relevant"}
                if arxiv_id in library
                else feedback.get(arxiv_id)
            )
            path = self._sync_paper_note(
                paper,
                item.get("verified") or {},
                preference,
                report_markdown="",
                report_dir=None,
            )
            paper_links.append((item, self._wikilink(path, str(paper.get("title") or arxiv_id)), preference))

        groups: dict[str, list[str]] = {"已加入文献库（相关）": [], "其他推荐": [], "不相关": []}
        for item, link, feedback_item in paper_links:
            paper = item.get("paper") or {}
            verdict = str((feedback_item or {}).get("verdict") or "")
            reason = self._escape_inline(str(paper.get("recommendation_reason") or ""))
            score = float(paper.get("final_score") or 0)
            line = f"- {link} · `{paper.get('primary_category', '')}` · {score:.1f}"
            if reason:
                line += f"\n  - {reason}"
            if verdict == "relevant":
                groups["已加入文献库（相关）"].append(line)
            elif verdict == "not_relevant":
                groups["不相关"].append(line)
            else:
                groups["其他推荐"].append(line)
        body = [
            f"# {date_label} 论文推荐",
            "",
            f"> [!info] 当前研究方向：[[{self._vault_relative(self._profile_path())}|{self.config.profile_name}]]",
            "",
        ]
        for heading, lines in groups.items():
            if lines:
                body.extend([f"## {heading}", "", *lines, ""])
        body.extend(
            [
                "## 今日阅读记录",
                "",
                "- [ ] 选出需要完整阅读的论文",
                "- [ ] 在应用或 Obsidian 中补充个人笔记",
            ]
        )
        path = self._managed_destination(
            self._folder("daily")
            / date_label[:4]
            / date_label[5:7]
            / f"{date_label} - {self._safe_name(self.config.profile_name)}.md"
        )
        path = self._write_managed(
            path,
            {
                "type": "daily-papers",
                "date": date_label,
                "profile": self.config.profile_name,
                "paper_count": len(recommendations),
                "source": "arxiv-research-assistant",
                "tags": ["daily-papers", self._tag(self.config.profile_name)],
            },
            "\n".join(body).rstrip() + "\n",
            "\n## 我的补充\n\n",
        )
        manifest = self._manifest()
        manifest.setdefault("daily", {})[
            f"{date_label} · {self.config.profile_name}"
        ] = self._vault_relative(path)
        self._save_manifest(manifest)
        if rebuild:
            self._sync_profile()
            self.rebuild_indexes()
        return path

    @_serialized_sync
    def sync_report(
        self,
        paper: dict[str, Any],
        verified: dict[str, Any],
        report_path: Path,
        feedback: dict[str, dict[str, Any]] | None = None,
        rebuild: bool = True,
    ) -> Path:
        self._require_enabled()
        report_markdown = report_path.read_text(encoding="utf-8")
        arxiv_id = str(paper.get("arxiv_id") or "")
        feedback = feedback or FeedbackStore(self.output_root, self.config.profile_id).all()
        library = PaperLibraryStore(self.output_root, self.config.profile_id).all()
        preference = (
            {"label": "已加入文献库（相关）", "verdict": "relevant"}
            if arxiv_id in library
            else feedback.get(arxiv_id)
        )
        path = self._sync_paper_note(
            paper,
            verified,
            preference,
            report_markdown=report_markdown,
            report_dir=report_path.parent,
        )
        if rebuild:
            self._sync_profile()
            self.rebuild_indexes()
        return path

    @_serialized_sync
    def sync_weekly(
        self, metadata: dict[str, Any], report_path: Path, rebuild: bool = True
    ) -> Path:
        self._require_enabled()
        if not self.settings.sync_weekly:
            return self._root(create=True) / "Home.md"
        week_id = str(metadata.get("week_id") or report_path.parent.name)
        body = report_path.read_text(encoding="utf-8")
        related: list[str] = []
        manifest = self._manifest()
        for paper in metadata.get("papers") or []:
            arxiv_id = str(paper.get("arxiv_id") or "")
            item = (manifest.get("papers") or {}).get(arxiv_id)
            if item:
                related.append(f"- [[{item['note']}|{paper.get('title') or arxiv_id}]]")
        if related:
            body += "\n\n## Obsidian 关联论文\n\n" + "\n".join(related) + "\n"
        path = self._managed_destination(
            self._folder("weekly")
            / week_id[:4]
            / f"{week_id} - {self._safe_name(self.config.profile_name)}.md"
        )
        path = self._write_managed(
            path,
            {
                "type": "weekly-synthesis",
                "week": week_id,
                "profile": self.config.profile_name,
                "paper_count": metadata.get("paper_count", 0),
                "source": "arxiv-research-assistant",
                "tags": ["weekly-review", self._tag(self.config.profile_name)],
            },
            body,
            "\n## 我的周总结\n\n",
        )
        manifest.setdefault("weekly", {})[week_id] = self._vault_relative(path)
        self._save_manifest(manifest)
        if rebuild:
            self._sync_profile()
            self.rebuild_indexes()
        return path

    @_serialized_sync
    def sync_feedback(
        self,
        paper: dict[str, Any],
        feedback: dict[str, Any],
        verified: dict[str, Any] | None = None,
    ) -> Path:
        self._require_enabled()
        path = self._sync_paper_note(
            paper, verified or {}, feedback, report_markdown="", report_dir=None
        )
        self.rebuild_indexes()
        return path

    @_serialized_sync
    def rebuild_indexes(self) -> Path:
        self._require_enabled()
        manifest = self._manifest()
        papers = list((manifest.get("papers") or {}).values())
        papers.sort(key=lambda item: (str(item.get("year") or ""), item.get("title", "")), reverse=True)
        paper_lines = [
            "# 论文索引",
            "",
            f"> 共 {len(papers)} 篇；由 arXiv Research Assistant 管理，用户笔记区不会被覆盖。",
            "",
            "| 论文 | 年份 | 会议/期刊 | 状态 | 方向 |",
            "|---|---:|---|---|---|",
        ]
        for item in papers:
            paper_link = self._wikilink_reference(
                str(item["note"]), str(item["title"]), in_table=True
            )
            paper_lines.append(
                f"| {paper_link} | {item.get('year', '')} | {self._escape_table(item.get('venue', ''))} | {self._escape_table(item.get('status', ''))} | {self._escape_table(item.get('profile', ''))} |"
            )
        paper_index = self._managed_destination(self._folder("indexes") / "Papers.md")
        paper_index = self._write_managed(paper_index, {"type": "paper-index", "source": "arxiv-research-assistant"}, "\n".join(paper_lines) + "\n")

        daily = sorted((manifest.get("daily") or {}).items(), reverse=True)
        daily_body = "# 每日推荐索引\n\n" + "\n".join(
            f"- [[{path}|{date} 论文推荐]]" for date, path in daily
        ) + "\n"
        daily_index = self._managed_destination(self._folder("indexes") / "Daily.md")
        daily_index = self._write_managed(daily_index, {"type": "daily-index", "source": "arxiv-research-assistant"}, daily_body)

        weekly = sorted((manifest.get("weekly") or {}).items(), reverse=True)
        weekly_body = "# 研究周报索引\n\n" + "\n".join(
            f"- [[{path}|{week} 研究周报]]" for week, path in weekly
        ) + "\n"
        weekly_index = self._managed_destination(self._folder("indexes") / "Weekly.md")
        weekly_index = self._write_managed(weekly_index, {"type": "weekly-index", "source": "arxiv-research-assistant"}, weekly_body)

        concept_map: dict[str, list[dict[str, Any]]] = {}
        for item in papers:
            for concept in item.get("concepts") or []:
                concept_map.setdefault(concept, []).append(item)
        concept_lines = ["# 概念索引", ""]
        for concept in sorted(concept_map, key=str.casefold):
            path = self._concept_path(concept)
            concept_lines.append(f"- {self._wikilink(path, concept)} · {len(concept_map[concept])} 篇")
            related = "\n".join(
                f"- [[{item['note']}|{item['title']}]]" for item in concept_map[concept]
            )
            self._write_managed(
                path,
                {"type": "concept", "concept": concept, "source": "arxiv-research-assistant", "tags": ["concept"]},
                f"# {concept}\n\n## 相关论文\n\n{related}\n",
                "\n## 我的理解\n\n",
            )
        concept_index = self._managed_destination(self._folder("indexes") / "Concepts.md")
        concept_index = self._write_managed(
            concept_index,
            {"type": "concept-index", "source": "arxiv-research-assistant"},
            "\n".join(concept_lines) + "\n",
        )

        home = self._managed_destination(self._folder("home") / "Research Hub.md")
        latest_daily = daily[0][1] if daily else ""
        latest_weekly = weekly[0][1] if weekly else ""
        home_body = [
            "# Research Hub",
            "",
            f"> [!info] 当前方向：[[{self._vault_relative(self._profile_path())}|{self.config.profile_name}]]",
            "",
            "## 导航",
            "",
            f"- [[{self._vault_relative(paper_index)}|论文索引]]",
            f"- {self._wikilink(daily_index, '每日推荐')}",
            f"- {self._wikilink(weekly_index, '研究周报')}",
            f"- {self._wikilink(concept_index, '概念索引')}",
        ]
        if latest_daily:
            home_body.extend(["", "## 最近更新", "", f"- 最新推荐：[[{latest_daily}]]"])
        if latest_weekly:
            home_body.append(f"- 最新周报：[[{latest_weekly}]]")
        home = self._write_managed(
            home,
            {"type": "knowledge-base-home", "source": "arxiv-research-assistant", "version": 2},
            "\n".join(home_body) + "\n",
            "\n## 我的研究入口\n\n",
        )
        return home

    def _sync_paper_note(
        self,
        paper: dict[str, Any],
        verified: dict[str, Any],
        feedback: dict[str, Any] | None,
        report_markdown: str,
        report_dir: Path | None,
    ) -> Path:
        arxiv_id = str(paper.get("arxiv_id") or "").strip()
        if not arxiv_id:
            raise ObsidianError("论文缺少 arXiv ID，无法同步")
        title = str(paper.get("title") or arxiv_id)
        date = str(paper.get("published") or "")[:10]
        year = date[:4]
        path = self._paper_path(arxiv_id, title, year)
        if not report_markdown:
            report_candidates = sorted(
                self.output_root.glob(
                    f"????-??-??/reports/{arxiv_id.replace('/', '-')}-*/report.md"
                )
            )
            if report_candidates:
                source_report = report_candidates[-1]
                report_markdown = source_report.read_text(encoding="utf-8")
                report_dir = source_report.parent
                metadata = read_json(source_report.parent / "metadata.json", {}) or {}
                if not verified:
                    verified = metadata.get("verified") or {}
        authors = [str(author.get("name") or "") for author in (paper.get("authors") or []) if author.get("name")]
        venue = str(verified.get("venue") or "未核实")
        status = str((feedback or {}).get("label") or "未标记")
        concepts = self._concepts_for(paper)
        concept_links = " · ".join(
            self._wikilink(self._concept_path(concept), concept, in_table=True)
            for concept in concepts
        ) or "暂无自动匹配概念"
        body = [
            f"# {title}",
            "",
            "> [!abstract] 论文入口",
            f"> - arXiv：[{arxiv_id}]({paper.get('abs_url') or f'https://arxiv.org/abs/{arxiv_id}'})",
            f"> - PDF：[打开 PDF]({paper.get('pdf_url') or f'https://arxiv.org/pdf/{arxiv_id}'})",
            f"> - 研究方向：[[{self._vault_relative(self._profile_path())}|{self.config.profile_name}]]",
            "",
            "## 元信息",
            "",
            "| 字段 | 内容 |",
            "|---|---|",
            f"| 作者 | {self._escape_table(', '.join(authors))} |",
            f"| 发表 | {self._escape_table(venue)} |",
            f"| arXiv 类别 | {self._escape_table(str(paper.get('primary_category') or ''))} |",
            f"| 阅读状态 | {self._escape_table(status)} |",
            f"| 概念 | {concept_links} |",
            "",
        ]
        reason = str(paper.get("recommendation_reason") or "")
        if reason:
            body.extend(["## 推荐理由", "", reason, ""])
        refined_summary = self._refined_summary(paper, report_markdown)
        if refined_summary:
            body.extend(
                [
                    "## 精炼摘要",
                    "",
                    "> [!summary] 模型精炼",
                    f"> {refined_summary.replace(chr(10), chr(10) + '> ')}",
                    "",
                ]
            )
        if report_markdown:
            report_body = re.sub(r"(?m)^#\s+.*?\n", "", report_markdown, count=1).strip()
            report_body = self._strip_redundant_abstract(
                report_body, str(paper.get("abstract") or "")
            )
            report_body = self._to_obsidian_math(report_body)
            if report_dir and self.settings.copy_figures:
                report_body = self._copy_and_rewrite_images(report_body, report_dir, arxiv_id)
            body.extend(["## 完整阅读报告", "", report_body, ""])
        if report_dir and self.settings.copy_pdf:
            pdf = report_dir / "paper.pdf"
            if pdf.exists():
                destination = self._attachment_folder(arxiv_id) / pdf.name
                self._copy_if_changed(pdf, destination)
                body.extend(["## 本地附件", "", f"![[{self._vault_relative(destination, keep_suffix=True)}]]", ""])
        path = self._write_managed(
            path,
            {
                "type": "paper-note",
                "title": title,
                "arxiv_id": arxiv_id,
                "authors": authors,
                "year": int(year) if year.isdigit() else None,
                "venue": venue,
                "profile": self.config.profile_name,
                "status": status,
                "categories": paper.get("categories") or [paper.get("primary_category")],
                "concepts": concepts,
                "source": "arxiv-research-assistant",
                "updated": str(paper.get("updated") or date or ""),
                "tags": ["paper", self._tag(self.config.profile_name)],
            },
            "\n".join(body).rstrip() + "\n",
            "\n## 我的笔记\n\n> [!note] 这里的内容由你维护，重新同步时不会覆盖。\n\n",
        )
        manifest = self._manifest()
        manifest.setdefault("papers", {})[arxiv_id] = {
            "note": self._vault_relative(path),
            "title": title,
            "year": year,
            "venue": venue,
            "status": status,
            "profile": self.config.profile_name,
            "concepts": concepts,
        }
        self._save_manifest(manifest)
        return path

    def _sync_profile(self) -> Path:
        path = self._profile_path()
        discovery = self.config.discovery
        body = [
            f"# {self.config.profile_name}",
            "",
            discovery.interest_description,
            "",
            "## 检索轴线",
            "",
        ]
        for group in discovery.concept_groups:
            body.append("- " + " / ".join(group))
        body.extend(["", "## 重点关键词", "", " · ".join(discovery.positive_keywords)])
        path = self._write_managed(
            path,
            {"type": "research-profile", "profile_id": self.config.profile_id, "source": "arxiv-research-assistant", "tags": ["research-profile"]},
            "\n".join(body).rstrip() + "\n",
            "\n## 我的方向备注\n\n",
        )
        return path

    def _copy_and_rewrite_images(
        self, markdown: str, source_dir: Path, arxiv_id: str
    ) -> str:
        pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

        def replace(match: re.Match[str]) -> str:
            value = match.group(2).strip()
            if re.match(r"https?://", value):
                return match.group(0)
            source = (source_dir / value).resolve()
            try:
                source.relative_to(source_dir.resolve())
            except ValueError:
                return match.group(0)
            if not source.is_file():
                return match.group(0)
            destination = self._attachment_folder(arxiv_id) / source.name
            self._copy_if_changed(source, destination)
            return f"![[{self._vault_relative(destination, keep_suffix=True)}]]"

        return pattern.sub(replace, markdown)

    @staticmethod
    def _to_obsidian_math(markdown: str) -> str:
        """Convert report KaTeX delimiters to Obsidian MathJax outside code."""
        lines: list[str] = []
        fence = ""
        for line in markdown.splitlines():
            stripped = line.lstrip()
            if fence:
                lines.append(line)
                if stripped.startswith(fence):
                    fence = ""
                continue
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
                lines.append(line)
                continue
            parts = re.split(r"(`+[^`]*`+)", line)
            for index in range(0, len(parts), 2):
                parts[index] = (
                    parts[index]
                    .replace(r"\[", "$$")
                    .replace(r"\]", "$$")
                    .replace(r"\(", "$")
                    .replace(r"\)", "$")
                )
            lines.append("".join(parts))
        return "\n".join(lines)

    def _concepts_for(self, paper: dict[str, Any]) -> list[str]:
        if not self.settings.generate_concepts:
            return []
        text = f"{paper.get('title', '')} {paper.get('abstract', '')}".casefold()
        concepts = [
            keyword
            for keyword in self.config.discovery.positive_keywords
            if keyword.casefold() in text and 2 < len(keyword) <= 64
        ]
        return list(dict.fromkeys(concepts))[:8]

    def _refined_summary(
        self, paper: dict[str, Any], report_markdown: str
    ) -> str:
        arxiv_id = str(paper.get("arxiv_id") or "")
        signature_source = "\n".join(
            [
                str(paper.get("title") or ""),
                str(paper.get("abstract") or ""),
                str(paper.get("recommendation_reason") or ""),
                report_markdown,
            ]
        )
        signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()
        cache = self._summary_cache()
        cached = cache.get(arxiv_id) or {}
        if cached.get("signature") == signature and cached.get("summary"):
            return str(cached["summary"])

        summary = self._report_summary(report_markdown)
        source = "reading-report"
        reason = self._clean_summary(str(paper.get("recommendation_reason") or ""))
        if not summary and reason:
            summary = reason
            source = "recommendation-rationale"
        if not summary and self.llm.enabled:
            try:
                summary = self._clean_summary(
                    self.llm.chat(
                        "你是严谨的 AI 论文笔记编辑。请将论文摘要精炼为中文研究笔记，不得逐句翻译、夸大贡献或补写原文没有的信息。",
                        f"""论文标题：{paper.get('title', '')}
推荐理由：{paper.get('recommendation_reason') or '无'}
原始摘要：
{str(paper.get('abstract') or '')[:3000]}

请输出 3-5 句、总计不超过 260 个汉字的精炼摘要。依次说明：研究问题、核心方法、关键结果、主要边界或局限。摘要没有提供的结果或局限明确写“摘要未说明”。只输出正文，不要标题、列表或 Markdown。""",
                    )
                )
                source = "llm-summary"
            except Exception:
                summary = ""
        if not summary:
            summary = self._fallback_summary(str(paper.get("abstract") or ""))
            source = "extractive-fallback"
        summary = summary[:800].strip()
        cache[arxiv_id] = {
            "signature": signature,
            "summary": summary,
            "source": source,
        }
        self._save_summary_cache(cache)
        return summary

    @classmethod
    def _report_summary(cls, markdown: str) -> str:
        if not markdown:
            return ""
        for heading in ("一句话总结", "为什么值得阅读"):
            match = re.search(
                rf"(?ms)^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)",
                markdown,
            )
            if match:
                value = cls._clean_summary(match.group(1))
                if value:
                    return value
        return ""

    @staticmethod
    def _clean_summary(value: str) -> str:
        value = re.sub(r"(?m)^\s*>\s?", "", value)
        value = re.sub(r"(?m)^\s*[-*]\s+", "", value)
        value = re.sub(r"[*_`]+", "", value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _fallback_summary(abstract: str) -> str:
        text = re.sub(r"\s+", " ", abstract).strip()
        if not text:
            return "暂无可用的模型精炼摘要。"
        sentences = re.split(r"(?<=[.!?。！？])\s+", text)
        return " ".join(sentences[:3])[:360].strip()

    @staticmethod
    def _strip_redundant_abstract(markdown: str, abstract: str) -> str:
        normalized_abstract = re.sub(r"\s+", " ", abstract).strip()
        pattern = re.compile(
            r"(?ms)^##\s+(摘要级主要内容|摘要)\s*\n(.*?)(?=^##\s+|\Z)"
        )

        def replace(match: re.Match[str]) -> str:
            heading = match.group(1)
            content = re.sub(r"\s+", " ", match.group(2)).strip()
            if heading == "摘要级主要内容" or (
                normalized_abstract and normalized_abstract in content
            ):
                return ""
            return match.group(0)

        return re.sub(r"\n{3,}", "\n\n", pattern.sub(replace, markdown)).strip()

    def _summary_cache(self) -> dict[str, Any]:
        path = self._root(create=True) / ".arxiv-ra-summary-cache.json"
        try:
            payload = read_json(path, {}) or {}
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_summary_cache(self, payload: dict[str, Any]) -> None:
        path = self._safe_target(
            self._root(create=True) / ".arxiv-ra-summary-cache.json"
        )
        write_json(path, payload)

    def _write_managed(
        self,
        path: Path,
        frontmatter: dict[str, Any],
        managed_body: str,
        default_tail: str = "",
    ) -> Path:
        path = self._safe_target(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tail = default_tail
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if MANAGED_START in existing and MANAGED_END in existing:
                tail = existing.split(MANAGED_END, 1)[1]
            else:
                path = self._alternate_path(path)
        yaml_text = yaml.safe_dump(
            frontmatter,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        content = (
            f"---\n{yaml_text}\n---\n{MANAGED_START}\n"
            f"{managed_body.rstrip()}\n{MANAGED_END}{tail}"
        )
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return path
        atomic_write_text(path, content)
        return path

    def _manifest(self) -> dict[str, Any]:
        path = self._root(create=True) / ".arxiv-ra-manifest.json"
        try:
            payload = read_json(path, {}) or {}
        except (OSError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _save_manifest(self, payload: dict[str, Any]) -> None:
        path = self._safe_target(self._root(create=True) / ".arxiv-ra-manifest.json")
        write_json(path, payload)

    def _paths(self, create: bool) -> tuple[Path, Path]:
        value = self.settings.vault_path.strip()
        if not value:
            raise ObsidianError("未配置 Obsidian vault 路径")
        vault = Path(value).expanduser()
        if not vault.is_absolute() or not vault.is_dir():
            raise ObsidianError("Obsidian vault 路径不存在或不是绝对目录")
        vault = vault.resolve()
        root_relative = self._relative_config(self.settings.root_folder, "root_folder")
        root = (vault / root_relative).resolve()
        try:
            root.relative_to(vault)
        except ValueError as exc:
            raise ObsidianError("Obsidian 输出目录超出 vault") from exc
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return vault, root

    def _root(self, create: bool) -> Path:
        return self._paths(create)[1]

    def _folder(self, kind: str) -> Path:
        mapping = {
            "daily": self.settings.daily_folder,
            "papers": self.settings.papers_folder,
            "weekly": self.settings.weekly_folder,
            "profiles": self.settings.profiles_folder,
            "concepts": self.settings.concepts_folder,
            "attachments": self.settings.attachments_folder,
            "home": self.settings.home_folder,
            "indexes": self.settings.indexes_folder,
        }
        value = self._relative_config(mapping[kind], f"{kind}_folder")
        path = self._safe_target(self._root(create=True) / value)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _paper_path(self, arxiv_id: str, title: str, year: str = "") -> Path:
        existing = (self._manifest().get("papers") or {}).get(arxiv_id)
        if existing and existing.get("note"):
            vault, _root = self._paths(create=True)
            candidate = (vault / f"{existing['note']}.md").resolve()
            try:
                candidate.relative_to(self._root(create=True).resolve())
                if candidate.is_file():
                    return candidate
            except ValueError:
                pass
        note_name = self._paper_note_name(title)
        profile_folder = self._safe_name(self.config.profile_name)
        destination = self._folder("papers") / profile_folder / f"{note_name}.md"
        destination = self._managed_destination(destination)
        if not destination.exists():
            return destination
        # A same-name managed paper from another arXiv record uses the year, never the ID.
        existing_text = destination.read_text(encoding="utf-8")
        if re.search(rf"(?m)^arxiv_id:\s*['\"]?{re.escape(arxiv_id)}['\"]?\s*$", existing_text):
            return destination
        suffix = f" ({year})" if year else " (paper)"
        return self._managed_destination(destination.with_name(f"{note_name}{suffix}.md"))

    @classmethod
    def _paper_note_name(cls, title: str) -> str:
        """Prefer the named method/model; otherwise use a readable shortened title."""
        clean_title = re.sub(r"\s+", " ", title).strip()
        prefix = re.split(r"\s*[:：]\s*", clean_title, maxsplit=1)[0].strip()
        generic = {
            "a survey", "survey", "towards", "learning", "on", "rethinking",
            "exploring", "understanding", "an empirical study",
        }
        if (
            prefix.casefold() not in generic
            and 2 <= len(prefix) <= 42
            and re.search(r"[A-Za-z0-9]", prefix)
        ):
            return cls._safe_name(prefix)
        return cls._safe_name(clean_title)[:90].rstrip(" .-") or "Untitled Paper"

    def _profile_path(self) -> Path:
        return self._managed_destination(
            self._folder("profiles") / f"{self._safe_name(self.config.profile_name)}.md"
        )

    def _concept_path(self, concept: str) -> Path:
        return self._managed_destination(
            self._folder("concepts") / f"{self._safe_name(concept)}.md"
        )

    def _attachment_folder(self, arxiv_id: str) -> Path:
        path = self._folder("attachments") / self._safe_name(arxiv_id.replace("/", "-"))
        path = self._safe_target(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _safe_target(self, path: Path) -> Path:
        root = self._root(create=True).resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ObsidianError("写入目标超出 Obsidian 专用目录") from exc
        return resolved

    @staticmethod
    def _relative_config(value: str, field: str) -> Path:
        path = Path(value.strip())
        if not value.strip() or path.is_absolute() or ".." in path.parts:
            raise ObsidianError(f"{field} 必须是安全的相对目录")
        return path

    def _vault_relative(self, path: Path, keep_suffix: bool = False) -> str:
        vault, _root = self._paths(create=True)
        relative = path.resolve().relative_to(vault)
        return (relative if keep_suffix else relative.with_suffix("")).as_posix()

    def _wikilink(self, path: Path, label: str, in_table: bool = False) -> str:
        return self._wikilink_reference(
            self._vault_relative(path), label, in_table=in_table
        )

    @staticmethod
    def _wikilink_reference(reference: str, label: str, in_table: bool = False) -> str:
        clean_label = str(label).replace("|", "-").replace("]", ")")
        separator = "\\|" if in_table else "|"
        return f"[[{reference}{separator}{clean_label}]]"

    @staticmethod
    def _safe_name(value: str) -> str:
        clean = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "-", value).strip(" .-")
        return clean[:100] or "未命名"

    @staticmethod
    def _tag(value: str) -> str:
        return re.sub(r"\s+", "-", value.strip()).replace("/", "-")

    @staticmethod
    def _escape_inline(value: str) -> str:
        return value.replace("\n", " ").strip()

    @staticmethod
    def _escape_table(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    @staticmethod
    def _alternate_path(path: Path) -> Path:
        candidate = path.with_name(f"{path.stem} (arXiv RA){path.suffix}")
        if len(str(candidate)) >= 240:
            digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
            candidate = path.with_name(f"arxiv-ra-{digest}{path.suffix}")
        counter = 2
        while candidate.exists() and MANAGED_START not in candidate.read_text(encoding="utf-8"):
            if len(str(path.parent / f"{path.stem} (arXiv RA {counter}){path.suffix}")) < 240:
                candidate = path.with_name(
                    f"{path.stem} (arXiv RA {counter}){path.suffix}"
                )
            else:
                digest = hashlib.sha256(
                    f"{path.name}:{counter}".encode("utf-8")
                ).hexdigest()[:12]
                candidate = path.with_name(f"arxiv-ra-{digest}{path.suffix}")
            counter += 1
        return candidate

    def _managed_destination(self, path: Path) -> Path:
        path = self._safe_target(path)
        if path.exists() and MANAGED_START not in path.read_text(encoding="utf-8"):
            return self._alternate_path(path)
        return path

    @staticmethod
    def _copy_if_changed(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and source.stat().st_size == destination.stat().st_size:
            return
        temporary = destination.with_name(f".{destination.name}.tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise ObsidianError("Obsidian 同步尚未启用")
        self._paths(create=True)
