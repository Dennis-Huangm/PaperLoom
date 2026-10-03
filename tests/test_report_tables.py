"""The table catalogue is the source of displayed cells, independent of prose."""
from arxiv_ra.report_tables import ReportTables


def test_repeated_multilevel_header_extraction_has_one_matrix():
    plain = ('### Table 6: Ablation over model size.\n\n'
             '| #L | #H | #A | LM (ppl) | MNLI-m |\n|---|---|---|---|---|\n'
             '| 3 | 768 | 12 | 5.84 | 77.9 |\n| 6 | 768 | 3 | 5.24 | 80.6 |')
    grouped = plain.replace('| #L | #H | #A | LM (ppl) | MNLI-m |',
                            '| Hyperparams #L | Hyperparams #H | Hyperparams #A | Dev Set Accuracy LM (ppl) | Dev Set Accuracy MNLI-m |')
    catalogue = ReportTables.from_notes([plain, grouped], [{'number': 6}])
    assert len(catalogue.tables[0].variants) == 1
    assert 'Hyperparams #L' in catalogue.tables[0].variants[0].headers
    output = catalogue.render('## 关键结果\n\n[[表格:table-6]]')
    assert output.count('| 3 | 768 |') == 1
    assert catalogue.review(output)['status'] == 'passed'
    # A changed value or a different split is not duplicate extraction.
    for different in [grouped.replace('77.9', '78.0'), grouped.replace('Dev Set Accuracy', 'Test Set Accuracy')]:
        retained = ReportTables.from_notes([plain, different], [{'number': 6}])
        assert len(retained.tables[0].variants) == 2


def test_multilevel_header_deduplication_preserves_both_citations_and_roundtrips():
    one = ('### Table 6: Model size\n\n| #L | #H | Score | 原文依据 |\n|---|---|---|---|\n'
           '| 3 | 768 | 77.9 | [[证据ID:p1-s1]] |')
    two = one.replace('#L | #H', 'Hyperparams #L | Hyperparams #H').replace('p1-s1', 'p1-s2')
    catalogue = ReportTables.from_notes([one, two], [{'number': 6}])
    assert len(catalogue.tables[0].variants) == 1
    rendered = catalogue.render('## 关键结果')
    assert rendered.count('77.9') == 1
    assert 'p1-s1' in rendered and 'p1-s2' in rendered
    assert ReportTables.from_dict(catalogue.to_dict()).render('## 关键结果') == rendered


def test_one_source_table_has_one_display_with_all_rows_after_duplicate_slots():
    note = ('### Table 1: Editing results\n\n*Results use five runs; MSE ↓.*\n\n'
            '| Model | # Params | MSE ↓ |\n|---|---|---|\n'
            '| Gemma 2 | 2.61B | 0.087 |\n| Gemma 2 | 9.24B | 0.076 |')
    catalogue = ReportTables.from_notes([note, note], [{'number': 1, 'title': 'Editing results', 'page': 5}])
    report = ('## 关键结果\n\n### 主评测结果\n分析。\n\n[[表格:table-1]]\n\n'
              '### 对比讨论\n再次引用。\n\n[[表格:table-1]]')
    rendered = catalogue.render(report)
    assert rendered.count('| Gemma 2 |') == 2
    assert '2.61B' in rendered and '9.24B' in rendered
    assert rendered.count('#### Table 1:') == 1
    assert 'five runs' in rendered and 'MSE ↓' in rendered
    assert catalogue.render(rendered) == rendered
    stored = catalogue.to_dict()
    assert stored['version'] == 3 and len(stored['tables']) == 1
    assert ReportTables.from_dict(stored).render(report) == rendered


def test_distinct_conditions_and_conflicting_values_stay_in_one_source_table():
    one = '### Table 3 (temperature=0.2)\n\n| Model | Score |\n|---|---|\n| A | 91.2 |'
    two = one.replace('0.2', '0.8').replace('91.2', '87.1')
    catalogue = ReportTables.from_notes([one, two], [{'number': 3}])
    output = catalogue.render('## 关键结果\n\n[[表格:table-3]]')
    assert output.count('#### Table 3') == 1
    assert 'temperature=0.2' in output and 'temperature=0.8' in output
    assert '91.2' in output and '87.1' in output
    assert catalogue.review(output)['status'] == 'passed'
    assert catalogue.review(output.replace('91.2', '9.12'))['status'] == 'failed'
    assert catalogue.review(output + '\n\n' + one)['status'] == 'failed'


def test_catalogue_cannot_be_silently_modified_or_lose_source_references():
    import pytest
    note = ('### Table 2\n| Model | Score | 原文依据 |\n|---|---|---|\n'
            '| A | 91.2 | [[证据ID:p1-s1]] |')
    catalogue = ReportTables.from_notes([note, note.replace('p1-s1', 'p1-s2')], [{'number': 2}])
    output = catalogue.render('## 关键结果')
    assert output.count('91.2') == 1 and 'p1-s1' in output and 'p1-s2' in output
    payload = catalogue.to_dict()
    payload['tables'][0]['caption'] = 'changed'
    with pytest.raises(ValueError, match='changed'):
        ReportTables.from_dict(payload)


def test_generator_repairs_prose_without_giving_it_mutable_table_copies(monkeypatch):
    from types import SimpleNamespace
    from arxiv_ra.config import LLMConfig
    from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
    from arxiv_ra.report import ReportGenerator
    import arxiv_ra.report as module
    note = '### Table 1: Results\n\n| Model | Score |\n|---|---|\n| A | 91.2 |\n| B | 87.1 |'
    draft = ('# Paper\n\n## 关键结果\n\n### 主结果\n分析。\n\n'
             '#### Table 1: Results\n\n| Model | Score |\n|---|---|\n| A | 999.0 |')
    def chat(system, prompt):
        if '这是论文第' in prompt:
            return note
        assert '[[表格:table-N]]' in prompt and 'table-1' in prompt
        return draft
    def repair(prose, parsed, callback):
        assert '| A |' not in prose and '999.0' not in prose
        assert '[[表格:table-1]]' in prose
        return prose
    monkeypatch.setattr(module, 'repair_numeric_citations', repair)
    generator = ReportGenerator(SimpleNamespace(enabled=True, chat=chat), LLMConfig())
    paper = Paper.from_dict({'arxiv_id':'2502.19453', 'title':'Paper', 'version':1})
    output = generator.generate(paper, VerifiedMetadata(), ParsedPaper(note, [note]), [])
    assert '999.0' not in output and '91.2' in output and '87.1' in output
    assert output.tables.review(output)['status'] == 'passed'
    assert output.draft == draft


def test_examples_do_not_insert_tables_and_unknown_directives_do_not_leak():
    catalogue = ReportTables.from_notes(['### Table 1\n| Model | Score |\n|---|---|\n| A | 91.2 |'], [{'number':1}])
    draft = '## 关键结果\n\n```text\n[[表格:table-1]]\n```\n\n[[表格:table-999]]\n\n[[表格:table-1]]'
    output = catalogue.render(draft)
    assert '```text\n[[表格:table-1]]\n```' in output
    assert '[[表格:table-999]]' not in output
    assert output.count('| A | 91.2 |') == 1


def test_publication_review_limits_do_not_reject_unchanged_experimental_cells():
    from arxiv_ra.evidence import attach_evidence
    from arxiv_ra.models import ParsedPaper
    note = ('### Table 1: Scores\n| Model | Score | 原文依据 |\n|---|---|---|\n'
            '| A | 91.2 | [[证据ID:p1-s1]] |')
    catalogue = ReportTables.from_notes([note], [{'number':1}])
    report, evidence = attach_evidence(catalogue.render('## 关键结果'),
                                       ParsedPaper('Sparse source', ['Sparse source']),
                                       pdf_available=True, full_report=True)
    assert '91.2' in report and catalogue.review(report)['status'] == 'passed'


def test_adjacent_large_tables_do_not_import_presentation_as_scientific_context():
    notes = [f'### Table {number}: Results\n\n*Five runs; higher is better.*\n\n'
             '| Model | Score |\n|---|---|\n' + '\n'.join(f'| M{i} | {number}.{i} |' for i in range(20))
             + f'\n\nFootnote for table {number}.' for number in (1, 2)]
    catalogue = ReportTables.from_notes(notes, [{'number':1}, {'number':2}])
    assert all('<details' not in context and '</details>' not in context
               for table in catalogue.tables for variant in table.variants for context in variant.context)
    output = catalogue.render('## 关键结果\n\n[[表格:table-1|折叠]]\n\n[[表格:table-2|折叠]]')
    assert output.count('<details') == output.count('</details>') == 2
    assert output.count('<summary>') == 2
    assert 'Footnote for table 1.' in output and 'Footnote for table 2.' in output
    assert catalogue.review(output)['status'] == 'passed'


def test_extraction_analysis_is_input_for_prose_without_becoming_a_repeated_table_footnote():
    note = ('### Table 1: Scores\n\n*说明：五次运行的均值 ± 标准差。*\n\n'
            '| Model | Score |\n|---|---|\n| A | 91.2 |\n\n'
            '**主要结果定性归纳**：\n\n- **主要发现**：模型 A 得到 91.2。\n\n'
            '*注：使用 temperature=0.2。*')
    catalogue = ReportTables.from_notes([note], [{'number':1}])
    assert '模型 A 得到 91.2' in catalogue.synthesis_notes([note])[0]
    output = catalogue.render('## 关键结果\n\n模型 A 得到 91.2。\n\n[[表格:table-1]]')
    assert output.count('模型 A 得到 91.2') == 1
    assert '主要结果定性归纳' not in output
    assert '五次运行的均值 ± 标准差' in output and 'temperature=0.2' in output
    adjacent = note.replace('归纳**：\n\n-', '归纳**：\n-')
    catalogue = ReportTables.from_notes([adjacent], [{'number':1}])
    assert '主要结果定性归纳' not in catalogue.render('## 关键结果')
