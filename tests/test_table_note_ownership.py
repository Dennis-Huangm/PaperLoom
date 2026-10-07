from arxiv_ra.report_tables import ReportTables


def test_trailing_note_stays_with_its_table_when_restoration_nests_headings():
    note = ('### Table 2: Overall results\n\n'
            '| Model | Binary Diagnosis Acc. |\n|---|---|\n| A | 89.40 |\n\n'
            'Note: Binary Diagnosis Acc. describes Part I.\n\n'
            '### Table 3: Dimension results\n\n'
            '| Model | MAE |\n|---|---|\n| A | 0.9060 |\n\n'
            'Note: Model names retain source spelling.\n\n'
            '### Table 4: Example outputs\n\n'
            '| Example | Score |\n|---|---|\n| icon | 1/5 |')
    catalogue = ReportTables.from_notes([note], [])
    contexts = {t.number: str(t.variants[0].context) for t in catalogue.tables}
    assert 'Binary Diagnosis' in contexts[2]
    assert 'Binary Diagnosis' not in contexts[3] + contexts[4]
    assert 'source spelling' in contexts[3]
    assert 'source spelling' not in contexts[2] + contexts[4]
