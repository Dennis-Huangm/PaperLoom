from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arxiv_ra.config import LLMConfig
from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.report import ReportGenerator
from arxiv_ra.report_citations import attach_citations
from arxiv_ra.source_spans import source_spans


@pytest.mark.parametrize('content', [
    '[附录](https://example.org/manual.pdf#page=12)',
    '<a href="https://example.org/manual.pdf#page=12">附录</a>',
    '普通正文 #page=12 和 paper.pdf#page=12。',
    '属性示例 href="paper.pdf#page=12"。',
    '`[示例](paper.pdf#page=12)`',
    '``带 ` 的 [示例](paper.pdf#page=12)``',
    '```markdown\n[示例](paper.pdf#page=12)\n```',
    '~~~~markdown\n<a href="paper.pdf#page=12">示例</a>\n~~~~',
    '````markdown\n```\n[示例](paper.pdf#page=12)\n```\n````',
    '    [示例](paper.pdf#page=12)\n',
])
def test_page_cleanup_preserves_external_links_and_scientific_content(content):
    report, _ = attach_citations(content, ParsedPaper('', []),
                                 pdf_available=False, full_report=False)
    assert report == content


def test_page_cleanup_only_removes_unregistered_source_link_pages():
    report = ('[原文](paper.pdf#page=99)\n'
              '[原文](https://arxiv.org/pdf/2407.05600v1#page=9)\n'
              '<a href="paper.pdf#page=8">page 8</a>')
    cleaned, _ = attach_citations(report, ParsedPaper('', []),
                                  pdf_available=False, full_report=False)
    assert cleaned == report.replace('#page=99', '').replace('#page=9', '').replace('#page=8', '')


def test_linking_never_runs_numeric_audit_or_changes_disagreeing_values(monkeypatch):
    import arxiv_ra.quality as quality
    monkeypatch.setattr(quality, 'audit_report_numbers', Mock(side_effect=AssertionError('retired')))
    source = '| Model | Score |\n|---|---|\n| A | 10.0 |'
    parsed = ParsedPaper(source, [source])
    key = next(iter(source_spans(parsed)))
    raw = f'| Model | Score | Source |\n|---|---|---|\n| A | 99.9 | [[证据ID:{key}]] |'
    report, evidence = attach_citations(raw, parsed, pdf_available=True, full_report=True)
    assert '| A | 99.9 | [1](paper.pdf#page=1) |' in report
    assert evidence['scope'] == 'source_location_only'
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence
    quality.audit_report_numbers.assert_not_called()


def test_broken_locator_delivers_original_content_without_internal_tokens(monkeypatch):
    import arxiv_ra.report_citations as module
    monkeypatch.setattr(module, 'source_spans', Mock(side_effect=RuntimeError('locator unavailable')))
    raw = '# Paper\n\nClaim 99.9 [[证据ID:unknown]]\n\n| A | 10 |'
    report, evidence = attach_citations(raw, ParsedPaper('text', ['text']), pdf_available=True, full_report=True)
    assert report == '# Paper\n\nClaim 99.9 \n\n| A | 10 |'
    assert evidence['status'] == 'unavailable'


def test_single_bad_reference_does_not_lose_good_references(monkeypatch):
    import arxiv_ra.report_citations as module
    parsed = ParsedPaper('', ['A sufficiently long source excerpt for a usable citation.'])
    key = next(iter(source_spans(parsed)))
    monkeypatch.setattr(module, 'exact_span', Mock(side_effect=ValueError('malformed ID')))
    report, data = attach_citations(f'A [[证据ID:{key}]] B [[证据ID:bad]]', parsed,
                                     pdf_available=True, full_report=True)
    assert report == 'A [1](paper.pdf#page=1) B '
    assert len(data['citations']) == 1


@pytest.mark.parametrize('failure', ['none', 'source_bank', 'table_import', 'table_render'])
def test_generation_only_calls_model_for_existing_chunks_and_synthesis(monkeypatch, failure):
    import arxiv_ra.report as module
    from arxiv_ra.report_tables import ReportTables
    retired = Mock(side_effect=AssertionError('No annotation review or retries'))
    monkeypatch.setattr(ReportTables, 'localize_annotations', retired)
    monkeypatch.setattr(ReportTables, 'consolidate_annotations', retired)
    if failure == 'source_bank':
        monkeypatch.setattr(module, 'source_spans', Mock(side_effect=ValueError('broken bank')))
    if failure == 'table_import':
        monkeypatch.setattr(ReportTables, 'from_notes', Mock(side_effect=ValueError('broken import')))
    if failure == 'table_render':
        monkeypatch.setattr(ReportTables, 'render', Mock(side_effect=ValueError('broken presentation')))
    note = '### Table 1: 中文结果\n| Model | Score |\n|---|---|\n| A | 91.2 |'
    draft = '# Paper\n\n## 关键结果\n\n分析结论 91.2。\n\n[[表格:table-1]]'
    if failure == 'table_import':
        draft = draft.replace('[[表格:table-1]]', note)
    llm = SimpleNamespace(enabled=True, chat=Mock(side_effect=[note, draft]))
    generator = ReportGenerator(llm, LLMConfig())
    result = generator.generate(Paper.from_dict({'arxiv_id': '2501.12345', 'title': 'Paper'}),
                                VerifiedMetadata(), ParsedPaper(note, [note]), [])
    assert '分析结论 91.2' in result and '| A | 91.2 |' in result
    assert llm.chat.call_count == 2
    retired.assert_not_called()


def test_source_pages_do_not_add_model_calls_to_the_analysis_plan():
    generator = ReportGenerator(SimpleNamespace(enabled=True), LLMConfig())
    parsed = ParsedPaper('A short complete parsed body.', ['Source context ' * 1000] * 20)
    chunks, banks, _ = generator.analysis_inputs(parsed)
    assert len(chunks) == len(banks) == 1
