"""Captions and notes have the same single owner as the experimental cells."""
import json

import pytest

from arxiv_ra.report_tables import ReportTables, SourceTable, TableVariant, TableRow


@pytest.mark.parametrize('model', ['BERT', 'Transformer', 'ResNet', 'Llama'])
def test_annotation_selection_cannot_discard_plain_model_names(model):
    catalogue = ReportTables.from_notes([
        f'### Table 1: Results\n\n{model} uses the baseline configuration.\n\n'
        f'| Model | Score |\n|---|---|\n| {model} | 1 |'], [])
    from arxiv_ra.table_annotations import annotation_units
    units = annotation_units(catalogue.tables[0])
    groups = [[u['id'] for u in units]]
    selected = catalogue.consolidate_annotations(lambda *_: json.dumps({
        'tables': [{'id': 'table-1', 'groups': groups}]}))
    assert not selected.annotations
    assert f'{model} uses the baseline configuration.' in selected.render('## Results')


def test_reference_cell_cleanup_does_not_count_as_deleted_scientific_note():
    note = '仅供比较'
    catalogue = ReportTables.from_notes([
        '### Table 1: Results\n\n说明：' + note + '。\n\n'
        '| Model | Score | 原文依据 |\n|---|---|---|\n| A | 1 | ' + note + ' |'], [])
    report = catalogue.render('## 关键结果')
    cleaned = report.replace('| 1 | ' + note + ' |', '| 1 | — |')
    assert catalogue.review(cleaned)['status'] == 'passed'
    assert catalogue.review(cleaned.replace(note + '。', ''))['status'] == 'failed'


def test_ambiguous_fragment_diagnostic_is_not_a_scientific_annotation():
    catalogue = ReportTables.from_notes([
        '### Table 1: Results\n\n说明：相对下降 64%（分片摘录未能唯一定位，缺少可定位原文依据）。\n\n'
        '| Model | Score |\n|---|---|\n| A | 9.0 |'], [])
    output = catalogue.render('## 关键结果')
    assert '64%' in output
    assert '分片摘录' not in output and '缺少可定位' not in output


def test_annotation_review_preserves_absolute_value_notes_outside_matrices():
    catalogue = ReportTables.from_notes([
        '### Table 1: Results\n\n| x | = 5。\n\n'
        '| Model | Score |\n|---|---|\n| A | 1 |'], [])
    output = catalogue.render('## 关键结果')
    assert catalogue.review(output)['status'] == 'passed'
    assert catalogue.review(output.replace('| x | = 5。', ''))['status'] == 'failed'


def test_parenthesized_caption_and_quoted_notes_are_atomic_clauses():
    from arxiv_ra.table_annotations import annotation_units
    catalogue = ReportTables.from_notes([
        '### Table 6: CIFAR results\n\n'
        '中文说明：CIFAR-10 测试集错误率（%，越低越好）。展示为“最优，均值；方差”。\n\n'
        '| Model | Error |\n|---|---|\n| A | 6.43 |'], [])
    texts = [u['text'] for u in annotation_units(catalogue.tables[0])]
    assert 'CIFAR-10 测试集错误率（%，越低越好）。' in texts
    assert '展示为“最优，均值；方差”。' in texts


def ruler_table():
    return ReportTables((SourceTable('table-10', 10,
        'Table 10: Rubric-generation cost for the RL training data. '
        'Avg. Output Tokens includes both the generated ideal SVG and rubric; '
        'Avg. Rubric Tokens counts the rubric portion only.', 18,
        (TableVariant('v1', '', ('Statistic', 'Overall'),
            (TableRow('r1', ('Total Cost', '$2,013.12'), ''),), (
                '*中文说明*：强化学习训练数据的细则生成成本统计。模型为 Claude-Opus-4.6。'
                '平均输出 token 包含生成的理想 SVG 和细则两部分；平均细则 token 仅计细则部分。',
                '*(表格数据依据正文 Appendix G 与 Table 10 原文陈述 '
                '（片段引用不可用，缺少可定位原文依据）)*',
                'Table 10：RL 训练数据的标准生成成本（Rubric-generation cost for the RL training data）',
                '表格说明：统计使用 Claude-Opus-4.6 预先生成理想 SVG 和标准时的 token 消耗与美元成本。'
                '`Avg. Output Tokens` 包括生成的理想 SVG 及评分标准两部分；'
                '`Avg. Rubric Tokens` 仅统计评分标准部分 （片段引用不可用，缺少可定位原文依据）。',
            )),)),))


def test_ruler_caption_and_paraphrased_notes_have_one_display_without_rewriting_cells():
    catalogue = ruler_table()
    from arxiv_ra.table_annotations import annotation_units
    units = annotation_units(catalogue.tables[0])
    by_text = {u['text']: u['id'] for u in units}
    # A semantic selector chooses existing text, never generates replacement facts.
    def select(system, prompt):
        groups = []
        def group(keep, *other):
            groups.append([by_text[keep], *(by_text[t] for t in other)])
        group('RL 训练数据的标准生成成本（Rubric-generation cost for the RL training data）',
              'Rubric-generation cost for the RL training data.', '强化学习训练数据的细则生成成本统计。')
        group('统计使用 Claude-Opus-4.6 预先生成理想 SVG 和标准时的 token 消耗与美元成本。',
              '模型为 Claude-Opus-4.6。')
        group('`Avg. Output Tokens` 包括生成的理想 SVG 及评分标准两部分；',
              'Avg. Output Tokens includes both the generated ideal SVG and rubric;',
              '平均输出 token 包含生成的理想 SVG 和细则两部分；')
        group('`Avg. Rubric Tokens` 仅统计评分标准部分。',
              'Avg. Rubric Tokens counts the rubric portion only.', '平均细则 token 仅计细则部分。')
        return json.dumps({'tables': [{'id': 'table-10', 'groups': groups}]})
    result = catalogue.consolidate_annotations(select)
    assert result.tables == catalogue.tables  # raw evidence is retained verbatim
    output = result.render('## 实验设置\n\n[[表格:table-10]]')
    assert output.count('Table 10') == 1
    assert output.count('Claude-Opus-4.6') == 1
    assert output.count('Avg. Output Tokens') == 1
    assert output.count('Avg. Rubric Tokens') == 1
    assert '片段引用不可用' not in output
    assert '$2,013.12' in output
    assert result.review(output)['status'] == 'passed'
    assert ReportTables.from_dict(result.to_dict()).render('## 实验设置\n\n[[表格:table-10]]') == output
    assert result.render(output) == output
    assert result.review(output.replace('| Statistic', 'Table 10: repeated caption\n\n| Statistic'))['status'] == 'failed'
    assert result.review(output.replace('| Statistic',
        '统计使用 Claude-Opus-4.6 预先生成理想 SVG 和标准时的 token 消耗与美元成本。\n\n| Statistic'))['status'] == 'failed'


def test_invalid_annotation_partition_preserves_every_scientific_note():
    catalogue = ruler_table()
    # Dropped units, unknown IDs, duplicate assignments must all fail closed.
    for groups in ([], [['invented']], [['title', 'title']]):
        result = catalogue.consolidate_annotations(lambda *_: json.dumps({'tables': [{'id': 'table-10', 'groups': groups}]}))
        assert not result.annotations
        output = result.render('## 结果')
        assert 'Claude-Opus-4.6' in output and '$2,013.12' in output


def test_unique_conditions_are_not_lost_by_numeric_deduplication():
    from arxiv_ra.table_annotations import annotation_units
    catalogue = ReportTables.from_notes([
        '### Table 2: Results\n\n说明：Five runs.\n\n'
        '*注：temperature=0.2; MSE ↓; Tokens in k.*\n\n'
        '| Model | Score |\n|---|---|\n| A | 91.2 |'], [{'number': 2}])
    units = annotation_units(catalogue.tables[0])
    # Even a model calling two different numeric conditions equivalent cannot drop them.
    groups = [[u['id'] for u in units]]
    result = catalogue.consolidate_annotations(lambda *_: json.dumps({'tables': [{'id': 'table-2', 'groups': groups}]}))
    assert not result.annotations
    text = result.render('## 结果')
    assert 'temperature=0.2' in text and 'MSE ↓' in text and 'Tokens in k' in text


def test_annotation_failure_does_not_fail_report_and_cancellation_propagates():
    import pytest
    def unavailable(*_):
        raise RuntimeError('offline')
    assert not ruler_table().consolidate_annotations(unavailable).annotations
    def cancelled(*_):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        ruler_table().consolidate_annotations(cancelled)


def test_formulas_lists_and_named_model_conditions_are_preserved():
    from dataclasses import replace
    from arxiv_ra.table_annotations import annotation_units, validate_groups
    base = ruler_table().tables[0]
    notes = ('$$\nx = 5; y = 7\n$$', '- only subset A\n- excludes invalid samples',
             'GPT-5.5 uses temperature=0.2。', 'GPT-5.5 uses temperature=0.8。')
    table = replace(base, variants=(replace(base.variants[0], context=notes),))
    units = annotation_units(table)
    groups = [[u['id']] for u in units]
    assert validate_groups(units, groups)
    # Model output may not equate different temperatures or fold a formula.
    assert not validate_groups(units, groups[:-2] + [groups[-2] + groups[-1]])
    assert not validate_groups(units, [groups[0] + groups[3]] + groups[1:3] + groups[4:])
    output = ReportTables((table,)).render('## 实验设置')
    for note in notes:
        assert note in output


def test_selected_annotation_does_not_end_in_a_dangling_comma():
    from dataclasses import replace
    from arxiv_ra.table_annotations import display_annotations
    table = ruler_table().tables[0]
    table = replace(table, variants=(replace(table.variants[0], context=('模型固定，',)),))
    _, notes = display_annotations(table)
    assert notes[0].endswith('模型固定。')


def test_review_rejects_removed_or_changed_table_note_even_without_selection():
    from dataclasses import replace
    from arxiv_ra.table_annotations import annotation_units
    import re
    table = ruler_table().tables[0]
    table = replace(table, variants=(replace(table.variants[0], context=('All scores exclude ties and use held-out samples.',)),))
    for selected in (False, True):
        groups = [[u['id']] for u in annotation_units(table)]
        catalogue = ReportTables((table,), ({'id':table.id,'groups':groups},) if selected else ())
        report = catalogue.render('## 结果')
        assert catalogue.review(report)['status']=='passed'
        removed = re.sub(r'\*\*说明：\*\*[^\n]+','',report)
        assert catalogue.review(removed)['status']=='failed'
        assert catalogue.review(report.replace('exclude ties','include ties'))['status']=='failed'
