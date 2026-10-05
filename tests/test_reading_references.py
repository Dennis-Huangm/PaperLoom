"""Public reading boundaries for report-backed, multi-paper conversations."""
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from arxiv_ra.models import Paper, Author
from arxiv_ra.web import create_app
from test_reading_chat import config_path, wait_answer


def report(config_path, aid='2401.12345', version=1):
    folder = config_path.parent / 'run' / '2026-10-05' / 'reports' / (aid + 'v' + str(version))
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    paper = Paper(arxiv_id=aid, title='Paper ' + aid, authors=[Author(name='A')], abstract='Report paper',
                  published=now, updated=now, categories=['cs.AI'], primary_category='cs.AI',
                  abs_url='', pdf_url='', version=version)
    (folder / 'metadata.json').write_text(json.dumps({'paper': paper.to_dict()}), encoding='utf-8')
    (folder / 'report.html').write_text('<p>report</p>', encoding='utf-8')
    (folder / 'report.md').write_text('Report for ' + aid, encoding='utf-8')
    return (folder / 'report.html').relative_to(config_path.parent / 'run').as_posix()


def test_reference_creation_is_report_gated_and_drafts_are_not_history(config_path):
    a, b = report(config_path), report(config_path, '2402.12345')
    with TestClient(create_app(config_path)) as client:
        assert client.post('/api/reading/sessions', json={'arxiv_id':'9999.99999v1'}).status_code == 400
        created = client.post('/api/reading/sessions', json={'report_ids':[a, b], 'mode':'workspace'})
        assert created.status_code == 200, created.text
        value = created.json()
        assert [r['report_id'] for r in value['references']] == [a, b]
        assert client.get('/api/reading/sessions').json() == []
        other = client.post('/api/reading/sessions', json={'report_ids':[a], 'mode':'workspace'}).json()
        assert other['id'] != value['id']
        changed = client.put('/api/reading/sessions/'+value['id']+'/references', json={'report_ids':[b]})
        assert changed.status_code == 200
        assert [r['report_id'] for r in changed.json()['references']] == [b]
        invalid = client.put('/api/reading/sessions/'+value['id']+'/references', json={'report_ids':['../outside']})
        assert invalid.status_code == 400
        assert client.get('/api/reading/sessions/'+value['id']).json()['references'][0]['report_id'] == b


def test_round_scope_is_preserved_and_deleted_report_blocks_retry(config_path, monkeypatch):
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    def complete(_self, **kwargs):
        yield NS(choices=[NS(delta=NS(content='比较结果', tool_calls=None), finish_reason='stop')])
    monkeypatch.setattr(Completions, 'create', complete)
    a, b = report(config_path), report(config_path, '2402.12345')
    with TestClient(create_app(config_path)) as client:
        sid = client.post('/api/reading/sessions', json={'report_ids':[a,b], 'mode':'workspace'}).json()['id']
        url = '/api/reading/sessions/'+sid
        assert client.post(url+'/messages', json={'text':'比较两篇', 'request_id':'first'}).status_code == 200
        value = wait_answer(client, sid)
        assert value['messages'][-1]['status'] == 'completed'
        first = value['messages'][0]
        assert [r['report_id'] for r in first['references']] == [a,b]
        assert all('report' not in r for r in first['references'])
        client.put(url+'/references', json={'report_ids':[a]})
        assert client.get(url).json()['messages'][0]['references'] == first['references']
        (config_path.parent / 'run' / b).unlink()
        assert client.post(url+'/messages', json={'text':'retry', 'request_id':'retry', 'retry_of':first['id']}).status_code == 400
        assert client.post(url+'/messages', json={'text':'只问A', 'request_id':'second'}).status_code == 200
        value = wait_answer(client,sid)
        assert len(value['messages'][-2]['references']) == 1
        client.put(url+'/references',json={'report_ids':[]})
        assert client.post(url+'/messages',json={'text':'没材料', 'request_id':'empty'}).status_code == 400


def test_tools_route_only_to_selected_papers_and_distinguish_citations(config_path):
    import fitz
    from types import SimpleNamespace as NS
    from arxiv_ra.reading_sources import ReferencedSources
    from arxiv_ra.reading_references import ReadingReferences
    root = config_path.parent / 'run'
    a, b = report(config_path), report(config_path, '2402.12345')
    for rid, text in [(a,'Alpha reports accuracy of ninety percent.'),(b,'Beta reports accuracy of eighty percent.')]:
        with fitz.open() as doc:
            doc.new_page().insert_text((50,50),text)
            doc.save(root / rid.replace('report.html','paper.pdf'))
        with (root / rid.replace('report.html','paper.pdf')).open('ab') as handle:
            handle.write(bytes([10,37]) + b'padding' * 1600)
    refs = ReadingReferences(root)
    materials = refs.snapshot(refs.resolve([a,b]))
    sources = ReferencedSources(root, {'paper': materials[0]['paper']}, {'references':materials}, NS(), lambda **kw:None)
    import pytest
    with pytest.raises(ValueError):
        sources.execute('read_pages',{'paper_id':'9999.99999v1','start':1,'end':1})
    alpha = sources.execute('read_pages',{'paper_id':'2401.12345v1','start':1,'end':1})
    assert 'Alpha' in alpha['pages'][0]['text']
    with pytest.raises(ValueError):
        sources.execute('cite',{'paper_id':'2402.12345v1','page':1,'quote':'Beta reports accuracy of eighty percent.'})
    sources.execute('read_pages',{'paper_id':'2402.12345v1','start':1,'end':1})
    one = sources.execute('cite',{'paper_id':'2401.12345v1','page':1,'quote':'Alpha reports accuracy of ninety percent.'})
    two = sources.execute('cite',{'paper_id':'2402.12345v1','page':1,'quote':'Beta reports accuracy of eighty percent.'})
    assert one['paper_id'] != two['paper_id']
    assert one['paper_title'] == 'Paper 2401.12345'
    assert len(sources.citations) == 2


def test_legacy_missing_reports_are_readable_and_workspace_history_stays_out_of_dock(config_path):
    import uuid
    from arxiv_ra.utils import write_json
    from arxiv_ra.arxiv_client import ArxivClient
    root = config_path.parent / 'run'
    sid = uuid.uuid4().hex
    paper = ArxivClient.get(None,'9999.99999v1').to_dict()
    write_json(root / '.reading' / (sid+'.json'), {'format':1,'id':sid,'paper':paper,'title':'Old discussion',
        'updated_at':'2026-10-01T00:00:00Z','messages':[{'id':'old','role':'user','text':'Old question','status':'completed'}]})
    a = report(config_path)
    with TestClient(create_app(config_path)) as client:
        old = client.get('/api/reading/sessions/'+sid).json()
        assert old['messages'][0]['text'] == 'Old question'
        assert old['references'][0]['missing']
        changed = client.put('/api/reading/sessions/'+sid+'/references',json={'report_ids':[a],'mode':'workspace'})
        assert changed.status_code == 200
        assert all(r['id'] != sid for r in client.get('/api/reading/sessions?mode=report').json())
        assert any(r['id'] == sid for r in client.get('/api/reading/sessions').json())


def test_unavailable_pdf_keeps_report_readable_and_context_names_each_paper(config_path, monkeypatch):
    import httpx
    from types import SimpleNamespace as NS
    from arxiv_ra.reading_sources import ReferencedSources
    from arxiv_ra.reading_references import ReadingReferences
    from arxiv_ra.reading_context import make_context
    root = config_path.parent / 'run'
    a, b = report(config_path), report(config_path, '2402.12345')
    refs = ReadingReferences(root)
    materials = refs.snapshot(refs.resolve([a,b]))
    def download(*args):
        raise httpx.ConnectError('offline')
    user = {'text':'compare','selection':'','images':[],'references':materials}
    sources = ReferencedSources(root, {}, user, NS(arxiv=NS(download_pdf=download)), lambda **kw:None)
    result = sources.execute('read_pages',{'paper_id':'2401.12345v1','start':1,'end':1})
    assert 'error' in result and '原文未核实' in result['error']
    assert sources.execute('read_report',{'paper_id':'2401.12345v1'})['text'] == 'Report for 2401.12345'
    context, _ = make_context({'paper':materials[0]['paper'],'messages':[{},{}]}, user, images=True, summarize=lambda s:s)
    assert '2401.12345v1' in context[1]['content'] and '2402.12345v1' in context[1]['content']


def test_streaming_model_receives_multi_paper_tools_and_report_results(config_path, monkeypatch):
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY','fixture')
    calls = []
    def complete(_self, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            functions = [NS(index=i,id='call-'+str(i),function=NS(name='read_report',arguments=json.dumps({'paper_id':pid})))
                         for i,pid in enumerate(['2401.12345v1','2402.12345v1'])]
            yield NS(choices=[NS(delta=NS(content=None,tool_calls=functions),finish_reason='tool_calls')])
        else:
            yield NS(choices=[NS(delta=NS(content='仅依据报告，原文未核实。两篇报告分别讨论不同方法。',tool_calls=None),finish_reason='stop')])
    monkeypatch.setattr(Completions,'create',complete)
    a,b = report(config_path),report(config_path,'2402.12345')
    with TestClient(create_app(config_path)) as client:
        sid=client.post('/api/reading/sessions',json={'mode':'workspace','report_ids':[a,b]}).json()['id']
        assert client.post('/api/reading/sessions/'+sid+'/messages',json={'text':'比较方法','request_id':'compare'}).status_code==200
        value=wait_answer(client,sid)
        assert value['messages'][-1]['status']=='completed'
        results=[json.loads(m['content']) for m in calls[-1]['messages'] if m['role']=='tool']
        assert {r['paper_id'] for r in results}=={'2401.12345v1','2402.12345v1'}
        assert {r['text'] for r in results}=={'Report for 2401.12345','Report for 2402.12345'}
        assert all('paper_id' in t['function']['parameters']['properties'] for t in calls[0]['tools'])
        assert len(value['messages'][-1]['steps'])==2


def test_tools_reject_malformed_paper_targets_as_recoverable_errors(config_path):
    from types import SimpleNamespace as NS
    import pytest
    from arxiv_ra.reading_sources import ReferencedSources
    from arxiv_ra.reading_references import ReadingReferences
    refs=ReadingReferences(config_path.parent/'run')
    materials=refs.snapshot(refs.resolve([report(config_path)]))
    sources=ReferencedSources(config_path.parent/'run',{}, {'references':materials}, NS(), lambda **kw:None)
    for pid in ([], {}, 12, True):
        with pytest.raises(ValueError):
            sources.execute('read_report',{'paper_id':pid})


def test_editing_references_preserves_retained_versions_and_allows_removing_missing_ones(config_path):
    a,b=report(config_path),report(config_path,'2402.12345')
    c=report(config_path,'2403.12345')
    root=config_path.parent/'run'
    with TestClient(create_app(config_path)) as client:
        sid=client.post('/api/reading/sessions',json={'mode':'workspace','report_ids':[a,b]}).json()['id']
        endpoint='/api/reading/sessions/'+sid
        p=root/a.replace('report.html','metadata.json')
        data=json.loads(p.read_text(encoding='utf-8'));data['paper']['version']=2;p.write_text(json.dumps(data),encoding='utf-8')
        changed=client.put(endpoint+'/references',json={'report_ids':[a,b,c]}).json()
        assert changed['references'][0]['paper']['version']==1
        assert changed['references'][0]['missing']
        (root/b).unlink()
        removed=client.put(endpoint+'/references',json={'report_ids':[b,c]})
        assert removed.status_code==200
        assert removed.json()['references'][0]['missing']
        assert client.put(endpoint+'/references',json={'report_ids':[c]}).status_code==200
