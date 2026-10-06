"""Evidence returned through the same tools used by the reading model."""
from types import SimpleNamespace as NS

import fitz
import pytest

from arxiv_ra.arxiv_client import ArxivClient
from arxiv_ra.reading_sources import ReadingSources
from test_reading_chat import config_path


@pytest.fixture
def source(config_path):
    root = config_path.parent / 'run'
    cache = root / '.reading' / 'sources'
    cache.mkdir(parents=True)
    with fitz.open() as doc:
        page = doc.new_page()
        lines = ['Table 3: Per-dimension mean absolute error.']
        lines += [f'Model {i:02}: aesthetic 0.4; semantic 0.5; spatial 0.6; structural 0.7.' for i in range(30)]
        page.insert_text((40, 50), '\n'.join(lines), fontsize=10)
        doc.new_page()  # A blank/scanned page must not acquire textual evidence.
        doc.save(cache / ('a' * 64 + '.pdf'))
    with (cache / ('a' * 64 + '.pdf')).open('ab') as handle:
        handle.write(b'\n%' + b' ' * 11000)
    return ReadingSources(root, {'paper': ArxivClient.get(None, '2401.12345v1').to_dict()},
                          {'report': {'source_id': 'a' * 64}}, NS(), lambda **kw: None)


def test_read_pages_returns_bounded_evidence_for_all_delivered_text(source):
    result = source.execute('read_pages', {'start': 1, 'end': 2})
    excerpts = result['pages']
    assert result['read_pages'] == [1, 2]
    assert len(excerpts) > 1
    assert {p['page'] for p in excerpts} == {1}
    assert all(len(p['text']) <= 800 for p in excerpts)
    with fitz.open(source.pdf) as doc:
        assert ''.join(p['text'] for p in excerpts) == doc[0].get_text()
    citations = {c['id']: c for c in source.citations}
    for piece in excerpts:
        citation = citations[piece['citation_id']]
        assert citation['quote'] == ' '.join(piece['text'].split())
        assert citation['page'] == 1
        assert citation['source_id'] == 'a' * 64
        assert citation['url'].endswith('#page=1')
    assert 'Model 29:' in excerpts[-1]['text']


def test_search_evidence_is_confined_to_returned_snippets_and_reused(source):
    result = source.execute('search', {'query': 'Table 3'})
    assert result['matched_pages'] == 1
    assert len(result['matches']) > 1
    assert result['matches'][0]['text'].startswith('Table 3:')
    assert 'Model 29:' not in ''.join(p['text'] for p in result['matches'])
    ids = {c['id'] for c in source.citations}
    assert ids == {p['citation_id'] for p in result['matches']}
    assert source.execute('search', {'query': 'Table 3'}) == result
    assert {c['id'] for c in source.citations} == ids
    assert len(source.citations) == len(ids)
    quote = source.citations[0]['quote']
    assert source.execute('cite', {'page': 1, 'quote': quote})['citation_id'] in ids
    with pytest.raises(ValueError, match='唯一核实'):
        source.execute('cite', {'page': 1, 'quote': 'A plausible but invented statement about Model 29.'})
    assert {c['id'] for c in source.citations} == ids


def test_report_and_empty_search_do_not_mint_original_evidence(source):
    assert 'citation_id' not in source.execute('read_report', {})
    assert source.execute('search', {'query': 'Not present anywhere'})['matches'] == []
    assert source.execute('read_pages', {'start': 2, 'end': 2})['pages'] == []
    assert source.citations == []


def test_same_excerpt_in_another_version_has_a_distinct_identity(source):
    original = source.execute('read_pages', {'start': 1, 'end': 1})
    other = ReadingSources(source.root, {'paper': {**source.session['paper'], 'version': 2}},
                           source.user, NS(), lambda **kw: None)
    revised = other.execute('read_pages', {'start': 1, 'end': 1})
    assert original['pages'][0]['text'] == revised['pages'][0]['text']
    assert original['pages'][0]['citation_id'] != revised['pages'][0]['citation_id']
