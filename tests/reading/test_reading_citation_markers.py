import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from paperloom.reading.reading_chat import ReadingService, normalize_source_markers
from paperloom.web import create_app
from tests.reading.test_reading_chat import config_path, new_chat, wait_answer


@pytest.mark.parametrize('marker', [
    '[[来源:{0}], [来源:{1}]]',
    '[[来源:{0}],[{1}]]',
    '[[来源:{0}]， [{1}]]',
    '[[来源:{0}]、[来源:{1}]]',
    '({0}, {1})',
    '（{0}，{1}）',
    '({0}) 和 ({1})',
    r'\({0}, {1}\)',
    '（`{0}`、`{1}`）',
    '（`{0}`）和（`{1}`）',
    '$({0}, {1})$',
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
    assert [a['href'] for a in BeautifulSoup(html, 'html.parser').find_all('a')] == [
        '/source/paper#page=1', '/source/paper#page=2']


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

    monkeypatch.setattr('paperloom.reading.reading_chat.run_reading', run)
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


@pytest.mark.parametrize('marker', ['（`{0}`、`{1}`）', '({0}, {1})'])
def test_bare_citations_survive_answer_completion(config_path, monkeypatch, marker):
    ids = ['ee528c7f4d6bff44', '2da64cbd1c4752f3']

    def run(_client, _model, _messages, sources, update, checkpoint, **kwargs):
        sources.citations = [{'id': cid, 'source_id': 'paper', 'page': page,
                              'url': f'/api/reading/sources/paper#page={page}'}
                             for page, cid in enumerate(ids, 1)]
        update(citations=sources.citations)
        return marker.format(*ids), False

    monkeypatch.setattr('paperloom.reading.reading_chat.run_reading', run)
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    with TestClient(create_app(config_path)) as client:
        sid = new_chat(client)['id']
        response = client.post(f'/api/reading/sessions/{sid}/messages',
                               json={'text': '解释标注流程', 'request_id': 'bare'})
        assert response.status_code == 200
        answer = wait_answer(client, sid)['messages'][-1]
        assert answer['status'] == 'completed', answer
        assert [c['id'] for c in answer['citations']] == ids
        assert all(f'[[来源:{cid}]]' in answer['text'] for cid in ids)
        assert '第1页' in answer['html'] and '第2页' in answer['html']
        assert all(cid not in answer['html'] for cid in ids)


def test_bare_citation_normalization_is_evidence_bound_and_preserves_code():
    cid, unknown = 'ee528c7f4d6bff44', '0123456789abcdef'
    untouched = (f'普通括号 (caption)，哈希 ({unknown})，公式 $f(x)$ 和 $({unknown})$。\n'
                 f'`({cid})` 与 [链接](https://example.com/({cid}))\n\n'
                 f'```text\n({cid})\n```\n')
    text = untouched + f'出处（`{cid}`）'
    normalized = normalize_source_markers(text, {cid})
    assert normalized == untouched + f'出处[[来源:{cid}]]'
    assert normalize_source_markers(normalized, {cid}) == normalized


def test_mixed_bare_group_does_not_turn_unknown_id_into_a_page_link(tmp_path):
    cid, unknown = 'ee528c7f4d6bff44', '0123456789abcdef'
    service = ReadingService(tmp_path, None)
    source = service.folder / 'sources' / 'paper.pdf'
    source.parent.mkdir()
    source.touch()
    service.save({'id': 'c' * 32, 'references': [], 'messages': [{
        'role': 'assistant', 'status': 'running',
        'text': f'出处（`{cid}`、`{unknown}`）',
        'citations': [{'id': cid, 'source_id': 'paper', 'page': 6,
                      'url': '/source/paper#page=6'}],
    }]})
    html = service.present('c' * 32)['messages'][0]['html']
    tree = BeautifulSoup(html, 'html.parser')
    assert [a['href'] for a in tree.find_all('a')] == ['/source/paper#page=6']
    assert '引用未核实' in tree.get_text()
    assert unknown not in html and cid not in html

    source.unlink()
    missing_html = service.present('c' * 32)['messages'][0]['html']
    assert '第6页，文件已缺失' in missing_html
    assert not BeautifulSoup(missing_html, 'html.parser').find_all('a')


@pytest.mark.parametrize('text', [
    '结论 `{id}`。', '结论 {id}。', '结论【{id}】。',
    '结论（来源：{id}）。', '结论 [[来源:{id}]]。',
    '结论 [{id}](https://provider.example/invented-source)。',
])
def test_registered_evidence_is_renderable_without_model_punctuation_contract(tmp_path, text):
    service = ReadingService(tmp_path, None)
    sid, mid, cid = 'd' * 32, 'answer', 'ecc8c9bbddca4fbf'
    source = service.folder / 'sources' / 'paper.pdf'
    source.parent.mkdir()
    source.touch()
    service.save({'id': sid, 'references': [], 'messages': [{
        'id': mid, 'role': 'assistant', 'status': 'running', 'text': '', 'citations': [],
    }]})
    evidence = [{'id': cid, 'source_id': 'paper', 'page': 22, 'quote': 'Source text',
                 'url': '/source/paper#page=22'}]
    service.update_answer(sid, mid, citations=evidence)
    # Finalization must not destroy the registered evidence even if citation
    # selection failed to understand the provider's punctuation.
    service.update_answer(sid, mid, text=text.format(id=cid), citations=[], status='completed')
    message = service.present(sid)['messages'][0]
    assert cid not in message['html']
    assert [a['href'] for a in BeautifulSoup(message['html'], 'html.parser').find_all('a')] == [
        '/source/paper#page=22']
    assert service.get(sid)['messages'][0]['evidence'] == evidence


def test_streamed_citation_does_not_expose_protocol_or_partial_ids(tmp_path):
    service = ReadingService(tmp_path, None)
    sid, mid, cid = 'e' * 32, 'answer', 'ecc8c9bbddca4fbf'
    source = service.folder / 'sources' / 'paper.pdf'
    source.parent.mkdir()
    source.touch()
    service.save({'id': sid, 'references': [], 'messages': [{
        'id': mid, 'role': 'assistant', 'status': 'running', 'text': '', 'citations': [],
    }]})
    service.update_answer(sid, mid, citations=[{'id': cid, 'source_id': 'paper', 'page': 22,
                                              'url': '/source/paper#page=22'}])
    prefix, citation = '论文结论。', f'[[来源:{cid}]]'
    for end in range(1, len(citation) + 1):
        service.update_answer(sid, mid, text=prefix + citation[:end])
        message = service.present(sid)['messages'][0]
        visible = BeautifulSoup(message['html'], 'html.parser').get_text()
        assert '来源' not in visible, (end, visible)
        assert cid[:6] not in visible, (end, visible)
        assert '引用未核实' not in visible, (end, visible)
        assert prefix in visible
    assert '第22页' in visible


def test_unused_evidence_survives_completion_and_restart_without_becoming_visible(tmp_path):
    service = ReadingService(tmp_path, None)
    sid, mid, used, unused = 'a' * 32, 'answer', 'ecc8c9bbddca4fbf', '099fbce4b5bc090c'
    service.save({'id': sid, 'references': [], 'messages': [{
        'id': mid, 'role': 'assistant', 'status': 'running', 'text': '', 'citations': [],
    }]})
    service.update_answer(sid, mid, citations=[{'id': cid, 'source_id': 'paper', 'page': page,
        'url': f'/source/paper#page={page}'} for page, cid in enumerate((used, unused), 1)])
    service.update_answer(sid, mid, text=f'结论 `{used}`。', status='completed')
    restarted = ReadingService(tmp_path, None)
    stored = restarted.get(sid)['messages'][0]
    assert [c['id'] for c in stored['evidence']] == [used, unused]
    assert [c['id'] for c in stored['citations']] == [used]
    presented = restarted.present(sid)['messages'][0]
    assert [c['id'] for c in presented['citations']] == [used]
    assert 'evidence' not in presented
