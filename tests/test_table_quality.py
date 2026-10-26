"""Row matching retained for legacy repair; normal publication never audits values."""
import pytest

from arxiv_ra.evidence import TOKEN
from arxiv_ra.table_quality import check_row, tables


SOURCE = "| Model | Accuracy (%) | F1 (%) |\n|---|---|---|\n| Alpha | 91.2 | 88.4 |\n| Beta | 88.4 | 91.2 |\n"
PDF = "Table 3: Editing results\nMethod\nACC\nrMSE\nRLD\nMSE\nEasy\nAlpha [12]†\n62.63\n75.16\n1.45\n5.63\nMedium\nAlpha [12]†\n46.00\n53.11\n140.68\n8.14\n"


def binding(source, headers, values):
    text = '| ' + ' | '.join([*headers, '原文依据']) + ' |\n'
    text += '|' + '---|' * (len(headers) + 1) + '\n'
    text += '| ' + ' | '.join([*values, '[[证据ID:source]]']) + ' |'
    row = next(tables(text, TOKEN))
    return check_row(row, [{'quote': source, 'page': 1, 'start': 0, 'end': len(source)}], [source])


@pytest.mark.parametrize('headers, values, status, bad', [
    (['Model', 'F1 (%)', 'Accuracy (%)'], ['Alpha', '88.4', '91.2'], 'headers_checked', []),
    (['Model', 'Accuracy (%)', 'F1 (%)'], ['Alpha', '88.4', '91.2'], 'mismatch', [1, 2]),
    (['Model', 'Accuracy (%)', 'F1 (%)'], ['Alpha', '91.2%', '88.4'], 'headers_checked', []),
    (['Model', 'Accuracy (%)', 'F1 (%)'], ['Alpha', '0.912', '88.4'], 'mismatch', [1]),
])
def test_legacy_row_matching_binds_columns_without_converting_values(headers, values, status, bad):
    result = binding(SOURCE, headers, values)
    assert result['status'] == status
    assert result['bad_columns'] == bad


@pytest.mark.parametrize('header', ['Accuracy (ms)', 'Accuracy (10^-2)', 'Accuracy'])
def test_header_units_and_scaling_cannot_be_silently_changed(header):
    result = binding(SOURCE, ['Model', header, 'F1 (%)'], ['Alpha', '91.2', '88.4'])
    assert result['status'] == 'mismatch'
    assert result['bad_columns'] == [1]


def test_header_unit_spacing_and_arrow_placement_are_formatting_only():
    result = binding(SOURCE.replace('Accuracy (%)', 'Accuracy ↑ (%)'),
                     ['Model', 'Accuracy (% ) ↑', 'F1 (%)'], ['Alpha', '91.2', '88.4'])
    assert result['status'] == 'headers_checked'


@pytest.mark.parametrize('arrow', [r'\(\uparrow\)', r'$\uparrow$'])
def test_latex_header_arrows_match_source_directions(arrow):
    source = 'Model\nCLIP (↑)\nMSE (↓)\nAlpha\n0.9634\n10488\n'
    result = binding(source, ['Model', f'CLIP ({arrow})', r'MSE (\(\downarrow\))'],
                     ['Alpha', '0.9634', '10488'])
    assert result['status'] == 'headers_checked'
    result = binding(source, ['Model', r'CLIP (\(\downarrow\))', r'MSE (\(\downarrow\))'],
                     ['Alpha', '0.9634', '10488'])
    assert result['status'] == 'mismatch' and result['bad_columns'] == [1]


def test_explicit_dataset_and_split_conditions_do_not_cross_bind():
    source = '| Model | Dataset | Split | Accuracy |\n|---|---|---|---|\n| Alpha | A | test | 91.2 |'
    result = binding(source, ['Model', 'Dataset', 'Split', 'Accuracy'], ['Alpha', 'B', 'test', '91.2'])
    assert result['status'] == 'mismatch'
    assert result['reason'] == 'row_context_mismatch'


def test_pdf_row_projection_preserves_order_and_reports_weaker_check():
    result = binding(PDF, ['Difficulty', 'Model', 'ACC', 'MSE'], ['Easy', 'Alpha', '62.63', '5.63'])
    assert result['status'] == 'row_order_checked'
    assert result['reason'] == 'headers_not_assessed'
    result = binding(PDF, ['Difficulty', 'Model', 'ACC', 'MSE'], ['Easy', 'Alpha', '5.63', '62.63'])
    assert result['status'] == 'mismatch' and result['bad_columns'] == [2, 3]


def test_flat_pdf_headers_bind_metrics_and_allow_projection():
    source = 'Model\nAccuracy (%)\nF1 (%)\nAlpha\n91.2\n88.4\nBeta\n88.4\n91.2\n'
    result = binding(source, ['Model', 'F1 (%)', 'Accuracy (%)'], ['Beta', '91.2%', '88.4'])
    assert result['status'] == 'headers_checked'
    result = binding(source, ['Model', 'Accuracy (%)', 'F1 (%)'], ['Alpha', '88.4', '91.2'])
    assert result['status'] == 'mismatch' and result['bad_columns'] == [1, 2]


@pytest.mark.parametrize('source, headers, values', [
    (PDF, ['Model', 'ACC', 'MSE'], ['Alpha', '62.63', '5.63']),
    ('Table 1: Results\nAlpha\n1.22\n33.90\n1656.5010.20\n34.87\n',
     ['Model', 'ACC', 'MSE', 'CCR'], ['Alpha', '1.22', '10.20', '34.87']),
    ('Table 3: Results\nModel\nLatency\nScore\n∗Seedream 4.0 5\n✗\n25.09.10\n14.55\n75.93\n',
     ['Model', 'Latency', 'Score'], ['∗Seedream 4.0', '14.55', '75.93']),
    ('Table 3: Results\nAlpha\n5\nunsupported categorical cell\n14.55\n75.93\n',
     ['Model', 'Latency', 'Score'], ['Alpha', '14.55', '75.93']),
])
def test_incomplete_or_damaged_source_rows_remain_unassessed(source, headers, values):
    result = binding(source, headers, values)
    assert result['status'] == 'unassessed'
    assert result['bad_columns'] == []


def test_plain_numeric_prefix_cannot_bind_mixed_categorical_columns():
    source = 'Table 3: Results\nModel\nRank\nIn-Context\nScore\nAlpha 5\nYes\n75.93\n'
    result = binding(source, ['Model', 'In-Context', 'Score'], ['Alpha', 'Yes', '75.93'])
    assert result['status'] == 'unassessed'
    assert result['reason'] == 'unsupported_mixed_type_row'
    source = '| Model | In-Context | Score |\n|---|---|---|\n| Alpha | Yes | 75.93 |'
    result = binding(source, ['Model', 'In-Context', 'Score'], ['Alpha', 'Yes', '99.9'])
    assert result['status'] == 'mismatch' and result['bad_columns'] == [2]
