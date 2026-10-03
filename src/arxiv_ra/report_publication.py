"""Assemble a reading report from independently owned content.

Outline edits receive prose and placement slots, never experimental matrices.
Metadata has one writer. Tables are assembled from their frozen catalogue only
after prose editing. Citation review may annotate them, but must retain data.
"""
from pathlib import Path

from .report_metadata import protect_metadata
from .report_completeness import _destination


def prepare_publication(report, paper, metadata, figures, catalogue, finalize):
    prose = catalogue.prose(report) if catalogue is not None else str(report)
    prose = finalize(prose, figures)
    prose = protect_metadata(prose, paper, metadata)
    return catalogue.render(prose) if catalogue is not None else prose


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
            report, position, _ = _destination(report, first['number'], first['title'])
            report = report[:position].rstrip() + '\n\n' + block + '\n' + report[position:]
            displays.append({'page': page, 'numbers': [item['number'] for item in entries],
                             'kind': 'source_page', 'image': filename})
    return report, displays
