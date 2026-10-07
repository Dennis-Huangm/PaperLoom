import json
from pathlib import Path

import pytest

from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper
from arxiv_ra.report_tables import ReportTables
from arxiv_ra.source_spans import source_spans
from arxiv_ra.utils import read_json, write_json
from scripts.repair_report_annotations import OUTPUTS, repair


def stored_report(tmp_path):
    folder = tmp_path / 'run' / '2026-10-06' / 'reports' / 'edival'
    folder.mkdir(parents=True)
    page = 'Table 3: Results\nModel\nLatency\nScore\n∗Seedream 4.0 5\n✗\n25.09.10\n14.55\n75.93\n'
    parsed = ParsedPaper(page, [page], total_pages=1)
    key = next(iter(source_spans(parsed)))
    note = ('### Table 3: Results\n\nScores use a 1–5 scale.\n\n'
            '| Model | Latency | Score | 原文依据 |\n|---|---|---|---|\n'
            f'| ∗Seedream 4.0 | 14.55 | 75.93 | [[证据ID:{key}]] |')
    catalogue = ReportTables.from_notes([note], [])
    markdown, evidence = attach_evidence(catalogue.render(
        '# Paper\n\n## 关键结果\n\n原有分析。\n\n[[表格:table-3]]'), parsed,
        pdf_available=True, full_report=True)
    # Simulate the saved output of the old checker, while retaining its raw
    # matrix independently in tables.json.
    markdown = markdown.replace('| 14.55 |', '| 14.55（引用冲突） |')
    (folder / 'report.md').write_text(markdown, encoding='utf-8')
    (folder / 'report.html').write_text('old html', encoding='utf-8')
    (folder / 'paper.pdf').write_bytes(b'unchanged PDF')
    write_json(folder / 'tables.json', catalogue.to_dict())
    write_json(folder / 'evidence.json', evidence)
    write_json(folder / 'table-preservation.json', {'status': 'passed'})
    write_json(folder / 'metadata.json', {'paper': {'title': 'Paper', 'arxiv_id': '2509.13399v3'},
                                         'profile_id': 'test', 'report_quality': 'full'})
    cache = tmp_path / 'parsed.json'
    write_json(cache, {'parsed': {'text': page, 'page_texts': [page], 'total_pages': 1}})
    return folder, cache, catalogue


def test_repair_previews_then_publishes_preserving_source_and_prose(tmp_path):
    folder, parsed, catalogue = stored_report(tmp_path)
    before = {n: (folder / n).read_bytes() for n in OUTPUTS}
    preview = repair(folder, parsed)
    assert preview['flagged_cells'] == 0
    assert preview['preservation']['status'] == 'passed'
    assert all((folder / n).read_bytes() == contents for n, contents in before.items())
    result = repair(folder, parsed, apply=True)
    markdown = (folder / 'report.md').read_text('utf-8')
    assert '引用冲突' not in markdown and '原有分析。' in markdown
    assert '| 14.55 | 75.93 |' in markdown
    assert ReportTables.from_dict(read_json(folder / 'tables.json')).tables == catalogue.tables
    assert (folder / 'paper.pdf').read_bytes() == b'unchanged PDF'
    assert Path(result['backup'], 'report.md').read_bytes() == before['report.md']
    assert 'numeric_audit' not in read_json(folder / 'metadata.json')['evidence']


def test_repair_rejects_concurrent_publication(tmp_path):
    folder, parsed, _ = stored_report(tmp_path)
    def concurrent_chat(stage, *_):
        (folder / 'report.md').write_text('concurrent report', encoding='utf-8')
        return '{}'
    with pytest.raises(RuntimeError, match='changed during repair'):
        repair(folder, parsed, chat=concurrent_chat, apply=True)
    assert (folder / 'report.md').read_text('utf-8') == 'concurrent report'


def test_repair_rejects_mismatched_source_checkpoint(tmp_path):
    folder, parsed, _ = stored_report(tmp_path)
    before = (folder / 'report.md').read_bytes()
    write_json(parsed, {'parsed': {'text': 'other paper', 'page_texts': ['other paper'], 'total_pages': 1}})
    with pytest.raises(ValueError, match='checkpoint does not match'):
        repair(folder, parsed, apply=True)
    assert (folder / 'report.md').read_bytes() == before


def test_repair_reuses_cached_model_outputs_and_checks_semantics(tmp_path):
    folder, parsed, _ = stored_report(tmp_path)
    calls = []
    def chat(stage, *_):
        calls.append(stage)
        return json.dumps({'approved': True} if stage.endswith('review') else {
            'entries': [{'scope': 'title', 'text': '评测结果', 'sources': ['title']},
                        {'scope': 'v0', 'text': '评分采用 1–5 分量表。', 'sources': ['u1']}], 'omitted': []})
    first = repair(folder, parsed, chat=chat)
    assert first['localized'] == 1 and len(calls) == 2
    second = repair(folder, parsed, chat=chat, apply=True)
    assert second['localized'] == 1 and len(calls) == 2
    assert '评分采用 1–5 分量表。' in (folder / 'report.html').read_text('utf-8')


def test_repair_rejects_preview_that_changes_matrix(tmp_path):
    from dataclasses import replace
    folder, parsed, catalogue = stored_report(tmp_path)
    table = catalogue.tables[0]
    variant = table.variants[0]
    row = variant.rows[0]
    changed = replace(catalogue, tables=(replace(table, variants=(replace(variant, rows=(
        replace(row, cells=(row.cells[0], '99.9', *row.cells[2:])),)),)),))
    candidate = tmp_path / 'changed-tables.json'
    write_json(candidate, changed.to_dict())
    with pytest.raises(ValueError, match='changed frozen source content'):
        repair(folder, parsed, editorials_file=candidate, apply=True)
