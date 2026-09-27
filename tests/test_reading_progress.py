from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from arxiv_ra.feedback import FeedbackStore
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.reading_state import ReadingConflict, ReadingStateStore
from arxiv_ra.utils import write_json
from arxiv_ra.web import create_app


def save(store, **changes):
    values = dict(status='read', notes='My conclusion', tags=['methods'], read_version=1, expected_updated_at='')
    values.update(changes)
    return store.update_reading('2407.05600', **values)


def test_reading_notes_survive_versions_removal_and_dismissal(tmp_path):
    library = PaperLibraryStore(tmp_path, 'a')
    library.add({'paper': {'arxiv_id': '2407.05600', 'version': 1}}, 'A')
    record = save(library.state)
    previous = library.all()['2407.05600']
    library.refresh(previous, {'paper': {'arxiv_id': '2407.05600', 'version': 3}})
    assert library.state.snapshot()['reading']['2407.05600'] == record
    FeedbackStore(tmp_path, 'a').set({'arxiv_id': '2407.05600'}, 'not_relevant')
    assert library.state.snapshot()['reading']['2407.05600'] == record
    library.add({'paper': {'arxiv_id': '2407.05600', 'version': 3}}, 'A')
    updated = save(library.state, read_version=3, expected_updated_at=record['updated_at'])
    assert [event['read_version'] for event in updated['history']] == [1, 3]
    assert not ReadingStateStore(tmp_path, 'b').snapshot().get('reading')


def test_concurrent_note_edit_rejects_stale_text(tmp_path):
    library = PaperLibraryStore(tmp_path, 'a')
    library.add({'paper': {'arxiv_id': '2407.05600', 'version': 1}}, 'A')
    def edit(text):
        try:
            return save(ReadingStateStore(tmp_path, 'a'), notes=text)
        except ReadingConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ['first', 'second']))
    assert sum(value is not None for value in results) == 1
    assert library.state.snapshot()['reading']['2407.05600']['notes'] in {'first', 'second'}


def test_failed_write_retains_personal_note(tmp_path, monkeypatch):
    library = PaperLibraryStore(tmp_path, 'a')
    library.add({'paper': {'arxiv_id': '2407.05600'}}, 'A')
    before = save(library.state)
    monkeypatch.setattr('arxiv_ra.reading_state.write_json', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        save(library.state, notes='replacement', expected_updated_at=before['updated_at'])
    assert library.state.snapshot()['reading']['2407.05600'] == before


def test_library_progress_editor_filter_and_note_search(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_text('output_dir: run\ndiscovery:\n  interest_description: alpha\n', encoding='utf-8')
    app = create_app(config)
    library = PaperLibraryStore(tmp_path / 'run', 'alpha')
    library.add({'paper': {'arxiv_id': '2407.05600', 'version': 2, 'title': 'Paper', 'authors': [],
                          'abstract_zh': '摘要', 'recommendation_detail': '依据'}}, 'Alpha')
    with TestClient(app) as client:
        response = client.post('/api/library/reading', data={'profile_id': 'alpha', 'arxiv_id': '2407.05600',
            'reading_status': 'read', 'notes': '<script>MY_NOTE</script>', 'tags': 'tools, reasoning', 'read_version': 1,
            'expected_updated_at': ''})
        assert response.status_code == 200
        page = client.get('/library?reading_status=read&q=MY_NOTE')
        assert 'reading-form' in page.text and '&lt;script&gt;MY_NOTE&lt;/script&gt;' in page.text
        assert '已读 v1' in page.text
        assert 'Paper' not in client.get('/library?reading_status=unread').text.split('<main')[1].split('</main>')[0]
        conflict = client.post('/api/library/reading', data={'profile_id': 'alpha', 'arxiv_id': '2407.05600',
            'reading_status': 'reading', 'notes': 'overwrite', 'expected_updated_at': ''})
        assert conflict.status_code == 409
        assert library.state.snapshot()['reading']['2407.05600']['notes'] == '<script>MY_NOTE</script>'
