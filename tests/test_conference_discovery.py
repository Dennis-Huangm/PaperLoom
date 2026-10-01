from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from arxiv_ra.config import DiscoveryConfig
from arxiv_ra.discovery import DiscoveryService
from arxiv_ra.models import Author, Paper


def paper(aid='2101.00001', published=None):
    old = published or datetime(2021, 1, 1, tzinfo=timezone.utc)
    return Paper(aid, 'Reliable agents', [Author('Ada Researcher')], 'Agent reasoning',
                 ['cs.AI'], 'cs.AI', old, old, f'https://arxiv.org/abs/{aid}',
                 f'https://arxiv.org/pdf/{aid}')


def test_conference_mode_keeps_old_arxiv_upload_and_roundtrips_evidence(tmp_path):
    p = paper()
    p.conference_publications = [{'conference': 'icml', 'year': 2022,
        'paper_type': 'long', 'evidence_url': 'https://proceedings.mlr.press/v162/example.html',
        'association_evidence': 'title and authors'}]
    config = DiscoveryConfig(mode='conference', conferences=['ICML'],
                             conference_year_from=2022, conference_year_to=2022)
    directory = SimpleNamespace(discover=Mock(return_value=([p], {'icml:2022': {'status': 'ok', 'count': 1}})))
    arxiv = SimpleNamespace(search=Mock(side_effect=AssertionError('must not search recent papers')))
    result = DiscoveryService(config, arxiv, None, tmp_path, conferences=directory).discover()
    assert [item.arxiv_id for item in result.papers] == ['2101.00001']
    assert result.papers[0].discovery_sources == ['conference']
    saved = Paper.from_dict(result.papers[0].to_dict())
    assert saved.conference_publications[0]['year'] == 2022
    assert result.sources['icml:2022']['status'] == 'ok'


def test_mixed_merges_same_paper_and_preserves_partial_failure(tmp_path):
    p = paper(published=datetime.now(timezone.utc))
    historic = paper()
    historic.conference_publications = [{'conference': 'icml', 'year': 2022,
        'paper_type': 'long', 'evidence_url': 'https://proceedings.mlr.press/v162/a.html',
        'association_evidence': 'title and authors'}]
    directory = SimpleNamespace(discover=Mock(return_value=([historic], {
        'icml:2022': {'status': 'ok', 'count': 1}, 'iclr:2022': {'status': 'failed', 'error': 'offline'}})))
    arxiv = SimpleNamespace(search=Mock(return_value=[p]), get_many=Mock(return_value=[]))
    cfg = DiscoveryConfig(mode='mixed', conferences=['ICML', 'ICLR'],
        conference_year_from=2022, conference_year_to=2022, alphaxiv_fallback_enabled=False)
    result = DiscoveryService(cfg, arxiv, None, tmp_path, conferences=directory).discover()
    assert len(result.papers) == 1
    assert result.papers[0].published == p.published
    assert result.papers[0].discovery_sources == ['arxiv', 'conference']
    assert result.papers[0].conference_publications[0]['year'] == 2022
    assert result.sources['iclr:2022']['status'] == 'failed'


def test_official_directory_matches_title_and_authors_and_reuses_cache(tmp_path):
    import httpx
    from arxiv_ra.conference_discovery import ConferenceClient
    fixture = '<collection id="2024.acl"><volume id="long"><meta><year>2024</year><venue>acl</venue><booktitle>Long Papers</booktitle></meta><paper id="1"><title>Reliable agents</title><author><first>Ada</first><last>Researcher</last></author></paper></volume><volume id="short"><meta><year>2024</year><venue>acl</venue></meta><paper id="1"><title>Short agents</title></paper></volume></collection>'
    directory = ConferenceClient()
    directory.client.close()
    directory.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=fixture)))
    arxiv = SimpleNamespace(find_by_title=Mock(return_value=[paper()]), search=Mock())
    cfg = DiscoveryConfig(mode='conference', conferences=['ACL'],
        conference_year_from=2024, conference_year_to=2024)
    with directory:
        service = DiscoveryService(cfg, arxiv, None, tmp_path, conferences=directory)
        result = service.discover()
        assert [p.arxiv_id for p in result.papers] == ['2101.00001']
        assert result.sources['acl:2024']['directory_count'] == 1
        assert result.papers[0].conference_publications[0]['evidence_url'] == 'https://aclanthology.org/2024.acl-long.1/'
        second = service.discover()
        assert second.sources['acl:2024']['cached']
        assert second.papers[0].arxiv_id == '2101.00001'
        assert arxiv.find_by_title.call_count == 1


def test_web_can_save_conference_scope_and_reject_invalid_years(tmp_path):
    import re
    from fastapi.testclient import TestClient
    from arxiv_ra.web import create_app
    from arxiv_ra.profiles import ProfileManager
    from arxiv_ra.config import load_config
    cfg = tmp_path / 'config.yaml'
    cfg.write_text('output_dir: run\n', encoding='utf-8')
    app = create_app(cfg)
    manager = ProfileManager(tmp_path)
    with TestClient(app) as client:
        active = manager.active_id()
        page = client.get(f'/profiles/{active}/edit')
        revision = re.search(r'name="profile_revision" value="([^"]+)"', page.text)[1]
        form = dict(profile_id=active, profile_revision=revision, name='Agents', description='Reliable agents',
            keywords='agents', negative_keywords='', categories='cs.AI', lookback_days=2,
            max_candidates=100, recommendation_count=5, mode='conference', conferences=['ACL', 'NIPS'],
            conference_year_from=2022, conference_year_to=2024)
        response = client.post(f'/profiles/{active}/edit', data=form, follow_redirects=False)
        assert response.status_code == 303
        loaded = load_config(cfg).discovery
        assert loaded.mode == 'conference'
        assert loaded.conferences == ['acl', 'neurips']
        assert loaded.conference_year_from == 2022
        assert '会议论文' in client.get(f'/profiles/{active}/edit').text
        assert client.get('/conferences').status_code == 200
        assert client.post('/api/jobs/conferences', data={**form, 'conference_year_from': 2025}).status_code == 400


def test_openreview_uses_api2_for_migrated_year_and_only_accepted_main_papers(tmp_path):
    import httpx
    from arxiv_ra.conference_discovery import ConferenceClient
    def handler(request):
        assert request.url.host == 'api2.openreview.net'
        assert request.url.params['content.venueid'] == 'ICLR.cc/2023/Conference'
        return httpx.Response(200, json={'count': 3, 'notes': [
            {'id': 'accepted', 'content': {k: {'value': v} for k, v in {
                'venueid': 'ICLR.cc/2023/Conference', 'venue': 'ICLR 2023 notable-top-5%',
                'title': 'Reliable agents', 'authors': ['Ada Researcher'], 'abstract': 'Agents'}.items()}},
            {'id': 'rejected', 'content': {'venueid': {'value': 'ICLR.cc/2023/Conference/Rejected_Submission'},
                'venue': {'value': 'ICLR 2023 poster'}, 'title': {'value': 'Rejected'}}},
            {'id': 'workshop', 'content': {'venueid': {'value': 'ICLR.cc/2023/Workshop'},
                'venue': {'value': 'ICLR 2023 poster'}, 'title': {'value': 'Workshop'}}}]})
    directory = ConferenceClient()
    directory.client.close()
    directory.client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = DiscoveryConfig(mode='conference', conferences=['ICLR'], conference_year_from=2023, conference_year_to=2023)
    arxiv = SimpleNamespace(find_by_title=Mock(return_value=[paper()]))
    with directory:
        result = DiscoveryService(cfg, arxiv, None, tmp_path, conferences=directory).discover()
    assert len(result.papers) == 1
    assert result.papers[0].conference_publications[0]['evidence_url'] == 'https://openreview.net/forum?id=accepted'
    assert result.sources['iclr:2023']['directory_count'] == 1


def test_openreview_account_login_is_used_only_for_official_api(tmp_path, monkeypatch):
    import httpx
    from arxiv_ra.conference_discovery import ConferenceClient
    monkeypatch.setenv('OPENREVIEW_USERNAME', 'reader@example.org')
    monkeypatch.setenv('OPENREVIEW_PASSWORD', 'test-only-password')
    def handler(request):
        if request.url.path == '/login':
            import json
            assert request.url.host == 'api2.openreview.net'
            assert json.loads(request.content)['id'] == 'reader@example.org'
            return httpx.Response(200, json={'token': 'test-token'})
        assert request.headers['Authorization'] == 'Bearer test-token'
        assert request.url.params['count'] == 'true'
        return httpx.Response(200, json={'count': 1, 'notes': [{'id': 'accepted', 'content': {
            'venueid': {'value': 'ICLR.cc/2024/Conference'}, 'venue': {'value': 'ICLR 2024 poster'},
            'title': {'value': 'Reliable agents'}, 'authors': {'value': ['Ada Researcher']}}}]})
    directory = ConferenceClient()
    directory.client.close()
    directory.client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = DiscoveryConfig(mode='conference', conferences=['ICLR'], conference_year_from=2024, conference_year_to=2024)
    with directory:
        result = DiscoveryService(cfg, SimpleNamespace(find_by_title=Mock(return_value=[paper()])), None,
                                  tmp_path, conferences=directory).discover()
    assert len(result.papers) == 1
    serialized = str(result.to_dict()) + str(result.papers[0].to_dict())
    assert 'test-token' not in serialized and 'test-only-password' not in serialized
