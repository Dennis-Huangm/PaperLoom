"""Material facts are usable without the web presentation adapter."""
from paperloom.utils import write_json


def test_material_snapshot_uses_newer_receipts_without_web_fields(tmp_path):
    from paperloom.versions.version_catalog import tracking_snapshot
    from paperloom.web.web_catalog import version_tracking_data

    write_json(tmp_path / 'version-state-test.json', {'checked_at': '2026-10-01T00:00:00+00:00', 'items': {
        '2501.00001': {'arxiv_id': '2501.00001', 'title': 'Paper', 'sources': ['Zotero'],
                       'latest_version': 3, 'zotero_version': 1, 'zotero_keys': ['KEY'],
                       'zotero_library': 'library', 'zotero_checked_at': '2026-10-01T00:00:00+00:00'}}})
    folder = tmp_path / 'papers/test/2501.00001'
    write_json(folder / 'sync.json', {'arxiv_id': '2501.00001', 'last_target': 3, 'operations': {
        '3': {'status': 'partial', 'options': {'zotero': True}, 'steps': {
            'zotero': {'status': 'succeeded', 'version': 3, 'item_key': 'KEY', 'library': 'library',
                       'verified_at': '2026-10-02T00:00:00+00:00'}}}}})
    item = tracking_snapshot(tmp_path, 'test')['items'][0]
    assert item['zotero_version'] == 3
    assert item['sync_status'] == 'partial'
    assert item['sync_report_path'] == folder / 'index.html'
    assert item['sync_steps'][0]['name'] == 'zotero'
    assert item['sync_steps'][0]['status'] == 'succeeded'
    assert 'sync_url' not in item and 'label' not in item['sync_steps'][0]

    displayed = version_tracking_data(tmp_path, 'test')['items'][0]
    assert displayed['zotero_version'] == 3
    assert displayed['sync_url'] == '/artifacts/papers/test/2501.00001/index.html'
    assert displayed['sync_status'] == '部分完成，可重试'
    assert displayed['sync_steps'][0]['label'] == 'Zotero'
