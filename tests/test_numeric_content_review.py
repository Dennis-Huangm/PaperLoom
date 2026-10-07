import json

import pytest

from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper


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
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


def test_disagreeing_values_are_not_annotated_by_citation_binding():
    source = '| Model | Accuracy | Latency |\n|---|---|---|\n| Alpha | 91.2 | 10 |'
    report = ('## 关键结果\n\n| Model | Accuracy | Latency | 原文依据 |\n|---|---|---|---|\n'
              f'| Alpha | 99.9 | 10 | [[证据:{source}]] |')
    output, evidence = attach_evidence(report, ParsedPaper(source, [source]),
                                      pdf_available=True, full_report=True)
    assert '| Alpha | 99.9 | 10 |' in output and '引用冲突' not in output
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


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
