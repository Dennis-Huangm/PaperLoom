"""History search through the public session-list API."""
from fastapi.testclient import TestClient
from arxiv_ra.web import create_app
from test_reading_chat import config_path


def test_history_search_matches_visible_papers_and_messages(config_path):
    app = create_app(config_path)
    service = app.state.reading
    row = {'id': 'a' * 32, 'title': '随手问问', 'mode': 'workspace',
           'paper': {}, 'references': [{'paper': {'title': 'VectorGym Benchmark', 'arxiv_id': '2401.12345', 'version': 2}}],
           'messages': [{'role': 'user', 'text': '实验输入是什么？',
                         'references': [{'paper': {'title': 'Removed Paper', 'arxiv_id': '2402.12345'}}]},
                        {'role': 'assistant', 'text': '模型使用结构化图片进行训练。Straße',
                         'trace': [{'text': 'private-tool-result'}]}]}
    service.save(row)
    service.save({'id': 'b' * 32, 'title': '旧对话', 'paper': {'title': 'Legacy Paper', 'arxiv_id': '2403.12345', 'version': 1},
                  'messages': [{'role': 'user', 'text': '归档问题'}], 'archived': True})
    service.save({'id': 'c' * 32, 'title': 'VectorGym draft', 'paper': {}, 'references': [], 'messages': []})
    with TestClient(app) as client:
        def search(q, **params):
            return client.get('/api/reading/sessions', params={'q': q, **params}).json()
        for query, label in [('随手', '会话标题'), ('vectorgym', '论文标题'), ('2401.12345v2', '论文 ID'),
                             ('实验输入', '提问'), ('结构化图片', '回答'), ('removed paper', '论文标题'),
                             ('ＳＴＲＡＳＳＥ', '回答')]:
            result = search(query)
            assert [r['id'] for r in result] == [row['id']]
            assert result[0]['search_match']['label'] == label
            assert 'messages' not in result[0]
        assert len(search('  VECTORGYM   结构化图片  ')) == 1
        assert search('vectorgym nonexistent') == []
        assert search('private-tool-result') == []
        assert search('vectorgym', mode='report') == []
        assert search('legacy')[0]['archived'] is True
        assert len(search('   ')) == 2
        assert all('search_match' not in r for r in search(''))
        assert len(search('输入', arxiv_id='2401.12345')) == 1
        assert search('输入', arxiv_id='2402.12345') == []
