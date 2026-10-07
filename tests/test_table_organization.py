import json
from arxiv_ra.table_organization import organize_tables
from arxiv_ra.report_tables import ReportTables


def test_model_merges_semantic_duplicates_without_retyping_cells():
    matrix = '| Step | FID |\n|---|---|\n| 0 | 29.76 |'
    notes = [f'### Table 4: Refinement Nmax = 3\n\n{matrix}',
             f'### Table 4: 原文标题，三轮修正\n\n{matrix}']
    def chat(key, system, prompt):
        return json.dumps({'tables': [{'number': 4, 'caption': '迭代修正', 'variants': [
            {'schema': 'e1', 'condition': '', 'note': '从初稿开始迭代。', 'rows': [['e1-r1', 'e2-r1']]}]}]})
    result = organize_tables(notes, [{'number': 4}], None, chat)
    output = result.render('[[表格:table-4]]')
    assert output.count('| 0 | 29.76 |') == 1
    assert '迭代修正' in output and '从初稿开始迭代' in output
    restored = ReportTables.from_dict(result.to_dict())
    assert restored.render('[[表格:table-4]]') == output
    assert len(restored.organization[0]['extractions']) == 2


def test_model_text_is_not_reinterpreted_by_legacy_caption_rules():
    note = '### Table 1\n| A | B |\n|---|---|\n| x | 2 |'
    def chat(*args):
        return json.dumps({'tables': [{'number': 1, 'caption': '质量。成本与 N = 3', 'variants': [
            {'schema': 'e1', 'condition': '', 'note': '单位为秒。数值越低越好。', 'rows': [['e1-r1']]}]}]})
    output = organize_tables([note], [], None, chat).render('[[表格:table-1]]')
    assert '#### Table 1: 质量。成本与 N = 3\n' in output
    assert '**说明：** 单位为秒。数值越低越好。\n' in output


import pytest
from arxiv_ra.task_runtime import TaskCancelled


@pytest.mark.parametrize('response', [RuntimeError('offline'), 'not json',
    '{"tables":[]}', '{"tables":[{"number":1,"caption":"结果","variants":[{"schema":"e1","condition":"","note":"","rows":[["missing"]]}]}]}'])
def test_failed_organization_keeps_readable_data_without_retry(response):
    calls = []
    def chat(*args):
        calls.append(args)
        if isinstance(response, Exception):
            raise response
        return response
    note = '### Table 1: 结果\n| Model | Score |\n|---|---|\n| A | 91.2 |'
    output = organize_tables([note], [], None, chat).render('[[表格:table-1]]')
    assert '| A | 91.2 |' in output
    assert len(calls) == 1
    assert '核查' not in output and '失败' not in output


def test_user_cancellation_is_not_swallowed():
    def chat(*args):
        raise TaskCancelled()
    with pytest.raises(TaskCancelled):
        organize_tables(['### Table 1\n| A | B |\n|---|---|\n| x | 2 |'], [], None, chat)


def test_model_retains_distinct_conditions_with_identical_numbers():
    matrix = '| Model | Score |\n|---|---|\n| A | 91.2 |'
    notes = [f'### Table 1\n\n数据集：{dataset}\n\n{matrix}' for dataset in ('甲', '乙')]
    def chat(*args):
        return json.dumps({'tables': [{'number': 1, 'caption': '两个数据集', 'variants': [
            {'schema': f'e{i}', 'condition': name, 'note': '', 'rows': [[f'e{i}-r1']]}
            for i, name in enumerate(('甲', '乙'), 1)]}]})
    result = organize_tables(notes, [], None, chat)
    output = result.render('[[表格:table-1]]')
    assert output.count('| A | 91.2 |') == 2
    assert '**实验条件：甲**' in output and '**实验条件：乙**' in output


def test_report_generation_uses_organized_tables_without_semantic_rule_import(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from arxiv_ra.config import LLMConfig
    from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
    from arxiv_ra.report import ReportGenerator
    note = '### Table 1\n| Model | Score |\n|---|---|\n| A | 91.2 |'
    plan = {'tables': [{'number': 1, 'caption': '模型表现', 'variants': [
        {'schema': 'e1', 'condition': '', 'note': '分数越高越好。', 'rows': [['e1-r1']]}]}]}
    legacy = Mock(side_effect=AssertionError('Legacy semantic importer must not run'))
    monkeypatch.setattr(ReportTables, 'from_notes', legacy)
    llm = SimpleNamespace(enabled=True, chat=Mock(side_effect=[note, json.dumps(plan),
        '# Paper\n\n## 关键结果\n\n分析。\n\n[[表格:table-1]]']))
    result = ReportGenerator(llm, LLMConfig()).generate(
        Paper.from_dict({'arxiv_id': '2501.12345', 'title': 'Paper'}),
        VerifiedMetadata(), ParsedPaper(note, [note]), [])
    assert '模型表现' in result and '分数越高越好。' in result and '| A | 91.2 |' in result
    assert llm.chat.call_count == 3
    legacy.assert_not_called()


def test_organization_api_failure_is_a_single_outbound_attempt():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from arxiv_ra.config import LLMConfig
    from arxiv_ra.llm import LLMClient
    from arxiv_ra.report import ReportGenerator
    client = Mock()
    client.with_options.return_value = client
    client.chat.completions.create.side_effect = RuntimeError('offline')
    llm = LLMClient.__new__(LLMClient)
    llm.config, llm.client = LLMConfig(), client
    generator = ReportGenerator(llm, llm.config)
    note = '### Table 1\n| A | B |\n|---|---|\n| x | 2 |'
    output = organize_tables([note], [], None, generator._chat).render('[[表格:table-1]]')
    assert '| x | 2 |' in output
    assert client.chat.completions.create.call_count == 1
    client.with_options.assert_called_with(max_retries=0)


def test_source_marker_in_header_never_leaks_internal_mask():
    note = '### Table 1\n| Model | Score [[证据ID:S1-test]] |\n|---|---|\n| A | 91.2 |'
    def chat(key, system, prompt):
        assert 'XXX' not in prompt
        return json.dumps({'tables': [{'number': 1, 'caption': '结果', 'variants': [
            {'schema': 'e1', 'condition': '', 'note': '', 'rows': [['e1-r1']]}]}]})
    result = organize_tables([note], [], None, chat)
    assert result.organization
    output = result.render('[[表格:table-1]]')
    assert 'XXX' not in output
    assert 'Score [[证据ID:S1-test]]' in output
