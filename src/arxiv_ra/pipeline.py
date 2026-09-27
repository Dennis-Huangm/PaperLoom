from __future__ import annotations

import shutil
import re
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .abstracts import localize_abstracts
from .discovery import DiscoveryService, PaperResolver
from .paper_data import local_paper_item
from .arxiv_client import offline_demo_papers
from .config import AppConfig
from .emailer import send_digest
from .feedback import FeedbackStore
from .evidence import attach_evidence
from .library import PaperLibraryStore
from .models import Paper, ReportArtifact, VerifiedMetadata
from .reading_state import _locked
from .obsidian import ObsidianExporter
from .ranker import matched_concept_groups, rank_papers, finish_explanation
from .render import render_recommendations, render_report
from .report import finalize_report_structure
from .research_clients import ResearchClients
from .utils import read_json, slugify, write_json
from .weekly import WeeklySynthesizer
from .version_tracker import VersionTracker
from .storage import recommendation_data_path, read_recommendations
from .task_runtime import task_checkpoint, task_checkpoint_data, task_progress, task_warning
from .task_runtime import supports_task_checkpoints
from .report_checkpoint import (ReportCheckpoint, CheckpointWriteError,
                                bind_report_checkpoint, current_report_checkpoint)


class DailyPipeline:
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

    def run(self, force: bool = False, demo: bool = False, deliver: bool = True) -> Path:
        task_progress("正在检索候选论文…", 5)
        now = datetime.now(ZoneInfo(self.config.timezone))
        date_label = now.date().isoformat()
        run_dir = self.output_root / date_label
        run_dir.mkdir(parents=True, exist_ok=True)

        candidates = self._rank_candidates(run_dir, demo, force=force)
        task_progress(f"候选检索完成，正在筛选 {len(candidates)} 篇论文…", 32)
        selected, processed, state_path = self._select_candidates(candidates, force)
        self._save_selection_audit(run_dir, candidates, selected)
        discovery_result = getattr(self, "_discovery_result", None) if not demo else None
        if discovery_result:
            write_json(self._discovery_manifest, {
                "profile_id": self.config.profile_id, **discovery_result.to_dict(),
                "prefiltered_count": len(candidates), "selected_count": len(selected),
                "selection_status": "selected" if selected else "no_eligible_new_papers",
            })
        if not selected and discovery_result and discovery_result.sources.get("arxiv", {}).get("status") == "failed":
            raise RuntimeError(
                "arXiv 不可用，alphaXiv 无符合日期、评分与历史过滤条件的候选，已阻止发布重复的空日报并保留已有结果"
            )
        if not selected and read_recommendations(self.output_root, date_label, self.config.profile_id):
            task_progress("没有新论文，正在生成本次结果…", 85)
            unchanged = run_dir / f"no-new-{self.config.profile_id or 'default'}.html"
            render_recommendations([], {}, unchanged, date_label)
            self._run_optional_integrations(
                now, date_label, run_dir,
                read_recommendations(self.output_root, date_label, self.config.profile_id), demo)
            return unchanged
        task_progress(f"已选出 {len(selected)} 篇论文，正在核验元数据…", 42)
        verified_metadata = self._verify_selected(selected, demo)
        recommendations = self._recommendation_payload(selected, verified_metadata)
        if not demo:
            task_progress("正在生成中文摘要…", 62)
            localize_abstracts(self.config, self.output_root, recommendations)

        task_progress("正在生成推荐页面并保存结果…", 76)
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
        task_progress("正在执行知识库等收尾同步…", 92)
        self._run_optional_integrations(
            now, date_label, run_dir,
            read_recommendations(self.output_root, date_label, self.config.profile_id), demo)
        task_progress("推荐结果已生成，正在完成任务…", 98)
        return digest_path

    def _rank_candidates(self, run_dir: Path, demo: bool, force: bool = False):
        papers = offline_demo_papers() if demo else self._discover_papers(run_dir, force=force)
        ranked = rank_papers(papers, self.config.discovery, self.config.ranking)
        feedback_store = FeedbackStore(self.output_root, self.config.profile_id)
        preferences = feedback_store.state.snapshot()
        feedback_entries = preferences["feedback"]
        feedback_store.apply(ranked, preferences["library"], feedback_entries=feedback_entries)
        ranked.sort(key=lambda item: (item.final_score, item.published_sort_key), reverse=True)
        minimum_groups = self.config.discovery.minimum_concept_groups
        processed_ids = set() if force or demo else self._processed_ids()
        eligible = []
        self._selection_audit = []
        for paper in ranked:
            reasons = feedback_store.block_reasons(paper, feedback_entries)
            if paper.arxiv_id in processed_ids:
                reasons.append({"reason": "历史已推荐"})
            required = (min(minimum_groups, self.config.discovery.alphaxiv_minimum_concept_groups)
                        if "alphaxiv" in paper.discovery_sources else minimum_groups)
            if matched_concept_groups(paper, self.config.discovery) < required:
                reasons.append({"reason": f"概念组命中不足（需要 {required} 组）"})
            if reasons:
                finish_explanation(paper)
                self._selection_audit.append({"paper": paper.to_dict(), "reasons": reasons})
            else:
                eligible.append(paper)
        candidates = eligible[: self.config.discovery.prefilter_count]
        for paper in eligible[self.config.discovery.prefilter_count:]:
            finish_explanation(paper)
            self._selection_audit.append({"paper": paper.to_dict(), "reasons": [{"reason": "超出预筛数量"}]})
        if self.config.ranking.llm_rerank and self.clients.llm.enabled:
            try:
                self.clients.llm.rerank(candidates, self._interest_description())
                scored = sum(paper.llm_score is not None for paper in candidates)
                if candidates and scored < len(candidates):
                    task_warning(
                        "LLM 精排",
                        f"模型只返回了 {scored}/{len(candidates)} 篇候选的有效评分；"
                        "未评分论文将按关键词结果处理。",
                    )
                candidates.sort(
                    key=lambda item: (item.final_score, item.published_sort_key), reverse=True
                )
            except Exception as exc:
                task_warning(
                    "LLM 精排",
                    "候选论文的模型精排失败，已回退到关键词排序；推荐质量可能明显下降。"
                    f"请检查 LLM Base URL、模型名和服务状态（{type(exc).__name__}: {exc}）。",
                )
                (run_dir / "rerank-error.txt").write_text(
                    f"LLM 精排失败，已回退到关键词排序：{type(exc).__name__}: {exc}",
                    encoding="utf-8",
                )
        self._annotate_feedback_reasons(candidates)
        for paper in candidates:
            finish_explanation(paper)
            suffix = paper.source_label + "。"
            if paper.resolution_note:
                suffix += paper.resolution_note + "。"
            paper.recommendation_reason = f"{paper.recommendation_reason}；{suffix}" if paper.recommendation_reason else suffix
        suffix = self.config.profile_id or "default"
        write_json(run_dir / f"candidates-{suffix}.json", [paper.to_dict() for paper in candidates])
        return candidates

    def _save_selection_audit(self, run_dir: Path, candidates, selected) -> None:
        selected_ids = {p.arxiv_id for p in selected}
        rows = list(getattr(self, "_selection_audit", []))
        for paper in candidates:
            reasons = []
            if paper.arxiv_id not in selected_ids:
                if paper.final_score < self.config.discovery.min_score:
                    reasons.append({"reason": f"低于最低综合分 {self.config.discovery.min_score}"})
                if paper.llm_score is not None and paper.llm_score < self.config.ranking.llm_min_score:
                    reasons.append({"reason": f"低于模型相关性阈值 {self.config.ranking.llm_min_score}"})
                if not reasons:
                    reasons.append({"reason": "推荐名额或历史去重限制"})
            rows.append({"paper": paper.to_dict(), "reasons": reasons, "selected": paper.arxiv_id in selected_ids})
        write_json(run_dir / f"selection-{self.config.profile_id or 'default'}-{uuid.uuid4().hex}.json",
                   {"profile_id": self.config.profile_id, "created_at": datetime.now(ZoneInfo(self.config.timezone)).isoformat(),
                    "items": rows})

    def _discover_papers(self, run_dir: Path, force: bool = False):
        manifest = run_dir / f"discovery-{self.config.profile_id or 'default'}-{uuid.uuid4().hex[:12]}.json"
        self._discovery_manifest = manifest
        self._discovery_result = None
        service = DiscoveryService(self.config.discovery, self.clients.arxiv, self.clients.alphaxiv, self.output_root)
        try:
            self._discovery_result = service.discover(set() if force else self._processed_ids())
        except RuntimeError as exc:
            result = getattr(exc, "discovery_result", None)
            if result:
                write_json(manifest, {"profile_id": self.config.profile_id, **result.to_dict()})
            raise
        write_json(manifest, {"profile_id": self.config.profile_id, **self._discovery_result.to_dict()})
        for source, source_state in self._discovery_result.sources.items():
            if source_state.get("status") == "failed":
                task_warning(
                    f"{source} 检索",
                    f"检索源不可用，任务使用其他来源继续：{source_state.get('error', '未知错误')}",
                )
        hydration = self._discovery_result.hydration
        if hydration.get("status") in {"failed", "partial", "deferred"}:
            task_warning(
                "arXiv 元数据补全",
                f"部分候选论文元数据未完整补全（{hydration.get('status')}），已使用缓存或现有信息。",
            )
        return self._discovery_result.papers

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
        total = len(selected)
        for index, paper in enumerate(selected, start=1):
            task_progress(
                f"正在核验元数据（{index}/{total}）：{paper.title[:36]}",
                42 + round(18 * (index - 1) / max(total, 1)),
            )
            if demo:
                metadata = VerifiedMetadata(
                    title=paper.title,
                    authors=paper.authors,
                    sources=[paper.metadata_label],
                )
            else:
                try:
                    metadata = self.clients.verifier.verify(paper)
                except Exception as exc:
                    metadata = VerifiedMetadata(
                        title=paper.title,
                        authors=paper.authors,
                        sources=[paper.metadata_label],
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
        digest_path = run_dir / (f"index-{self.config.profile_id}.html" if self.config.profile_id else "index.html")
        # The lock spans read/merge/publication, including independent GUI/CLI runs.
        lock_path = self.output_root / f"publication-{self.config.profile_id or 'default'}.lock"
        batch_dir = run_dir / "batches" / (self.config.profile_id or "default") / uuid.uuid4().hex
        batch_dir.mkdir(parents=True, exist_ok=True)
        with _locked(lock_path):
            # Refuse to overwrite malformed authoritative history.
            if profile_path.exists() and not isinstance(read_json(profile_path), list):
                raise ValueError("推荐历史格式错误，已保留原文件")
            previous = read_recommendations(self.output_root, date_label, self.config.profile_id)
            write_json(batch_dir / "recommendations.json", recommendations)
            write_json(batch_dir / "previous.json", previous)
            merged = {}
            for item in [*recommendations, *previous]:
                raw = item.get("paper") or {}
                if raw.get("arxiv_id"):
                    merged.setdefault((raw["arxiv_id"], raw.get("version")), item)
            daily = list(merged.values())
            write_json(profile_path, daily)
            legacy_path = run_dir / "recommendations.json"
            if profile_path != legacy_path:
                write_json(legacy_path, daily)
            daily_papers = [Paper.from_dict(item["paper"]) for item in daily]
            daily_metadata = {
                (item["paper"]["arxiv_id"], item["paper"].get("version")):
                    VerifiedMetadata(venue=(item.get("verified") or {}).get("venue"))
                for item in daily
            }
            render_recommendations(daily_papers, daily_metadata, digest_path, date_label)
            if digest_path.name != "index.html":
                shutil.copyfile(digest_path, run_dir / "index.html")
        # Email only the current batch; the daily page contains the full history.
        html_body = render_recommendations(selected, verified, batch_dir / "index.html", date_label)
        if not demo and deliver:
            send_digest(
                self.config.delivery,
                f"PaperLoom 每日推荐｜{self.config.profile_name}｜{date_label}",
                html_body,
            )
        # Commit deduplication only after the requested delivery succeeds so a
        # transient SMTP failure can be retried without --force.
        with _locked(lock_path):
            latest = read_json(state_path, {}) or {}
            processed = processed | set(latest.get("processed", []))
            write_json(state_path, {"processed": sorted(processed), "updated_at": now.isoformat()})
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
                ObsidianExporter(self.config, self.project_root, clients=self.clients).sync_daily(
                    date_label, recommendations
                )
            except Exception as exc:
                task_warning(
                    "Obsidian 同步",
                    f"每日推荐同步失败：{type(exc).__name__}: {exc}",
                )
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
                WeeklySynthesizer(self.config, self.project_root, clients=self.clients).generate(now)
            except Exception as exc:
                task_warning(
                    "自动周报", f"自动生成周报失败：{type(exc).__name__}: {exc}"
                )
                (run_dir / "weekly-error.txt").write_text(
                    f"周报生成失败：{type(exc).__name__}: {exc}", encoding="utf-8"
                )
        if self.config.version_tracking.enabled and self.config.version_tracking.auto_check:
            try:
                VersionTracker(self.config, self.project_root, clients=self.clients).check(now)
            except Exception as exc:
                task_warning(
                    "版本追踪", f"自动检查版本失败：{type(exc).__name__}: {exc}"
                )
                (run_dir / "version-tracking-error.txt").write_text(
                    f"版本追踪失败：{type(exc).__name__}: {exc}", encoding="utf-8"
                )

    def report_arxiv_id(self, arxiv_id: str, force: bool = True, *, snapshot: Paper | None = None,
                        local_pdf: Path | None = None, resume: dict | None = None) -> Path:
        task_progress(f"正在获取 arXiv:{arxiv_id} 的论文信息…", 5)
        now = datetime.now(ZoneInfo(self.config.timezone))
        run_dir = self.output_root / now.date().isoformat()
        run_dir.mkdir(parents=True, exist_ok=True)
        historical = snapshot is not None
        paper = snapshot or self._paper_snapshot(arxiv_id)
        paper = PaperResolver(getattr(self.config, "discovery", None), self.clients.arxiv,
                              self.clients.alphaxiv, self.output_root).resolve(
                                  arxiv_id, paper, intent="snapshot" if historical else "latest")
        task_checkpoint_data("paper", paper.to_dict())
        task_progress("论文信息已获取，准备下载和解析全文…", 14)
        paper.lexical_score = 0
        checkpoint = (ReportCheckpoint(self.output_root, self.config, paper, resume)
                      if supports_task_checkpoints() else None)
        with bind_report_checkpoint(checkpoint):
            artifact = (self._process_paper(paper, run_dir, demo=False, local_pdf=local_pdf)
                        if local_pdf else self._process_paper(paper, run_dir, demo=False))
        return artifact.report_path

    def _paper_snapshot(self, arxiv_id: str):
        """Find a locally saved paper before requiring another arXiv API call."""
        profile_id = getattr(self.config, "profile_id", "")
        item = local_paper_item(self.output_root, profile_id, arxiv_id, latest=True)
        return Paper.from_dict(item["paper"]) if item else None

    def _process_paper(self, paper, run_dir: Path, demo: bool, *, local_pdf: Path | None = None) -> ReportArtifact:
        revision = f"v{paper.version}" if paper.version else ""
        profile = getattr(self.config, "profile_id", "") or "default"
        # Every attempt is a separate artifact: a failed regeneration must not
        # replace a previously usable report or its figures/PDF.
        paper_dir = run_dir / "reports" / f"{paper.arxiv_id.replace('/', '-')}-{revision or 'unknown'}-{profile}-{slugify(paper.title, 42)}-{uuid.uuid4().hex[:10]}"
        paper_dir.mkdir(parents=True, exist_ok=True)
        metadata = VerifiedMetadata(title=paper.title, authors=paper.authors, sources=[paper.metadata_label])
        parsed = None
        main_figure = None
        method_figures = []
        checkpoint = current_report_checkpoint()
        errors: list[str] = [paper.resolution_note] if paper.resolution_note else []
        if not demo:
            task_progress("正在核验论文元数据…", 18)
            try:
                metadata = self.clients.verifier.verify(paper)
            except Exception as exc:
                errors.append(f"元数据核验失败：{exc}")
                task_warning(
                    "出版元数据核验",
                    f"核验失败，已使用 arXiv 元数据：{type(exc).__name__}: {exc}",
                )
            pdf_path = paper_dir / "paper.pdf"
            try:
                task_progress("正在下载论文 PDF…", 27)
                parsed = checkpoint.restore_parsed(paper_dir, local_pdf) if checkpoint else None
                if parsed is None:
                    if local_pdf:
                        shutil.copy2(local_pdf, pdf_path)
                    else:
                        self.clients.arxiv.download_pdf(paper, pdf_path)
                    task_progress("PDF 已下载，正在解析正文和图片…", 40)
                    parsed = self.clients.parser.parse(pdf_path, paper_dir)
                    if checkpoint and parsed.text.strip():
                        checkpoint.save_parsed(parsed, pdf_path)
                else:
                    task_progress("已复用校验通过的 PDF 正文与图片解析结果…", 40)
                if not parsed.text.strip():
                    raise ValueError("PDF 未提取到有效正文，将生成摘要级报告")
                pdf_figures = [
                    figure for figure in parsed.figures if figure.kind == "page_figure"
                ]
                try:
                    task_progress("正在补充提取论文方法图…", 54)
                    html_figures = self.clients.arxiv_html.fetch(f"{paper.arxiv_id}{revision}", paper_dir)
                except Exception as exc:
                    task_warning(
                        "arXiv HTML",
                        f"HTML 方法图提取失败，已仅使用 PDF 图片：{type(exc).__name__}: {exc}",
                    )
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
            except CheckpointWriteError:
                raise
            except Exception as exc:
                errors.append(f"PDF下载或解析失败：{exc}")
                task_warning(
                    "PDF 下载与解析",
                    f"全文处理失败，将生成摘要级报告：{type(exc).__name__}: {exc}",
                )
        task_progress("正在调用模型阅读全文并撰写报告…", 65)
        report_quality = "full" if parsed and parsed.text.strip() and self.clients.llm.enabled else "abstract"
        try:
            report = self.clients.reporter.generate(paper, metadata, parsed, method_figures)
        except CheckpointWriteError:
            raise
        except Exception as exc:
            report_quality = "abstract"
            errors.append(f"LLM 深度报告失败，已生成摘要级回退报告：{type(exc).__name__}: {exc}")
            task_warning(
                "LLM 深度报告",
                "模型调用失败，已生成摘要级回退报告。"
                f"请检查 LLM Base URL、模型名和服务状态（{type(exc).__name__}: {exc}）。",
            )
            report = self.clients.reporter._extractive_report(paper, metadata, method_figures)
        if checkpoint:
            checkpoint.publish(resumable=report_quality != "full")
        if errors:
            report += "\n\n## 运行警告\n\n" + "\n".join(f"- {error}" for error in errors)
        report = finalize_report_structure(report, method_figures)
        report, evidence = attach_evidence(report, parsed, pdf_available=(paper_dir / "paper.pdf").is_file(),
                                           full_report=report_quality == "full")
        numeric_issues = evidence.get("numeric_audit", {}).get("issues", [])
        if numeric_issues:
            task_warning("报告数值核对", f"{len(numeric_issues)} 个实验数值条目存在依据或归属问题，相关数值/陈述已暂不展示；原始内容和处理详情保存在 evidence.json。")
        write_json(paper_dir / "evidence.json", evidence)
        task_progress("报告内容已生成，正在保存和渲染…", 88)
        report_path = paper_dir / "report.md"
        report_path.write_text(report, encoding="utf-8")
        render_report(
            report,
            report_path.with_suffix(".html"),
            paper.title,
            arxiv_id=paper.arxiv_id,
            profile_id=self.config.profile_id,
            report_id=report_path.with_suffix(".html").relative_to(self.output_root).as_posix(),
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
        write_json(paper_dir / "metadata.json", {"profile_id": profile, "profile_name": self.config.profile_name,
                   "paper": paper.to_dict(), "verified": metadata.to_dict(), "parser": parsed.parser if parsed else None,
                   "report_quality": report_quality, "errors": errors,
                   "evidence": {k: v for k, v in evidence.items() if k != "citations"},
                   "generated_at": datetime.now(ZoneInfo(self.config.timezone)).isoformat(),
                   "model": self.config.llm.model if self.clients.llm.enabled else "",
                   "parsed_pages": len(parsed.page_texts) if parsed else 0,
                   "parsed_characters": len(parsed.text.strip()) if parsed else 0})
        if (
            self.config.obsidian.enabled
            and self.config.obsidian.auto_sync
            and self.config.obsidian.sync_reports
        ):
            try:
                task_progress("正在同步阅读报告到 Obsidian…", 95)
                ObsidianExporter(self.config, self.project_root, clients=self.clients).sync_report(
                    paper.to_dict(), metadata.to_dict(), report_path
                )
            except Exception as exc:
                task_warning(
                    "Obsidian 同步",
                    f"阅读报告同步失败：{type(exc).__name__}: {exc}",
                )
                (paper_dir / "obsidian-sync-error.txt").write_text(
                    f"Obsidian 阅读报告同步失败：{type(exc).__name__}: {exc}",
                    encoding="utf-8",
                )
        task_checkpoint()
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
