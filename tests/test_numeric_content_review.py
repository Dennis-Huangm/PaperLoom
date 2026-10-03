import json

import pytest

from arxiv_ra.citation_repair import repair_numeric_citations
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper


@pytest.mark.parametrize('replacement', [
    '- SVGAnim-SFT 含 123k 样本。',
    '- SVGAnim-SFT 含 123k 样本；剩余 1k 样本。',
])
def test_citation_repair_cannot_drop_values_or_experiment_conditions(replacement):
    page = 'SVGAnim-SFT contains 123k training samples. The remaining 1k samples form a held-out test set.'
    report = '## 实验设置\n\n- SVGAnim-SFT 含 123k 样本；剩余 1k 样本作为测试集。'
    def chat(_system, prompt):
        source = json.loads(prompt.split('候选原文 JSON：\n', 1)[1])[0]
        return json.dumps({'repairs': [{'index': 0, 'segments': [
            {'text': replacement, 'source_ids': [source['id']], 'separator': ''}]}]})
    assert repair_numeric_citations(report, ParsedPaper(page, [page]), chat) == report


def test_lookup_limits_preserve_caption_and_prose_without_warning_prefixes():
    report = ('# Paper\n\n## 关键结果\n\n### Table 5\n\n'
              '*结果为 5 次运行的均值 ± 标准差；指标越大越好。*\n\n'
              '| Model | Score |\n|---|---|\n| Alpha | 91.2 |\n\n'
              '方法采用对象检测，准确率为 81.3%，并维护对象状态。')
    output, evidence = attach_evidence(report, ParsedPaper('', ['The results average five independent runs.']),
                                      pdf_available=True, full_report=True)
    assert report in output
    assert '**[待核对]**' not in output
    assert '实验数值待核对' not in output
    assert evidence['numeric_audit']['prose_diagnostics']
    assert evidence['publication_gate']['unverified_claims'] == 2
    assert evidence['numeric_audit']['publication']['withheld_claims'] == 0


def test_explicit_table_conflict_keeps_value_and_only_labels_affected_cell():
    source = '| Model | Accuracy | Latency |\n|---|---|---|\n| Alpha | 91.2 | 10 |'
    report = ('## 关键结果\n\n| Model | Accuracy | Latency | 原文依据 |\n|---|---|---|---|\n'
              f'| Alpha | 99.9 | 10 | [[证据:{source}]] |')
    output, evidence = attach_evidence(report, ParsedPaper(source, [source]),
                                      pdf_available=True, full_report=True)
    assert '| Alpha | 99.9（引用冲突） | 10 |' in output
    assert evidence['numeric_audit']['publication']['withheld_cells'] == 0
    assert evidence['numeric_audit']['publication']['flagged_cells'] == 1
    assert evidence['publication_gate']['conflicting_claims'] == 1


def test_restored_named_baselines_update_stale_availability_note_only():
    from arxiv_ra.report_completeness import restore_note_tables
    report = ('## 关键结果\n\n### Table 2\n| Model | Score |\n|---|---|\n| Alpha | 1 |\n\n'
              'Table 2 中其他基线（Beta、Gamma 等）完整数值在当前材料中未完全保留。但 Alpha 表现较好。')
    partial = '### Table 2\n| Model | Score |\n|---|---|\n| Beta | 2 |'
    assert '未完全保留' in restore_note_tables(report, [partial], [{'number': 2}])
    complete = partial + '\n| Gamma | 3 |'
    output = restore_note_tables(report, [complete], [{'number': 2}])
    assert '未完全保留' not in output
    assert '已提取数值见上表' in output and '但 Alpha 表现较好。' in output


def test_populated_rows_remove_stale_source_availability_placeholder_only():
    from arxiv_ra.report_completeness import reconcile_table_availability
    report = ('### Table 2\n| Model | Score | 原文依据 |\n|---|---|---|\n'
              '| A | 1 | [1](paper.pdf#page=1)，（部分数值材料未提供） |\n'
              '| B | — | （部分数值材料未提供） |')
    output = reconcile_table_availability(report)
    assert '| A | 1 | [1](paper.pdf#page=1) |' in output
    assert '| B | — | （部分数值材料未提供） |' in output
