from arxiv_ra.report_completeness import restore_note_tables


def test_restored_table_context_does_not_repeat_caption_or_evidence_window_limit():
    report = ('## 关键结果\n\n### Table 11: Scores by difficulty\n\n'
              'Table 11: Scores by difficulty\n\nTable 11: Scores by difficulty\n\n'
              '| Method | Score |\n|---|---|\n| A | 1.25 |\n\n'
              '*(注：独立文本片段中包含部分物理第 13 页的表格残片，但因附属表主体不完整，此处仅保留解析说明。)*\n\n'
              '*注：实验独立运行 5 次，分数为均值。*\n')
    output = restore_note_tables(report, [], [{'number': 11}])
    assert output.count('Table 11: Scores by difficulty') == 1
    assert '附属表主体不完整' not in output
    assert '| A | 1.25 |' in output and '实验独立运行 5 次' in output


def test_group_headings_ignore_source_placeholders_after_publication():
    from arxiv_ra.report_completeness import _blocks
    text = ('### Table 11\n| Method | Score | 原文依据 |\n|---|---|---|\n'
            '| Easy | | — |\n| A | 1 | — |\n| Medium | | — |\n| A | 2 | — |')
    rows = list(_blocks(text))[0][1]
    assert [row.get('category') for row in rows if not row.get('category_heading_row')] == ['easy', 'medium']


def test_explicit_difficulty_column_matches_grouped_source_without_duplicate_rows():
    report = ('### Table 5\n| Level | Method | Score |\n|---|---|---|\n'
              '| Easy | A | 1 |\n| Medium | A | 2 |')
    note = ('### Table 5\n| Method | Score |\n|---|---|\n'
            '| Easy | |\n| A | 1 |\n| Medium | |\n| A | 2 |\n| B | 3 |')
    output = restore_note_tables(report, [note], [{'number': 5}])
    assert output.count('| Easy | A | 1 |') == 1
    assert output.count('| Medium | A | 2 |') == 1
    assert '| Medium | B | 3 |' in output
    assert output.count('| Level |') == 1
    assert restore_note_tables(output, [note], [{'number': 5}]) == output


def test_evidence_id_header_suffix_is_evidence_instead_of_experimental_data():
    report = '### Table 10\n| Method | Score | 原文依据 |\n|---|---|---|\n| A | 1 |'
    note = report.replace('原文依据', '原文依据（证据ID）') + ' [[证据ID:p1-s1]]'
    output = restore_note_tables(report, [note], [{'number': 10}])
    assert output.count('| A |') == 1
    assert '[[证据ID:p1-s1]]' in output


def test_difficulty_in_later_column_and_model_category_remain_distinct():
    report = ('### Table 5\n| Method | Model Category | Difficulty | Score |\n|---|---|---|---|\n'
              '| A | Open | Easy | 1 |')
    note = report.replace('| A | Open | Easy | 1 |', '| A | Open | Hard | 1 |')
    output = restore_note_tables(report, [note], [{'number': 5}])
    assert '| A | Open | Easy | 1 |' in output
    assert '| A | Open | Hard | 1 |' in output
    assert restore_note_tables(output, [note], [{'number': 5}]) == output


def test_bilingual_value_header_merges_but_does_not_repair_number_spacing():
    report = ('### Table 4\n| 统计指标 (Statistic) | 对应数值 (Value) |\n|---|---|\n'
              '| Threshold | > 0.55 |\n| Count | 6.9k |')
    note = report.replace('统计指标 (Statistic)', 'Statistic').replace('对应数值 (Value)', 'Value')
    note = note.replace('> 0.55', '> 0 . 55')
    output = restore_note_tables(report, [note], [{'number': 4}])
    assert output.count('| Count |') == 1
    assert output.count('| Threshold |') == 2
    assert '> 0.55' in output and '> 0 . 55' in output
    assert output.count('|---') <= 1


def test_shortened_judge_name_restores_from_exact_positional_source_schema():
    source = ('Table 7: Scores for human evaluation and VLMAJ.\n'
              'Task\nGenerator\nHuman\nModels used as Judges\nGemini 2.5 Flash\nGPT 5.1\nVG-Sketch\nA\n4.0\n3.0\n2.0\n')
    report = ('### Table 7\n| Task | Generator | Human | Gemini 2.5 | GPT 5.1 |\n'
              '|---|---|---|---|---|\n| VG-Sketch | A | 4.0 | 3.0 | 2.0 |')
    note = report.replace('Gemini 2.5 |', 'Gemini 2.5 Flash |')
    output = restore_note_tables(report, [note], [{'number': 7}], source_pages=[source])
    assert output.count('| VG-Sketch | A |') == 1
    assert '| Gemini 2.5 Flash |' in output
    assert restore_note_tables(output, [note], [{'number': 7}], source_pages=[source]) == output


def test_shortened_source_name_is_not_globally_equated_or_completed_from_row_labels():
    from arxiv_ra.report_completeness import _restore_source_header_names, _source_table_headers
    report = ('### Table 7\n| Task | Generator | Human | Gemini 2.5 | GPT 5.1 |\n'
              '|---|---|---|---|---|\n| VG-Sketch | A | 4.0 | 3.0 | 2.0 |')
    ambiguous = ('Table 7: scores\nTask\nGenerator\nHuman\nGemini 2.5 Flash\nGemini 2.5 Pro\nVG-Sketch\nA\n4.0\n')
    assert _restore_source_header_names(report, _source_table_headers([ambiguous])) == report
    row_name = ('Table 7: scores\nTask\nGenerator\nHuman\nGemini 2.5\nGPT 5.1\nVG-Sketch\nA\n4.0\nGemini 2.5 Flash\n')
    assert _restore_source_header_names(report, _source_table_headers([row_name])) == report
    no_source = restore_note_tables(report, [report.replace('Gemini 2.5 |', 'Gemini 2.5 Flash |')], [{'number': 7}])
    assert no_source.count('| VG-Sketch | A |') == 2


def test_short_separator_is_repaired_before_table_presence_and_merging():
    report = '### Table 3\n| Model | Score | 原文依据 |\n|---|---|\n| A | 1 | [[证据ID:p1-s1]] |'
    note = report.replace('|---|---|', '|---|---|---|') + '\n| B | 2 | [[证据ID:p1-s2]] |'
    output = restore_note_tables(report, [note], [{'number': 3}])
    assert output.count('| Model |') == 1
    assert output.count('| A |') == 1
    assert '| B | 2 |' in output
    assert restore_note_tables(output, [note], [{'number': 3}]) == output


def test_extraction_suffix_does_not_make_evidence_a_data_column():
    report = '### Table 2\n| Model | Score | 原文依据 |\n|---|---|---|\n| A | 1 | [[证据ID:p1-s1]] |'
    note = report.replace('原文依据', '原文依据（摘录）').replace('p1-s1', 'p1-s2')
    output = restore_note_tables(report, [note], [{'number': 2}])
    assert output.count('| A |') == 1
    assert '[[证据ID:p1-s1]] [[证据ID:p1-s2]]' in output


def test_known_note_value_fills_truncation_without_repeating_the_model_row():
    report = '### Table 2（具备文本引用之部分）\n| Model | A ↑ | B ↓ |\n|---|---|---|\n| Alpha | 1 | 当前材料截断 |'
    note = report.replace('当前材料截断', '2').replace('（具备文本引用之部分）', '')
    output = restore_note_tables(report, [note], [{'number': 2}])
    assert output.count('| Alpha |') == 1
    assert '| Alpha | 1 | 2 |' in output
    assert '具备文本引用之部分' not in output and '当前材料截断' not in output
    assert restore_note_tables(output, [note], [{'number': 2}]) == output


def test_ambiguous_truncation_candidates_and_conflicting_retained_cells_do_not_fill():
    from arxiv_ra.report_completeness import _fill_extraction_placeholders
    report = '### Table 2\n| Model | A | B |\n|---|---|---|\n| Alpha | 1 | 当前材料截断 |'
    first = report.replace('当前材料截断', '2')
    other = report.replace('当前材料截断', '3')
    assert _fill_extraction_placeholders(report, [first, other], [{'number': 2}]) == report
    conflict = first.replace('| 1 |', '| 9 |')
    assert _fill_extraction_placeholders(report, [conflict], [{'number': 2}]) == report


def test_truncation_filling_preserves_conditions_categories_and_real_dashes():
    from arxiv_ra.report_completeness import _fill_extraction_placeholders
    report = '### Table 2 (seed=42)\n| Model | A | B |\n|---|---|---|\n| Alpha | 1 | 当前材料截断 |'
    note = report.replace('当前材料截断', '2')
    assert _fill_extraction_placeholders(report, [note.replace('seed=42', 'seed=24')], [{'number': 2}]) == report
    category = note.replace('| Alpha |', '| Closed Models | | |\n| Alpha |')
    assert _fill_extraction_placeholders(report, [category], [{'number': 2}]) == report
    dash = report.replace('当前材料截断', '—')
    assert _fill_extraction_placeholders(dash, [note], [{'number': 2}]) == dash


def test_merged_task_truncation_keeps_later_column_offsets_and_binds_generator():
    report = ('### Table 7\n| Task | Generator | Human | Judge |\n|---|---|---|---|\n'
              '| Sketch | A | 4 | 3 |\n| | B | 2 | 当前材料截断 |')
    note = report.replace('当前材料截断', '1')
    output = restore_note_tables(report, [note], [{'number': 7}])
    assert '| Sketch | B | 2 | 1 |' in output
    assert output.count('| B |') == 1


def test_currency_table_rows_deduplicate_by_separate_cost_columns():
    report = ('### Table 10\n| Statistic | Icon | Illustration | Overall | 原文依据 |\n'
              '|---|---|---|---|---|\n| Total Cost | $1,135.77 | $877.35 | $2,013.12 | 缺少依据 |')
    note = report.replace('缺少依据', '[[证据ID:p1-s1]]')
    output = restore_note_tables(report, [note], [{'number': 10}])
    assert output.count('| Total Cost |') == 1
    assert '| $1,135.77 | $877.35 | $2,013.12 |' in output
    assert '[[证据ID:p1-s1]]' in output


def test_math_direction_glyphs_are_presentation_aliases_but_opposite_direction_is_not():
    report = '### Table 9\n| Method | CLIP ↑ |\n|---|---|\n| A | 0.269 ± 0.001 |'
    note = report.replace('CLIP ↑', r'CLIP $\uparrow$')
    opposite = report.replace('CLIP ↑', r'CLIP \(\downarrow\)')
    output = restore_note_tables(report, [note, opposite], [{'number': 9}])
    assert output.count('| A |') == 2
    assert r'CLIP \(\downarrow\)' in output


def test_bilingual_cost_row_labels_keep_body_labels_without_repeated_data():
    report = ('### Table 10\n| 统计指标 (Statistic) | MMSVG-Icon | MMSVG-Illustration | Overall (总计) |\n'
              '|---|---|---|---|\n| 总开销 (Total Cost, 美元) | $1,135.77 | $877.35 | $2,013.12 |')
    note = report.replace('统计指标 (Statistic)', 'Statistic').replace('Overall (总计)', 'Overall')
    note = note.replace('总开销 (Total Cost, 美元)', 'Total Cost')
    output = restore_note_tables(report, [note], [{'number': 10}])
    assert output.count('$1,135.77') == 1
    assert '总开销 (Total Cost, 美元)' in output
    assert restore_note_tables(output, [note], [{'number': 10}]) == output


def test_explicit_note_categories_survive_merging_into_flat_body():
    report = '### Table 2\n| Model | Score |\n|---|---|\n| A | 1 |'
    note = ('### Table 2\n| 模型分类 | Model | Score |\n|---|---|---|\n'
            '| Closed | B | 2 |\n| Open | C | 3 |')
    output = restore_note_tables(report, [note], [{'number': 2}])
    assert '| Closed | B | 2 |' in output
    assert '| Open | C | 3 |' in output
    assert restore_note_tables(output, [note], [{'number': 2}]) == output


def test_grouped_note_category_survives_explicit_body_column():
    report = '### Table 2\n| 模型分类 | Model | Score |\n|---|---|---|\n| Open | A | 1 |'
    note = '### Table 2\n| Model | Score |\n|---|---|\n| Closed Models | |\n| B | 2 |'
    output = restore_note_tables(report, [note], [{'number': 2}])
    assert '| Closed Models | B | 2 |' in output
    assert restore_note_tables(output, [note], [{'number': 2}]) == output


def test_heading_conditions_preserve_distinct_results_and_retry_identity():
    report = '## 关键结果'
    note = '### Table 2 (temperature=0.8, seed=42)\n| Model | Score |\n|---|---|\n| B | 2 |'
    other = note.replace('temperature=0.8', 'temperature=0.2')
    output = restore_note_tables(report, [note, other], [{'number': 2}])
    assert output.count('| B | 2 |') == 2
    assert 'temperature=0.8' in output and 'temperature=0.2' in output
    assert restore_note_tables(output, [note, other], [{'number': 2}]) == output


def test_original_heading_caption_survives_body_merge():
    report = '### Table 2\n| Model | Score |\n|---|---|\n| A | 1 |'
    note = '### Table 2 Human evaluation, five independent runs\n| Model | Score |\n|---|---|\n| B | 2 |'
    output = restore_note_tables(report, [note], [{'number': 2}])
    assert 'Human evaluation, five independent runs' in output
    assert '| B | 2 |' in output
    assert restore_note_tables(output, [note], [{'number': 2}]) == output


def test_middle_evidence_column_keeps_all_candidate_quotes():
    report = '### Table 2\n| Model | 原文依据 | Score |\n|---|---|---|\n| A | [[证据ID:p1-s1]] | 1 |'
    note = report.replace('p1-s1', 'p1-s2')
    output = restore_note_tables(report, [note], [{'number': 2}])
    assert '[[证据ID:p1-s1]] [[证据ID:p1-s2]] | 1 |' in output
    assert output.count('| A |') == 1
    assert restore_note_tables(output, [note], [{'number': 2}]) == output


def test_arbitrary_heading_conditions_are_not_cosmetic_aliases():
    note = '### Table 2 (fp16, ablation B, batch 8)\n| Model | Score |\n|---|---|\n| B | 2 |'
    other = note.replace('batch 8', 'batch 32')
    output = restore_note_tables('## 关键结果', [note, other], [{'number': 2}])
    assert output.count('| B | 2 |') == 2
    assert 'batch 8' in output and 'batch 32' in output
    assert restore_note_tables(output, [note, other], [{'number': 2}]) == output


def test_matching_flat_row_does_not_discard_explicit_category():
    report = '### Table 2\n| Model | Score |\n|---|---|\n| A | 1 |'
    note = '### Table 2\n| 模型分类 | Model | Score |\n|---|---|---|\n| Closed | A | 1 |'
    output = restore_note_tables(report, [note], [{'number': 2}])
    assert '| Closed | A | 1 |' in output
    assert restore_note_tables(output, [note], [{'number': 2}]) == output


def test_bilingual_caption_is_preserved_without_duplicate_experimental_rows():
    report = ('## 关键结果\n\n### Table 8: Render success rates under single-run evaluation\n'
              '| Method | MMSVG-Icon | MMSVG-Illustration |\n|---|---|---|\n| RULER | 99.3% | 100.0% |')
    note = ('### Table 8: 单次运行基准评测下的渲染成功率（Render success rates under single-run evaluation）\n'
            '| Method | MMSVG-Icon | MMSVG-Illustration |\n|---|---|---|\n| RULER | 99.3% | 100.0% |')
    other = note.replace('Table 8: 单次运行基准评测下的渲染成功率（Render success rates under single-run evaluation）',
                         '表 8：单次运行基准评测下的渲染成功率（Render Success Rates）')
    output = restore_note_tables(report, [note, other], [{'number': 8}])
    assert output.count('| RULER | 99.3% | 100.0% |') == 1
    assert '单次运行基准评测下的渲染成功率' in output
    assert restore_note_tables(output, [note, other], [{'number': 8}]) == output
