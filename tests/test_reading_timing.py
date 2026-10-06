from types import SimpleNamespace

import pytest

from arxiv_ra.reading_chat import ReadingService


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'stopped'])
def test_execution_time_freezes_at_terminal_state(tmp_path, monkeypatch, terminal):
    service = ReadingService(tmp_path, SimpleNamespace(cancel=lambda _: None))
    sid = 'a' * 32
    value = {'id': sid, 'messages': [{'id': 'answer', 'role': 'assistant', 'status': 'queued'}]}
    service.save(value)
    monkeypatch.setattr('arxiv_ra.reading_chat.timestamp', lambda: '2026-10-07T00:00:00+00:00')
    service.update_answer(sid, 'answer', status='running')
    monkeypatch.setattr('arxiv_ra.reading_chat.timestamp', lambda: '2026-10-07T00:01:18+00:00')
    if terminal == 'stopped':
        service.stop(sid)
    else:
        service.update_answer(sid, 'answer', status=terminal)
    assert service.get(sid)['messages'][0]['elapsed_seconds'] == 78
    monkeypatch.setattr('arxiv_ra.reading_chat.timestamp', lambda: '2026-10-08T00:00:00+00:00')
    service.save(service.get(sid))
    assert service.get(sid)['messages'][0]['elapsed_seconds'] == 78


def test_restart_uses_last_progress_not_downtime(tmp_path, monkeypatch):
    service = ReadingService(tmp_path, None)
    sid = 'b' * 32
    monkeypatch.setattr('arxiv_ra.reading_chat.timestamp', lambda: '2026-10-07T00:00:00+00:00')
    service.save({'id': sid, 'messages': [{'id': 'answer', 'role': 'assistant', 'status': 'running'}]})
    monkeypatch.setattr('arxiv_ra.reading_chat.timestamp', lambda: '2026-10-07T00:00:12+00:00')
    service.update_answer(sid, 'answer', text='Partial')
    monkeypatch.setattr('arxiv_ra.reading_chat.timestamp', lambda: '2026-10-08T00:00:00+00:00')
    recovered = ReadingService(tmp_path, None).get(sid)['messages'][0]
    assert recovered['status'] == 'interrupted'
    assert recovered['elapsed_seconds'] == 12
