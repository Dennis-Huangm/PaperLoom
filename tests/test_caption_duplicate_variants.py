from arxiv_ra.report_tables import ReportTables


def test_caption_wrappers_do_not_create_experiment_variants():
    title = 'Table 4. Quantitative refinement up to Nmax = 3.'
    matrix = '| Iteration | FID |\n|---|---|\n| 0 | 29.76 |\n| 3 | 26.18 |'
    notes = [f'### {title}\n\n表注说明：迭代指标。\n\n{matrix}',
             f'### 原文标题：{title} [[证据ID:S1-test]]\n\n{matrix}']
    catalogue = ReportTables.from_notes(notes, [{'number': 4}])
    output = catalogue.render('[[表格:table-4]]')
    assert output.count('| 0 | 29.76 |') == 1
    assert '实验条件：' not in output
    assert '原文标题：' not in output
    assert '说明：** 表注说明：' not in output


def test_different_experiment_conditions_are_not_merged():
    matrix = '| Model | Score |\n|---|---|\n| A | 1 |'
    notes = [f'### Table 4: Evaluation (seed={seed})\n\n{matrix}' for seed in (1, 2)]
    catalogue = ReportTables.from_notes(notes, [{'number': 4}])
    output = catalogue.render('[[表格:table-4]]')
    assert output.count('| A | 1 |') == 2
    assert 'seed=1' in output and 'seed=2' in output


def test_duplicate_caption_keeps_its_source_locator():
    title = 'Table 4. Refinement up to Nmax = 3.'
    note = f'### {title}\n\n原文标题：{title} [[证据ID:S1-test]]\n\n| Step | Score |\n|---|---|\n| 0 | 1 |'
    output = ReportTables.from_notes([note], [{'number': 4}]).render('[[表格:table-4]]')
    assert output.count('Refinement up to Nmax = 3') == 1
    assert '[[证据ID:S1-test]]' in output
