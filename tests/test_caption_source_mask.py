import pytest

from arxiv_ra.report_tables import ReportTables
from arxiv_ra.report_completeness import _headings


@pytest.mark.parametrize('citation', ['[[证据ID:S1-example-28-766-1266]]',
                                    '[[证据:Original caption with\n## Quoted heading and | cells]]'])
def test_caption_import_never_publishes_internal_source_mask(citation):
    caption = 'Table 14: Turn-3 instruction following'
    note = f'### {caption} {citation}\n\n| Model | Score |\n|---|---|\n| A | 35.35 |'
    catalogue = ReportTables.from_notes([note], [{'number': 14}])
    output = catalogue.render('## 关键结果\n\n[[表格:table-14]]')
    assert 'XXXX' not in output
    assert citation in output
    assert '| A | 35.35 |' in output
    assert len(_headings(note)) == 1


def test_caption_reference_does_not_make_repeated_caption_a_new_note():
    caption = 'Table 14: Turn-3 instruction following'
    note = (f'### {caption} [[证据ID:S1-example-28-766-1266]]\n\n'
            f'{caption}\n\n*说明：得分单位为百分比。*\n\n'
            '| Model | Score |\n|---|---|\n| A | 35.35 |')
    output = ReportTables.from_notes([note], [{'number': 14}]).render('[[表格:table-14]]')
    assert output.count('Turn-3 instruction following') == 1
    assert '得分单位为百分比' in output


def test_literal_x_name_is_preserved():
    note = '### Table 1: XXXXX benchmark\n\n| Model | Score |\n|---|---|\n| X | 1 |'
    output = ReportTables.from_notes([note], [{'number': 1}]).render('[[表格:table-1]]')
    assert 'XXXXX benchmark' in output
