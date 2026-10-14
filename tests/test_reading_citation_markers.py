import pytest
from fastapi.testclient import TestClient

from arxiv_ra.reading_chat import ReadingService, normalize_source_markers
from arxiv_ra.web import create_app
from test_reading_chat import config_path, new_chat, wait_answer


@pytest.mark.parametrize('marker', [
    '[[来源:{0}], [来源:{1}]]',
    '[[来源:{0}],[{1}]]',
    '[[来源:{0}]， [{1}]]',
    '[[来源:{0}]、[来源:{1}]]',
])
def test_grouped_source_markers_render_as_page_links(tmp_path, marker):
    service = ReadingService(tmp_path, None)
    source = service.folder / 'sources' / 'paper.pdf'
    source.parent.mkdir()
    source.touch()
    ids = ['7368932172e308ed', '19895b556816b72b']
    service.save({'id': 'a' * 32, 'references': [], 'messages': [{
        'role': 'assistant', 'status': 'completed',
        'text': '视觉推进 ' + marker.format(*ids) + '。',
        'citations': [{'id': cid, 'source_id': 'paper', 'page': i + 1,
                      'url': f'/source/paper#page={i + 1}'} for i, cid in enumerate(ids)],
    }]})
    html = service.present('a' * 32)['messages'][0]['html']
    assert '来源:' not in html
    assert '第1页' in html and '第2页' in html
    assert all(cid not in html for cid in ids)


@pytest.mark.parametrize('marker', [
    '[[来源:missing-a], [来源:missing-b]]',
    '[[来源:missing-a],[missing-b]]',
])
def test_unknown_grouped_markers_do_not_leak_ids(tmp_path, marker):
    service = ReadingService(tmp_path, None)
    service.save({'id': 'b' * 32, 'references': [], 'messages': [{
        'role': 'assistant', 'status': 'completed',
        'text': marker, 'citations': [],
    }]})
    html = service.present('b' * 32)['messages'][0]['html']
    assert '来源:' not in html
    assert 'missing-' not in html
    assert html.count('引用未核实') == 2


@pytest.mark.parametrize('marker', [
    '[[来源:evidence-a], [来源:evidence-b], [来源:invented]]',
    '[[来源:evidence-a],[evidence-b],[invented]]',
])
def test_grouped_citations_survive_answer_completion(config_path, monkeypatch, marker):
    def run(_client, _model, _messages, sources, update, checkpoint, **kwargs):
        citations = [{'id': cid, 'source_id': 'paper', 'page': page,
                      'url': f'/api/reading/sources/paper#page={page}'}
                     for page, cid in enumerate(('evidence-a', 'evidence-b'), 1)]
        sources.citations = citations
        update(citations=citations)
        return marker, False

    monkeypatch.setattr('arxiv_ra.reading_chat.run_reading', run)
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    with TestClient(create_app(config_path)) as client:
        sid = new_chat(client)['id']
        response = client.post(f'/api/reading/sessions/{sid}/messages',
                               json={'text': '解释方法', 'request_id': 'grouped'})
        assert response.status_code == 200
        answer = wait_answer(client, sid)['messages'][-1]
        assert answer['status'] == 'completed', answer
        assert [c['id'] for c in answer['citations']] == ['evidence-a', 'evidence-b']
        assert '[[来源:evidence-a]]' in answer['text']
        assert 'invented' not in answer['text']
        assert '来源:' not in answer['html']
        assert '第1页' in answer['html'] and '第2页' in answer['html']


def test_normalization_preserves_unrelated_brackets_and_is_idempotent():
    text = '普通 [text] 和 [link](https://example.com)，[[来源:a],[b],[来源:c]]。'
    normalized = normalize_source_markers(text)
    assert normalized == '普通 [text] 和 [link](https://example.com)，[[来源:a]],[[来源:b]],[[来源:c]]。'
    assert normalize_source_markers(normalized) == normalized
