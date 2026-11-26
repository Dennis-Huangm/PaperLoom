"""Assemble a reading report from independently owned content.

Outline edits receive prose and placement slots, never experimental matrices.
Metadata has one writer. Tables are assembled from their frozen catalogue only
after prose editing. Citation review may annotate them, but must retain data.
"""
from pathlib import Path
import re

from ..runtime.jobs.task_runtime import task_checkpoint, task_warning
from .report_checkpoint import CheckpointWriteError
from .report_figures import figure_number, render_experiment_figures
from .report_metadata import protect_metadata
from .tables.placement import destination
from ..papers.models import FigureCandidate
from ..papers.paper_data import versioned
from ..utils import atomic_write_text, read_json, write_json
from .render import render_report
from .report_store import catalog_lock


def figure_record(figure):
    row = {"filename": figure.path.name, "page": figure.page, "caption": figure.caption,
           "source": figure.kind, "explanation": figure.explanation,
           "explanation_basis": figure.explanation_basis or "unavailable"}
    if figure.role == "experiment":
        row.update(section=figure.section, image_available=figure.image_available,
                   filename=figure.path.name if figure.image_available else None)
        if figure.review_signature:
            row["review_signature"] = figure.review_signature
    return row


def saved_figures(folder, role):
    rows = read_json(folder / f"{role}-figures.json", [])
    figures = []
    for row in rows:
        filename = row.get("filename")
        if filename and Path(filename).name != filename:
            raise ValueError("图像清单中的文件名无效")
        path = folder / (filename or "missing.png")
        figures.append(FigureCandidate(path, row["page"], row.get("caption", ""), role=role,
                        section=row.get("section", ""), kind=row.get("source", "embedded"),
                        image_available=bool(filename and path.is_file() and row.get("image_available", True)),
                        explanation=row.get("explanation", ""), explanation_basis=row.get("explanation_basis", ""),
                        review_signature=row.get("review_signature", "")))
    return rows, figures


def save_report_files(folder, report, paper, payload, report_id, *, catalog_root=None, write_markdown=True):
    """Write the readable files first. Metadata makes a full attempt visible to the catalog."""
    if write_markdown:
        atomic_write_text(folder / "report.md", report)
    render_report(report, folder / "report.html", paper.title, paper.arxiv_id,
                  profile_id=payload.get("profile_id") or "", report_id=report_id)
    if catalog_root is None:
        write_json(folder / "metadata.json", payload)  # revision is still in its staging directory
    else:
        with catalog_lock(catalog_root):
            task_checkpoint()
            write_json(folder / "metadata.json", payload)


def prepare_publication(report, paper, metadata, figures, catalogue, finalize):
    try:
        prose = catalogue.prose(report) if catalogue is not None else str(report)
        prose = finalize(prose, figures)
        prose = protect_metadata(prose, paper, metadata)
        result = catalogue.render(prose) if catalogue is not None else prose
        if catalogue is None or catalogue.data_intact(result):
            return result
        task_warning("报告排版", "表格展示未能保留全部数据，已采用原表。")
    except (CheckpointWriteError, OSError):
        raise
    except Exception as exc:
        task_warning("报告排版", f"已回退到草稿排版（{type(exc).__name__}）。")
    # A table/layout fallback still needs to resolve the experiment slots.
    fallback = catalogue.fallback_report(report) if catalogue is not None else str(report)
    candidates = figures if isinstance(figures, list) else ([figures] if figures else [])
    fallback = render_experiment_figures(fallback, candidates)
    return protect_metadata(fallback, paper, metadata) if paper and metadata else fallback


def assemble_report(report, paper, metadata, report_figures, table_catalogue, parsed, paper_dir,
                    report_quality, *, finalize_report_structure, attach_evidence):
    """Publish both freshly composed and restored drafts through the same steps."""
    report = prepare_publication(report, paper, metadata, report_figures, table_catalogue,
                                 finalize_report_structure)
    from .report_citations import strip_tokens, unavailable_citations
    unlinked_report = report
    try:
        report, evidence = attach_evidence(report, parsed, pdf_available=(paper_dir / "paper.pdf").is_file(),
                                           full_report=report_quality == "full")
        if table_catalogue is not None and not table_catalogue.data_intact(report):
            report = strip_tokens(unlinked_report)
            evidence = unavailable_citations()
    except (CheckpointWriteError, OSError):
        raise
    except Exception as exc:
        report = strip_tokens(unlinked_report)
        evidence = unavailable_citations()
        task_warning("出处绑定", f"已保留正文，引用定位不可用（{type(exc).__name__}）。")
    pdf_url = figure_source_pdf(paper_dir, paper)
    report = restore_figure_source_links(report, report_figures, pdf_url)
    try:
        from .evidence import source_table_inventory
        visual = [item for item in source_table_inventory(parsed) if item.get('kind') == 'visual']
        report, _ = present_source_pages(report, {'visual': visual}, paper_dir / 'paper.pdf')
    except (CheckpointWriteError, OSError):
        raise
    except Exception as exc:
        task_warning("原表页面", f"原表页面展示不可用（{type(exc).__name__}）。")
    return report, evidence


def figure_source_pdf(folder, paper):
    if (folder / "paper.pdf").is_file():
        return "paper.pdf"
    return versioned(paper).pdf_url or f"https://arxiv.org/pdf/{paper.arxiv_id}"


def restore_figure_source_links(report, figures, pdf_url):
    # The citation binder strips untrusted model-written page fragments.
    # Restore only the source destinations of our own numbered figure blocks.
    pages = {figure_number(f): f.page for f in figures if f.role == "experiment"}
    def restore_source(match):
        page = pages.get(int(match[1]))
        if not page:
            return match[0].replace("[查看论文原文](paper.pdf)", f"[查看论文原文]({pdf_url})")
        return re.sub(rf"\[查看原文第 {page} 页\]\(paper\.pdf(?:#page={page})?\)",
                      lambda _: f"[查看原文第 {page} 页]({pdf_url}#page={page})", match[0])
    return re.sub(r"<!-- experiment-figure:(\d+) -->.*?<!-- /experiment-figure -->", restore_source, report, flags=re.S)


def present_source_pages(report: str, coverage: dict, pdf_path: Path) -> tuple[str, list[dict]]:
    """Show visual examples / unavailable matrices beside their discussion.

    Full physical pages deliberately preserve image/text relationships without
    inferring table boundaries from PDF reading order. Tables sharing a page use
    one expandable image. This never claims that a source image is a verified
    numeric transcription.
    """
    items = coverage.get('visual', []) + coverage.get('missing', [])
    if not items or not pdf_path.is_file():
        return report, []
    import fitz
    grouped = {}
    for item in items:
        grouped.setdefault(item['page'], []).append(item)
    displays = []
    with fitz.open(pdf_path) as pdf:
        for page, entries in grouped.items():
            if not 1 <= page <= len(pdf):
                continue
            marker = f'<!-- paperloom-source-page:{page} -->'
            if marker in report:
                continue
            filename = f'source-table-page-{page:03d}.png'
            pdf[page - 1].get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False).save(pdf_path.with_name(filename))
            names = '、'.join(f'Table {item["number"]}' for item in entries)
            kind = '定性示例' if all(item.get('kind') == 'visual' for item in entries) else '原文表格'
            anchors = '\n'.join(f'<a id="paper-table-{item["number"]}-source"></a>' for item in entries)
            block = (f'{marker}\n{anchors}\n\n<details class="report-table-details">\n'
                     f'<summary>{kind}（{names}）</summary>\n\n'
                     f'![{names} 原文第 {page} 页]({filename})\n\n'
                     f'[打开原文第 {page} 页](paper.pdf#page={page})\n\n</details>\n')
            first = entries[0]
            report, position, _ = destination(report, first['number'], first['title'])
            report = report[:position].rstrip() + '\n\n' + block + '\n' + report[position:]
            displays.append({'page': page, 'numbers': [item['number'] for item in entries],
                             'kind': 'source_page', 'image': filename})
    return report, displays
