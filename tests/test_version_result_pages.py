from fastapi import FastAPI
from fastapi.testclient import TestClient

from arxiv_ra.web_artifacts import ReportStaticFiles


def test_existing_tracking_result_uses_task_view(tmp_path):
    folder = tmp_path / 'versions'
    folder.mkdir()
    (folder / 'index-demo.html').write_text('<html>old report layout</html>', encoding='utf-8')
    (folder / 'index-demo.md').write_text(
        '# Demo · arXiv 版本追踪\n\n最近检查：2026-10-06 11:25；追踪 1 篇论文。\n\n'
        '## 本次发现\n\n本次没有发现新版本。\n\n## 正在追踪\n\n'
        '- [Example](https://arxiv.org/abs/2601.00001) · arXiv:2601.00001 · vNone · Zotero\n',
        encoding='utf-8')
    app = FastAPI()
    app.mount('/artifacts', ReportStaticFiles(directory=tmp_path))
    with TestClient(app) as client:
        response = client.get('/artifacts/versions/index-demo.html')
    assert response.status_code == 200
    assert '返回版本追踪' in response.text
    assert 'vNone' not in response.text
    assert '版本待核实' in response.text
    assert '返回报告库' not in response.text
    assert '/static/task-result.css?v=' in response.text
    assert '2601.00001' in response.text


def test_new_check_result_is_scoped_and_has_material_versions(tmp_path):
    from types import SimpleNamespace
    from arxiv_ra.config import AppConfig
    from arxiv_ra.utils import write_json
    from arxiv_ra.version_tracker import VersionTracker
    from test_tracking_scopes import report
    from test_version_sync import Arxiv
    config = AppConfig(output_dir=str(tmp_path), profile_id='test')
    config.zotero.enabled = False
    report(tmp_path)
    write_json(tmp_path / 'version-state-test.json', {'items': {
        '2501.00002': {'arxiv_id': '2501.00002', 'title': 'Other scope', 'sources': ['Zotero']}}})
    with VersionTracker(config, tmp_path, clients=SimpleNamespace(arxiv=Arxiv())) as tracker:
        page = tracker.check(scope='reports', auto_sync=False)
    document = page.read_text(encoding='utf-8')
    assert '返回版本追踪' in document
    assert 'Other scope' not in document
    assert '本地报告 v1' in document
    assert '检查范围：本地报告' in document


def test_sync_and_batch_outputs_use_task_layout(tmp_path):
    from types import SimpleNamespace
    from arxiv_ra.config import AppConfig
    from arxiv_ra.version_batch import VersionSyncBatch
    from test_version_sync import Arxiv
    config = AppConfig(output_dir=str(tmp_path), profile_id='test')
    service = VersionSyncBatch(config, tmp_path)
    state = service.preview(['2501.00001'], [{'arxiv_id': '2501.00001', 'title': 'Example',
        'latest_version': 3, 'local_version': 1}], report=False)
    page = service.run(state['id'], clients=SimpleNamespace(arxiv=Arxiv(), alphaxiv=None))
    assert '返回版本追踪' in page.read_text(encoding='utf-8')
    sync_page = tmp_path / 'papers/test/2501.00001/index.html'
    assert '返回版本追踪' in sync_page.read_text(encoding='utf-8')
    assert 'v3' in sync_page.read_text(encoding='utf-8')
    assert page.with_suffix('.md').is_file()
    assert sync_page.with_suffix('.md').is_file()


def test_legacy_html_receipts_and_diff_keep_content_and_http_assets(tmp_path):
    from pathlib import Path
    from bs4 import BeautifulSoup
    from fastapi.staticfiles import StaticFiles
    package_static = Path(__file__).parents[1] / 'src/arxiv_ra/static'
    paths = ['version-batches/test/batch/index.html', 'papers/test/2501.00001/index.html',
             'versions/test/2501.00001/v1-to-v2/report.html']
    for relative in paths:
        page = tmp_path / relative
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text('<article class="report-article"><h1>Historical task</h1><p>2026-09-01</p>'
            '<h2>v2 · 已完成</h2><ul><li>PDF 已完成</li></ul>'
            '<a href="v2/paper.pdf">打开 PDF</a><script>alert(1)</script>'
            '<img src="x" onerror="alert(2)"></article>', encoding='utf-8')
    app = FastAPI()
    app.mount('/static', StaticFiles(directory=package_static))
    app.mount('/artifacts', ReportStaticFiles(directory=tmp_path))
    with TestClient(app) as client:
        for relative in paths:
            response = client.get('/artifacts/' + relative)
            assert response.status_code == 200
            tree = BeautifulSoup(response.text, 'html.parser')
            assert tree.select_one('.result-panel h2').get_text() == 'v2 · 已完成'
            assert '2026-09-01' in tree.get_text()
            assert tree.find('a', string='打开 PDF')['href'] == 'v2/paper.pdf'
            assert 'onerror=' not in response.text and 'alert(1)' not in response.text
            for asset in tree.select('link[href], script[src]'):
                url = asset.get('href') or asset.get('src')
                assert url.startswith('/static/')
                assert client.get(url).status_code == 200
            assert client.head('/artifacts/' + relative).content == b''
            assert response.headers['cache-control'] == 'no-cache'


def test_legacy_membership_labels_remain_visible(tmp_path):
    folder = tmp_path / 'versions'
    folder.mkdir()
    (folder / 'index-old.html').write_text('old', encoding='utf-8')
    (folder / 'index-old.md').write_text('# Old\n\n## 正在追踪\n\n'
        '- [Paper](https://arxiv.org/abs/2501.00001) · arXiv:2501.00001 · v1 · 文献库, 已同步文件\n', encoding='utf-8')
    app = FastAPI()
    app.mount('/artifacts', ReportStaticFiles(directory=tmp_path))
    with TestClient(app) as client:
        response = client.get('/artifacts/versions/index-old.html')
    assert '文献库, 已同步文件' in response.text
