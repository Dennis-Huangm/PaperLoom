from dataclasses import replace
import json

from arxiv_ra.report_tables import ReportTables
from arxiv_ra.table_editorial import validate_editorial


def sample():
    return ReportTables.from_notes(['### Table 3: Per-dimension MAE\n\nScores use a 1–5 scale.\n\n'
                                   '| Model | MAE |\n|---|---|\n| A | 0.9060 |'], [])


def proposal():
    return {'entries': [{'scope': 'title', 'text': '各维度的平均绝对误差（MAE）', 'sources': ['title']},
                        {'scope': 'v0', 'text': '评分采用 1–5 分量表。', 'sources': ['u1']}], 'omitted': []}


def test_chinese_editorial_preserves_raw_sources_cells_and_survives_serialization():
    catalogue = sample()
    calls = []
    def chat(stage, system, prompt):
        calls.append(stage)
        return json.dumps({'approved': True} if stage.endswith('review') else proposal())
    result = catalogue.localize_annotations(chat)
    assert result.tables == catalogue.tables
    output = result.render('## 关键结果')
    assert 'Per-dimension' not in output
    assert '评分采用 1–5 分量表。' in output
    assert '| A | 0.9060 |' in output
    assert result.review(output)['status'] == 'passed'
    assert result.review(output.replace('1–5 分', '1–7 分'))['status'] == 'failed'
    assert ReportTables.from_dict(result.to_dict()).render('## 关键结果') == output
    assert len(calls) == 2
    assert result.localize_annotations(chat) == result
    assert len(calls) == 2


def test_editorial_rejects_missing_sources_changed_numbers_and_html():
    t = sample().tables[0]
    assert validate_editorial(t, proposal())
    for value in ['评分采用 1–7 分量表。', '<script>评分采用 1–5 分量表。</script>']:
        p = proposal(); p['entries'][1]['text'] = value
        assert not validate_editorial(t, p)
    p = proposal(); p['entries'].pop(); p['omitted'] = ['u1']
    assert not validate_editorial(t, p)


def test_semantic_rejection_keeps_original_scientific_notes():
    result = sample().localize_annotations(lambda stage, *_: json.dumps(
        {'approved': False} if stage.endswith('review') else proposal()))
    assert not result.editorials
    assert 'Scores use a 1–5 scale.' in result.render('## Results')
    assert result.editorial_status[0]['status'] == 'review_rejected'


def test_wrong_direction_and_cross_variant_merges_are_rejected():
    from arxiv_ra.table_editorial import editorial_units
    from arxiv_ra.table_annotations import annotation_units
    table = sample().tables[0]
    v = table.variants[0]
    table = replace(table, variants=(replace(v, context=('MAE ↓ at 5 runs.',)),
                                      replace(v, id='other', context=('MAE ↑ at 7 runs.',))))
    units = editorial_units(table)
    p = {'entries': [
        {'scope':'title','text':'各维度 MAE','sources':['title']},
        {'scope':'v0','text':'MAE 越低越好，采用 5 次运行。','sources':['u1']},
        {'scope':'v1','text':'MAE 越高越好，采用 7 次运行。','sources':['u2']}], 'omitted':[]}
    assert validate_editorial(table,p)
    p['entries'][1]['text'] = 'MAE 越高越好，采用 5 次运行。'
    assert not validate_editorial(table,p)
    p['entries'][1] = {'scope':'v0','text':'MAE ↑ ↓，采用 5 和 7 次运行。','sources':['u1','u2']}
    p['entries'].pop()
    assert not validate_editorial(table,p)


def test_editorial_service_failure_and_cancellation():
    from arxiv_ra.report_checkpoint import CheckpointWriteError
    import pytest
    def unavailable(*args): raise RuntimeError('offline')
    assert not sample().localize_annotations(unavailable).editorials
    def cancelled(*args): raise CheckpointWriteError('cannot save')
    with pytest.raises(CheckpointWriteError):
        sample().localize_annotations(cancelled)


def test_transcription_notes_are_auditable_but_not_shown_as_scientific_notes():
    from arxiv_ra.table_editorial import editorial_units
    table = sample().tables[0]
    table = replace(table, variants=(replace(table.variants[0], context=(
        '*注：Table 3 模型行中的命名与 Table 2 保持一致，仅 `Model-X` 在 Table 3 原文中多带连字符。*',
        '在此以文字标记说明；', 'Only held-out samples are evaluated.',)),))
    units = editorial_units(table)
    assert [u['omittable'] for u in units] == [False, True, True, True, False]
    assert 'Model-X' in str(table.variants[0].context)


def test_roman_task_numbers_and_plural_acronyms_allow_natural_translation():
    from arxiv_ra.table_editorial import _terms, editorial_units
    assert _terms('Part II SVGs') == _terms('任务 2 SVG')
    table = replace(sample().tables[0], caption='Table 3: Part II SVGs')
    assert editorial_units(table)[0]['protected'] == ['2', 'svg']


def test_equivalent_editorial_terms_preserve_versions_and_code_literals():
    from arxiv_ra.table_editorial import _terms
    assert _terms('Turn-3 DINOv2 DINOv3-based L1-based') == _terms('第 3 轮 DINO-v2 DINOv3 L1')
    assert _terms('DINOv2') != _terms('DINOv3')
    assert _terms('`Turn-3`') != _terms('第 3 轮')
    assert _terms('PDF 原表中包含 Technique 分组列') == _terms('原表中包含 Technique 分组列')
    assert 'pdf' in _terms('PDF 输入文件格式')


def test_invalid_editorial_records_reason_and_repairs_with_feedback():
    calls = []
    def chat(stage, system, prompt):
        calls.append((stage, prompt))
        if stage.endswith('review'):
            return json.dumps({'approved': True})
        p = proposal()
        if len(calls) == 1:
            p['entries'][1]['text'] = '评分采用 1–7 分量表。'
        return json.dumps(p)
    result = sample().localize_annotations(chat)
    assert result.editorial_status[0]['status'] == 'localized'
    assert len(calls) == 3
    assert '5' in calls[1][1] and '7' in calls[1][1]
    attempts = result.editorial_status[0]['attempts']
    assert attempts[0]['status'] == 'invalid' and attempts[0]['errors']
    assert attempts[0]['draft']['entries'][1]['text'] == '评分采用 1–7 分量表。'


def test_failed_editorial_retries_are_bounded_and_diagnostic():
    p = proposal(); p['entries'][1]['text'] = '评分采用 1–7 分量表。'
    calls = []
    def chat(stage, *_):
        calls.append(stage)
        return json.dumps(p)
    result = sample().localize_annotations(chat)
    assert len(calls) == 2
    status = result.editorial_status[0]
    assert status['status'] == 'invalid' and status['detail']
    assert len(status['attempts']) == 2
    assert not result.editorials
    calls.clear()
    def repaired(stage, _, prompt):
        calls.append(stage)
        if not stage.endswith('review'):
            assert '上次稿件未通过' in prompt and '1–7' in prompt
        return json.dumps({'approved': True} if stage.endswith('review') else proposal())
    resumed = ReportTables.from_dict(result.to_dict()).localize_annotations(repaired)
    assert len(calls) == 2 and resumed.editorial_status[0]['status'] == 'localized'
    assert len(resumed.editorial_status[0]['attempts']) == 3


def test_semantic_feedback_is_retried_but_never_bypassed():
    calls = []
    def chat(stage, _, prompt):
        calls.append((stage, prompt))
        return json.dumps({'approved': False, 'reason': '遗漏单位'} if stage.endswith('review') else proposal())
    result = sample().localize_annotations(chat)
    assert len(calls) == 4 and '遗漏单位' in calls[2][1]
    assert result.editorial_status[0]['status'] == 'review_rejected'
    assert len(result.editorial_status[0]['attempts']) == 2
    assert not result.editorials


def test_malformed_review_is_recorded_and_does_not_skip_review():
    def chat(stage, *_):
        return 'not JSON' if stage.endswith('review') else json.dumps(proposal())
    result = sample().localize_annotations(chat)
    assert result.editorial_status[0]['status'] == 'review_rejected'
    assert all(a['errors'] for a in result.editorial_status[0]['attempts'])
    assert not result.editorials


def test_source_partition_errors_are_specific_and_models_use_model_column():
    from arxiv_ra.table_editorial import editorial_errors
    table = sample().tables[0]
    p = proposal(); p['entries'].pop()
    assert any('u1' in error for error in editorial_errors(table, p))
    table = replace(table, variants=(replace(table.variants[0],
        headers=('Technique', 'Model', 'MAE'), column_roles=(),
        rows=(replace(table.variants[0].rows[0], cells=('Unknown', 'Alpha Model', '0.9')),),
        context=('Alpha Model uses a 1–5 scale.',)),))
    assert any('模型名称' in error for error in editorial_errors(table, proposal()))


def test_publication_rejects_reintroduced_english_annotation():
    c = sample().localize_annotations(lambda stage, *_: json.dumps(
        {'approved':True} if stage.endswith('review') else proposal()))
    report = c.render('## 关键结果')
    assert c.review(report.replace('评分采用 1–5 分量表。',
        '评分采用 1–5 分量表。 Scores use a 1–5 scale.'))['status'] == 'failed'


def test_real_svgeval_source_notes_have_chinese_display_and_correct_ownership():
    from pathlib import Path
    from arxiv_ra.table_editorial import display_editorial
    stored = json.loads((Path(__file__).parent/'fixtures/table-editorial-svgeval.json').read_text('utf-8'))
    catalogue = ReportTables.from_dict(stored)
    report = catalogue.render('## 关键结果')
    assert catalogue.review(report)['status'] == 'passed'
    assert '连字符' not in report
    assert 'Representative rendered' not in report
    assert '在此以文字标记' not in report
    titles = []
    for table, editorial in zip(catalogue.tables, catalogue.editorials):
        caption, notes = display_editorial(table, editorial)
        titles.append(caption)
        if table.number == 4:
            assert '二元诊断' not in str(notes)
        assert '原文' in str(table.variants[0].context) or table.number == 2
    assert all(any('\u4e00' <= ch <= '\u9fff' for ch in title) for title in titles)
