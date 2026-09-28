from pathlib import Path

import httpx
import pytest

from arxiv_ra.citation_graph import CitationExplorer
from arxiv_ra.config import AppConfig, CitationConfig
from arxiv_ra.utils import read_json


def test_citation_map_combines_three_relationship_types(tmp_path: Path) -> None:
    config = AppConfig(
        output_dir=str(tmp_path / "run"),
        citations=CitationConfig(
            max_references=2,
            max_citations=2,
            max_similar=2,
            min_interval=0,
            max_retries=0,
        ),
    )
    explorer = CitationExplorer(config, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paper = {
            "paperId": "S2SEED",
            "title": "Seed Paper",
            "abstract": "Visual agents learn image generation through iterative planning and feedback.",
            "externalIds": {"ArXiv": "2407.05600"},
            "authors": [],
        }
        if path.endswith("/paper/ARXIV:2407.05600"):
            return httpx.Response(200, json=paper)
        if path.endswith("/S2SEED/references"):
            return httpx.Response(200, json={"data": [{"citedPaper": {**paper, "paperId": "REF", "externalIds": {}, "title": "Reference"}}]})
        if path.endswith("/S2SEED/citations"):
            return httpx.Response(200, json={"data": [{"citingPaper": {**paper, "paperId": "CITE", "externalIds": {}, "title": "Citation"}}]})
        if "/forpaper/S2SEED" in path:
            return httpx.Response(200, json={"recommendedPapers": [{**paper, "paperId": "SIM", "externalIds": {}, "title": "Similar"}]})
        if request.method == "POST" and path.endswith("/paper/batch"):
            return httpx.Response(
                200,
                json=[
                    {"paperId": "CITE", "references": [{"paperId": "REF"}]},
                    {"paperId": "REF", "references": []},
                    {"paperId": "SIM", "references": []},
                    {"paperId": "S2SEED", "references": [{"paperId": "REF"}]},
                ],
            )
        return httpx.Response(404)

    explorer.client = httpx.Client(transport=httpx.MockTransport(handler))

    report = explorer.generate("2407.05600")

    graph = read_json(report.parent / "graph.json", {})
    assert report.exists()
    assert graph["references"][0]["title"] == "Reference"
    assert graph["citations"][0]["title"] == "Citation"
    assert graph["similar"][0]["title"] == "Similar"
    assert any(
        edge["source"] == "CITE" and edge["target"] == "REF"
        for edge in graph["citation_edges"]
    )
    assert graph["edges"]
    assert all(edge["kind"] == "similarity" for edge in graph["edges"])
    assert all("score" in edge for edge in graph["edges"])
    assert "Seed Paper" in report.read_text(encoding="utf-8")
    assert 'id="graph"' in report.read_text(encoding="utf-8")
    assert "#c7650e" in (report.parent / "graph.js").read_text(encoding="utf-8")
    assert "如何阅读这张图" in report.read_text(encoding="utf-8")
    assert 'id="toggle-similarity"' in report.read_text(encoding="utf-8")
    assert 'id="toggle-citations"' in report.read_text(encoding="utf-8")

    explorer.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"message": "rate limited"})
        )
    )
    cached_report = explorer.generate("2407.05600")
    cached_graph = read_json(cached_report.parent / "graph.json", {})
    assert len(cached_graph["nodes"]) == len(graph["nodes"])
    assert any(
        edge["kind"] == "cross-citation"
        for edge in cached_graph["citation_edges"]
    )


def test_unrelated_recommended_paper_has_no_invented_relationship(tmp_path: Path) -> None:
    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(min_interval=0, max_retries=0))
    def handler(request):
        if "/paper/ARXIV:" in request.url.path:
            return httpx.Response(200, json={"paperId": "S", "title": "Quantum photon entanglement"})
        if "/forpaper/" in request.url.path:
            return httpx.Response(200, json={"recommendedPapers": [{"paperId": "R", "title": "Medieval pottery archaeology"}]})
        return httpx.Response(200, json=[] if request.method == "POST" else {"data": []})
    with CitationExplorer(config, tmp_path) as explorer:
        explorer.client.close()
        explorer.client = httpx.Client(transport=httpx.MockTransport(handler))
        report = explorer.generate("2407.05600")
    graph = read_json(report.parent / "graph.json")
    assert len(graph["nodes"]) == 2
    assert graph["edges"] == []
    assert graph["citation_edges"] == []


def test_snapshot_paginates_and_keeps_previous_publication_on_cancel(tmp_path: Path):
    import pytest
    from arxiv_ra.task_runtime import TaskCancelled, TaskHooks, bind_task_hooks
    from arxiv_ra.web_catalog import citation_library

    calls = []
    def handler(request):
        calls.append(request)
        if '/paper/ARXIV:' in request.url.path:
            return httpx.Response(200, json={'paperId': 'S', 'title': 'Visual planning agents'})
        if request.url.path.endswith('/references'):
            if request.url.params.get('offset') == '1':
                return httpx.Response(200, json={'data': [{'citedPaper': {'paperId': 'B', 'title': 'Visual planning feedback'}}]})
            return httpx.Response(200, json={'next': 1, 'data': [{'citedPaper': {'paperId': 'A', 'title': 'Visual planning systems'}}]})
        if '/forpaper/' in request.url.path:
            return httpx.Response(200, json={'recommendedPapers': []})
        if request.method == 'POST':
            return httpx.Response(200, json=[{'paperId': x, 'references': []} for x in ['S', 'A', 'B']])
        return httpx.Response(200, json={'data': []})

    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(min_interval=0, max_retries=0))
    with CitationExplorer(config, tmp_path) as explorer:
        explorer.client.close()
        explorer.client = httpx.Client(transport=httpx.MockTransport(handler))
        report = explorer.generate('2407.05600')
        graph = read_json(report.parent / 'graph.json')
        assert {n['paperId'] for n in graph['nodes']} == {'S', 'A', 'B'}
        assert graph['schema_version'] == 2
        assert graph['sources']['references']['status'] == 'ok'
        assert graph['sources']['references']['fetched_at']
        before = citation_library(tmp_path)
        cancelled = False
        def progress(detail, percent):
            nonlocal cancelled
            cancelled = percent == 95
        with bind_task_hooks(TaskHooks(progress, lambda *a: None, lambda: cancelled)):
            with pytest.raises(TaskCancelled):
                explorer.generate('2407.05600')
        assert citation_library(tmp_path) == before
        assert report.read_text(encoding='utf-8')


def test_direction_constraints_and_budgets_are_visible(tmp_path: Path):
    from arxiv_ra.config import DiscoveryConfig
    from arxiv_ra.profile_plan import condition
    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(max_nodes=2, max_candidates=3, max_requests=5, min_interval=0, max_retries=2),
                       discovery=DiscoveryConfig(search_plan={'version': 2, 'conditions': [condition('required', 'Must evaluate clinical outcomes')]}))
    def handler(request):
        if '/paper/ARXIV:' in request.url.path:
            return httpx.Response(200, json={'paperId': 'S', 'title': 'Visual planning'})
        if request.url.path.endswith('/references'):
            return httpx.Response(200, json={'data': [{'citedPaper': {'paperId': 'R', 'title': 'Visual planning for medicine', 'abstract': 'Clinical application of visual planning.'}}]})
        if request.method == 'POST':
            return httpx.Response(200, json=[{'paperId': 'S', 'references': []}])
        return httpx.Response(429, headers={'Retry-After': '0'})
    with CitationExplorer(config, tmp_path) as explorer:
        explorer.client.close()
        explorer.client = httpx.Client(transport=httpx.MockTransport(handler))
        report = explorer.generate('2407.05600')
    graph = read_json(report.parent / 'graph.json')
    assert len(graph['nodes']) == 1
    assert graph['pending'][0]['paperId'] == 'R'
    assert graph['budget']['requests_used'] <= 5
    assert graph['status'] == 'partial'
    assert '待判断' in report.read_text(encoding='utf-8')


def test_real_web_job_publishes_readable_csp_safe_map(tmp_path: Path, monkeypatch):
    import json
    import time
    from fastapi.testclient import TestClient
    from arxiv_ra.web import create_app
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('output_dir: run\ncitations:\n  min_interval: 0\n  max_retries: 0\n', encoding='utf-8')
    def handle(self, request):
        if '/paper/ARXIV:' in request.url.path:
            return httpx.Response(200, json={'paperId': 'S', 'title': 'Visual planning <script>alert(1)</script>'}, request=request)
        if '/forpaper/' in request.url.path:
            assert request.url.params['from'] == 'recent'
            return httpx.Response(200, json={'recommendedPapers': [{'paperId': 'R', 'title': 'Visual planning with feedback', 'url': 'javascript:alert(1)'}]}, request=request)
        if request.method == 'POST':
            return httpx.Response(200, json=[{'paperId': pid, 'references': []} for pid in json.loads(request.content)['ids']], request=request)
        return httpx.Response(200, json={'data': []}, request=request)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    app = create_app(config_path)
    with TestClient(app) as client:
        result = client.post('/api/jobs/citation', data={'arxiv_id': '2407.05600'})
        assert result.status_code == 202
        job = result.json()
        for _ in range(300):
            job = client.get('/api/jobs/' + job['id']).json()
            if job['status'] not in {'queued', 'running'}:
                break
            time.sleep(.01)
        assert job['status'] == 'succeeded', job
        report = client.get(job['result_url'])
        assert report.status_code == 200
        assert "script-src 'self'" in report.headers['content-security-policy']
        assert '<script>alert(1)</script>' not in report.text
        assert 'src="/static/graph.js"' in report.text
        assert client.get('/static/graph.js').status_code == 200
        assert 'Visual planning &lt;script&gt;' in client.get('/citations').text
        config_path.write_text('output_dir: run\ncitations:\n  enabled: false\n', encoding='utf-8')
        assert client.post('/api/jobs/citation', data={'arxiv_id': '2407.05600'}).status_code == 409
        assert client.get(job['result_url']).status_code == 200


def test_legacy_map_is_adapted_without_rewriting(tmp_path: Path):
    import json
    from fastapi.testclient import TestClient
    from arxiv_ra.web import create_app
    folder = tmp_path / 'run' / 'citations' / '2407.05600-default'
    folder.mkdir(parents=True)
    payload = {'arxiv_id': '2407.05600', 'seed': {'paperId': 'S', 'title': 'Seed'},
               'nodes': [{'paperId': 'S', 'title': 'Seed', 'roles': ['seed']}, {'paperId': 'R', 'title': 'Other', 'roles': ['similar']}],
               'edges': [{'source': 'S', 'target': 'R', 'kind': 'similarity', 'score': 0}],
               'citation_edges': [{'source': 'S', 'target': 'R', 'kind': 'similar'}]}
    original = json.dumps(payload)
    (folder / 'graph.json').write_text(original, encoding='utf-8')
    (folder / 'index.html').write_text('old page', encoding='utf-8')
    config = tmp_path / 'config.yaml'; config.write_text('output_dir: run', encoding='utf-8')
    with TestClient(create_app(config)) as client:
        response = client.get('/artifacts/citations/2407.05600-default/index.html')
        assert response.status_code == 200
        graph = json.loads(response.text.split('<script id="graph-data" type="application/json">')[1].split('</script>')[0])
        assert graph['edges'] == graph['citation_edges'] == []
        assert graph['status'] == 'legacy'
        assert (folder / 'graph.json').read_text(encoding='utf-8') == original
        payload['schema_version'] = 999
        (folder / 'graph.json').write_text(json.dumps(payload), encoding='utf-8')
        assert client.get('/artifacts/citations/2407.05600-default/index.html').status_code == 422


def test_partial_cross_reference_refresh_preserves_per_paper_cache(tmp_path: Path):
    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(min_interval=0, max_retries=0))
    phase = 0
    def handler(request):
        if '/paper/ARXIV:' in request.url.path:
            return httpx.Response(200, json={'paperId': 'S', 'title': 'Visual planning'})
        if '/forpaper/' in request.url.path:
            return httpx.Response(200, json={'recommendedPapers': [{'paperId': 'R', 'title': 'Visual planning feedback'}]})
        if request.method == 'POST':
            return httpx.Response(200, json=[{'paperId': 'S', 'references': []}, {'paperId': 'R', 'references': [{'paperId': 'S', 'title': 'Visual planning'}]}] if phase == 0 else [{'paperId': 'S', 'references': []}, None])
        return httpx.Response(200, json={'data': []})
    with CitationExplorer(config, tmp_path) as explorer:
        explorer.client.close(); explorer.client = httpx.Client(transport=httpx.MockTransport(handler))
        first = explorer.generate('2407.05600')
        before = read_json(first.parent / 'graph.json')
        phase = 1
        second = explorer.generate('2407.05600')
        after = read_json(second.parent / 'graph.json')
    assert first != second and first.exists()
    assert after['reference_records']['S']['status'] == 'ok'
    assert after['reference_records']['R']['status'] == 'cached'
    assert after['reference_records']['R']['fetched_at'] == before['reference_records']['R']['fetched_at']
    assert any(e['source'] == 'R' and e['target'] == 'S' for e in after['citation_edges'])
    assert after['status'] == 'partial'


def test_low_budget_keeps_all_sources_and_bounds_duplicate_pages(tmp_path: Path):
    import json
    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(max_candidates=3, max_nodes=4, max_requests=5, min_interval=0, max_retries=0))
    seen = []
    def handler(request):
        seen.append(request)
        if '/paper/ARXIV:' in request.url.path:
            return httpx.Response(200, json={'paperId': 'S', 'title': 'Visual planning'})
        if '/forpaper/' in request.url.path:
            return httpx.Response(200, json={'recommendedPapers': [{'paperId': 'M', 'title': 'Visual planning medicine'}]})
        if request.method == 'POST':
            return httpx.Response(200, json=[{'paperId': p, 'references': []} for p in json.loads(request.content)['ids']])
        key, pid = ('citedPaper', 'R') if request.url.path.endswith('references') else ('citingPaper', 'C')
        return httpx.Response(200, json={'next': int(request.url.params.get('offset', 0)) + 1, 'data': [{key: {'paperId': pid, 'title': 'Visual planning research'}}]})
    with CitationExplorer(config, tmp_path) as explorer:
        explorer.client.close(); explorer.client = httpx.Client(transport=httpx.MockTransport(handler))
        path = explorer.generate('2407.05600')
    graph = read_json(path.parent / 'graph.json')
    assert {n['paperId'] for n in graph['nodes']} == {'S', 'R', 'C', 'M'}
    assert len(seen) <= 5
    assert graph['sources']['references']['truncated']


def test_transient_failure_retries_without_exceeding_request_budget(tmp_path: Path):
    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(max_requests=5, min_interval=0, max_retries=1))
    seed_attempts = 0
    def handler(request):
        nonlocal seed_attempts
        if '/paper/ARXIV:' in request.url.path:
            seed_attempts += 1
            if seed_attempts == 1:
                return httpx.Response(503, headers={'Retry-After': '0'})
            return httpx.Response(200, json={'paperId': 'S', 'title': 'Visual planning'})
        if '/forpaper/' in request.url.path:
            return httpx.Response(200, json={'recommendedPapers': []})
        return httpx.Response(200, json=[] if request.method == 'POST' else {'data': []})
    with CitationExplorer(config, tmp_path) as explorer:
        explorer.client.close(); explorer.client = httpx.Client(transport=httpx.MockTransport(handler))
        path = explorer.generate('2407.05600')
    graph = read_json(path.parent / 'graph.json')
    assert seed_attempts == 2
    assert graph['budget']['requests_used'] <= 5
    assert graph['sources']['seed']['status'] == 'ok'


def test_duplicate_identity_and_missing_metadata_stay_readable(tmp_path: Path):
    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(min_interval=0, max_retries=0))
    def handler(request):
        if '/paper/ARXIV:' in request.url.path:
            return httpx.Response(200, json={'paperId': 'S', 'title': 'Visual planning', 'externalIds': {'ArXiv': '2407.05600'}})
        if request.url.path.endswith('references'):
            return httpx.Response(200, json={'data': [{'citedPaper': {'paperId': 'R', 'title': '医学图像研究', 'externalIds': {'DOI': '10.1234/shared'}, 'authors': None, 'year': 'unknown'}}]})
        if '/forpaper/' in request.url.path:
            return httpx.Response(200, json={'recommendedPapers': [{'paperId': 'ALIAS', 'title': '医学图像研究', 'externalIds': {'DOI': '10.1234/SHARED'}, 'abstract': '跨学科的医学图像研究。'}]})
        return httpx.Response(200, json=[] if request.method == 'POST' else {'data': []})
    with CitationExplorer(config, tmp_path) as explorer:
        explorer.client.close(); explorer.client = httpx.Client(transport=httpx.MockTransport(handler))
        path = explorer.generate('2407.05600')
    graph = read_json(path.parent / 'graph.json')
    assert len(graph['nodes']) == 2
    node = graph['nodes'][1]
    assert set(node['roles']) == {'reference', 'similar'}
    assert node['text_status'] == 'unsupported'
    assert node['year'] is None and node['authors'] == []
    assert graph['edges'] == []


def test_job_cancellation_after_publication_keeps_committed_result(tmp_path: Path):
    import threading
    import time
    from arxiv_ra.web_jobs import JobManager
    from arxiv_ra.task_runtime import task_commit
    manager = JobManager(tmp_path, 1)
    published, finish = threading.Event(), threading.Event()
    result = tmp_path / 'citations' / 'fixture' / 'index.html'
    def run():
        with task_commit():
            result.parent.mkdir(parents=True)
            result.write_text('complete publication')
        published.set()
        finish.wait(3)
        return result
    try:
        job = manager.submit('citation', 'fixture', run)
        assert published.wait(3)
        manager.cancel(job.id)
        finish.set()
        for _ in range(300):
            state = manager.get(job.id)
            if state.status in {'succeeded', 'failed', 'cancelled'}:
                break
            time.sleep(.01)
        assert state.status == 'succeeded'
        assert state.result_url and result.exists()
    finally:
        finish.set(); manager.close()


def test_tiny_candidate_cap_queries_each_source_and_merges_aliases(tmp_path: Path):
    import json
    calls = []
    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(max_candidates=1, max_nodes=2, min_interval=0, max_retries=0))
    def handler(request):
        calls.append(request.url.path)
        if '/paper/ARXIV:' in request.url.path:
            return httpx.Response(200, json={'paperId': 'S', 'title': 'Visual planning'})
        paper = {'paperId': 'R', 'title': 'Visual feedback', 'externalIds': {'DOI': '10.1234/shared'}}
        if request.url.path.endswith('references'):
            return httpx.Response(200, json={'data': [{'citedPaper': paper}]})
        if request.url.path.endswith('citations'):
            return httpx.Response(200, json={'data': [{'citingPaper': {**paper, 'paperId': 'ALIAS'}}]})
        if '/forpaper/' in request.url.path:
            return httpx.Response(200, json={'recommendedPapers': [{**paper, 'paperId': 'ALIAS2'}]})
        return httpx.Response(200, json=[{'paperId': p, 'references': []} for p in json.loads(request.content)['ids']])
    with CitationExplorer(config, tmp_path) as explorer:
        explorer.client.close(); explorer.client = httpx.Client(transport=httpx.MockTransport(handler))
        path = explorer.generate('2407.05600')
    graph = read_json(path.parent / 'graph.json')
    assert any('/forpaper/' in path for path in calls)
    assert any(path.endswith('/citations') for path in calls)
    assert len(graph['nodes']) == 2
    assert set(graph['nodes'][1]['roles']) == {'reference', 'citation', 'similar'}


@pytest.mark.parametrize('all_unknown', [False, True])
def test_configured_model_separates_verified_condition_failure_from_unknown(tmp_path: Path, monkeypatch, all_unknown):
    import json
    from arxiv_ra.config import DiscoveryConfig, LLMConfig
    from arxiv_ra.profile_plan import condition
    requirement = condition('required', 'Evaluates clinical outcomes')
    config = AppConfig(output_dir=str(tmp_path), citations=CitationConfig(min_interval=0, max_retries=0),
                       llm=LLMConfig(api_key_env='GRAPH_TEST_KEY', base_url_env='GRAPH_TEST_URL'),
                       discovery=DiscoveryConfig(search_plan={'version': 2, 'conditions': [requirement]}))
    monkeypatch.setenv('GRAPH_TEST_KEY', 'fixture')
    monkeypatch.setenv('GRAPH_TEST_URL', 'https://graph-model.test/v1')
    model_calls = []
    abstracts = {'S':'We evaluate clinical outcomes.', 'PASS':'We evaluate clinical outcomes.',
                 'FAIL':'This study evaluates simulated environments only.', 'UNKNOWN':'Evidence is incomplete.'}
    def paper(pid):
        return {'paperId': pid, 'title': 'Visual planning ' + pid, 'abstract': abstracts[pid]}
    def handle(self, request):
        if request.url.host == 'graph-model.test':
            model_calls.append(request)
            inputs = json.loads(json.loads(request.content)['messages'][1]['content'].split('数据：')[1])
            rows = []
            for p in inputs['papers']:
                pid = p['id']; verdict = 'unknown' if all_unknown else 'not_satisfied' if pid == 'FAIL' else 'unknown' if pid == 'UNKNOWN' else 'satisfied'
                rows.append({'id':pid,'score':8,'reason':'fixture','conditions':[{'id':requirement['id'],'verdict':verdict,'quote':abstracts[pid] if verdict != 'unknown' else '', 'reason':'grounded fixture'}]})
            return httpx.Response(200, json={'id':'fixture','object':'chat.completion','created':0,'model':'fixture',
              'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps({'papers':rows})},'finish_reason':'stop'}]}, request=request)
        if '/paper/ARXIV:' in request.url.path:
            return httpx.Response(200, json=paper('S'), request=request)
        if '/forpaper/' in request.url.path:
            return httpx.Response(200, json={'recommendedPapers':[paper(pid) for pid in ['PASS','FAIL','UNKNOWN']]}, request=request)
        if request.method == 'POST':
            return httpx.Response(200, json=[{'paperId':p,'references':[]} for p in json.loads(request.content)['ids']], request=request)
        return httpx.Response(200, json={'data':[]}, request=request)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    with CitationExplorer(config, tmp_path) as explorer:
        path = explorer.generate('2407.05600')
    graph = read_json(path.parent / 'graph.json')
    assert {n['paperId'] for n in graph['nodes']} == ({'S'} if all_unknown else {'S','PASS'})
    assert {n['paperId'] for n in graph['pending']} == ({'PASS','FAIL','UNKNOWN'} if all_unknown else {'UNKNOWN'})
    assert graph['excluded_count'] == (0 if all_unknown else 1)
    if all_unknown:
        assert graph['sources']['conditions']['status'] == 'unknown'
    assert len(model_calls) == 1
    assert graph['budget']['model_requests_used'] == 1
    assert graph['budget']['requests_used'] == 6
