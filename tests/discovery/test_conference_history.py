"""Conference history deletion is explicit, durable and profile scoped."""
import hashlib
import threading
import time

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from arxiv_ra.papers.library import PaperLibraryStore
from arxiv_ra.profiles.profiles import ProfileManager
from arxiv_ra.utils import write_json
from arxiv_ra.web import create_app


def seed_result(output, profile, search_id, *, failed=True):
    path = output / '.conference-search' / hashlib.sha256(profile.encode()).hexdigest() / f'{search_id}.json'
    write_json(path, {'profile_id': profile, 'search_id': search_id,
        'created_at': '2026-10-03T09:54:18+00:00',
        'scope': {'conferences': ['neurips'], 'conference_year_from': 2025, 'conference_year_to': 2026},
        'topic_mode': 'custom', 'topic': '研究主题 ' * 50, 'filtered_count': 0,
        'error': '', 'papers': [], 'sources': {
            'neurips:2026': {'status': 'unpublished' if failed else 'ok',
                'error': "Client error '404 Not Found' for url 'https://example.org/'" if failed else ''}}})
    return path


def test_history_navigation_delete_and_profile_isolation(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('output_dir: run\n', encoding='utf-8')
    app = create_app(config)
    profiles = ProfileManager(tmp_path)
    active = profiles.active_id()
    output = tmp_path / 'run'
    first = seed_result(output, active, 'a' * 32)
    second = seed_result(output, active, 'b' * 32, failed=False)
    foreign = seed_result(output, 'beta', 'c' * 32)
    library = PaperLibraryStore(output, active)
    library.add({'paper': {'arxiv_id': '2407.05600', 'title': 'Saved paper'}}, 'Fixture')
    before = library.state.snapshot()
    report = output / 'saved-report.html'
    report.write_text('Saved report', encoding='utf-8')
    with TestClient(app) as client:
        page = client.get('/conferences?search_id=' + 'a' * 32)
        assert page.status_code == 200
        soup = BeautifulSoup(page.text, 'html.parser')
        assert len(soup.select('.conference-history-item')) == 2
        assert soup.select_one('.conference-history-item.is-current a')['aria-current'] == 'true'
        assert not soup.select_one('.conference-error-details').has_attr('open')
        assert '不代表该范围内没有相关论文' in page.text
        assert client.post('/conferences/' + 'c' * 32 + '/delete', data={'profile_id': active}).status_code == 404
        assert client.post('/conferences/' + 'a' * 32 + '/delete', data={'profile_id': 'beta'}).status_code == 409
        assert client.post('/conferences/not-an-id/delete', data={'profile_id': active}).status_code == 400
        assert client.post('/conferences/' + 'a' * 32 + '/delete', data={
            'profile_id': active, 'selected_search_id': 'invalid'}).status_code == 400
        assert first.exists()
        response = client.post('/conferences/' + 'b' * 32 + '/delete', data={
            'profile_id': active, 'selected_search_id': 'a' * 32}, follow_redirects=False)
        assert response.status_code == 303 and 'search_id=' + 'a' * 32 in response.headers['location']
        assert not second.exists() and first.exists() and foreign.exists()
        response = client.post('/conferences/' + 'a' * 32 + '/delete', data={
            'profile_id': active, 'selected_search_id': 'a' * 32})
        assert response.status_code == 200 and '暂无历史记录' in response.text
        assert '开始一次会议论文检索' in response.text
        assert not first.exists() and foreign.exists()
        assert library.state.snapshot() == before
        assert report.read_text(encoding='utf-8') == 'Saved report'


def test_delete_job_preserves_results_and_rejects_active_or_foreign_jobs(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('output_dir: run\n', encoding='utf-8')
    app = create_app(config)
    active = ProfileManager(tmp_path).active_id()
    result = seed_result(tmp_path / 'run', active, 'd' * 32)
    release = threading.Event()
    with TestClient(app) as client:
        jobs = app.state.jobs
        failed = jobs.submit('conferences', 'Failed conference search',
            lambda: (_ for _ in ()).throw(ValueError('Source unavailable')), profile_id=active)
        deadline = time.monotonic() + 5
        while jobs.get(failed.id).status in {'queued', 'running'} and time.monotonic() < deadline:
            time.sleep(.01)
        assert jobs.get(failed.id).status == 'failed'
        page = client.get('/conferences')
        assert 'Failed conference search' in page.text and 'conference-jobs-data' in page.text
        running = jobs.submit('conferences', 'Still running', lambda: release.wait(5), profile_id=active)
        foreign = jobs.submit('conferences', 'Foreign search', lambda: None, profile_id='beta')
        try:
            assert client.delete('/api/jobs/' + running.id).status_code == 409
            assert client.delete('/api/jobs/' + foreign.id).status_code == 404
            assert 'Foreign search' not in client.get('/conferences').text
            assert client.delete('/api/jobs/' + failed.id).status_code == 200
            assert client.get('/api/jobs/' + failed.id).status_code == 404
            assert not (tmp_path / 'run' / '.jobs' / f'{failed.id}.json').exists()
            assert result.exists()
        finally:
            release.set()
