"""Cited row completion and independent clause preservation through publication."""
import hashlib
import json

import pytest

from arxiv_ra.evidence import attach_evidence, restore_table_row_citations
from arxiv_ra.models import ParsedPaper
from arxiv_ra.source_spans import source_spans


PAGE = 'Table 2: Results\nMethod\nGroup A\nGroup B\nCLIP\nTokens\nCLIP\nTokens\nAlpha\n0.313\n31.4k\n0.309\n31.4k\nBeta\n0.293\n124.3k\n0.276\n124.3k\n'


def token(page, start, end):
    digest = hashlib.sha256(json.dumps([page], ensure_ascii=False).encode()).hexdigest()[:24]
    return f'[[证据ID:Q1-{digest}-1-{start}-{end}]]'


def row_report(page=PAGE, values=('0.313', '31.4k', '0.309', '31.4k'), start=None):
    start = page.index('0.309') if start is None else start
    ref = token(page, start, len(page))
    report = '## 关键结果\n\n| Method | A CLIP | A Tokens | B CLIP | B Tokens | 原文依据 |\n|---|---|---|---|---|---|\n'
    report += '| Alpha | ' + ' | '.join(values) + f' | {ref} |'
    return attach_evidence(report, ParsedPaper(page, [page]), pdf_available=True, full_report=True)


def test_partial_quote_completes_only_intersecting_named_row():
    output, evidence = row_report()
    assert '| Alpha | 0.313 | 31.4k | 0.309 | 31.4k |' in output
    assert not evidence['numeric_audit']['issues']
    assert evidence['numeric_audit']['table_checks'][0]['status'] == 'row_order_checked'
    assert any('Alpha 0.313 31.4k' in c['quote'] for c in evidence['citations'])


def test_synthesis_recovers_exact_note_row_when_model_omits_its_citation():
    page = ('Table 3: Results\nModel\nAccuracy\nF1\n'
            'Alpha\n91.20\n88.40\nBeta\n87.10\n82.60\n')
    parsed = ParsedPaper(page, [page])
    spans = source_spans(parsed)
    key = next(iter(spans))
    note = ('| Model | Accuracy | F1 | 原文依据 |\n|---|---|---|---|\n'
            f'| Alpha | 91.20 | 88.40 | [[证据ID:{key}]] |\n'
            f'| Beta | 87.10 | 82.60 | [[证据ID:{key}]] |')
    report = ('## 关键结果\n\n| 模型 (Model) | Accuracy | F1 | 原文依据 |\n'
              '|---|---|---|---|\n'
              f'| Alpha | 91.20 | 88.40 | [[证据ID:{key}]] |\n'
              '| Beta | 87.10 | 82.60 | 当前材料缺少可定位原文依据 |')
    raw_output, raw_evidence = attach_evidence(report, parsed, pdf_available=True, full_report=True)
    assert raw_evidence['numeric_audit']['issues'] and '| Beta | 87.10（待核对）' in raw_output

    restored = restore_table_row_citations(report, [note], parsed, spans)
    output, evidence = attach_evidence(restored, parsed, pdf_available=True, full_report=True)
    assert '| Beta | 87.10 | 82.60 |' in output
    assert not evidence['numeric_audit']['issues']
    assert 'paper.pdf#page=1' in output


def test_synthesis_does_not_recover_wrong_or_ambiguous_rows():
    page = 'Table 3: Results\nModel\nAccuracy\nAlpha\n91.20\nBeta\n87.10\n'
    parsed = ParsedPaper(page, [page])
    spans = source_spans(parsed)
    key = next(iter(spans))
    base = '| Model | Accuracy | 原文依据 |\n|---|---|---|\n'
    note = base + f'| Beta | 87.10 | [[证据ID:{key}]] |'
    wrong = '## 关键结果\n\n' + base + '| Beta | 99.90 | 当前材料缺少可定位原文依据 |'
    assert restore_table_row_citations(wrong, [note], parsed, spans) == wrong
    ambiguous = '## 关键结果\n\n' + base + '| Beta | 87.10 | 当前材料缺少可定位原文依据 |'
    assert restore_table_row_citations(ambiguous, [note, note], parsed, spans) == ambiguous


def test_synthesis_does_not_borrow_a_row_from_another_pdf_table():
    page = ('Table 1: Earlier results\nModel\nAccuracy\nAlpha\n91.20\n'
            'Table 2: Different setting\nModel\nAccuracy\nBeta\n87.10\n')
    parsed = ParsedPaper(page, [page])
    spans = source_spans(parsed)
    key = next(iter(spans))
    base = '| Model | Accuracy | 原文依据 |\n|---|---|---|\n'
    note = base + f'| Beta | 87.10 | [[证据ID:{key}]] |'
    report = ('## 关键结果\n\n' + base +
              f'| Alpha | 91.20 | [[证据ID:{key}]] |\n' +
              '| Beta | 87.10 | 当前材料缺少可定位原文依据 |')
    assert restore_table_row_citations(report, [note], parsed, spans) == report


@pytest.mark.parametrize('values', [
    ('0.293', '31.4k', '0.309', '31.4k'),  # Neighbor's value cannot rescue Alpha.
    ('0.309', '31.4k', '0.313', '31.4k'),  # Wrong column order.
    ('0.313', '31400', '0.309', '31.4k'),  # No implicit suffix conversion.
])
def test_completed_row_still_rejects_wrong_owner_order_and_scale(values):
    _, evidence = row_report(values=values)
    assert evidence['numeric_audit']['issues']
    assert evidence['numeric_audit']['publication']['withheld_cells'] > 0


def test_unrelated_citation_cannot_expand_back_to_previous_row():
    output, evidence = row_report(start=PAGE.index('Beta'))
    assert '| Alpha | 0.313 |' not in output
    assert evidence['numeric_audit']['issues']


def test_independently_cited_training_clause_survives_bad_sibling():
    quote = 'The training batch size is 128 and the micro batch size is 8.'
    report = f'## 可复现性\n\n- 训练参数：batch size = 128，micro batch size = 8 [[证据:{quote}]]；熵系数为 99.9；未记录其他设置。'
    output, evidence = attach_evidence(report, ParsedPaper(quote, [quote]), pdf_available=True, full_report=True)
    assert 'batch size = 128' in output and 'micro batch size = 8' in output
    assert '99.9' in output and '**[待核对]**' in output
    assert evidence['numeric_audit']['issues'][0]['action'] == 'flag_claim'


@pytest.mark.parametrize('tail', ['', '；熵系数 99.9。'])
def test_sibling_quote_does_not_rescue_uncited_clause_with_same_number(tail):
    quote = 'The training batch size is 128 for every training run.'
    report = f'## 可复现性\n\n- batch size = 128 [[证据:{quote}]]；测试准确率为 128{tail}'
    output, evidence = attach_evidence(report, ParsedPaper(quote, [quote]), pdf_available=True, full_report=True)
    assert 'batch size = 128' in output
    assert '测试准确率为 128' in output and '**[待核对]**' in output
    assert evidence['numeric_audit']['issues']


def test_semicolon_in_math_or_quote_does_not_split_a_claim():
    quote = 'The training batch size is 128; we preserve this exact source text.'
    report = f'## 可复现性\n\n- batch size = 128 [[证据:{quote}]]，参数 $f(x;y)$ 为 99.9。'
    output, evidence = attach_evidence(report, ParsedPaper(quote, [quote]), pdf_available=True, full_report=True)
    assert '99.9' in output and '**[待核对]**' in output
    assert evidence['numeric_audit']['issues'][0]['action'] == 'flag_claim'


def test_supported_semicolon_clause_preserves_nonnumeric_commentary():
    quote = 'The training batch size is 128 for every training run.'
    report = f'## 可复现性\n\n- batch size = 128 [[证据:{quote}]]；原文未列出更多设置。'
    output, evidence = attach_evidence(report, ParsedPaper(quote, [quote]), pdf_available=True, full_report=True)
    assert not evidence['numeric_audit']['issues']
    assert '原文未列出更多设置。' in output


def test_ambiguous_repeated_model_rows_are_not_completed():
    page = PAGE + '\nTable 3: Other settings\n' + PAGE
    # The excerpt intersects both Alpha rows with no condition to distinguish them.
    output, evidence = row_report(page=page, start=0)
    assert not evidence['numeric_audit']['completed_row_citations']


def test_parenthetical_breakdown_is_not_split_into_a_dangling_fragment():
    quote = 'The final sample has 19,531 Icon cases and 11,206 Illustration cases.'
    report = f'## 可复现性\n\n- 输入为 99,999 条；输出总计 30,737 条（Icon: 19,531；Illustration: 11,206）[[证据:{quote}]]。'
    output, evidence = attach_evidence(report, ParsedPaper(quote, [quote]), pdf_available=True, full_report=True)
    assert 'Illustration: 11,206）' in output and '**[待核对]**' in output
    assert evidence['numeric_audit']['issues'][0]['action'] == 'flag_claim'


def test_qualitative_intro_is_not_withheld_with_an_unrelated_numeric_clause():
    quote = 'The final rendering style score of the model is 0.599.'
    report = f'## 关键结果\n\n- 维度消融：去除视觉轴影响较大；Style 评分 0.599 [[证据:{quote}]]；其他评分 99.9。'
    output, _ = attach_evidence(report, ParsedPaper(quote, [quote]), pdf_available=True, full_report=True)
    assert '维度消融：去除视觉轴影响较大' in output and 'Style 评分 0.599' in output
    assert '99.9' in output and '**[待核对]**' in output
