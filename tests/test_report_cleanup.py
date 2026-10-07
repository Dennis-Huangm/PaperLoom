import json

import pytest

from arxiv_ra.report_cleanup import clean_report_diagnostics
from scripts.remove_report_diagnostics import clean_report


SOURCE = '''# Paper

科学局限性：存在统计不确定性。[1](paper.pdf#page=2)
**[待核对]** 数值 99.9（引用冲突）（原文摘录未能唯一定位，请核对）。

## 引用与核对

用于定位的文本包含 3 页。[查看核对详情](evidence.json)。

## 待核对内容恢复

以下内容曾因引用核对失败被隐藏，现保留供核查；由于旧占位符无法唯一定位，未自动插回正文。

| Model | Score |
|---|---|
| A | 91.2（待核对） |

## 原文表格索引

- [Table 2](paper.pdf#page=4)：正文未按表号匹配；Qualitative examples.
'''


def test_cleanup_keeps_recovered_data_scientific_limitations_and_pdf_links():
    result = clean_report_diagnostics(SOURCE)
    assert '| A | 91.2 |' in result and '数值 99.9。' in result
    assert '科学局限性：存在统计不确定性。[1](paper.pdf#page=2)' in result
    assert '[Table 2](paper.pdf#page=4)' in result
    assert '## 补充实验数据' in result and '## 补充原文表格' in result
    assert '待核对' not in result and '引用冲突' not in result
    assert clean_report_diagnostics(result) == result
    example = '```markdown\n## 引用与核对\n（引用冲突）\n```\n'
    assert clean_report_diagnostics(example) == example


def fixture(tmp_path):
    directory = tmp_path / '2026-10-06/reports/paper'
    directory.mkdir(parents=True)
    (directory / 'report.md').write_text(SOURCE, encoding='utf-8')
    (directory / 'report.html').write_text('previous HTML', encoding='utf-8')
    citation = {'id': 1, 'page': 2, 'quote': 'Unchanged source quote'}
    (directory / 'evidence.json').write_text(json.dumps({'status': 'checked', 'citations': [citation],
                                                       'numeric_audit': {'issues': []}}), encoding='utf-8')
    (directory / 'metadata.json').write_text(json.dumps({'paper': {'title': 'Paper'}, 'profile_id': 'test'}))
    return directory, citation


def test_preview_then_backup_apply_is_repeatable_and_preserves_citations(tmp_path):
    directory, citation = fixture(tmp_path)
    path = directory / 'report.md'
    original = {p.name: p.read_bytes() for p in directory.iterdir()}
    preview = clean_report(path, tmp_path)
    assert preview['status'] == 'preview'
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == original
    result = clean_report(path, tmp_path, apply=True)
    from pathlib import Path
    assert all((Path(result['backup']) / name).read_bytes() == value for name, value in original.items())
    assert json.loads((directory / 'evidence.json').read_text())['citations'] == [citation]
    assert '| A | 91.2 |' in path.read_text(encoding='utf-8')
    assert path.read_text(encoding='utf-8') == clean_report_diagnostics(SOURCE)
    assert b'\r\r\n' not in path.read_bytes()
    assert clean_report(path, tmp_path, apply=True)['status'] == 'unchanged'


def test_save_failure_restores_previous_report(tmp_path, monkeypatch):
    import scripts.remove_report_diagnostics as module
    directory, _ = fixture(tmp_path)
    original = {p.name: p.read_bytes() for p in directory.iterdir()}
    write = module.atomic_write_text
    def fail(path, text):
        if path == directory / 'evidence.json':
            raise OSError('disk error')
        return write(path, text)
    monkeypatch.setattr(module, 'atomic_write_text', fail)
    with pytest.raises(OSError):
        clean_report(directory / 'report.md', tmp_path, apply=True)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == original
