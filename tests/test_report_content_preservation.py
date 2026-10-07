from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper


def test_existing_report_row_keeps_sources_from_matching_note():
    from arxiv_ra.report_completeness import restore_note_tables
    report = '### Table 2\n| Model | Score | 原文依据 |\n|---|---|---|\n| A | 1 | |'
    note = report.replace('| A | 1 | |', '| A | 1 | [[证据ID:p1-s1]] |')
    restored = restore_note_tables(report, [note], [{'number': 2}])
    assert '[[证据ID:p1-s1]]' in restored
    assert restored.count('| A | 1 |') == 1
    assert restore_note_tables(restored, [note], [{'number': 2}]) == restored


def test_restored_categories_remain_correct_across_repeated_chunk_headings():
    from arxiv_ra.report_completeness import restore_note_tables
    from arxiv_ra.table_quality import tables
    from arxiv_ra.evidence import TOKEN
    header = '### Table 2\n| Model | Score |\n|---|---|\n'
    note = header + '| Open-source Models | |\n| A | 1 |\n| Closed Models | |\n| B | 2 |'
    other = header + '| Open-source Models | |\n| C | 3 |'
    restored = restore_note_tables('## 关键结果', [note, other], [{'number': 2}])
    category = ''
    found = {}
    for row in tables(restored, TOKEN):
        first, score = [cell[0] for cell in row['cells']]
        if not score:
            category = first
        else:
            found[first] = category
    assert found == {'A': 'Open-source Models', 'B': 'Closed Models', 'C': 'Open-source Models'}
    assert restore_note_tables(restored, [note, other], [{'number': 2}]) == restored


def test_source_merging_pads_short_rows_without_changing_experimental_values():
    from arxiv_ra.report_completeness import restore_note_tables
    from arxiv_ra.table_quality import tables
    from arxiv_ra.evidence import TOKEN
    short = '### Table 2\n| Model | Score | 原文依据 |\n|---|---|---|\n| A | 1 |'
    note = short.replace('| A | 1 |', '| A | 1 | [[证据ID:p1-s1]] |')
    for report, notes in [(short, [note]), ('## 关键结果', [short, note])]:
        restored = restore_note_tables(report, notes, [{'number': 2}])
        rows = list(tables(restored, TOKEN))
        assert len(rows) == 1
        assert rows[0]['cells'][1][0] == '1'
        assert '[[证据ID:p1-s1]]' in restored
        assert restore_note_tables(restored, notes, [{'number': 2}]) == restored


def test_restored_tables_ignore_emphasis_and_citations_and_use_one_section():
    from arxiv_ra.report_completeness import restore_note_tables
    note = ('### Table 6\n\nSize is reported in SVG samples.\n\n'
            '| Dataset | Size | 原文依据 |\n|---|---|---|\n'
            '| **Alpha** | 6.5k | [[证据ID:p23-s1]] |')
    other = note.replace('**Alpha**', 'Alpha').replace('p23-s1', 'p23-s2')
    restored = restore_note_tables('## 关键结果\n正文。', [note, other], [{'number': 6}])
    assert restored.count('### Table 6') == 1
    assert restored.count('6.5k') == 1
    assert '分片' not in restored


def test_restored_table_keeps_conflicting_values_as_separate_conditions():
    from arxiv_ra.report_completeness import restore_note_tables
    note = '### Table 3\n\n| Model | Split | Score |\n|---|---|---|\n| A | test | 91.2 |'
    other = note.replace('91.2', '87.1')
    restored = restore_note_tables('## 关键结果', [note, other], [{'number': 3}])
    assert '91.2' in restored and '87.1' in restored


def test_restored_task_table_preserves_merged_labels_and_source_candidates():
    from arxiv_ra.report_completeness import restore_note_tables
    note = ('### Table 7\n\n| Task | Generator | Score | 原文依据 |\n|---|---|---|---|\n'
            '| Sketch | A | 1.0 | [[证据ID:p1-s1]] |\n| | Ground Truth | 5.0 | |\n'
            '| Edit | A | 1.0 | |\n| | Ground Truth | 5.0 | |')
    other = note.replace('p1-s1', 'p1-s2')
    restored = restore_note_tables('## 关键结果', [note, other], [{'number': 7}])
    assert '| Sketch | Ground Truth | 5.0 |' in restored
    assert '| Edit | Ground Truth | 5.0 |' in restored
    assert 'p1-s1' in restored and 'p1-s2' in restored


def test_report_view_keeps_numeric_source_links_compact_with_page_tooltip():
    from arxiv_ra.report_presentation import compact_report
    from arxiv_ra.render import report_document
    from bs4 import BeautifulSoup
    output = compact_report('*Size means SVG samples.* [48](paper.pdf#page=23)')
    assert '[48](paper.pdf#page=23 "原文第 23 页")' in output
    assert compact_report(output) == output
    link = BeautifulSoup(report_document(output, 'Test'), 'html.parser').find('a', href='paper.pdf#page=23')
    assert link.get_text() == '48'
    assert link['title'] == '原文第 23 页'


def test_category_column_and_group_row_are_equivalent_but_test_splits_are_not():
    from arxiv_ra.report_completeness import restore_note_tables
    report = ('### Table 2\n| Model | Sketch: Score |\n|---|---|\n'
              '| **Open-source Models** | |\n| A | 91.2 |')
    note = ('### Table 2\n| 模型分类 | Model | Sketch Score |\n|---|---|---|\n'
            '| Open-source | A | 91.2 |')
    assert restore_note_tables(report, [note], [{'number': 2}]) == report.replace('### Table 2', '#### Table 2')


def test_note_table_survives_synthesis_omission_without_claiming_verified():
    from arxiv_ra.report_completeness import restore_note_tables
    note = '### Table 3 主结果\n\n| Model | Score |\n|---|---|\n| Alpha | 91.2 |\n| Beta | 87.1 |'
    report = '# Paper\n\n## 关键结果\n\n模型表现良好。\n\n## 局限性\n限制。'
    inventory = [{'number': 3, 'page': 1, 'title': 'Results'}]
    restored = restore_note_tables(report, [note, note], inventory)
    assert restored.count('| Beta | 87.1 |') == 1
    assert '## 原文表格摘录' not in restored
    assert restored.index('## 关键结果') < restored.index('| Beta |') < restored.index('## 局限性')
    assert '部分摘录' not in restored and '待核对' not in restored
    assert restore_note_tables(restored, [note], inventory) == restored


def test_note_table_fills_synthesis_excerpt_and_preserves_conditions():
    from arxiv_ra.report_completeness import restore_note_tables
    table = '| Model | Test split | Score |\n|---|---|---|\n| Alpha | held-out | 91.2 |'
    note = '### Table 3 Main\n\n' + table + '\n| Beta | held-out | 87.1 |'
    report = '## 关键结果\n\n### Table 3 节选\n\n' + table
    restored = restore_note_tables(report, [note], [{'number': 3}])
    assert '| Beta | held-out | 87.1 |' in restored
    assert restored.count('| Alpha | held-out | 91.2 |') == 1
    assert restored.count('| Beta | held-out | 87.1 |') == 1
    assert restore_note_tables(restored, [note], [{'number': 3}]) == restored
    assert restore_note_tables(report, [note.replace('Table 3', 'Table 99')], [{'number': 3}]) == report.replace('### Table 3', '#### Table 3')


def test_rewritten_headers_and_model_labels_merge_into_body_table():
    from arxiv_ra.report_completeness import restore_note_tables
    report = ('## 关键结果\n\n### 主实验（Table 2）\n\n'
              '| Method | Illustration CLIP ↑ | 原文依据 |\n|---|---|---|\n'
              '| **RULER (本文)** | 0.249 | |\n\n结果分析。\n\n## 局限性\n限制。')
    note = ('### Table 2\n\n| Method | MMSVG-Illustration CLIP ↑ | 原文依据 |\n|---|---|---|\n'
            '| RULER | 0.249 | [[证据ID:p1-s1]] |\n| Qwen3-8B | 0.244 | [[证据ID:p1-s1]] |')
    restored = restore_note_tables(report, [note], [{'number': 2}])
    assert restored.count('0.249') == 1
    assert restored.index('Qwen3-8B') < restored.index('结果分析。')
    assert '[[证据ID:p1-s1]]' in restored
    assert '## 原文表格摘录' not in restored
    assert restore_note_tables(restored, [note], [{'number': 2}]) == restored


def test_missing_table_joins_existing_discussion_and_costs_join_reproducibility():
    from arxiv_ra.report_completeness import restore_note_tables
    report = ('## 关键结果\n\n### 规模鲁棒性（Table 5）\n\n规模对比。\n\n'
              '## 可复现性\n\n### 数据与成本\n\n成本说明。')
    notes = ['### Table 5\n| Model | Score |\n|---|---|\n| A | 1 |',
             '### Table 10\n| Statistic | Cost |\n|---|---|\n| Total | $2 |']
    restored = restore_note_tables(report, notes, [{'number': 5, 'title': 'Scaling'},
                                                  {'number': 10, 'title': 'Cost statistics'}])
    assert restored.index('| A |') < restored.index('## 可复现性') < restored.index('| Total |')
    assert '## 原文表格摘录' not in restored


def test_legacy_appendix_merges_bilingual_reordered_tables_and_keeps_links():
    from arxiv_ra.report_completeness import restore_note_tables
    report = ('## 关键结果\n\n### 人类评价（Table 3）\n\n'
              '| 对比项 (Baseline) | Win | 原文依据 |\n|---|---|---|\n'
              '| **vs. Qwen3-8B** | 122 | [1](paper.pdf#page=7) |\n\n'
              '## 原文表格摘录\n\n### Table 3（部分摘录）\n\n'
              '| Win | Baseline | 原文依据 |\n|---|---|---|\n'
              '| 122 | Qwen3-8B | [2](paper.pdf#page=7) |\n'
              '| 84 | Qwen3-32B | [2](paper.pdf#page=7) |\n\n## 引用与核对\n详情。')
    restored = restore_note_tables(report, [], [{'number': 3}])
    assert restored.count('122') == 1
    assert '[1](paper.pdf#page=7)' in restored and '[2](paper.pdf#page=7)' in restored
    assert '| Qwen3-32B | 84 |' in restored
    assert restored.index('Qwen3-32B') < restored.index('## 引用与核对')
    assert '## 原文表格摘录' not in restored
    assert restore_note_tables(restored, [], [{'number': 3}]) == restored


def test_restoring_citation_to_uncited_existing_table_does_not_drop_source():
    from arxiv_ra.report_completeness import restore_note_tables
    report = '### Table 2\n| Model | Score |\n|---|---|\n| A | 1 |'
    note = '### Table 2\n| Model | Score | 原文依据 |\n|---|---|---|\n| A | 1 | [[证据ID:p1-s1]] |'
    restored = restore_note_tables(report, [note], [{'number': 2}])
    assert restored.count('| A | 1 |') == 1
    assert '[[证据ID:p1-s1]]' in restored
    assert restore_note_tables(restored, [note], [{'number': 2}]) == restored


def test_different_conditions_and_explicit_categories_never_collapse():
    from arxiv_ra.report_completeness import restore_note_tables
    note = ('### Table 2\n| Model | Split | Score |\n|---|---|---|\n'
            '| A | validation | 1 |\n| A | test | 1 |')
    report = restore_note_tables('## 关键结果', [note], [{'number': 2}])
    assert '| A | validation | 1 |' in report and '| A | test | 1 |' in report
    categories = '### Table 2\n| Model | Score |\n|---|---|\n| Open Models | |\n| A | 1 |\n| Closed Models | |\n| A | 1 |'
    report = restore_note_tables('## 关键结果', [categories], [{'number': 2}])
    assert report.count('| A | 1 |') == 2


def test_duplicate_extraction_keeps_footnotes_and_legacy_unknown_tables():
    from arxiv_ra.report_completeness import restore_note_tables
    report = '## 关键结果\n\n### Table 2\n| Model | Score |\n|---|---|\n| A | 1 |'
    note = '### Table 2\n五次运行。\n\n| Model | Score |\n|---|---|\n| A | 1 |\n\n脚注：不包含失败样本。'
    restored = restore_note_tables(report, [note], [{'number': 2}])
    assert '五次运行。' in restored and '脚注：不包含失败样本。' in restored
    assert restore_note_tables(restored, [note], [{'number': 2}]) == restored
    legacy = '## 原文表格摘录\n\n### Table 99\n| Model | Score |\n|---|---|\n| B | 2 |'
    restored = restore_note_tables(report + '\n\n' + legacy, [], [{'number': 2}])
    assert '| B | 2 |' in restored and '## 原文表格摘录' not in restored


def test_merged_task_body_keeps_column_offsets_when_sources_are_added():
    from arxiv_ra.report_completeness import restore_note_tables
    from arxiv_ra.render import report_document
    report = ('## 关键结果\n\n### Table 7\n| Task | Generator | Score |\n|---|---|---|\n'
              '| Sketch | A | 1.0 |\n| | Ground Truth | 5.0 |')
    note = ('### Table 7\n| Task | Generator | Score | 原文依据 |\n|---|---|---|---|\n'
            '| Sketch | Ground Truth | 5.0 | [[证据ID:p1-s1]] |\n| Sketch | B | 2.0 | |')
    restored = restore_note_tables(report, [note], [{'number': 7}])
    assert '| Sketch | Ground Truth | 5.0 | [[证据ID:p1-s1]] |' in restored
    assert '| Sketch | B | 2.0 |' in restored
    report_document(restored, 'Test')
    assert restore_note_tables(restored, [note], [{'number': 7}]) == restored


def test_rubric_generator_dash_variants_do_not_duplicate_baseline_rows():
    from arxiv_ra.report_completeness import restore_note_tables
    note = '### Table 9\n| Method | Rubric Generator | Score |\n|---|---|---|\n| A | - | 0.266 ± 0.003 |'
    other = note.replace(' - ', ' – ').replace('0.266 ± 0.003', '0.266±0.003')
    restored = restore_note_tables('## 关键结果', [note, other], [{'number': 9}])
    assert restored.count('0.266') == 1


def test_ambiguous_category_and_repeated_columns_are_preserved():
    from arxiv_ra.report_completeness import restore_note_tables
    report = '## 关键结果\n\n### Table 2\n| Model | Score |\n|---|---|\n| A | 1 |'
    note = '### Table 2\n| Model | Score |\n|---|---|\n| Open Models | |\n| A | 1 |\n| Closed Models | |\n| A | 1 |'
    restored = restore_note_tables(report, [note], [{'number': 2}])
    assert '| Open Models |' in restored and '| Closed Models |' in restored
    repeated = '### Table 3\n| Model | Score | Score |\n|---|---|---|\n| A | 1 | 2 |'
    restored = restore_note_tables('## 关键结果', [repeated, repeated.replace('1 | 2', '2 | 1')], [{'number': 3}])
    assert '| A | 1 | 2 |' in restored and '| A | 2 | 1 |' in restored


def test_neutral_table_heading_does_not_certify_completeness():
    from arxiv_ra.report_completeness import normalize_table_titles
    report = ('## 关键结果\n\n### Table 5（部分摘录）\n\n'
              '| Model | Score |\n|---|---|\n| A | 1 |\n\n'
              '### 主实验（Table 2 部分数据重现）\n\n说明。')
    normalized = normalize_table_titles(report)
    assert '### Table 5\n' in normalized
    assert '### 主实验（Table 2）' in normalized
    assert '| A | 1 |' in normalized
    assert normalize_table_titles(normalized) == normalized
    _, evidence = attach_evidence(normalized, ParsedPaper('Table 5: Results', ['Table 5: Results']),
                                 pdf_available=True, full_report=True)
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


def test_generator_preserves_extracted_table_when_final_model_omits_it():
    from types import SimpleNamespace
    from arxiv_ra.config import LLMConfig
    from arxiv_ra.models import Paper, VerifiedMetadata
    from arxiv_ra.report import ReportGenerator
    from arxiv_ra.source_spans import source_spans
    source = 'Table 3: Results\n| Model | Score |\n|---|---|\n| Alpha | 91.2 |\n| Beta | 87.1 |'
    parsed = ParsedPaper(source, [source])
    key = next(iter(source_spans(parsed)))
    note = ('### Table 3 主结果\n\n| Model | Score | 依据 |\n|---|---|---|\n'
            f'| Alpha | 91.2 | [[证据ID:{key}]] |\n| Beta | 87.1 | [[证据ID:{key}]] |')
    llm = SimpleNamespace(enabled=True, chat=lambda system, prompt:
                          note if '这是论文第' in prompt else '# Paper\n\n## 关键结果\n表现良好。')
    report = ReportGenerator(llm, LLMConfig()).generate(
        Paper.from_dict({'arxiv_id': '2501.00001', 'title': 'Paper'}), VerifiedMetadata(title='Paper'), parsed, None)
    assert '| Beta | 87.1 |' in report
    published, evidence = attach_evidence(report, parsed, pdf_available=True, full_report=True)
    assert '| Beta | 87.1 |' in published
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


def test_missing_numeric_citation_keeps_explanation_with_explicit_uncertainty():
    source = 'The model is evaluated with accuracy on the benchmark.'
    claim = '方法采用逐对象检测，能区分编辑失败原因。准确率为 81.3%，并维护对象状态。'
    report, evidence = attach_evidence('# Test\n\n## 关键结果\n\n' + claim,
                                      ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    assert claim in report
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


def test_numeric_summary_is_published_without_review():
    source = 'The paper studies object-based evaluation of editing.'
    report, evidence = attach_evidence(
        '# Test\n\n## 一句话总结\n\n该方法达到 91.2% 的准确率。\n\n## 关键结果\n\n定性结果。',
        ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    assert '该方法达到 91.2%' in report and '**[待核对]**' not in report
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


def test_partial_table_is_distinct_from_reproduced_and_remains_in_index():
    from arxiv_ra.evidence import report_table_coverage, source_table_index
    inventory = [{'number': 3, 'page': 2, 'title': 'Main results'}]
    report = '## 关键结果\n\n### Table 3（分片摘录，完整性待核对）\n\n| Model | Score |\n|---|---|\n| A | 91.2 |'
    coverage = report_table_coverage(report, inventory)
    assert coverage['presented'] == []
    assert [item['number'] for item in coverage['partial']] == [3]
    assert '摘录' in source_table_index(coverage)


def test_markdown_table_is_not_treated_as_proof_of_pdf_completeness():
    report = '## 关键结果\n\n### Table 3 主结果\n\n| Model | Score |\n|---|---|\n| A | 91.2 |'
    source = 'Table 3: Main results\nModel A 91.2'
    _, evidence = attach_evidence(report, ParsedPaper(source, [source]),
                                  pdf_available=True, full_report=True)
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


def test_missing_table_citation_keeps_cells_without_flags():
    report = '# Test\n\n## 关键结果\n\n| Model | Score |\n|---|---|\n| Alpha | 91.2 |'
    output, evidence = attach_evidence(report, ParsedPaper('', ['Table 1: Results']),
                                      pdf_available=True, full_report=True)
    assert '| Alpha | 91.2 |' in output and '待核对' not in output
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


def test_legacy_recovery_is_idempotent_and_does_not_guess_duplicate_locations():
    from arxiv_ra.report_recovery import recover_numeric_content
    old = '定量陈述暂不展示。'
    evidence = {'citations': [], 'numeric_audit': {'checked_claims': 1, 'issues': [
        {'action': 'withhold_claim', 'original': '解释文字，结果为 91.2%。',
         'replacement': old, 'start': 0, 'end': 20}], 'publication': {'withheld_claims': 1}}}
    output, data, stats = recover_numeric_content('## 关键结果\n' + old, evidence)
    assert '**[待核对]** 解释文字，结果为 91.2%。' in output
    assert stats['restored_in_place'] == 1
    assert evidence['numeric_audit']['issues'][0]['action'] == 'withhold_claim'
    assert recover_numeric_content(output, data)[0] == output
    output, _, stats = recover_numeric_content(old + '\n' + old, evidence)
    assert output.startswith(old + '\n' + old)
    assert '## 待核对内容恢复' in output and stats['appended_for_review'] == 1
