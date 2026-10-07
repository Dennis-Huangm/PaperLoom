"""Cited row completion and independent clause preservation through publication."""
import hashlib
import json

import pytest

from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper
from arxiv_ra.source_spans import source_spans


PAGE = 'Table 2: Results\nMethod\nGroup A\nGroup B\nCLIP\nTokens\nCLIP\nTokens\nAlpha\n0.313\n31.4k\n0.309\n31.4k\nBeta\n0.293\n124.3k\n0.276\n124.3k\n'


def token(page, start, end):
    digest = hashlib.sha256(json.dumps([page], ensure_ascii=False).encode()).hexdigest()[:24]
    return f'[[证据ID:Q1-{digest}-1-{start}-{end}]]'


def row_report(page=PAGE, values=('0.313', '31.4k', '0.309', '31.4k'), start=None):
    start = page.index('0.309') if start is None else start
    ref = token(page, start, len(page))
    report = '## 关键结果\n\n| Method | A CLIP | A Tokens | B CLIP | B Tokens | 原文依据 |\n|---|---|---|---|---|---|\n'
    report += '| Alpha | ' + ' | '.join(values) + f' | {ref} |'
    return attach_evidence(report, ParsedPaper(page, [page]), pdf_available=True, full_report=True)


def test_partial_quote_is_not_expanded_to_make_values_match():
    output, evidence = row_report()
    assert '| Alpha | 0.313 | 31.4k | 0.309 | 31.4k |' in output
    assert evidence['citations'][0]['quote'].startswith('0.309')
    assert len(evidence['citations']) == 1
    assert 'numeric_audit' not in evidence


@pytest.mark.parametrize('values', [
    ('0.293', '31.4k', '0.309', '31.4k'),
    ('0.309', '31.4k', '0.313', '31.4k'),
    ('0.313', '31400', '0.309', '31.4k'),
])
def test_values_and_units_are_preserved_without_claim_verdicts(values):
    output, evidence = row_report(values=values)
    assert '| Alpha | ' + ' | '.join(values) + ' |' in output
    assert '引用冲突' not in output and '待核对' not in output
    assert 'publication_gate' not in evidence


def test_unrelated_citation_does_not_expand_to_previous_row():
    output, evidence = row_report(start=PAGE.index('Beta'))
    assert '| Alpha | 0.313 |' in output
    assert evidence['citations'][0]['quote'].startswith('Beta')
