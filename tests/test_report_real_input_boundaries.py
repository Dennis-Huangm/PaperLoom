from arxiv_ra.evidence import source_table_inventory
from arxiv_ra.models import ParsedPaper
from arxiv_ra.source_spans import cited_span_material
from arxiv_ra.report_tables import ReportTables


def test_period_caption_inventory_ignores_inline_references():
    page = 'See Table 5. We compare methods below.\nTable 5. Error rates of ensembles\nTable 6: CIFAR results\n'
    inventory = source_table_inventory(ParsedPaper(page, [page]))
    assert [(i['number'], i['title']) for i in inventory] == [(5, 'Error rates of ensembles'), (6, 'CIFAR results')]


def test_malformed_reference_with_nested_brackets_and_pipes_cannot_erase_table():
    note = ('### Table 5: Error rates\n\n| method | error | 原文依据 |\n|---|---|---|\n'
            '| VGG [41] | 7.32 | [[证据ID:逐字复制:VGG [41] | 7.32]] |\n'
            '| ResNet | 3.57 | [[证据ID:unknown]] |')
    cleaned, _ = cited_span_material([note], {})
    catalogue = ReportTables.from_notes(cleaned, [])
    assert len(catalogue.tables) == 1
    rows = catalogue.tables[0].variants[0].rows
    assert [r.cells[:2] for r in rows] == [('VGG [41]', '7.32'), ('ResNet', '3.57')]
    assert '[[证据ID:' not in cleaned[0]


def test_unclosed_reference_cannot_swallow_prose_before_the_next_reference():
    from arxiv_ra.evidence import TOKEN
    text = 'Score 91.2 [[证据ID:broken]；temperature=0.8 and gain=42 [[证据ID:unknown]]'
    cleaned, _ = cited_span_material([text], {})
    assert 'temperature=0.8 and gain=42' in cleaned[0]
    assert 'temperature=0.8 and gain=42' in TOKEN.sub('', text)


def test_explicit_wide_and_long_dataset_axes_have_one_lossless_matrix():
    wide = ('### Table 9: Results\n| training data | Train A | Train A | Train B | Train B |\n'
            '|---|---|---|---|---|\n| test data | Val | Val | Test | Test |\n'
            '| mAP | @.5 | @.75 | @.5 | @.75 |\n| Model (X) | 41.5 | 21.2 | 53.3 | 32.2 |')
    long = ('### Table 9: Results\n| training data | test data | mAP @.5 | mAP @.75 |\n'
            '|---|---|---|---|\n| Train A | Val (Model, X) | 41.5 | 21.2 |\n'
            '| **Train B** | **Test** | | |\n| Model (X) | Test | 53.3 | 32.2 |')
    result = ReportTables.from_notes([wide, long], [])
    assert len(result.tables[0].variants) == 1
    assert '53.3' in result.render('## Results')
    for changed in (long.replace('32.2', '32.3'), long.replace('Train B', 'Train C'),
                    long.replace('mAP @.75', 'mAP @.8'), long.replace('| 53.3 | 32.2 |', '| | |')):
        assert len(ReportTables.from_notes([wide, changed], []).tables[0].variants) == 2


def test_model_scheme_header_alias_does_not_duplicate_identical_detection_results():
    note = '### Table 12: Results\n| 模型 / 方案 | val2 | test |\n|---|---|---|\n| A | 60.5 | 58.8 |'
    other = note.replace('模型 / 方案', '方法/模型')
    result = ReportTables.from_notes([note, other], [])
    assert len(result.tables[0].variants) == 1
    assert result.render('## Results').count('60.5') == 1
    assert len(ReportTables.from_notes([note, other.replace('58.8', '58.9')], []).tables[0].variants[0].rows) == 2


def test_layout_reconciliation_keeps_group_references_and_distinct_categories():
    from dataclasses import replace
    from arxiv_ra.report_tables import TableVariant, TableRow
    from arxiv_ra.table_layouts import reconcile_layouts
    wide = TableVariant('w', '', ('training data', 'Train A', 'Train A'), (
        TableRow('test', ('test data', 'Val', 'Val'), ''),
        TableRow('metric', ('mAP', '@.5', '@.75'), ''),
        TableRow('x', ('Model X', '41.5', '21.2'), ''),), ('wide note',))
    long = TableVariant('l', '', ('training data', 'test data', 'mAP @.5', 'mAP @.75', '原文依据'), (
        TableRow('group', ('Train A', 'Val', '', '', '[[证据ID:group-source]]'), ''),
        TableRow('x', ('Model X', 'Val', '41.5', '21.2', '[[证据ID:model-source]]'), ''),), ('long note',))
    for variants in ((wide, long), (long, wide)):
        result = reconcile_layouts(variants)
        assert len(result) == 1
        assert 'group-source' in str(result[0].rows)
        assert 'model-source' in str(result[0].rows)
        assert set(result[0].context) == {'wide note', 'long note'}
    categorized = replace(long, rows=(long.rows[0], replace(long.rows[1], category='independent-condition')))
    assert len(reconcile_layouts((wide, categorized))) == 2
    assert len(reconcile_layouts((wide, replace(long, condition='seed=42')))) == 2
