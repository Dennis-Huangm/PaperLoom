from arxiv_ra.report_completeness import restore_note_tables, _blocks


SOURCE = ('Model\nType\n# Params\nMSE ↓\nExtraction ↓\n'
          'Original\n0.061\nOpen\nGemma 2\n2.61B\n0.087\n33.6%\n'
          'Gemma 2\n9.24B\n0.076\n37.3%\nClosed\nGPT-4o\nMM\n0.070\n0.1%\n'
          'Table 1: SVG editing results\n')
NOTE = ('### Table 1: SVG editing results\n\n'
        '| | Model | Type | # Params | MSE ↓ | Extraction ↓ |\n|---|---|---|---|---|---|\n'
        '| | Original | | | 0.061 | |\n'
        '| Open | Gemma 2 | | 2.61B | 0.087 | 33.6% |\n'
        '| | Gemma 2 | | 9.24B | 0.076 | 37.3% |\n'
        '| Closed | GPT-4o | MM | | 0.070 | 0.1% |')


def test_original_pdf_anchors_flat_rows_to_merged_model_groups_and_recovers_missing_cells():
    report = ('## 关键结果\n\n### 主评测结果重现\n结果。\n\n'
              '#### Table 1: SVG editing results\n\n'
              '| Model | Type | # Params | MSE ↓ | Extraction ↓ |\n|---|---|---|---|---|\n'
              '| Original | - | - | 0.061 | - |\n'
              '| Gemma 2 | - | 2.61B | 0.087 | 当前材料未完全保留 |\n'
              '| Gemma 2 | - | 9.24B | 0.076 | 37.3% |\n\n'
              '### 编辑成功与失败机制\n定性说明。')
    output = restore_note_tables(report,[NOTE],[{'number':1,'title':'SVG editing results'}],source_pages=[SOURCE])
    blocks = list(_blocks(output))
    assert len(blocks)==1 and len(blocks[0][1])==4
    assert output.count('2.61B')==1 and output.count('9.24B')==1
    assert '当前材料未完全保留' not in output and '33.6%' in output
    assert output.index('#### Table 1') < output.index('### 编辑成功与失败机制')
    assert restore_note_tables(output,[NOTE],[{'number':1,'title':'SVG editing results'}],source_pages=[SOURCE])==output


def test_unnamed_group_column_is_not_inferred_for_ordinary_blank_columns():
    note = NOTE.replace('Open','Seed 42').replace('Closed','Seed 43')
    output = restore_note_tables('## 关键结果',[note],[{'number':1}],source_pages=[SOURCE])
    assert 'Seed 42' in output and 'Seed 43' in output
    assert 'Model Category' not in output


def test_model_group_is_not_inferred_when_original_pdf_does_not_confirm_the_row():
    report = ('#### Table 1: SVG editing results\n\n'
              '| Model | Type | # Params | MSE ↓ | Extraction ↓ |\n|---|---|---|---|---|\n'
              '| Gemma 2 | - | 2.61B | 0.087 | 33.6% |')
    output = restore_note_tables(report,[NOTE],[{'number':1}],source_pages=[SOURCE.replace('2.61B','2.62B')])
    assert '| Gemma 2 | - | 2.61B | 0.087 | 33.6% |' in output
    flat = [row for _,rows,*_ in _blocks(output) for row in rows
            if row['cells'][1][0].strip()=='Gemma 2' and row['cells'][3][0].strip()=='2.61B']
    assert any(row.get('category','')=='' for row in flat)
    assert '2.62B' not in output


def test_recovered_placeholder_summary_and_stage_note_do_not_survive_the_full_table():
    report = ('## 关键结果\n\n#### Table 1: SVG editing results\n\n'
              '| Model | Type | # Params | MSE ↓ | Extraction ↓ |\n|---|---|---|---|---|\n'
              '| Original | - | - | 0.061 | - |\n'
              '| Gemma 2 | - | 2.61B | 0.087 | 33.6% |\n'
              '| Gemma 2 | - | 9.24B | 0.076 | 37.3% |\n'
              '| GPT-4o 等其余 1 个模型 | - | - | 当前材料缺少可定位原文依据 | 当前材料缺少可定位原文依据 |\n\n'
              '*(注：Table 1 完整存在，但由于分片取回的有效文本摘录仅覆盖前几个模型，其余模型行的数据在主表中作缺失说明)*\n')
    output = restore_note_tables(report,[NOTE],[{'number':1}],source_pages=[SOURCE])
    assert '等其余 1 个模型' not in output
    assert '分片取回' not in output
    assert output.count('GPT-4o')==1 and '0.070' in output


def test_restored_identical_metric_explanation_appears_once_after_stage_cleanup():
    explanation = '*说明：MSE ↓，Extraction ↓。*'
    report = ('#### Table 1: SVG editing results\n\n' + explanation + '\n\n'
              '| Model | MSE ↓ |\n|---|---|\n| Original | 0.061 |\n\n'
              + explanation + '\n\n*说明：结果为 5 次运行的均值。*\n\n'
              '*说明：结果为 10 次运行的均值。*')
    output = restore_note_tables(report, [], [{'number': 1}])
    assert output.count(explanation) == 1
    assert '5 次运行' in output and '10 次运行' in output


def test_identical_explanations_in_different_table_sections_remain():
    explanation = '*说明：结果为 5 次运行的均值。*'
    report = '\n\n'.join(f'#### Table {n}: Results\n\n{explanation}\n\n'
                         '| Model | MSE ↓ |\n|---|---|\n| Original | 0.061 |'
                         for n in (1, 2))
    output = restore_note_tables(report, [], [{'number': 1}, {'number': 2}])
    assert output.count(explanation) == 2
