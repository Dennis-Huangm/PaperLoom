import runpy
from pathlib import Path

import fitz

from arxiv_ra.utils import read_json, write_json


def test_migration_updates_markdown_html_audit_and_keeps_backup(tmp_path):
    migrate = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/integrate_report_tables.py'))['integrate']
    directory = tmp_path / '2026-10-01' / 'reports' / 'paper'
    directory.mkdir(parents=True)
    original = ('# Paper\n\n## 关键结果\n\n### Table 2\n\n'
                '| Model | Score |\n|---|---|\n| A | 1 |\n\n'
                '## 原文表格摘录\n\n### Table 2（部分摘录）\n\n'
                '| Model | Score |\n|---|---|\n| A | 1 |\n| B | 2 |\n\n'
                '## 引用与核对\n旧详情。')
    (directory / 'report.md').write_text(original, encoding='utf-8')
    (directory / 'report.html').write_text('Old HTML', encoding='utf-8')
    write_json(directory / 'metadata.json', {'paper': {'title': 'Paper', 'arxiv_id': '2501.00001'},
                                            'profile_id': 'test', 'report_quality': 'full'})
    write_json(directory / 'evidence.json', {'citations': []})
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), 'Table 2: Results\nModel Score\nA 1\nB 2')
        pdf.save(directory / 'paper.pdf')
    assert migrate(directory, tmp_path, apply=True)
    report = (directory / 'report.md').read_text(encoding='utf-8')
    assert '## 原文表格摘录' not in report and report.count('| A | 1 |') == 1
    assert '| B | 2 |' in report
    html = (directory / 'report.html').read_text(encoding='utf-8')
    assert '原文表格摘录' not in html and '<td>B</td>' in html
    evidence = read_json(directory / 'evidence.json')
    assert evidence == read_json(directory / 'metadata.json')['evidence']
    assert evidence['numeric_audit']['table_diagnostics']
    backup = next((tmp_path / '.jobs' / 'table-integration-backups').rglob('report.md')).parent
    assert (backup / 'report.md').read_text(encoding='utf-8') == original
    assert (backup / 'report.html').read_text(encoding='utf-8') == 'Old HTML'
    assert not migrate(directory, tmp_path, apply=True)


def test_migration_backups_preserve_same_markdown_from_different_dates(tmp_path):
    import shutil
    test_migration_updates_markdown_html_audit_and_keeps_backup(tmp_path)
    first = tmp_path / '2026-10-01' / 'reports' / 'paper'
    backup_root = tmp_path / '.jobs' / 'table-integration-backups'
    original_backup = next(backup_root.rglob('report.md')).parent
    second = tmp_path / '2026-10-02' / 'reports' / 'paper'
    shutil.copytree(first, second)
    shutil.copy2(original_backup / 'report.md', second / 'report.md')
    (second / 'report.html').write_text('Second date original HTML', encoding='utf-8')
    metadata = read_json(second / 'metadata.json')
    metadata['generated_at'] = '2026-10-02'
    write_json(second / 'metadata.json', metadata)
    migrate = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/integrate_report_tables.py'))['integrate']
    assert migrate(second, tmp_path, apply=True)
    backups = [path.parent for path in backup_root.rglob('report.md')]
    assert len(backups) == 2
    second_backup = next(b for b in backups if (b / 'report.html').read_text(encoding='utf-8') == 'Second date original HTML')
    assert read_json(second_backup / 'metadata.json')['generated_at'] == '2026-10-02'


def test_audit_upgrade_restores_suppressed_legacy_prose_without_guessing_location(tmp_path):
    migrate = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/integrate_report_tables.py'))['integrate']
    directory = tmp_path / '2026-10-01' / 'reports' / 'paper'
    directory.mkdir(parents=True)
    (directory / 'report.md').write_text('# Paper\n\n## 关键结果\n定量陈述暂不展示。\n\n定量陈述暂不展示。', encoding='utf-8')
    write_json(directory / 'metadata.json', {'paper': {'title': 'Paper', 'arxiv_id': '2501.00001'},
                                            'profile_id': 'test', 'report_quality': 'full'})
    write_json(directory / 'evidence.json', {'citations': [], 'numeric_audit': {'issues': [
        {'action': 'withhold_claim', 'original': '保留对象状态，准确率为 91.2%。', 'replacement': '定量陈述暂不展示。'},
        {'action': 'withhold_clauses', 'original': '均值为 5，标准差为 1。', 'replacement': '均值为 5，定量陈述暂不展示。'}]}})
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), 'Source text with quantitative details.')
        pdf.save(directory / 'paper.pdf')
    report, evidence = migrate(directory, tmp_path, audit_only=True)
    assert '## 恢复的报告内容' in report
    assert '保留对象状态，准确率为 91.2%。' in report and '均值为 5，标准差为 1。' in report
    assert evidence['previous_numeric_audit']['issues']


def test_audit_upgrade_preserves_valid_id_for_repeated_source_text(tmp_path):
    from arxiv_ra.models import ParsedPaper
    from arxiv_ra.source_spans import source_spans
    migrate = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/integrate_report_tables.py'))['integrate']
    directory = tmp_path / '2026-10-01' / 'reports' / 'paper'
    directory.mkdir(parents=True)
    page = 'Alpha accuracy is 91.2 on the held-out test benchmark.'
    parsed = ParsedPaper(page, [page, page], total_pages=2)
    key, span = next(iter(source_spans(parsed).items()))
    (directory / 'paper.pdf').write_bytes(b'Stored parsed snapshot is supplied')
    (directory / 'report.md').write_text('# Paper\n\n## 关键结果\nAlpha accuracy 为 91.2 [1](paper.pdf#page=1)。', encoding='utf-8')
    write_json(directory / 'metadata.json', {'paper': {'title': 'Paper', 'arxiv_id': '2501.00001'},
                                            'profile_id': 'test', 'report_quality': 'full'})
    write_json(directory / 'evidence.json', {'citations': [{'id': 1, **span}], 'numeric_audit': {}})
    parsed_file = tmp_path / 'parsed.json'
    write_json(parsed_file, {'parsed': {'text': page, 'page_texts': [page, page], 'total_pages': 2}})
    report, evidence = migrate(directory, tmp_path, audit_only=True, parsed_file=parsed_file)
    assert '[1](paper.pdf#page=1)' in report
    assert evidence['validated_citations'] == 1 and evidence['rejected_citations'] == 0
    assert evidence['citations'][0]['source_id'] == key
