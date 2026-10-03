"""Conflicting extractions must not become additional experimental rows."""
from arxiv_ra.report_tables import ReportTables
import pytest


GOOD = '''### Table 3: Architecture variations

| Model | N | dff | PPL | BLEU | 原文依据 |
|---|---|---|---|---|---|
| base | 6 | 1024 | 4.92 | 25.8 | [[证据ID:p9-good]] |
| small | 2 | 512 | 6.11 | 23.7 | [[证据ID:p9-good]] |
| big | 6 | 4096 | 4.33 | 26.4 | [[证据ID:p9-good]] |
'''
BAD = GOOD.replace('1024', '2048').replace('p9-good', 'p9-bad')
PAGE = '''Table 3: Architecture variations
Model N dff PPL BLEU
base
6
1024
4.92
25.8
small 2 512 6.11 23.7
big 6 4096 4.33 26.4
Table 4: Other results
base 6 2048 4.92 25.8
'''


def test_conflicting_chunk_rows_resolve_against_complete_source_matrix():
    catalogue = ReportTables.from_notes([BAD, GOOD], [{'number': 3, 'page': 1}], source_pages=[PAGE])
    rows = catalogue.tables[0].variants[0].rows
    assert len(rows) == 3
    assert rows[0].cells[2] == '1024'
    output = catalogue.render('## Results\n\n[[表格:table-3]]')
    assert output.count('| base |') == 1
    assert 'p9-good' in output and 'p9-bad' in output
    assert catalogue.review(output)['status'] == 'passed'
    restored = ReportTables.from_dict(catalogue.to_dict())
    assert restored.source_resolutions == catalogue.source_resolutions
    assert '2048' in restored.source_resolutions[0]['edits'][0]['before']
    assert restored.render('## Results\n\n[[表格:table-3]]') == output


@pytest.mark.parametrize('notes', [[GOOD, BAD], [BAD, BAD, GOOD], [GOOD, BAD, GOOD]])
def test_source_evidence_beats_order_or_majority(notes):
    catalogue = ReportTables.from_notes(notes, [{'number': 3}], source_pages=[PAGE])
    assert len(catalogue.tables[0].variants[0].rows) == 3
    assert catalogue.tables[0].variants[0].rows[0].cells[2] == '1024'


@pytest.mark.parametrize('pages', [[], [PAGE.replace('1024', '9999')],
    [PAGE.replace('small 2 512 6.11 23.7', 'intervening text')],
    [PAGE + '\n' + PAGE],
    [PAGE.replace('Table 4:', 'base 6 2048 4.92 25.8 small 2 512 6.11 23.7 big 6 4096 4.33 26.4\nTable 4:')]])
def test_ambiguous_or_incomplete_source_never_selects_a_value(pages):
    catalogue = ReportTables.from_notes([BAD, GOOD], [{'number': 3}], source_pages=pages)
    assert not catalogue.source_resolutions
    output = catalogue.render('## Results')
    assert '2048' in output and '1024' in output


@pytest.mark.parametrize('different', [GOOD.replace('Table 3:', 'Table 3 (test):'),
    GOOD.replace('dff', 'training steps'), GOOD.replace('| base |', '| other |'),
    GOOD.replace('| 1024 |', '| |')])
def test_distinct_axes_labels_conditions_or_blanks_are_not_corrected(different):
    catalogue = ReportTables.from_notes([BAD, different], [{'number': 3}], source_pages=[PAGE])
    assert not catalogue.source_resolutions
    assert '2048' in catalogue.render('## Results')


def test_rejected_transcription_claim_does_not_poison_synthesis_or_erase_notes():
    stale = '*注：原表 base 的 dff 在 PDF 中为 2048（Markdown 识别片段误作 1024）；空白单元格沿用 base。*'
    scientific = '*识别任务分别使用 2048 和 1024 个输入特征。*'
    notes = [BAD + '\n' + stale + '\n\n' + scientific, GOOD]
    catalogue = ReportTables.from_notes(notes, [{'number': 3}], source_pages=[PAGE])
    for output in [catalogue.render('## Results'), '\n'.join(catalogue.synthesis_notes(notes))]:
        assert '误作' not in output
        assert '空白单元格沿用 base' in output
        assert scientific.strip('*') in output
    assert stale in str(catalogue.source_resolutions)


@pytest.mark.parametrize('placement', ['before', 'after'])
@pytest.mark.parametrize('conflicting', [False, True])
def test_explicit_note_conditions_keep_independent_variants(placement, conflicting):
    def condition(note, split):
        text = f'Training split: {split}.\n\n'
        return note.replace('\n\n', '\n\n' + text, 1) if placement == 'before' else note + '\n' + text
    notes = [condition(GOOD, 'test'), condition(BAD if conflicting else GOOD, 'dev')]
    catalogue = ReportTables.from_notes(notes, [{'number': 3}], source_pages=[PAGE])
    assert not catalogue.source_resolutions
    variants = catalogue.tables[0].variants
    assert len(variants) == 2
    assert all(len(v.rows) == 3 for v in variants)
    assert len({v.condition for v in variants}) == 2
    output = catalogue.render('## Results')
    assert output.count('| base |') == 2
    assert 'Training split: test.' in output and 'Training split: dev.' in output
    if conflicting:
        assert '2048' in output and '1024' in output
    assert catalogue.review(output)['status'] == 'passed'


def test_conditions_between_sibling_matrices_belong_to_the_following_matrix():
    note = (GOOD.replace('\n\n', '\n\nTraining split: test.\n\n', 1)
            + '\nTraining split: dev.\n\n' + BAD.split('\n\n', 1)[1])
    catalogue = ReportTables.from_notes([note], [{'number': 3}], source_pages=[PAGE])
    assert not catalogue.source_resolutions
    variants = {variant.condition: variant for variant in catalogue.tables[0].variants}
    assert set(variants) == {'Training split: test.', 'Training split: dev.'}
    assert variants['Training split: test.'].rows[0].cells[2] == '1024'
    assert variants['Training split: dev.'].rows[0].cells[2] == '2048'
