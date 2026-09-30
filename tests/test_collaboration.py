"""Collection behavior through the Web boundary, with a temporary vault."""
import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from arxiv_ra.web import create_app
from arxiv_ra.utils import write_json


@pytest.fixture
def workspace(tmp_path):
    vault = tmp_path / '研究 & notes'
    vault.mkdir()
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'output_dir': 'run', 'profile_id': 'default',
        'llm': {'api_key_env': 'PAPERLOOM_TEST_NO_KEY'}, 'delivery': {'email_enabled': False},
        'weekly': {'enabled': False}, 'version_tracking': {'enabled': False},
        'zotero': {'enabled': True, 'attach_pdf': False, 'collection_name': ''},
        'obsidian': {'enabled': True, 'vault_path': str(vault)}}), encoding='utf-8')
    paper = {'arxiv_id': '2609.00001', 'version': 2, 'title': 'Shared Paper',
        'abstract': 'Test abstract', 'authors': [], 'published': '2026-09-01',
        'primary_category': 'cs.AI', 'abs_url': 'https://arxiv.org/abs/2609.00001v2'}
    write_json(tmp_path / 'run/2026-09-01/recommendations.json',
        [{'paper': paper, 'verified': {}}])
    return config, vault


def collect(client, **options):
    return client.post('/api/collection', data={'arxiv_id': '2609.00001v2',
        'origin': 'recommendation', 'source_date': '2026-09-01',
        'obsidian': 'true', **options})


def test_collect_note_survives_restart_and_preserves_personal_text(workspace):
    config, vault = workspace
    with TestClient(create_app(config)) as client:
        first = collect(client)
        assert first.status_code == 200, first.text
        receipt = first.json()
        assert receipt['targets']['obsidian']['status'] == 'succeeded'
        note = vault / receipt['targets']['obsidian']['path']
        note.write_text(note.read_text('utf-8') + '\nMY PERSONAL NOTE\n', encoding='utf-8')
        second = collect(client).json()
        assert second['operation_id'] == receipt['operation_id']
    with TestClient(create_app(config)) as client:
        state = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert state['operations'][0]['operation_id'] == receipt['operation_id']
        assert 'MY PERSONAL NOTE' in note.read_text('utf-8')
        assert len(list(vault.rglob('*.md'))) >= 1
        assert state['links']['obsidian'].startswith('obsidian://open?')
        assert '%26' in state['links']['obsidian']


@pytest.fixture
def zotero_server(monkeypatch):
    from arxiv_ra.zotero import ZoteroClient
    monkeypatch.setenv('ZOTERO_LOCAL_API_KEY', 'test')
    state = {'items': {}, 'writes': 0, 'fail': True}
    def handler(request):
        import json
        path = request.url.path.removeprefix('/api')
        if path == '/':
            return httpx.Response(200, headers={'Zotero-Server-ID': 'stable-library'})
        if path == '/users/0/items/top':
            return httpx.Response(503 if state['fail'] else 200, json=list(state['items'].values()))
        if path == '/users/0/collections':
            return httpx.Response(200, json=[])
        if path == '/items/new':
            return httpx.Response(404)
        if path == '/users/0/items' and request.method == 'POST':
            item = json.loads(request.content)[0]
            key = f'KEY{len(state["items"]):05}'
            state['items'][key] = {'data': {**item, 'key': key}}
            state['writes'] += 1
            return httpx.Response(200, json={'successful': {'0': {'key': key}}})
        if path.endswith('/children'):
            parent = path.split('/')[-2]
            return httpx.Response(200, json=[v for v in state['items'].values() if v['data'].get('parentItem') == parent])
        key = path.split('/')[-1]
        if key in state['items']:
            return httpx.Response(200, json=state['items'][key])
        return httpx.Response(404)
    monkeypatch.setattr('arxiv_ra.collaboration.ZoteroClient',
        lambda cfg: ZoteroClient(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    monkeypatch.setattr('arxiv_ra.web.ZoteroClient',
        lambda cfg: ZoteroClient(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    return state


def test_partial_collection_retries_only_failed_target(workspace, zotero_server):
    config, vault = workspace
    with TestClient(create_app(config)) as client:
        first = collect(client, zotero='true').json()
        assert first['targets']['zotero']['status'] == 'failed'
        assert first['targets']['obsidian']['status'] == 'succeeded'
        note = vault / first['targets']['obsidian']['path']
        note.write_text(note.read_text('utf-8') + '\nKEEP ME\n', encoding='utf-8')
        zotero_server['fail'] = False
        retried = client.post('/api/collection/retry', data={'operation_id': first['operation_id']}).json()
        assert retried['status'] == 'succeeded', retried
        assert 'KEEP ME' in note.read_text('utf-8')
        assert 'zotero://select/library/items/' in note.read_text('utf-8')
        writes = zotero_server['writes']
        collect(client, zotero='true')
        assert zotero_server['writes'] == writes
        state = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert state['links']['zotero'].startswith('zotero://select/library/items/')
        assert state['associations']['zotero']['version'] == 2
        assert not state['associations']['zotero']['update_needed']


def test_moved_note_is_repaired_without_recreating_deleted_note(workspace):
    config, vault = workspace
    with TestClient(create_app(config)) as client:
        receipt = collect(client).json()
        old = vault / receipt['targets']['obsidian']['path']
        moved = old.with_name('我的笔记 #1.md')
        old.rename(moved)
        checked = client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'}).json()
        assert checked['associations']['obsidian']['path'].endswith('我的笔记 #1.md')
        again = collect(client).json()
        assert again['targets']['obsidian']['status'] == 'succeeded'
        assert not old.exists()
        moved.unlink()
        refused = collect(client).json()
        assert refused['targets']['obsidian']['status'] == 'needs_attention'
        assert not old.exists() and not moved.exists()


def test_collection_page_previews_snapshot_and_receipts(workspace):
    config, _ = workspace
    with TestClient(create_app(config)) as client:
        page = client.get('/collection', params={'arxiv_id': '2609.00001v2',
            'origin': 'recommendation', 'source_date': '2026-09-01'})
        assert page.status_code == 200
        assert 'Shared Paper' in page.text
        assert 'name="obsidian"' in page.text and 'name="zotero"' in page.text
        assert 'v2' in page.text
        collect(client)
        page = client.get('/collection', params={'arxiv_id': '2609.00001v2'})
        assert 'obsidian://open?' in page.text
        assert '/api/collection/retry' in page.text


def test_existing_zotero_save_uses_durable_receipt(workspace, zotero_server):
    config, _ = workspace
    zotero_server['fail'] = False
    with TestClient(create_app(config)) as client:
        result = client.post('/api/zotero/save-paper', data={'arxiv_id': '2609.00001v2',
            'origin': 'recommendation', 'source_date': '2026-09-01'})
        assert result.status_code == 200, result.text
        assert result.json()['operation_id']
        state = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert state['operations'][0]['targets']['zotero']['status'] == 'succeeded'


def test_changed_target_requires_explicit_retry_and_deleted_item_is_not_recreated(workspace, zotero_server):
    config, _ = workspace
    zotero_server['fail'] = False
    with TestClient(create_app(config)) as client:
        first = collect(client, zotero='true', obsidian='false').json()
        key = first['targets']['zotero']['item_key']
        del zotero_server['items'][key]
        writes = zotero_server['writes']
        again = collect(client, zotero='true', obsidian='false').json()
        assert again['targets']['zotero']['status'] == 'needs_attention'
        assert zotero_server['writes'] == writes
        values = yaml.safe_load(config.read_text('utf-8'))
        values['zotero']['collection_name'] = 'New destination'
        config.write_text(yaml.safe_dump(values), encoding='utf-8')
        refused = client.post('/api/collection/retry', data={'operation_id': first['operation_id']})
        assert refused.status_code == 409


def test_explicit_recollection_preserves_old_receipt(workspace):
    config, vault = workspace
    with TestClient(create_app(config)) as client:
        first = collect(client).json()
        (vault / first['targets']['obsidian']['path']).unlink()
        result = client.post('/api/collection/recollect', data={'operation_id': first['operation_id'], 'target': 'obsidian'})
        assert result.status_code == 200, result.text
        new = result.json()
        assert new['operation_id'] != first['operation_id']
        assert new['targets']['obsidian']['status'] == 'succeeded'
        state = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert len(state['operations']) == 2


def test_historical_report_selection_is_exact(workspace):
    config, vault = workspace
    root = config.parent / 'run'
    for name, body in [('old', 'SELECTED REPORT'), ('new', 'UNSELECTED REPORT')]:
        folder = root / '2026-09-01/reports' / name
        write_json(folder / 'metadata.json', {'profile_id': 'default', 'paper': {
            'arxiv_id': '2609.00001', 'version': 2, 'title': 'Shared Paper', 'authors': [],
            'abstract': '', 'published': '2026-09-01'}})
        (folder / 'report.md').write_text('# Shared Paper\n\n' + body, encoding='utf-8')
        (folder / 'report.html').write_text(body, encoding='utf-8')
    with TestClient(create_app(config)) as client:
        result = collect(client, origin='report', report_id='2026-09-01/reports/old/report.html').json()
        assert result['targets']['obsidian']['status'] == 'succeeded', result
        content = (vault / result['targets']['obsidian']['path']).read_text('utf-8')
        assert 'SELECTED REPORT' in content and 'UNSELECTED REPORT' not in content


def test_unknown_pdf_version_requires_attention_without_download(workspace, zotero_server):
    config, _ = workspace
    values = yaml.safe_load(config.read_text('utf-8'))
    values['zotero']['attach_pdf'] = True
    config.write_text(yaml.safe_dump(values), encoding='utf-8')
    paper_path = config.parent / 'run/2026-09-01/recommendations.json'
    import json
    papers = json.loads(paper_path.read_text('utf-8'))
    papers[0]['paper'].pop('version')
    paper_path.write_text(json.dumps(papers), encoding='utf-8')
    zotero_server['fail'] = False
    with TestClient(create_app(config)) as client:
        response = collect(client, arxiv_id='2609.00001', zotero='true', obsidian='false').json()
        assert response['targets']['zotero']['status'] == 'needs_attention'
        assert zotero_server['writes'] == 0


def test_restored_receipts_require_external_verification(workspace):
    import json
    config, _ = workspace
    with TestClient(create_app(config)) as client:
        first = collect(client).json()
    path = config.parent / 'run/collaboration.json'
    data = json.loads(path.read_text('utf-8'))
    data['storage_root'] = '/another/computer/run'
    path.write_text(json.dumps(data), encoding='utf-8')
    with TestClient(create_app(config)) as client:
        state = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert state['links'] == {}
        refused = collect(client).json()
        assert refused['targets']['obsidian']['status'] == 'needs_attention'
        client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'})
        resumed = client.post('/api/collection/retry', data={'operation_id': first['operation_id']}).json()
        assert resumed['status'] == 'succeeded'


def test_check_links_existing_zotero_and_supports_candidate_selection(workspace, zotero_server):
    config, _ = workspace
    zotero_server['fail'] = False
    zotero_server['items']['EXIST001'] = {'data': {'key': 'EXIST001', 'archiveID': '2609.00001', 'title': 'First'}}
    zotero_server['items']['EXIST002'] = {'data': {'key': 'EXIST002', 'archiveID': '2609.00001', 'title': 'Second'}}
    with TestClient(create_app(config)) as client:
        state = client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'}).json()
        assert state['associations']['zotero']['status'] == 'needs_attention'
        assert len(state['associations']['zotero']['candidates']) == 2
        selected = client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2', 'selected_key': 'EXIST002'}).json()
        assert selected['associations']['zotero']['item_key'] == 'EXIST002'
        assert zotero_server['writes'] == 0
        page = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert page['links']['zotero'].endswith('/EXIST002')


def test_concurrent_collection_requests_share_external_items(workspace, zotero_server):
    from concurrent.futures import ThreadPoolExecutor
    config, _ = workspace
    zotero_server['fail'] = False
    with TestClient(create_app(config)) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: collect(client, zotero='true').json(), range(2)))
        assert results[0]['operation_id'] == results[1]['operation_id']
        assert all(result['status'] == 'succeeded' for result in results)
        parents = [item for item in zotero_server['items'].values() if not item['data'].get('parentItem')]
        assert len(parents) == 1


def test_missing_selected_html_report_is_not_reported_as_success(workspace, zotero_server):
    config, _ = workspace
    zotero_server['fail'] = False
    folder = config.parent / 'run/2026-09-01/reports/selected'
    write_json(folder / 'metadata.json', {'paper': {'arxiv_id': '2609.00001', 'version': 2,
        'title': 'Shared Paper', 'published': '2026-09-01', 'authors': []}})
    (folder / 'report.md').write_text('SELECTED REPORT', encoding='utf-8')
    (folder / 'report.html').write_text('SELECTED HTML', encoding='utf-8')
    with TestClient(create_app(config)) as client:
        receipt = collect(client, zotero='true', origin='report', report_id='2026-09-01/reports/selected/report.html').json()
        (folder / 'report.html').unlink()
        result = client.post('/api/collection/retry', data={'operation_id': receipt['operation_id']})
        assert result.json()['targets']['zotero']['status'] != 'succeeded'
        assert result.json()['targets']['obsidian']['status'] == 'succeeded'


def test_user_selected_duplicate_note_remains_selected_on_retry(workspace):
    config, vault = workspace
    with TestClient(create_app(config)) as client:
        receipt = collect(client).json()
        note = vault / receipt['targets']['obsidian']['path']
        duplicate = note.with_name('duplicate.md')
        duplicate.write_text(note.read_text('utf-8'), encoding='utf-8')
        checked = client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'}).json()
        assert checked['associations']['obsidian']['status'] == 'needs_attention'
        picked = client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2',
            'selected_path': duplicate.relative_to(vault).as_posix()}).json()
        assert picked['associations']['obsidian']['status'] == 'verified'
        retried = client.post('/api/collection/retry', data={'operation_id': receipt['operation_id']}).json()
        assert retried['targets']['obsidian']['status'] == 'succeeded'
        assert retried['targets']['obsidian']['path'] == duplicate.relative_to(vault).as_posix()
        # A fresh material snapshot exercises the actual writer, rather than its success shortcut.
        items = json.loads((config.parent / 'run/2026-09-01/recommendations.json').read_text('utf-8'))
        items[0]['paper']['abstract'] = 'New abstract'
        write_json(config.parent / 'run/2026-09-01/recommendations.json', items)
        assert collect(client).json()['targets']['obsidian']['status'] == 'succeeded'
        checked = client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'}).json()
        assert checked['associations']['obsidian']['status'] == 'verified'
        assert checked['associations']['obsidian']['path'] == duplicate.relative_to(vault).as_posix()


def test_deleted_linked_report_requires_explicit_recollection(workspace, zotero_server):
    config, _ = workspace
    zotero_server['fail'] = False
    folder = config.parent / 'run/2026-09-01/reports/linked'
    write_json(folder / 'metadata.json', {'paper': {'arxiv_id': '2609.00001', 'version': 2,
        'title': 'Shared Paper', 'published': '2026-09-01', 'authors': []}})
    (folder / 'report.md').write_text('REPORT', encoding='utf-8')
    (folder / 'report.html').write_text('HTML', encoding='utf-8')
    with TestClient(create_app(config)) as client:
        first = collect(client, zotero='true', obsidian='false', origin='report',
                        report_id='2026-09-01/reports/linked/report.html').json()
        attachments = [key for key, value in zotero_server['items'].items()
                       if value['data'].get('itemType') == 'attachment']
        assert len(attachments) == 1
        del zotero_server['items'][attachments[0]]
        writes = zotero_server['writes']
        retry = client.post('/api/collection/retry', data={'operation_id': first['operation_id']}).json()
        assert retry['targets']['zotero']['status'] == 'needs_attention'
        assert zotero_server['writes'] == writes


def test_restore_at_same_path_invalidates_previous_verification(workspace):
    config, _ = workspace
    with TestClient(create_app(config)) as client:
        first = collect(client).json()
        client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'})
        write_json(config.parent / '.paperloom-restore.json', {'version': 1, 'generation': 'first-restore'})
        state = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert state['links'] == {}
        client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'})
        client.post('/api/collection/retry', data={'operation_id': first['operation_id']})
        write_json(config.parent / '.paperloom-restore.json', {'version': 1, 'generation': 'second-restore'})
        state = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert state['links'] == {}


def test_deleted_zotero_item_requires_selection_before_rebinding(workspace, zotero_server):
    config, _ = workspace
    zotero_server['fail'] = False
    with TestClient(create_app(config)) as client:
        receipt = collect(client, zotero='true', obsidian='false').json()
        old_key = receipt['targets']['zotero']['item_key']
        del zotero_server['items'][old_key]
        zotero_server['items']['REPLACE1'] = {'data': {'key': 'REPLACE1', 'archiveID': '2609.00001', 'title': 'Replacement'}}
        checked = client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'}).json()
        assert checked['associations']['zotero']['status'] == 'needs_attention'
        assert checked['associations']['zotero']['item_key'] == old_key
        selected = client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2', 'selected_key': 'REPLACE1'}).json()
        assert selected['associations']['zotero']['status'] == 'verified'
        retried = client.post('/api/collection/retry', data={'operation_id': receipt['operation_id']}).json()
        assert retried['targets']['zotero']['item_key'] == 'REPLACE1'
        assert retried['status'] == 'succeeded'


def test_downloaded_pdf_is_pinned_across_retries(workspace, zotero_server, monkeypatch):
    config, _ = workspace
    values = yaml.safe_load(config.read_text('utf-8'))
    values['zotero'].update(attach_pdf=True, pdf_attachment_mode='linked_file')
    config.write_text(yaml.safe_dump(values), encoding='utf-8')
    items = json.loads((config.parent / 'run/2026-09-01/recommendations.json').read_text('utf-8'))
    items[0]['paper']['pdf_url'] = 'https://arxiv.org/pdf/2609.00001v2'
    write_json(config.parent / 'run/2026-09-01/recommendations.json', items)
    pdf = config.parent / 'downloaded.pdf'
    pdf.write_bytes(b'%PDF-original-revision')
    # Inject the HTTP transport's downloaded bytes through the existing PDF boundary.
    from contextlib import contextmanager
    @contextmanager
    def download(*args, **kwargs):
        class Response:
            def raise_for_status(self):
                pass
            def iter_bytes(self):
                yield pdf.read_bytes()
        yield Response()
    monkeypatch.setattr('arxiv_ra.web.httpx.stream', download)
    zotero_server['fail'] = False
    with TestClient(create_app(config)) as client:
        first = collect(client, zotero='true', obsidian='false').json()
        assert first['status'] == 'succeeded'
        cached = next((config.parent / 'run/zotero-cache').glob('*.pdf'))
        cached.write_bytes(b'%PDF-modified-revision')
        retry = client.post('/api/collection/retry', data={'operation_id': first['operation_id']}).json()
        assert retry['targets']['zotero']['status'] != 'succeeded'


def test_restore_check_with_disabled_targets_keeps_old_links_unverified(workspace, zotero_server):
    config, _ = workspace
    zotero_server['fail'] = False
    with TestClient(create_app(config)) as client:
        assert collect(client, zotero='true').json()['status'] == 'succeeded'
    values = yaml.safe_load(config.read_text('utf-8'))
    values['zotero']['enabled'] = False
    config.write_text(yaml.safe_dump(values), encoding='utf-8')
    write_json(config.parent / '.paperloom-restore.json', {'generation': 'restored-disabled'})
    with TestClient(create_app(config)) as client:
        client.post('/api/collection/check', data={'arxiv_id': '2609.00001v2'})
        state = client.get('/api/collection', params={'arxiv_id': '2609.00001v2'}).json()
        assert 'zotero' not in state['links']
