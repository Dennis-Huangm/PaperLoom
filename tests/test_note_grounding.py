"""Ground legacy chunk quotations before the final model rewrites a report."""
import json
from types import SimpleNamespace

import pytest

from arxiv_ra.config import LLMConfig
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper, Paper, VerifiedMetadata
from arxiv_ra.report import ReportGenerator
from arxiv_ra.source_spans import (ID_TOKEN, source_spans, ground_note_quotes,
                                   exact_span, cited_span_material)


QUOTE = 'Model A test accuracy is 91.2% on the held-out benchmark.'


def test_exact_quote_preserves_offsets_and_does_not_borrow_adjacent_numbers():
    page = 'Other model scored 99.8%.\n' + QUOTE + '\nMore unrelated results 85.0%.'
    parsed = ParsedPaper(page, [page])
    notes, bank, stats = ground_note_quotes([f'A: 91.2% [[证据:{QUOTE}]]'], parsed, source_spans(parsed))
    key = ID_TOKEN.search(notes[0])[1]
    assert stats == {'quotes_seen': 1, 'quotes_grounded': 1, 'quotes_unresolved': 0}
    assert bank[key]['quote'] == QUOTE
    assert page[bank[key]['start']:bank[key]['end']] == QUOTE
    assert exact_span(key, parsed) == bank[key]
    raw = f'## 关键结果\n\nA 99.8% [[证据ID:{key}]]'
    _, evidence = attach_evidence(raw, parsed, pdf_available=True, full_report=True)
    assert evidence['citations'][0]['source_id'] == key
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


@pytest.mark.parametrize('pages,quote', [
    ([QUOTE, QUOTE], QUOTE),
    ([QUOTE + '\n' + QUOTE], QUOTE),
    ([QUOTE], QUOTE.replace('91.2', '91.3')),
    ([QUOTE], QUOTE.replace('test accuracy is', '...')),
    ([QUOTE[:30], QUOTE[30:]], QUOTE),
    ([QUOTE], 'too short'),
    (['A' * 601], 'A' * 601),
])
def test_unmatched_ambiguous_changed_or_cross_page_quotes_never_get_ids(pages, quote):
    parsed = ParsedPaper('', pages)
    notes, _, stats = ground_note_quotes([f'claim [[证据:{quote}]]'], parsed, source_spans(parsed))
    assert not ID_TOKEN.search(notes[0])
    assert notes[0] == 'claim '
    assert stats['quotes_unresolved'] == 1


def test_whitespace_and_nfkc_only_normalization_preserves_source_characters():
    page = 'Accuracy of Model Ａ on the\nheld-out test is ９１.２％.'
    quote = 'Accuracy of Model A on the held-out test is 91.2%.'
    parsed = ParsedPaper('', [page])
    notes, bank, _ = ground_note_quotes([f'[[证据:{quote}]]'], parsed, source_spans(parsed))
    span = bank[ID_TOKEN.search(notes[0])[1]]
    assert span['quote'] == page and span['end'] == len(page)


def test_precise_ids_invalidated_by_source_changes_bounds_and_availability():
    parsed = ParsedPaper('', [QUOTE])
    notes, bank, _ = ground_note_quotes([f'[[证据:{QUOTE}]]'], parsed, source_spans(parsed))
    key = ID_TOKEN.search(notes[0])[1]
    assert exact_span(key, ParsedPaper('', [QUOTE + ' changed'])) is None
    prefix = key.rsplit('-', 3)[0]
    for invalid in [prefix + '-0-0-25', prefix + '-1-5-9999', prefix + '-1-0-3',
                    prefix + '-1-20-1', key.replace('Q1-', 'Q2-'), key + '-extra']:
        assert exact_span(invalid, parsed) is None
    for pdf, full in [(False, True), (True, False)]:
        _, evidence = attach_evidence(f'[[证据ID:{key}]]', parsed, pdf_available=pdf, full_report=full)
        assert evidence['rejected_citations'] == 1 and not evidence['citations']


def test_grounded_quotes_share_existing_budget_and_do_not_lose_late_formula():
    pages = [f'Page {i}: ' + ('Source words ' * 35) for i in range(100)]
    parsed = ParsedPaper('', pages)
    notes = [f'[[证据:{p}]]' for p in pages]
    notes[-1] = r'\[x=\sqrt{1-y}\]' + '\n' + notes[-1]
    grounded, bank, stats = ground_note_quotes(notes, parsed, source_spans(parsed))
    cleaned, material = cited_span_material(grounded, bank)
    selected = json.loads(material)
    assert stats['quotes_grounded'] == 100
    assert sum(len(s['text']) for s in selected) <= 32000
    assert any(bank[s['id']]['page'] == 100 for s in selected)
    assert {m[1] for n in cleaned for m in ID_TOKEN.finditer(n)} == {s['id'] for s in selected}


def test_auto_generation_promotes_chunk_quotes_without_an_extra_model_call():
    parsed = ParsedPaper(QUOTE, [QUOTE])
    calls = []
    def chat(system, user):
        calls.append((system, user))
        if len(calls) == 1:
            return f'A 91.2% [[证据:{QUOTE}]]\nBad [[证据:Missing source that cannot be found in this paper.]]'
        assert '[[证据:Model A' not in user
        assert '[[证据:Missing' not in user
        assert '不要把片段 ID 改成自由抄写' in user
        key = next(m[1] for m in ID_TOKEN.finditer(user) if m[1].startswith('Q1-'))
        assert QUOTE in user
        return f'# Paper\n\n## 关键结果\nA 91.2% [[证据ID:{key}]]'
    paper = Paper.from_dict({'arxiv_id': '2506.15903', 'title': 'Paper', 'version': 1})
    raw = ReportGenerator(SimpleNamespace(enabled=True, chat=chat), LLMConfig()).generate(
        paper, VerifiedMetadata(), parsed, None)
    _, evidence = attach_evidence(raw, parsed, pdf_available=True, full_report=True)
    assert len(calls) == 2
    assert evidence['validated_citations'] == 1 and evidence['rejected_citations'] == 0
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence


@pytest.mark.parametrize('first,second', [('- ', '- '), ('1. ', '2. '), ('- ', '  - ')])
def test_neighboring_list_claims_cannot_borrow_numeric_evidence(first, second):
    parsed = ParsedPaper('', [QUOTE])
    key = next(iter(source_spans(parsed)))
    raw = ('## 关键结果\n说明：\n'
           f'{first}A 91.2% [[证据ID:{key}]]\n'
           f'{second}B 91.2% 未附来源。')
    _, evidence = attach_evidence(raw, parsed, pdf_available=True, full_report=True)
    assert 'numeric_audit' not in evidence and 'publication_gate' not in evidence
