from bs4 import BeautifulSoup

from arxiv_ra.report_completeness import restore_note_tables
from arxiv_ra.render import report_document


def test_supplementary_tables_live_under_their_experiment_not_a_tail_list():
    report = ('## 核心方法\n\n### 关键模块与流程\n数据分层。\n\n### 公式与目标函数解析\n公式。\n\n'
              '## 关键结果\n\n### SVG 理解维度评测\n理解结果。\n\n'
              '### SVG 编辑维度评测\n编辑结果。\n\n### SVG 生成与风格迁移维度评测\n生成结果。\n\n'
              '### 风格迁移全局排名评测\n排名结果。\n\n## 与已有工作的区别\n比较。\n')
    inventory = [
        {'number': 1, 'title': 'Comparison with existing benchmarks'},
        {'number': 6, 'title': 'Instance-level complexity metrics'},
        {'number': 8, 'title': 'Performance on SVG understanding'},
        {'number': 9, 'title': 'Performance on SVG editing'},
        {'number': 10, 'title': 'Performance on SVG generation'},
        {'number': 11, 'title': 'Performance on SVG generation'},
    ]
    notes = [f'### Table {item["number"]}: {item["title"]}\n\n| Method | Score |\n|---|---|\n| A | {item["number"]}.25 |'
             for item in inventory]
    output = restore_note_tables(report, notes, inventory)
    assert output.index('Table 6:') < output.index('### 公式与目标函数解析')
    assert output.index('### SVG 理解维度评测') < output.index('Table 8:') < output.index('### SVG 编辑维度评测')
    assert output.index('### SVG 编辑维度评测') < output.index('Table 9:') < output.index('### SVG 生成与风格迁移维度评测')
    assert output.index('### SVG 生成与风格迁移维度评测') < output.index('Table 10:') < output.index('### 风格迁移全局排名评测')
    assert output.index('Table 11:') < output.index('### 风格迁移全局排名评测')
    assert output.index('## 与已有工作的区别') < output.index('Table 1:')
    soup = BeautifulSoup(report_document(output, 'Test'), 'html.parser')
    assert 'Table ' not in soup.select_one('.report-toc').get_text()
    assert len(soup.select('table tbody tr')) == 6
    assert restore_note_tables(output, notes, inventory) == output


def test_existing_duplicate_model_row_uses_one_row_and_keeps_both_sources():
    report = ('## 关键结果\n\n### 总体排名\n\n#### Table 12: Overall rank evaluation\n\n'
              '| Models | Winrate (%) | 原文依据 |\n|---|---|---|\n'
              '| DeepSeek-R1 (Reference Model) [12] | 50.55 | [[证据ID:p1-s1]] |\n'
              '| DeepSeek-R1(Reference Model) [12] | 50.55 | [[证据ID:p1-s2]] |\n'
              '| GPT-4o mini | 45.45 | |\n| GPT-4omini | 45.45 | |')
    output = restore_note_tables(report, [], [{'number': 12}])
    assert output.count('50.55') == 1
    assert 'p1-s1' in output and 'p1-s2' in output
    assert 'GPT-4o mini' in output and 'GPT-4omini' in output


def test_restored_footnotes_do_not_keep_chunk_availability_diagnostics():
    note = ('### Table 12: Overall rank evaluation\n\n'
            '| Models | Winrate (%) |\n|---|---|\n| GPT-4o | 45.45 |\n\n'
            '*(注：后续模型 GPT-4o 在 Table 12 中的具体胜率数值在本片段未截获全。)*\n\n'
            '*注：胜率基于独立人工判断，实验运行 5 次。*\n')
    output = restore_note_tables('## 关键结果\n### 总体排名\n排名说明。', [note], [{'number': 12}])
    assert '本片段未截获' not in output
    assert '实验运行 5 次' in output
    assert '45.45' in output


def test_svg_report_extraction_notes_are_removed_after_the_full_rows_are_restored():
    report = ('## 关键结果\n\n#### Table 12\n\n| Model | Score |\n|---|---|\n| A | 45.45 |\n\n'
              '*(注：后续模型在 Table 12 中的具体胜率数值在本片段未截全)* （片段引用不可用，缺少可定位原文依据）。\n\n'
              '*(注：独立文本片段中包含部分物理第 11 页的表格残片，但因所属表格主体不完整，此处提取 Table 10 全貌。)*\n\n'
              '*说明*：总体排名评估胜率（Winrate(%)），片段中包含前 3 名模型的胜率。\n\n'
              '*注：独立实验运行 5 次。*')
    output = restore_note_tables(report, [], [{'number': 12}])
    assert '本片段' not in output and '表格残片' not in output and '前 3 名' not in output
    assert '总体排名评估胜率（Winrate(%)）' in output and '独立实验运行 5 次' in output


def test_large_restored_matrix_is_expandable_inside_its_discussion_with_all_cells():
    report = '## 关键结果\n\n### SVG 理解维度评测\n理解说明。\n\n### SVG 编辑维度评测\n编辑说明。'
    note = '### Table 8: SVG understanding\n\n| Method | Score |\n|---|---|\n' + '\n'.join(
        f'| Model-{i} | {i}.25 |' for i in range(20))
    output = restore_note_tables(report, [note], [{'number': 8, 'title': 'SVG understanding'}])
    soup = BeautifulSoup(report_document(output, 'Test'), 'html.parser')
    details = soup.select_one('details.report-table-details')
    assert details is not None and len(details.select('tbody tr')) == 20
    assert 'Table 8' in details.select_one('summary').get_text()
    assert output.index('</details>') < output.index('### SVG 编辑维度评测')
    assert restore_note_tables(output, [note], [{'number': 8, 'title': 'SVG understanding'}]) == output


def test_report_view_compacts_only_unknown_source_cells_without_touching_scores():
    from arxiv_ra.report_presentation import compact_report
    report = ('#### Table 12\n| Models | Winrate (%) | 原文依据 |\n|---|---|---|\n'
              '| A | 50.55 | 当前材料缺少可定位原文依据 |\n| B | 45.45 | [1](paper.pdf#page=16) |')
    output = compact_report(report)
    assert '| A | 50.55 |  |' in output
    assert '45.45' in output and 'paper.pdf#page=16' in output


def test_image_to_svg_table_uses_its_specific_discussion_and_no_stage_caption_copy():
    report = ('## 关键结果\n\n### SVG 生成评测\n生成说明。\n\n'
              '#### Table 5: Performance on SVG generation (Image-to-SVG).\n\n'
              '| Model | Score |\n|---|---|\n| A | 91.2 |\n\n'
              'Table 5: Performance on SVG generation (Image-to-SVG 部分)\n\n'
              '### 图像到 SVG 生成维度评测（Image-to-SVG）\n图像说明。\n\n### 总体排名\n排名。')
    output = restore_note_tables(report, [], [{'number':5,'title':'Performance on SVG generation'}])
    assert output.index('### 图像到 SVG') < output.index('#### Table 5') < output.index('### 总体排名')
    assert output.count('Table 5:') == 1 and '91.2' in output
    assert restore_note_tables(output, [], [{'number':5,'title':'Performance on SVG generation'}]) == output


def test_extended_caption_keeps_metric_definitions_without_repeating_the_heading():
    report = ('#### Table 3: Editing results for selected models.\n\n'
              '| Model | Score |\n|---|---|\n| A | 91.2 |\n\n'
              'Table 3: Editing results for selected models. Results use ACC and CCR, with units 10^-2.\n\n'
              '*Five independent experimental runs.*')
    output = restore_note_tables(report, [], [{'number':3}])
    assert output.count('Table 3: Editing results for selected models') == 1
    assert 'Results use ACC and CCR, with units 10^-2.' in output
    assert 'Five independent experimental runs.' in output and '91.2' in output
