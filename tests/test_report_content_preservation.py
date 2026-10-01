from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper


def test_note_table_survives_synthesis_omission_without_claiming_verified():
    from arxiv_ra.report_completeness import restore_note_tables
    note = '### Table 3 主结果\n\n| Model | Score |\n|---|---|\n| Alpha | 91.2 |\n| Beta | 87.1 |'
    report = '# Paper\n\n## 关键结果\n\n模型表现良好。\n\n## 局限性\n限制。'
    inventory = [{'number': 3, 'page': 1, 'title': 'Results'}]
    restored = restore_note_tables(report, [note, note], inventory)
    assert restored.count('| Beta | 87.1 |') == 1
    assert restored.index('| Beta |') < restored.index('## 局限性')
    assert '部分摘录' in restored and '待核对' not in restored
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
    assert restore_note_tables(report, [note.replace('Table 3', 'Table 99')], [{'number': 3}]) == report


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
    assert not evidence['numeric_audit']['issues']


def test_missing_numeric_citation_keeps_explanation_with_explicit_uncertainty():
    source = 'The model is evaluated with accuracy on the benchmark.'
    claim = '方法采用逐对象检测，能区分编辑失败原因。准确率为 81.3%，并维护对象状态。'
    report, evidence = attach_evidence('# Test\n\n## 关键结果\n\n' + claim,
                                      ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    assert claim in report
    assert '待核对' in report
    assert evidence['numeric_audit']['publication']['withheld_claims'] == 0


def test_numeric_summary_is_reviewed_before_publication():
    source = 'The paper studies object-based evaluation of editing.'
    report, evidence = attach_evidence(
        '# Test\n\n## 一句话总结\n\n该方法达到 91.2% 的准确率。\n\n## 关键结果\n\n定性结果。',
        ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    assert '**[待核对]** 该方法达到 91.2%' in report
    assert evidence['publication_gate']['status'] == 'review_required'
    assert evidence['publication_gate']['unverified_claims'] == 1
    assert evidence['numeric_audit']['issues'][0]['review_state'] == 'pending_verification'


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
    gate = evidence['publication_gate']
    assert gate['table_completeness_unassessed'] == 1
    assert gate['status'] == 'automatic_checks_passed'


def test_missing_table_citation_keeps_cells_without_flags():
    report = '# Test\n\n## 关键结果\n\n| Model | Score |\n|---|---|\n| Alpha | 91.2 |'
    output, evidence = attach_evidence(report, ParsedPaper('', ['Table 1: Results']),
                                      pdf_available=True, full_report=True)
    assert '| Alpha | 91.2 |' in output and '待核对' not in output
    assert evidence['numeric_audit']['table_diagnostics']
    assert evidence['numeric_audit']['publication']['withheld_cells'] == 0


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
