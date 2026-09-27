"""Cross-feature regressions identified by the September 24 core review."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

from arxiv_ra.abstracts import localize_abstracts
from arxiv_ra.config import AppConfig
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.render import markdown_with_math
from arxiv_ra.report import ReportGenerator
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.web import create_app


def paper(version=1):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    return Paper('2407.05600', 'Review paper', [], 'Source abstract', ['cs.AI'], 'cs.AI',
                 now, now, 'https://arxiv.org/abs/2407.05600',
                 'https://arxiv.org/pdf/2407.05600', version)


def test_report_body_and_toc_remove_active_html_and_keep_evidence():
    body, toc = markdown_with_math('''## Heading <img src="x" onerror="alert(1)">
<script>alert(1)</script><iframe src="/settings"></iframe>
[bad](javascript:alert%281%29)
<img src="figure.png" onload="alert(1)">

| Method | Result |
|---|---|
| A | 1 |

$$x^2$$
''')
    for fragment in (body, toc):
        tree = BeautifulSoup(fragment, 'html.parser')
        assert not tree.find(['script', 'iframe', 'object', 'form'])
        assert not any(name.startswith('on') for tag in tree.find_all(True) for name in tag.attrs)
        assert not tree.find('a', href=lambda value: value and value.startswith('javascript:'))
    assert '<table>' in body and 'math-block' in body and 'figure.png' in body


@pytest.mark.parametrize('text', ['', ' \n\t '])
def test_empty_fulltext_does_not_call_model(text):
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value='# Wrong full report'))
    result = ReportGenerator(llm, AppConfig().llm).generate(paper(), VerifiedMetadata(), ParsedPaper(text, []), [])
    assert '摘要级报告' in result
    llm.chat.assert_not_called()


def test_no_new_papers_still_runs_optional_tasks_without_sending_old_email(tmp_path, monkeypatch):
    config = AppConfig(output_dir=str(tmp_path), profile_id='a')
    pipeline = DailyPipeline(config, tmp_path, clients=SimpleNamespace())
    day = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    previous = [{'paper': paper().to_dict()}]
    write_json(tmp_path / day / 'recommendations-a.json', previous)
    monkeypatch.setattr(pipeline, '_rank_candidates', lambda *a, **k: [])
    monkeypatch.setattr(pipeline, '_select_candidates', lambda *a: ([], [], tmp_path / 'state-a.json'))
    integrations = Mock()
    monkeypatch.setattr(pipeline, '_run_optional_integrations', integrations)
    delivery = Mock()
    monkeypatch.setattr('arxiv_ra.pipeline.send_digest', delivery)
    result = pipeline.run(deliver=True)
    assert result.is_file()
    integrations.assert_called_once()
    assert integrations.call_args.args[3] == previous
    delivery.assert_not_called()
    assert read_json(tmp_path / day / 'recommendations-a.json') == previous


def test_fallback_abstract_retries_only_on_explicit_generation(tmp_path, monkeypatch):
    cfg = AppConfig(profile_id='a')
    llm = SimpleNamespace(enabled=True, client=SimpleNamespace(close=lambda: None), chat=Mock(
        side_effect=[TimeoutError('temporary'), '{"papers":[{"id":"2407.05600","abstract_zh":"中文摘要","recommendation_detail":"推荐依据"}]}']))
    monkeypatch.setattr('arxiv_ra.abstracts.LLMClient', lambda _: llm)
    original = {'paper': paper().to_dict()}
    localize_abstracts(cfg, tmp_path, [deepcopy(original)])
    localize_abstracts(cfg, tmp_path, [deepcopy(original)], generate=False)
    assert llm.chat.call_count == 1
    recovered = deepcopy(original)
    localize_abstracts(cfg, tmp_path, [recovered])
    assert recovered['paper']['abstract_zh'] == '中文摘要'
    localize_abstracts(cfg, tmp_path, [deepcopy(original)])
    assert llm.chat.call_count == 2


@pytest.fixture
def web_context(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_text('output_dir: run\ndiscovery:\n  interest_description: alpha\n', encoding='utf-8')
    app = create_app(config)
    profiles = ProfileManager(tmp_path)
    profiles.save({'id': 'beta', 'name': 'Beta'})
    output = tmp_path / 'run'
    for profile in ('alpha', 'beta'):
        for day, version in [('2026-09-23', 1), ('2026-09-24', 2)]:
            write_json(output / day / f'recommendations-{profile}.json', [{'paper': paper(version).to_dict()}])
    monkeypatch.setattr('arxiv_ra.web.localize_abstracts', lambda *a, **k: None)
    with TestClient(app) as client:
        yield client, output, profiles


def test_historical_save_uses_selected_revision_and_source_date(web_context):
    client, output, _ = web_context
    response = client.post('/api/library/toggle', data={'arxiv_id': '2407.05600v1',
        'profile_id': 'alpha', 'origin': 'recommendation', 'source_date': '2026-09-23'})
    assert response.status_code == 200
    saved = PaperLibraryStore(output, 'alpha').all()['2407.05600']
    assert saved['paper']['version'] == 1
    assert saved['source_date'] == '2026-09-23'
    # The next click must remove the base ID, even though UI carries a revision.
    assert client.post('/api/library/toggle', data={'arxiv_id': '2407.05600v1',
        'profile_id': 'alpha', 'origin': 'recommendation', 'source_date': '2026-09-23'}).json()['saved'] is False


@pytest.mark.parametrize('endpoint', ['/api/library/toggle', '/api/library/add', '/api/feedback', '/library/remove', '/api/jobs/report'])
def test_old_tab_cannot_mutate_new_profile(web_context, endpoint):
    client, output, profiles = web_context
    profiles.activate('beta')
    response = client.post(endpoint, data={'arxiv_id': '2407.05600', 'profile_id': 'alpha', 'verdict': 'not_relevant'})
    assert response.status_code == 409
    assert not PaperLibraryStore(output, 'beta').all()


def test_report_save_uses_exact_artifact_without_downgrading_existing(web_context):
    client, output, _ = web_context
    folder = output / '2026-09-24/reports/v3-alpha'
    write_json(folder / 'metadata.json', {'paper': paper(3).to_dict(), 'profile_id': 'alpha'})
    (folder / 'report.html').write_text('Report')
    response = client.post('/api/library/add', data={'arxiv_id': '2407.05600', 'profile_id': 'alpha',
        'origin': 'report', 'report_id': '2026-09-24/reports/v3-alpha/report.html'})
    assert response.status_code == 200
    library = PaperLibraryStore(output, 'alpha')
    assert library.all()['2407.05600']['paper']['version'] == 3
    client.post('/api/library/add', data={'arxiv_id': '2407.05600v1', 'profile_id': 'alpha',
        'origin': 'recommendation', 'source_date': '2026-09-23'})
    assert library.all()['2407.05600']['paper']['version'] == 3


def test_report_artifact_has_csp_and_regeneration_keeps_selected_version(web_context, monkeypatch):
    client, output, _ = web_context
    from arxiv_ra.render import render_report
    folder = output / '2026-09-24/reports/selected'
    write_json(folder / 'metadata.json', {'paper': paper(1).to_dict(), 'profile_id': 'alpha', 'report_quality': 'abstract'})
    report_id = '2026-09-24/reports/selected/report.html'
    render_report('# Test', folder / 'report.html', 'Test', arxiv_id='2407.05600', profile_id='alpha', report_id=report_id)
    page = client.get('/artifacts/' + report_id)
    assert "script-src 'self'" in page.headers['content-security-policy']
    assert 'data-profile-id="alpha"' in page.text
    assert 'data-report-id="' + report_id + '"' in page.text
    assert '摘要级回退' in client.get('/reports').text
    captured = []
    def run(self, aid, snapshot=None):
        captured.append(snapshot.version)
        return folder / 'report.html'
    monkeypatch.setattr(DailyPipeline, 'report_arxiv_id', run)
    response = client.post('/api/jobs/report', data={'arxiv_id': '2407.05600v1', 'profile_id': 'alpha',
        'origin': 'report', 'report_id': report_id})
    assert response.status_code == 202
    # Drain the executor to observe the actual report invocation.
    client.app.state.jobs.executor.shutdown(wait=True)
    assert captured == [1]


def test_zotero_trackable_papers_are_parsed_in_adapter(monkeypatch):
    from arxiv_ra.zotero import ZoteroClient
    from arxiv_ra.config import ZoteroConfig
    adapter = ZoteroClient(ZoteroConfig(), client=SimpleNamespace())
    monkeypatch.setattr(adapter, 'status', lambda: {'ready': True})
    monkeypatch.setattr(adapter, '_get_items', lambda *a: [
        {'data': {'url': 'https://arxiv.org/abs/2407.05600v3', 'title': 'A'}},
        {'data': {'archiveID': '2407.05600v2', 'title': 'A'}},
        {'data': {'extra': 'arXiv: math.GT/0309136v1', 'title': 'B'}},
        {'data': {'url': 'https://other.example/1234.56789', 'title': 'Other'}}])
    assert {value['arxiv_id'] for value in adapter.list_trackable_papers()} == {'2407.05600', 'math.GT/0309136'}


def test_failed_regeneration_retains_previous_full_artifact_and_evidence(tmp_path):
    from arxiv_ra.report_store import matching_report
    from arxiv_ra.web_catalog import preferred_report_index, report_library
    cfg = AppConfig(output_dir=str(tmp_path / 'run'), profile_id='alpha')
    cfg.obsidian.enabled = False
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value='# Successful full report\n\n## 核心方法\nSource evidence'))
    clients = SimpleNamespace(
        llm=llm, reporter=ReportGenerator(llm, cfg.llm),
        verifier=SimpleNamespace(verify=lambda value: VerifiedMetadata(title=value.title)),
        arxiv=SimpleNamespace(download_pdf=lambda value, path: path.write_bytes(b'fixture PDF')),
        parser=SimpleNamespace(parse=lambda *a: ParsedPaper('Original paper text', ['Original paper text'])),
        arxiv_html=SimpleNamespace(fetch=lambda *a: []))
    pipeline = DailyPipeline(cfg, tmp_path, clients=clients)
    directory = tmp_path / 'run/2026-09-24'
    first = pipeline._process_paper(paper(), directory, demo=False)
    old_bytes = first.report_path.read_bytes()
    llm.chat.side_effect = TimeoutError('temporary model failure')
    second = pipeline._process_paper(paper(), directory, demo=False)
    assert first.report_path != second.report_path
    assert first.report_path.read_bytes() == old_bytes
    assert read_json(second.report_path.with_name('metadata.json'))['report_quality'] == 'abstract'
    assert matching_report(pipeline.output_root, 'alpha', paper().to_dict()) == first.report_path
    index = preferred_report_index(report_library(pipeline.output_root), 'alpha')
    assert index[('2407.05600', 1)]['report_path'] == first.report_path.with_suffix('.html')


def test_legacy_report_is_safely_presented_without_mutating_files(web_context):
    client, output, _ = web_context
    folder = output / '2026-09-24/reports/legacy'
    write_json(folder / 'metadata.json', {'paper': paper().to_dict(), 'profile_id': 'alpha'})
    (folder / 'report.md').write_text('# Legacy\n\n<script>unsafe()</script>\n\n$$x^2$$', encoding='utf-8')
    original = '<script>unsafe()</script><p>Old HTML</p>'
    (folder / 'report.html').write_text(original, encoding='utf-8')
    response = client.get('/artifacts/2026-09-24/reports/legacy/report.html')
    assert response.status_code == 200
    assert 'unsafe()' not in response.text
    assert '/static/report.js' in response.text and 'math-block' in response.text
    assert 'data-report-id="2026-09-24/reports/legacy/report.html"' in response.text
    assert (folder / 'report.html').read_text(encoding='utf-8') == original
