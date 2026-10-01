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


def test_ambiguous_authors_and_budget_are_reported_without_false_matches(tmp_path):
    import httpx
    from arxiv_ra.conference_discovery import ConferenceClient
    fixture = '<collection><volume id="long"><meta><year>2024</year><venue>acl</venue></meta>' + ''.join(
        f'<paper id="{i}"><title>{title}</title><author><first>Ada</first><last>Researcher</last></author></paper>'
        for i, title in enumerate(['Reliable agents', 'Different authors', 'Beyond budget'], 1)) + '</volume></collection>'
    other = paper('2101.00002')
    wrong = paper(); wrong.title = 'Different authors'; wrong.authors = [Author('Bob Researcher')]
    arxiv = SimpleNamespace(find_by_title=Mock(side_effect=[[paper(), other], [wrong]]))
    cfg = DiscoveryConfig(mode='conference', conferences=['ACL'], conference_year_from=2024,
                          conference_year_to=2024, max_candidates=2)
    with ConferenceClient() as directory:
        directory.client.close()
        directory.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=fixture)))
        result = DiscoveryService(cfg, arxiv, None, tmp_path, conferences=directory).discover()
    assert result.papers == []
    status = result.sources['acl:2024']
    assert (status['uncertain'], status['unmatched'], status['not_examined']) == (1, 1, 1)
    assert status['status'] == 'truncated'


def test_manual_browse_is_readonly_until_explicit_collection(tmp_path, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from arxiv_ra.web import create_app
    from arxiv_ra.profiles import ProfileManager
    from arxiv_ra.library import PaperLibraryStore
    from arxiv_ra.research_clients import ResearchClients
    p = paper()
    p.conference_publications = [{'conference': 'acl', 'year': 2024, 'paper_type': 'long',
        'evidence_url': 'https://aclanthology.org/2024.acl-long.1/', 'association_evidence': 'title and authors'}]
    monkeypatch.setattr(ResearchClients, 'conferences', property(lambda self: SimpleNamespace(
        discover=lambda *a, **kw: ([p], {'acl:2024': {'status': 'ok', 'count': 1}}))))
    monkeypatch.setattr(ResearchClients, 'arxiv', property(lambda self: SimpleNamespace()))
    monkeypatch.setattr(ResearchClients, 'alphaxiv', property(lambda self: SimpleNamespace()))
    cfg = tmp_path / 'config.yaml'
    cfg.write_text('output_dir: run\n', encoding='utf-8')
    app = create_app(cfg)
    manager = ProfileManager(tmp_path)
    active = manager.active_id()
    before = cfg.read_bytes()
    library = PaperLibraryStore(tmp_path / 'run', active)
    state = library.state.snapshot()
    with TestClient(app) as client:
        response = client.post('/api/jobs/conferences', data={'profile_id': active, 'mode': 'conference',
            'conferences': 'ACL', 'conference_year_from': 2024, 'conference_year_to': 2024,
            'topic_mode': 'browse', 'max_candidates': 2})
        assert response.status_code == 202
        for _ in range(100):
            job = client.get('/api/jobs/' + response.json()['id']).json()
            if job['status'] in {'succeeded', 'failed', 'completed', 'cancelled'}:
                break
            time.sleep(.02)
        assert job['status'] == 'succeeded', job
        page = client.get('/conferences')
        assert 'Reliable agents' in page.text
        assert cfg.read_bytes() == before and library.state.snapshot() == state
        import re
        search_id = re.search(r'name="search_id" value="([^"]+)"', page.text)[1]
        save = client.post('/conferences/save', data={'profile_id': active,
            'search_id': search_id, 'arxiv_id': p.arxiv_id}, follow_redirects=False)
        assert save.status_code == 303
        assert library.all()[p.arxiv_id]['paper']['conference_publications'][0]['year'] == 2024
        manager.save({'id': 'beta', 'name': 'Beta'}); manager.activate('beta')
        assert client.get('/conferences?search_id=' + search_id).status_code == 404
        assert client.post('/conferences/save', data={'profile_id': active,
            'search_id': search_id, 'arxiv_id': p.arxiv_id}).status_code == 409


def test_cached_candidates_do_not_prevent_progress_to_later_papers(tmp_path):
    import httpx
    from arxiv_ra.conference_discovery import ConferenceClient
    fixture = '<collection><volume id="long"><meta><year>2024</year><venue>acl</venue></meta>' + ''.join(
        f'<paper id="{i}"><title>{title}</title><author><first>Ada</first><last>Researcher</last></author></paper>'
        for i, title in enumerate(['Reliable agents', 'Later agents'], 1)) + '</volume></collection>'
    later = paper('2101.00002'); later.title = 'Later agents'
    arxiv = SimpleNamespace(find_by_title=Mock(side_effect=[[paper()], [later]]))
    cfg = DiscoveryConfig(mode='conference', conferences=['ACL'], conference_year_from=2024,
                          conference_year_to=2024, max_candidates=1)
    with ConferenceClient() as directory:
        directory.client.close()
        directory.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=fixture)))
        service = DiscoveryService(cfg, arxiv, None, tmp_path, conferences=directory)
        first = service.discover()
        assert len(first.papers) == 1
        second = service.discover()
        assert {p.arxiv_id for p in second.papers} == {'2101.00001', '2101.00002'}
        assert second.sources['acl:2024']['not_examined'] == 0
        third = service.discover(excluded_ids={'2101.00001'})
        assert [p.arxiv_id for p in third.papers] == ['2101.00002']
    assert arxiv.find_by_title.call_count == 2


def test_historical_cvpr_reads_each_day_and_preserves_old_evidence_paths(tmp_path):
    import httpx
    from arxiv_ra.conference_discovery import ConferenceClient
    def handler(request):
        if request.url.params.get('day') in ('all', None):
            return httpx.Response(200, text='<a href="CVPR2018.py?day=2018-06-19">Day 1</a><a href="CVPR2018.py?day=2018-06-20">Day 2</a>')
        title = 'Reliable agents' if request.url.params['day'].endswith('19') else 'Later agents'
        return httpx.Response(200, text=f'<dl><dt class="ptitle"><a href="content_cvpr_2018/html/{title.replace(" ", "_")}.html">{title}</a></dt><dd><form class="authsearch"><input name="query" value="Ada Researcher"></form></dd></dl>')
    later = paper('2101.00002'); later.title = 'Later agents'
    cfg = DiscoveryConfig(mode='conference', conferences=['CVPR'], conference_year_from=2018, conference_year_to=2018)
    with ConferenceClient() as directory:
        directory.client.close()
        directory.client = httpx.Client(transport=httpx.MockTransport(handler))
        result = DiscoveryService(cfg, SimpleNamespace(find_by_title=Mock(side_effect=[[paper()], [later]])),
                                  None, tmp_path, conferences=directory).discover()
    assert len(result.papers) == 2
    assert result.sources['cvpr:2018']['directory_count'] == 2
    assert result.papers[0].conference_publications[0]['evidence_url'].startswith('https://openaccess.thecvf.com/content_cvpr_2018/')


def test_pipeline_conference_recommendations_respect_history_and_feedback(tmp_path):
    from arxiv_ra.config import AppConfig
    from arxiv_ra.pipeline import DailyPipeline
    from arxiv_ra.feedback import FeedbackStore
    from arxiv_ra.research_clients import ResearchClients
    from arxiv_ra.utils import write_json
    values = [paper(f'2101.0000{i}') for i in range(1, 4)]
    for p in values:
        p.conference_publications = [{'conference': 'acl', 'year': 2024, 'paper_type': 'long',
            'evidence_url': 'https://aclanthology.org/2024.acl-long.1/', 'association_evidence': 'title and authors'}]
    cfg = AppConfig(output_dir=str(tmp_path), profile_id='a')
    cfg.discovery = DiscoveryConfig(mode='conference', conferences=['ACL'], conference_year_from=2024,
        conference_year_to=2024, min_score=-100, minimum_concept_groups=0)
    cfg.ranking.llm_rerank = False
    write_json(tmp_path / 'state-a.json', {'processed': [values[0].arxiv_id]})
    FeedbackStore(tmp_path, 'a').set(values[1].to_dict(), 'not_relevant')
    clients = ResearchClients(cfg, arxiv=SimpleNamespace(search=Mock(side_effect=AssertionError('recent search'))),
        alphaxiv=SimpleNamespace(), llm=SimpleNamespace(enabled=False), conferences=SimpleNamespace(
            discover=lambda *a, **kw: (values, {'acl:2024': {'status': 'ok', 'count': 3}})))
    pipeline = DailyPipeline(cfg, tmp_path, clients=clients)
    run = tmp_path / '2026-10-01'; run.mkdir()
    selected, _, _ = pipeline._select_candidates(pipeline._rank_candidates(run, False), False)
    assert [p.arxiv_id for p in selected] == [values[2].arxiv_id]
    assert selected[0].conference_publications[0]['year'] == 2024
