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
