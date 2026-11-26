"""Publish an independent revision of a selected local report from saved material."""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import fitz
import httpx

from ..papers.models import Author, Paper, ParsedPaper, VerifiedMetadata
from ..papers.paper_data import base_id, requested_version
from ..runtime.jobs.task_runtime import task_commit, task_checkpoint_data, task_progress, task_warning
from ..sources.pdf_pipeline import extract_report_figures
from ..sources.source_spans import normalize_grouped_source_ids
from ..utils import read_json, write_json
from .evidence import attach_evidence
from .report import finalize_report_structure
from .reviewed_material import apply_figure_review, apply_publication_review, figure_review_signature, reviewed_material
from .report_figures import experiment_block, expand_experiment_groups, figure_number
from .report_metadata import protect_metadata
from .report_citations import strip_tokens
from .report_publication import assemble_report, figure_record, figure_source_pdf, saved_figures, save_report_files
from .report_store import catalog_lock, explicit_report
from .tables.report_tables import ReportTables


def _source_text(folder, page_limit=None):
    pdf = folder / "paper.pdf"
    if not pdf.is_file():
        return None
    try:
        with fitz.open(pdf) as doc:
            total = len(doc)
            count = min(total, page_limit) if page_limit else total
            pages = [doc[i].get_text() for i in range(count)]
    except (fitz.FileDataError, ValueError) as exc:
        task_warning("本地 PDF", f"无法恢复原文页文本（{type(exc).__name__}）。")
        return None
    return ParsedPaper("\n\n".join(pages), pages, total_pages=total)


def _republish(folder, paper, payload):
    draft = folder / "report-draft.md"
    if not draft.is_file() or not (folder / "tables.json").is_file():
        raise ValueError("这份历史报告没有完整的草稿和表格快照，可选择仅更新排版。")
    catalogue = ReportTables.from_dict(read_json(folder / "tables.json", {}))
    figures = saved_figures(folder, "method")[1] + saved_figures(folder, "experiment")[1]
    verified = dict(payload["verified"])
    verified["authors"] = [Author(**a) for a in verified.get("authors", [])]
    prose = expand_experiment_groups(draft.read_text(encoding="utf-8"), figures)
    prose = normalize_grouped_source_ids(prose)
    corrections = payload.get("finding_revisions", {})
    if corrections:
        prose = "\n\n".join(corrections.get(_finding_key(p), p) for p in prose.split("\n\n"))
    if payload.get("errors"):
        prose += "\n\n## 运行警告\n\n" + "\n".join("- " + error for error in payload["errors"])
    report, evidence = assemble_report(prose, paper, VerifiedMetadata(**verified), figures, catalogue,
                                       _source_text(folder, payload.get("parsed_pages")), folder, payload.get("report_quality"),
                                       finalize_report_structure=finalize_report_structure,
                                       attach_evidence=attach_evidence)
    write_json(folder / "evidence.json", evidence)
    payload["evidence"] = {k: v for k, v in evidence.items() if k != "citations"}
    return _mark_old_fields(report, payload.get("superseded_publication_fields", []))


def refresh_metadata(report, paper, previous, verified):
    """Keep unrelated science and mark body paragraphs that quote superseded fields."""
    report = protect_metadata(report, paper, verified)
    obsolete = _changed_fields(previous, verified)
    return _mark_old_fields(report, obsolete)


def _changed_fields(previous, verified):
    return [previous[field] for field in ("doi", "venue", "publication_date")
            if previous.get(field) and previous[field] != getattr(verified, field)]


def _mark_old_fields(report, obsolete):
    body = re.search(r"(?m)^##\s+", report)
    if not body or not obsolete:
        return report
    paragraphs = report[body.start():].split("\n\n")
    notice = "> 出版信息已更新。下段沿用旧记录，引用出版信息时请以文首基本信息为准。"
    for i, paragraph in enumerate(paragraphs):
        plain = re.sub(r"\\([^\w\s])", r"\1", paragraph).casefold()
        if (any(term.casefold() in plain for term in obsolete)
                and not paragraph.startswith(("|", "#", "<!--", "![", "> 出版信息已更新"))
                and not (i and paragraphs[i - 1] == notice)):
            paragraphs[i] = notice + "\n\n" + paragraph
    return report[:body.start()] + "\n\n".join(paragraphs)


def _finding_key(paragraph):
    return re.sub(r"\s+", "", re.sub(r"\[\d+\]\(paper\.pdf#page=\d+\)", "", strip_tokens(paragraph)))


def _retain_used_citations(folder, report, payload):
    evidence_path = folder / "evidence.json"
    evidence = read_json(evidence_path, {})
    citations = evidence.get("citations") or []
    references = {(int(n), int(page)) for n, page in re.findall(r"\[(\d+)\]\(paper\.pdf#page=(\d+)\)", report)}
    kept = [c for c in citations if (c["id"], c["page"]) in references]
    if len(kept) == len(citations) or evidence.get("scope") != "source_location_only":
        return
    evidence.update(citations=kept, validated_citations=len(kept), cited_pages=sorted({c["page"] for c in kept}),
                    status="located" if kept else "unavailable")
    if "source_spans" in evidence:
        evidence["source_spans"]["cited"] = sum("source_id" in c for c in kept)
    write_json(evidence_path, evidence)
    payload["evidence"] = {k: v for k, v in evidence.items() if k != "citations"}


def _review_findings(report, draft, number, figures, llm, corrections):
    """Review only shared findings depending on this figure, including legacy draft groups."""
    legacy = {}
    for group in re.finditer(r"(?ms)^:::实验图组[ \t]+([\d ,，、]+)\s*\n(.*?)^:::", draft):
        members = set(map(int, re.findall(r"\d+", group[1])))
        finding = re.search(r"(?m)^共同结论[：:]\s*(.+)$", group[2])
        if finding and len(members) > 1:
            key = re.split(r"\[\[", finding[1])[0].strip()
            if key:
                legacy[key] = members

    visible = {figure_number(f): f.explanation for f in figures}
    def review(paragraph, members):
        if number not in members:
            return paragraph
        readings = "\n".join(f"图 {n}：{visible.get(n) or '解读不可用'}" for n in sorted(members))
        if all(visible.get(n) for n in members):
            try:
                answer = llm.chat("核对跨图结论是否仍受所有相关图解支持。仅回复 KEEP 或 REVIEW。",
                                  f"图解：\n{readings}\n已有结论：\n{paragraph}")
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                task_warning("跨图结论", f"核对未完成（{type(exc).__name__}）。")
            else:
                if answer.strip() == "KEEP":
                    return paragraph
        revised = "跨图结论待复核，请结合" + "、".join(f"图 {n}" for n in sorted(members)) + "下方更新后的解读阅读。"
        corrections[_finding_key(paragraph)] = revised
        return revised

    # Preserve every figure block byte for byte while locating body paragraphs.
    parts = re.split(r"(<!-- experiment-figure:\d+ -->.*?<!-- /experiment-figure -->|"
                     r"<!-- experiment-finding:[\d,]+ -->.*?<!-- /experiment-finding -->)", report, flags=re.S)
    for index, part in enumerate(parts):
        if part.startswith("<!-- experiment-figure:"):
            continue
        group = re.fullmatch(r"(<!-- experiment-finding:([\d,]+) -->\s*)(.*?)(\s*<!-- /experiment-finding -->)", part, re.S)
        if group:
            parts[index] = group[1] + review(group[3], set(map(int, group[2].split(",")))) + group[4]
            continue
        paragraphs = part.split("\n\n")
        for i, p in enumerate(paragraphs):
            if not p.strip() or p.lstrip().startswith(("#", "|", "![", "<", ">")):
                continue
            members = set()
            for refs in re.finditer(r"(?:Figures?|Figs?\.?|图)\s*(\d+(?:\s*(?:[,，、和与及&–—-]|and)\s*\d+)*)", p, re.I):
                members.update(map(int, re.findall(r"\d+", refs[1])))
            members.update(next((ns for key, ns in legacy.items() if key in p), set()))
            if len(members) > 1:
                paragraphs[i] = review(p, members)
        parts[index] = "\n\n".join(paragraphs)
    return "".join(parts)


def _revise_figure(folder, report, paper, number, clients, corrections, readings):
    rows, figures = saved_figures(folder, "experiment")
    matches = [i for i, f in enumerate(figures) if figure_number(f) == number]
    pattern = re.compile(rf"<!-- experiment-figure:{number} -->.*?<!-- /experiment-figure -->", re.S)
    if len(matches) != 1 or len(pattern.findall(report)) != 1:
        raise ValueError(f"此报告没有可独立更新的实验图 {number}。")
    index = matches[0]
    figure = figures[index]
    pdf = folder / "paper.pdf"
    if pdf.is_file():
        with TemporaryDirectory(dir=folder) as tmp:
            try:
                crops = extract_report_figures(pdf, Path(tmp), max_pages=max(60, figure.page), only_number=number)
            except (fitz.FileDataError, ValueError) as exc:
                task_warning("实验图裁取", f"无法从保存的 PDF 重新裁图（{type(exc).__name__}）。")
                crops = []
            crop = next((f for f in crops if figure_number(f) == number and f.image_available), None)
            if crop:
                figure.path = folder / f"experiment-figure-{number:02d}-pdf.png"
                shutil.copyfile(crop.path, figure.path)
                figure.kind, figure.page, figure.image_available = crop.kind, crop.page, True
    if figure.path.suffix.lower() == ".svg" or figure.kind == "html_figure":
        figure.image_available = False
    old_explanation = figure.explanation
    apply_figure_review(figure, readings)
    checked = figure.review_signature or rows[index].get("review_signature")
    reuse = checked and figure.image_available and checked == figure_review_signature(figure)
    if not reuse:
        figure.review_signature = ""
        rows[index].pop("review_signature", None)
        clients.reporter.explain_figure(number, paper, figure, _source_text(folder))
    rows[index].update(figure_record(figure))
    write_json(folder / "experiment-figures.json", rows)
    pdf_url = figure_source_pdf(folder, paper)
    report = pattern.sub(lambda _: experiment_block(figure, number, pdf_url=pdf_url), report, count=1)
    if reuse and figure.explanation == old_explanation:
        return report
    draft = folder / "report-draft.md"
    return _review_findings(report, draft.read_text(encoding="utf-8") if draft.exists() else "",
                            number, figures, clients.llm, corrections)


def revise_report(output_root, config, clients, arxiv_id, report_id, stage, revision_id, revision_date, figure=0):
    """Build outside the catalog and expose a completed directory in a single rename."""
    root = Path(output_root).resolve()
    record = explicit_report(root, base_id(arxiv_id), report_id)
    if not record or (requested_version(arxiv_id) and requested_version(arxiv_id) != record.paper.get("version")):
        raise ValueError("找不到指定版本的本地报告")
    if not re.fullmatch(r"[a-f0-9]{12}", revision_id) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", revision_date):
        raise ValueError("无效的报告修订标识")
    dest = root / revision_date / "reports" / f"revision-{revision_id}"
    if (dest / "metadata.json").is_file():
        if read_json(dest / "metadata.json", {}).get("revision_of") != report_id:
            raise ValueError("本地报告修订标识已被使用")
        return dest / "report.md"
    payload = record.metadata
    paper = Paper.from_dict(record.paper)
    task_checkpoint_data("paper", record.paper)
    task_progress("正在读取选定报告的本地材料…", 15)
    staging = root / ".jobs"
    staging.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=staging, prefix="revision-") as tmp:
        folder = Path(tmp) / "report"
        shutil.copytree(record.metadata_path.parent, folder)
        report = (folder / "report.md").read_text(encoding="utf-8")
        if stage == "metadata":
            verified = clients.verifier.verify(paper)
            if not {"OpenAlex", "Semantic Scholar", "Crossref"}.intersection(verified.sources):
                raise ValueError("未取得可核对的外部记录，请稍后重试。")
            previous = payload.get("verified", {})
            review = reviewed_material(root, paper).get("publication")
            if apply_publication_review(verified, review):
                payload["publication_review"] = review
            else:
                payload.pop("publication_review", None)
            report = refresh_metadata(report, paper, previous, verified)
            obsolete = [*payload.get("superseded_publication_fields", []), *_changed_fields(previous, verified)]
            payload["superseded_publication_fields"] = list(dict.fromkeys(obsolete))
            payload["verified"] = verified.to_dict()
        elif stage == "figure":
            task_progress(f"正在重新裁取并解读实验图 {figure}…", 35)
            report = _revise_figure(folder, report, paper, figure, clients, payload.setdefault("finding_revisions", {}),
                                    reviewed_material(root, paper).get("figures", {}))
            _retain_used_citations(folder, report, payload)
        elif stage == "publish":
            report = _republish(folder, paper, payload)
        elif stage != "render":
            raise ValueError("未知的局部更新范围")
        now = datetime.now(ZoneInfo(config.timezone))
        payload.update(generated_at=now.isoformat(), revision_of=report_id, revision_stage=stage)
        rid = (dest / "report.html").relative_to(root).as_posix()
        task_progress("正在保存修订后的本地报告…", 88)
        save_report_files(folder, report, paper, payload, rid, write_markdown=stage != "render")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with task_commit(), catalog_lock(root):
            if not record.metadata_path.is_file():
                raise ValueError("原报告已删除，请刷新报告库")
            folder.rename(dest)
    return dest / "report.md"
