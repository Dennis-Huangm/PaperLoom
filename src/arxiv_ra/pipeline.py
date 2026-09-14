from __future__ import annotations

import shutil
import re
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .abstracts import localize_abstracts
from .alphaxiv import AlphaXivClient, AlphaXivError
from .arxiv_client import ArxivClient, offline_demo_papers
from .arxiv_html import ArxivHtmlFigureClient
from .config import AppConfig
from .emailer import send_digest
from .feedback import FeedbackStore
from .library import PaperLibraryStore
from .llm import LLMClient
from .metadata import MetadataVerifier
from .models import Paper, ReportArtifact, VerifiedMetadata
from .obsidian import ObsidianExporter
from .pdf_pipeline import PDFParser
from .ranker import matched_concept_groups, rank_papers
from .render import render_recommendations, render_report
from .report import ReportGenerator, finalize_report_structure
from .utils import read_json, slugify, write_json
from .weekly import WeeklySynthesizer
from .version_tracker import VersionTracker
from .storage import recommendation_data_path, read_recommendations


class DailyPipeline:
    def __init__(self, config: AppConfig, project_root: Path) -> None:
        self.config = config
        self.project_root = project_root
        output = Path(config.output_dir)
        self.output_root = output if output.is_absolute() else project_root / output
        self.arxiv = ArxivClient(min_interval=3.0)
        self.alphaxiv = AlphaXivClient(
            api_key=os.getenv(config.discovery.alphaxiv_api_key_env, ""),
            endpoint=config.discovery.alphaxiv_endpoint,
        )
        self.arxiv_html = ArxivHtmlFigureClient()
        self.llm = LLMClient(config.llm)
        self.verifier = MetadataVerifier(config.metadata)
        self.parser = PDFParser(config.pdf)
        self.reporter = ReportGenerator(self.llm, config.llm)

    def run(self, force: bool = False, demo: bool = False, deliver: bool = True) -> Path:
        now = datetime.now(ZoneInfo(self.config.timezone))
        date_label = now.date().isoformat()
        run_dir = self.output_root / date_label
        run_dir.mkdir(parents=True, exist_ok=True)

        candidates = self._rank_candidates(run_dir, demo)
        selected, processed, state_path = self._select_candidates(candidates, force)
        if not selected and (run_dir / "discovery-source.txt").exists():
            raise RuntimeError(
                "alphaXiv 备用检索返回的候选均已在历史推荐中，已阻止发布重复的空日报；请稍后刷新或扩大研究方向关键词"
            )
        verified_metadata = self._verify_selected(selected, demo)
        recommendations = self._recommendation_payload(selected, verified_metadata)
        if not demo:
            localize_abstracts(self.config, self.output_root, recommendations)

        digest_path = self._publish_digest(
            run_dir,
            date_label,
            selected,
            verified_metadata,
            recommendations,
            processed,
            state_path,
            now,
            demo=demo,
            deliver=deliver,
        )
        self._run_optional_integrations(now, date_label, run_dir, recommendations, demo)
        return digest_path

    def _rank_candidates(self, run_dir: Path, demo: bool):
        if not demo:
            (run_dir / "discovery-source.txt").unlink(missing_ok=True)
        papers = offline_demo_papers() if demo else self._discover_papers(run_dir)
        ranked = rank_papers(papers, self.config.discovery, self.config.ranking)
        library_entries = PaperLibraryStore(
            self.output_root, self.config.profile_id
        ).all()
        feedback_store = FeedbackStore(self.output_root, self.config.profile_id)
        feedback_store.apply(ranked, library_entries)
        ranked.sort(key=lambda item: (item.final_score, item.published), reverse=True)
        used_alphaxiv = (run_dir / "discovery-source.txt").exists()
        minimum_groups = self.config.discovery.minimum_concept_groups
        if used_alphaxiv and minimum_groups > 0:
            # alphaXiv already performs semantic retrieval and returns truncated
            # abstract previews, so requiring every exact concept group would
            # incorrectly discard the whole fallback set.
            minimum_groups = max(1, minimum_groups - 1)
        eligible = [
            paper
            for paper in ranked
            if not feedback_store.blocks(paper)
            and matched_concept_groups(paper, self.config.discovery)
            >= minimum_groups
        ]
        candidates = eligible[: self.config.discovery.prefilter_count]
        if self.config.ranking.llm_rerank and self.llm.enabled:
            try:
                self.llm.rerank(candidates, self._interest_description())
                candidates.sort(
                    key=lambda item: (item.final_score, item.published), reverse=True
                )
            except Exception as exc:
                (run_dir / "rerank-error.txt").write_text(
                    f"LLM 精排失败，已回退到关键词排序：{type(exc).__name__}: {exc}",
                    encoding="utf-8",
                )
        self._annotate_feedback_reasons(candidates)
        if used_alphaxiv:
            for paper in candidates:
                suffix = "由 alphaXiv 语义检索在 arXiv API 不可用时召回。"
                paper.recommendation_reason = (
                    f"{paper.recommendation_reason}；{suffix}"
                    if paper.recommendation_reason
                    else suffix
                )
        write_json(run_dir / "candidates.json", [paper.to_dict() for paper in candidates])
        return candidates

    def _discover_papers(self, run_dir: Path):
        discovery = self.config.discovery
        try:
            return self.arxiv.search(
                discovery.arxiv_categories,
                discovery.lookback_days,
                discovery.max_candidates,
                discovery.arxiv_query_terms,
            )
        except Exception as arxiv_error:
            if discovery.provider != "auto" or not discovery.alphaxiv_fallback_enabled:
                raise
            try:
                today = datetime.now(timezone.utc).date()
                published_after = (today - timedelta(days=discovery.lookback_days)).isoformat()
                keywords = discovery.positive_keywords + discovery.arxiv_query_terms
                question = self._interest_description()
                papers = self.alphaxiv.discover(
                    keywords=keywords,
                    question=question,
                    published_after=published_after,
                    limit=discovery.max_candidates,
                    difficulty=discovery.alphaxiv_difficulty,
                    excluded_ids=self._processed_ids(),
                )
            except Exception as alphaxiv_error:
                if isinstance(alphaxiv_error, AlphaXivError):
                    detail = str(alphaxiv_error)
                else:
                    detail = f"{type(alphaxiv_error).__name__}: {alphaxiv_error}"
                raise RuntimeError(
                    f"arXiv API 不可用（{type(arxiv_error).__name__}），alphaXiv 备用检索也失败：{detail}"
                ) from arxiv_error
            if not papers:
                raise RuntimeError(
                    f"arXiv API 不可用（{type(arxiv_error).__name__}），alphaXiv 未返回可用论文"
                ) from arxiv_error
            (run_dir / "discovery-source.txt").write_text(
                "本次推荐使用 alphaXiv 备用检索；元数据仍由本地核验链补充。\n",
                encoding="utf-8",
            )
            return papers

    @staticmethod
    def _annotate_feedback_reasons(candidates) -> None:
        for paper in candidates:
            if paper.feedback_score > 0.05:
                suffix = "它与近期加入文献库的论文较为接近，因此提高了优先级。"
            elif paper.feedback_score < -0.05:
                suffix = "它与曾标记为不相关的论文较为接近，因此降低了优先级。"
            else:
                continue
            paper.recommendation_reason = (
                f"{paper.recommendation_reason}；{suffix}"
                if paper.recommendation_reason
                else suffix
            )

    def _select_candidates(self, candidates, force: bool):
        state_path = self._state_path()
        state = read_json(state_path, {"processed": []}) or {"processed": []}
        processed = set(state.get("processed", []))
        selected = [
            paper
            for paper in candidates
            if paper.final_score >= self.config.discovery.min_score
            and (
                paper.llm_score is None
                or paper.llm_score >= self.config.ranking.llm_min_score
            )
            and (force or paper.arxiv_id not in processed)
        ][: self.config.discovery.recommendation_count]
        processed.update(paper.arxiv_id for paper in selected)
        return selected, processed, state_path

    def _state_path(self) -> Path:
        suffix = f"-{self.config.profile_id}" if self.config.profile_id else ""
        return self.output_root / f"state{suffix}.json"

    def _processed_ids(self) -> set[str]:
        state = read_json(self._state_path(), {"processed": []}) or {"processed": []}
        return {str(item) for item in state.get("processed", []) if item}

    def _verify_selected(self, selected, demo: bool) -> dict[str, VerifiedMetadata]:
        verified: dict[str, VerifiedMetadata] = {}
        for paper in selected:
            if demo:
                metadata = VerifiedMetadata(
                    title=paper.title,
                    authors=paper.authors,
                    sources=["arXiv only"],
                )
            else:
                try:
                    metadata = self.verifier.verify(paper)
                except Exception as exc:
                    metadata = VerifiedMetadata(
                        title=paper.title,
                        authors=paper.authors,
                        sources=["arXiv only"],
                        conflicts=[f"元数据核验失败：{type(exc).__name__}"],
                    )
            verified[paper.arxiv_id] = metadata
        return verified

    def _recommendation_payload(
        self, selected, verified: dict[str, VerifiedMetadata]
    ) -> list[dict]:
        return [
            {
                "paper": paper.to_dict(),
                "verified": verified[paper.arxiv_id].to_dict(),
                "profile_id": self.config.profile_id,
                "profile_name": self.config.profile_name,
            }
            for paper in selected
        ]

    def _publish_digest(
        self,
        run_dir: Path,
        date_label: str,
        selected,
        verified: dict[str, VerifiedMetadata],
        recommendations: list[dict],
        processed: set[str],
        state_path: Path,
        now: datetime,
        *,
        demo: bool,
        deliver: bool,
    ) -> Path:
        profile_path = recommendation_data_path(
            self.output_root, date_label, self.config.profile_id
        )
        write_json(profile_path, recommendations)
        legacy_path = run_dir / "recommendations.json"
        if profile_path != legacy_path:
            write_json(legacy_path, recommendations)

        digest_path = run_dir / "index.html"
        html_body = render_recommendations(selected, verified, digest_path, date_label)
        if not demo and deliver:
            send_digest(
                self.config.delivery,
                f"arXiv 每日推荐｜{self.config.profile_name}｜{date_label}",
                html_body,
            )
        # Commit deduplication only after the requested delivery succeeds so a
        # transient SMTP failure can be retried without --force.
        write_json(
            state_path,
            {"processed": sorted(processed), "updated_at": now.isoformat()},
        )
        return digest_path

    def _run_optional_integrations(
        self,
        now: datetime,
        date_label: str,
        run_dir: Path,
        recommendations: list[dict],
        demo: bool,
    ) -> None:
        if demo:
            return
        if (
            self.config.obsidian.enabled
            and self.config.obsidian.auto_sync
            and self.config.obsidian.sync_daily
        ):
            try:
                ObsidianExporter(self.config, self.project_root).sync_daily(
                    date_label, recommendations
                )
            except Exception as exc:
                (run_dir / "obsidian-sync-error.txt").write_text(
                    f"Obsidian 每日推荐同步失败：{type(exc).__name__}: {exc}",
                    encoding="utf-8",
                )
        if (
            self.config.weekly.enabled
            and self.config.weekly.auto_generate
            and now.weekday() == self.config.weekly.weekday
        ):
            try:
                WeeklySynthesizer(self.config, self.project_root).generate(now)
            except Exception as exc:
                (run_dir / "weekly-error.txt").write_text(
                    f"周报生成失败：{type(exc).__name__}: {exc}", encoding="utf-8"
                )
        if self.config.version_tracking.enabled and self.config.version_tracking.auto_check:
            try:
                VersionTracker(self.config, self.project_root).check(now)
            except Exception as exc:
                (run_dir / "version-tracking-error.txt").write_text(
                    f"版本追踪失败：{type(exc).__name__}: {exc}", encoding="utf-8"
                )

    def report_arxiv_id(self, arxiv_id: str, force: bool = True) -> Path:
        now = datetime.now(ZoneInfo(self.config.timezone))
        run_dir = self.output_root / now.date().isoformat()
        run_dir.mkdir(parents=True, exist_ok=True)
        paper = self._paper_snapshot(arxiv_id)
        if paper is None:
            try:
                paper = self.arxiv.get(arxiv_id)
            except Exception as arxiv_error:
                paper = None
                if (
                    self.config.discovery.provider == "auto"
                    and self.config.discovery.alphaxiv_fallback_enabled
                    and self.alphaxiv.enabled
                ):
                    try:
                        paper = self.alphaxiv.lookup(arxiv_id)
                    except Exception:
                        paper = None
                if paper is None:
                    raise RuntimeError(
                        f"arXiv API 无法获取 {arxiv_id}，本地没有论文快照，alphaXiv 也未能精确解析该 ID；请稍后重试"
                    ) from arxiv_error
        paper.lexical_score = 0
        artifact = self._process_paper(paper, run_dir, demo=False)
        return artifact.report_path

    def _paper_snapshot(self, arxiv_id: str):
        """Find a locally saved paper before requiring another arXiv API call."""
        profile_id = getattr(self.config, "profile_id", "")
        for date_dir in sorted(self.output_root.glob("????-??-??"), reverse=True):
            for item in read_recommendations(
                self.output_root, date_dir.name, profile_id
            ):
                paper = item.get("paper") or {}
                if str(paper.get("arxiv_id") or "") == arxiv_id:
                    return Paper.from_dict(paper)
        saved = PaperLibraryStore(self.output_root, profile_id).all().get(arxiv_id)
        if saved and saved.get("paper"):
            return Paper.from_dict(saved["paper"])
        for metadata_path in self.output_root.glob(
            f"????-??-??/reports/{arxiv_id.replace('/', '-')}-*/metadata.json"
        ):
            payload = read_json(metadata_path, {}) or {}
            if payload.get("paper"):
                return Paper.from_dict(payload["paper"])
        return None

    def _process_paper(self, paper, run_dir: Path, demo: bool) -> ReportArtifact:
        paper_dir = run_dir / "reports" / f"{paper.arxiv_id.replace('/', '-')}-{slugify(paper.title, 42)}"
        paper_dir.mkdir(parents=True, exist_ok=True)
        metadata = VerifiedMetadata(title=paper.title, authors=paper.authors, sources=["arXiv only"])
        parsed = None
        main_figure = None
        method_figures = []
        errors: list[str] = []
        if not demo:
            try:
                metadata = self.verifier.verify(paper)
            except Exception as exc:
                errors.append(f"元数据核验失败：{exc}")
            pdf_path = paper_dir / "paper.pdf"
            try:
                self.arxiv.download_pdf(paper, pdf_path)
                parsed = self.parser.parse(pdf_path, paper_dir)
                pdf_figures = [
                    figure for figure in parsed.figures if figure.kind == "page_figure"
                ]
                try:
                    html_figures = self.arxiv_html.fetch(paper.arxiv_id, paper_dir)
                except Exception:
                    html_figures = []

                def figure_number(figure):
                    match = re.match(r"(?:Figure|Fig\.?)\s*(\d+)", figure.caption, re.I)
                    return int(match.group(1)) if match else 10_000 + figure.page

                by_number = {figure_number(figure): figure for figure in pdf_figures}
                for figure in html_figures:
                    by_number[figure_number(figure)] = figure
                selected_figures = [by_number[number] for number in sorted(by_number)][:8]
                for index, figure in enumerate(selected_figures, start=1):
                    suffix = figure.path.suffix or ".png"
                    figure_copy = paper_dir / f"method-figure-{index:02d}{suffix}"
                    if figure_copy.resolve() != figure.path.resolve():
                        shutil.copy2(figure.path, figure_copy)
                    figure.path = figure_copy
                    method_figures.append(figure)
                main_figure = method_figures[0] if method_figures else None
            except Exception as exc:
                errors.append(f"PDF下载或解析失败：{exc}")
        try:
            report = self.reporter.generate(paper, metadata, parsed, method_figures)
        except Exception as exc:
            errors.append(f"LLM 深度报告失败，已生成摘要级回退报告：{type(exc).__name__}: {exc}")
            report = self.reporter._extractive_report(paper, metadata, method_figures)
        if errors:
            report += "\n\n## 运行警告\n\n" + "\n".join(f"- {error}" for error in errors)
        report = finalize_report_structure(report, method_figures)
        report_path = paper_dir / "report.md"
        report_path.write_text(report, encoding="utf-8")
        render_report(
            report,
            report_path.with_suffix(".html"),
            paper.title,
            arxiv_id=paper.arxiv_id,
        )
        write_json(
            paper_dir / "method-figures.json",
            [
                {
                    "filename": figure.path.name,
                    "page": figure.page,
                    "caption": figure.caption,
                    "source": figure.kind,
                    "explanation": figure.explanation,
                }
                for figure in method_figures
            ],
        )
        write_json(paper_dir / "metadata.json", {"paper": paper.to_dict(), "verified": metadata.to_dict(), "parser": parsed.parser if parsed else None})
        if (
            self.config.obsidian.enabled
            and self.config.obsidian.auto_sync
            and self.config.obsidian.sync_reports
        ):
            try:
                ObsidianExporter(self.config, self.project_root).sync_report(
                    paper.to_dict(), metadata.to_dict(), report_path
                )
            except Exception as exc:
                (paper_dir / "obsidian-sync-error.txt").write_text(
                    f"Obsidian 阅读报告同步失败：{type(exc).__name__}: {exc}",
                    encoding="utf-8",
                )
        return ReportArtifact(paper, report_path, main_figure, metadata, report)

    def _interest_description(self) -> str:
        discovery = self.config.discovery
        return (
            f"研究主题描述：{discovery.interest_description or '未提供'}。"
            f"关注 arXiv 类别：{', '.join(discovery.arxiv_categories)}。"
            f"重点关键词：{', '.join(discovery.positive_keywords)}。"
            f"降低优先级：{', '.join(discovery.negative_keywords)}。"
            f"种子论文：{', '.join(discovery.seed_papers) or '未提供'}。"
        )
