from copy import deepcopy

import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.profile_plan import new_draft
from arxiv_ra.profiles import ProfileManager, DraftConflict
from test_profile_drafts import workspace


def test_delete_restore_preserve_formal_profile_and_prevent_stale_resurrection(tmp_path):
    manager = ProfileManager(tmp_path)
    draft = new_draft(AppConfig(), 'svg', 'SVG', ['SVG'], [], [], 'SVG', {})
    manager.save_draft(draft)
    manager.activate_draft('svg', 1)
    formal = manager.get('svg')
    manager.delete_draft('svg', 1)
    assert manager.drafts() == []
    assert manager.active_id() == 'svg'
    assert manager.get('svg') == formal
    assert manager.deleted_drafts()[0]['id'] == 'svg'
    with pytest.raises(DraftConflict):
        manager.save_draft(deepcopy(draft), expected_revision=1)
    with pytest.raises(DraftConflict):
        manager.save_draft(deepcopy(draft))
    manager.save_preview('svg', {'revision': 1, 'status': 'ok'})
    assert manager.drafts() == []
    restored = manager.restore_draft('svg', 1)
    assert restored['revision'] == 2
    assert manager.deleted_drafts() == []
    assert restored['plan'] == draft['plan']
    assert manager.get('svg') == formal
    with pytest.raises(DraftConflict):
        manager.save_draft(deepcopy(draft), expected_revision=1)
    with pytest.raises(DraftConflict):
        manager.delete_draft('svg', 1)
    manager.delete_draft('svg', 2)
    assert manager.unique_id('svg') == 'svg-2'
    with pytest.raises(ValueError):
        manager.restore_draft('../svg', 2)


def test_delete_and_restore_routes_and_cards(workspace):
    client, _, root = workspace
    draft = client.post('/api/profile-drafts', json={'name': 'Disposable', 'keywords': ['SVG']}).json()
    did = draft['id']
    page = client.get('/profiles')
    assert '删除草稿' in page.text and 'draft-delete-dialog' in page.text
    assert client.post(f'/api/profile-drafts/{did}/delete', json={'revision': 99}).status_code == 409
    assert client.post(f'/api/profile-drafts/{did}/delete', json={'revision': 1}).status_code == 200
    assert client.get(f'/api/profile-drafts/{did}').status_code == 404
    page = client.get('/profiles')
    assert '恢复草稿' in page.text
    assert client.post(f'/api/profile-drafts/{did}/edit', json={'revision': 1}).status_code == 404
    assert client.post(f'/api/profile-drafts/{did}/restore', json={'revision': True}).status_code == 400
    assert client.post(f'/api/profile-drafts/{did}/restore', json={'revision': 9}).status_code == 409
    restored = client.post(f'/api/profile-drafts/{did}/restore', json={'revision': 1})
    assert restored.status_code == 200 and restored.json()['revision'] == 2
    assert client.post(f'/api/profile-drafts/{did}/restore', json={'revision': 1}).status_code == 404
    assert client.get(f'/profiles/drafts/{did}').status_code == 200
    assert client.post(f'/profiles/drafts/{did}/delete', data={'revision': 2}, follow_redirects=False).status_code == 303
    assert client.post(f'/profiles/drafts/{did}/restore', data={'revision': 2}, follow_redirects=False).status_code == 303


def test_clear_deleted_drafts_preserves_live_data_and_blocks_stale_writers(tmp_path):
    manager = ProfileManager(tmp_path)
    deleted = new_draft(AppConfig(), 'discarded', 'Discarded', ['SVG'], [], [], 'SVG', {})
    live = new_draft(AppConfig(), 'live', 'Live', ['SVG'], [], [], 'SVG', {})
    manager.save_draft(deleted)
    manager.save_draft(live)
    manager.activate_draft('discarded', 1)
    formal = manager.get('discarded')
    manager.delete_draft('discarded', 1)
    second = new_draft(AppConfig(), 'second', 'Second', ['SVG'], [], [], 'SVG', {})
    manager.save_draft(second)
    manager.delete_draft('second', 1)
    unrelated = manager.root / 'deleted-drafts' / 'notes.txt'
    unrelated.write_text('keep', encoding='utf-8')
    report = tmp_path / 'run' / 'report.html'
    report.parent.mkdir()
    report.write_text('keep report', encoding='utf-8')
    assert manager.clear_deleted_drafts() == 2
    assert manager.deleted_drafts() == []
    assert manager.drafts() == [live]
    assert manager.get('discarded') == formal
    assert manager.active_id() == 'discarded'
    assert unrelated.read_text(encoding='utf-8') == 'keep'
    assert report.read_text(encoding='utf-8') == 'keep report'
    assert manager.clear_deleted_drafts() == 0
    with pytest.raises(KeyError):
        manager.restore_draft('discarded', 1)
    for revision in (None, 1):
        with pytest.raises(DraftConflict):
            manager.save_draft(deepcopy(deleted), expected_revision=revision)
    manager.save_preview('discarded', {'revision': 1, 'status': 'ok'})
    assert manager.drafts() == [live]
    assert manager.unique_id('second') == 'second-2'


def test_clear_deleted_drafts_route_and_empty_state(workspace):
    client, _, root = workspace
    payload = {'name': 'Disposable', 'keywords': ['SVG'], 'request_id': 'purge-fixture'}
    draft = client.post('/api/profile-drafts', json=payload).json()
    live = client.post('/api/profile-drafts', json={'name': 'Keep', 'keywords': ['SVG']}).json()
    did = draft['id']
    active = (root / 'profiles' / 'active.txt').read_text()
    assert '清空已删除草稿' not in client.get('/profiles').text
    client.post(f'/api/profile-drafts/{did}/delete', json={'revision': 1})
    assert '清空已删除草稿' in client.get('/profiles').text
    assert client.post('/profiles/deleted-drafts/clear', headers={'Origin': 'https://external.example'}).status_code == 403
    assert client.post('/profiles/deleted-drafts/clear', data={'profile_id': 'stale'}).status_code == 409
    assert (root / 'profiles' / 'deleted-drafts' / f'{did}.yaml').exists()
    response = client.post('/profiles/deleted-drafts/clear', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/profiles#deleted-drafts'
    page = client.get('/profiles').text
    assert '暂无已删除草稿' in page and '清空已删除草稿' not in page
    assert client.get(f"/api/profile-drafts/{live['id']}").status_code == 200
    assert client.post(f'/api/profile-drafts/{did}/restore', json={'revision': 1}).status_code == 404
    assert client.post('/api/profile-drafts', json=payload).status_code == 409
    assert (root / 'profiles' / 'active.txt').read_text() == active
    assert client.post('/profiles/deleted-drafts/clear', follow_redirects=False).status_code == 303
