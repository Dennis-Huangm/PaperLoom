import time
from datetime import datetime, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from arxiv_ra.arxiv_client import ArxivClient
from arxiv_ra.models import Paper, Author
from arxiv_ra.web import create_app


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'output_dir': str(tmp_path / 'run'),
                                    'llm': {'model': 'test-reader'}}), encoding='utf-8')
    def get(_self, aid):
        revision = int(aid.rsplit('v', 1)[1]) if 'v' in aid else 2
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return Paper(arxiv_id=aid.split('v')[0], title='Contrastive learning', authors=[Author(name='A')],
                     abstract='Paired images and text.', published=now, updated=now,
                     categories=['cs.LG'], primary_category='cs.LG',
                     abs_url='', pdf_url='', version=revision)
    monkeypatch.setattr(ArxivClient, 'get', get)
    from arxiv_ra.utils import write_json
    for revision in (1, 2):
        folder = tmp_path / 'run' / '2026-10-01' / 'reports' / ('fixture-v' + str(revision))
        write_json(folder / 'metadata.json', {'paper': get(None, '2401.12345v' + str(revision)).to_dict()})
        (folder / 'report.html').write_text('Report', encoding='utf-8')
        (folder / 'report.md').write_text('Report text', encoding='utf-8')
    return path


def new_chat(client, version=1, **extra):
    response = client.post('/api/reading/sessions', json={'arxiv_id': f'2401.12345v{version}', **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_version_sessions_history_and_restart(config_path):
    with TestClient(create_app(config_path)) as client:
        first = new_chat(client)
        assert new_chat(client)['id'] == first['id']
        second = new_chat(client, 2)
        assert second['id'] != first['id']
        third = new_chat(client, new=True)
        assert third['id'] != first['id']
        assert client.patch('/api/reading/sessions/' + first['id'], json={'title': 'Alignment'}).status_code == 200
        found = client.get('/api/reading/sessions', params={'q': 'Alignment'}).json()
        assert found == []  # Renaming a draft does not make it conversation history.
    with TestClient(create_app(config_path)) as client:
        restored = client.get('/api/reading/sessions/' + first['id']).json()
        assert restored['title'] == 'Alignment'
        assert restored['paper']['version'] == 1
        assert client.delete('/api/reading/sessions/' + first['id']).status_code == 200
        assert client.get('/api/reading/sessions/' + first['id']).status_code == 404
        assert client.get('/api/reading/sessions/' + second['id']).status_code == 200


def wait_answer(client, sid):
    for _ in range(300):
        value = client.get('/api/reading/sessions/' + sid).json()
        if value['messages'] and value['messages'][-1]['status'] not in {'queued', 'running'}:
            return value
        time.sleep(.02)
    pytest.fail('answer did not finish')


def test_sse_delivers_partial_answer_and_reconnects_without_replay(config_path, monkeypatch):
    import json
    import socket
    import threading
    import httpx
    import uvicorn
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    gate=threading.Event()
    calls=[]
    monkeypatch.setenv('LLM_API_KEY','fixture')
    def complete(_self, **kwargs):
        calls.append(1)
        yield NS(choices=[NS(delta=NS(content='先显示的文字',reasoning_content='正在核对',tool_calls=None),finish_reason=None)])
        gate.wait(8)
        yield NS(choices=[NS(delta=NS(content='，后续内容。',tool_calls=None),finish_reason='stop')])
    monkeypatch.setattr(Completions,'create',complete)
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(create_app(config_path),log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    def event(lines):
        kind=''
        for line in lines:
            if line.startswith('event: '):kind=line[7:]
            if line.startswith('data: '):return kind,json.loads(line[6:])
        pytest.fail('stream closed before an event')
    try:
        for _ in range(200):
            if server.started:break
            time.sleep(.01)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=5) as client:
            sid=new_chat(client)['id']
            with client.stream('GET',f'/api/reading/sessions/{sid}/events') as response:
                assert response.headers['content-type'].startswith('text/event-stream')
                lines=response.iter_lines()
                assert event(lines)[0]=='snapshot'
                client.post(f'/api/reading/sessions/{sid}/messages',json={'text':'解释','request_id':'stream'})
                for _ in range(20):
                    _,value=event(lines)
                    answers=[m for m in value['messages'] if m['role']=='assistant']
                    if answers and answers[-1]['text']:break
                assert answers[-1]['status']=='running'
                assert answers[-1]['text']=='先显示的文字'
                assert answers[-1]['reasoning']=='正在核对'
            gate.set()
            assert wait_answer(client,sid)['messages'][-1]['text']=='先显示的文字，后续内容。'
            with client.stream('GET',f'/api/reading/sessions/{sid}/events') as response:
                kind,value=event(response.iter_lines())
                assert kind=='snapshot' and value['messages'][-1]['status']=='completed'
            assert len(calls)==1
    finally:
        gate.set();server.should_exit=True;thread.join(10);sock.close()


def test_rollback_archives_and_clears_future_context(config_path, monkeypatch):
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY','fixture')
    seen=[]
    def complete(_self, **kwargs):
        seen.append(str(kwargs['messages']))
        yield NS(choices=[NS(delta=NS(content=None,tool_calls=None,reasoning_content='先核对论文问题。'),finish_reason=None)])
        yield NS(choices=[NS(delta=NS(content='解释内容',tool_calls=None),finish_reason='stop')])
    monkeypatch.setattr(Completions,'create',complete)
    with TestClient(create_app(config_path)) as client:
        sid=new_chat(client)['id']
        for index,text in enumerate(['前一个问题','需要回退的内容']):
            client.post(f'/api/reading/sessions/{sid}/messages',json={'text':text,'request_id':str(index)})
            value=wait_answer(client,sid)
        assert value['messages'][-1]['reasoning']=='先核对论文问题。'
        assert value['messages'][-1]['context']['messages']>0
        result=client.post(f'/api/reading/sessions/{sid}/rollback',json={'message_id':value['messages'][-1]['id']}).json()
        assert len(result['session']['messages'])==2
        assert result['draft']['text']=='需要回退的内容'
        archive=client.get('/api/reading/sessions/'+result['archive_id']).json()
        assert len(archive['messages'])==4 and archive['archived']
        assert new_chat(client)['id']==sid
        assert client.post('/api/reading/sessions/'+archive['id']+'/messages',json={'text':'不应发送','request_id':'archive'}).status_code==400
        client.post(f'/api/reading/sessions/{sid}/messages',json={'text':'重新提问','request_id':'new'})
        assert wait_answer(client,sid)['messages'][-1]['status']=='completed'
        assert '需要回退的内容' not in seen[-1]
        assert client.post(f'/api/reading/sessions/{sid}/rollback',json={'message_id':value['messages'][-1]['id']}).status_code==400


def test_reasoning_visible_before_answer_and_rollback_rejects_running(config_path, monkeypatch):
    import threading
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    gate=threading.Event()
    monkeypatch.setenv('LLM_API_KEY','fixture')
    def complete(_self, **kwargs):
        yield NS(choices=[NS(delta=NS(content=None,tool_calls=None,reasoning_content='正在核对原文依据'),finish_reason=None)])
        gate.wait(5)
        yield NS(choices=[NS(delta=NS(content='解释完成',tool_calls=None),finish_reason='stop')])
    monkeypatch.setattr(Completions,'create',complete)
    with TestClient(create_app(config_path)) as client:
        sid=new_chat(client)['id']
        client.post(f'/api/reading/sessions/{sid}/messages',json={'text':'解释','request_id':'stream-reasoning'})
        try:
            for _ in range(100):
                value=client.get('/api/reading/sessions/'+sid).json()
                if value['messages'][-1].get('reasoning'):break
                time.sleep(.01)
            answer=value['messages'][-1]
            assert answer['status']=='running' and answer['text']==''
            assert answer['reasoning']=='正在核对原文依据'
            assert answer['context']['characters']>0
            assert client.post(f'/api/reading/sessions/{sid}/rollback',json={'message_id':answer['id']}).status_code==409
        finally:gate.set()
        assert wait_answer(client,sid)['messages'][-1]['status']=='completed'


@pytest.mark.parametrize('failure', ['gateway', 'midstream', 'bad_request', 'persistent'])
def test_reading_transport_recovery_at_http_boundary(config_path, monkeypatch, failure):
    import httpx
    from types import SimpleNamespace as NS
    from openai import InternalServerError, BadRequestError
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    attempts = []
    def chunk(text=None, calls=None, finish='stop'):
        return NS(choices=[NS(delta=NS(content=text, tool_calls=calls), finish_reason=finish)])
    def complete(_self, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            return iter([chunk(calls=[NS(index=0,id='read-1',function=NS(name='metadata',arguments='{}'))],finish='tool_calls')])
        if len(attempts) == 2 or failure == 'persistent':
            response=httpx.Response(502 if failure != 'bad_request' else 400, request=httpx.Request('POST','https://fixture.invalid'))
            exc=(BadRequestError if failure == 'bad_request' else InternalServerError)('private-provider-details',response=response,body=None)
            if failure == 'midstream':
                def interrupted():
                    yield chunk('保留这段已输出的回答',finish=None)
                    raise exc
                return interrupted()
            raise exc
        return iter([chunk('已恢复，继续解释当前论文。')])
    monkeypatch.setattr(Completions, 'create', complete)
    with TestClient(create_app(config_path)) as client:
        sid=new_chat(client)['id']
        client.post(f'/api/reading/sessions/{sid}/messages',json={'text':'解释论文','request_id':'recover'})
        answer=wait_answer(client,sid)['messages'][-1]
        assert 'private-provider-details' not in str(answer)
        if failure == 'gateway':
            assert answer['status']=='completed',answer
            assert len(attempts)==3
            assert attempts[1]==attempts[2], 'Retry only the failed model request'
            assert len(answer['steps'])==1, 'Do not repeat executed tools'
            assert answer['steps'][0]['status']=='completed'
        else:
            assert answer['status']=='failed'
            assert len(attempts)==(4 if failure == 'persistent' else 2)
            if failure == 'persistent':assert 'HTTP 502' in answer['detail']
            if failure == 'midstream':assert answer['text']=='保留这段已输出的回答'


def test_tool_budget_never_reports_empty_answer_as_completed(config_path, monkeypatch):
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY','fixture')
    def complete(_self, **kwargs):
        call=NS(index=0,id='metadata-call',function=NS(name='metadata',arguments='{}'))
        return iter([NS(choices=[NS(delta=NS(content=None,tool_calls=[call]),finish_reason='tool_calls')])])
    monkeypatch.setattr(Completions,'create',complete)
    with TestClient(create_app(config_path)) as client:
        sid=new_chat(client)['id']
        client.post(f'/api/reading/sessions/{sid}/messages',json={'text':'解释论文','request_id':'limit'})
        answer=wait_answer(client,sid)['messages'][-1]
        assert answer['status']=='failed', 'An endpoint ignoring tool_choice must not produce an empty success'


def test_polling_while_streaming_never_races_windows_file_replace(config_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY','fixture')
    def complete(_self, **kwargs):
        for _ in range(100):
            yield NS(choices=[NS(delta=NS(content='流式文字',tool_calls=None),finish_reason=None)])
            time.sleep(.001)
        yield NS(choices=[NS(delta=NS(content=None,tool_calls=None),finish_reason='stop')])
    monkeypatch.setattr(Completions,'create',complete)
    with TestClient(create_app(config_path)) as client:
        sid=new_chat(client)['id']
        client.post(f'/api/reading/sessions/{sid}/messages',json={'text':'解释论文','request_id':'stream'})
        def poll():
            for _ in range(30):
                response=client.get('/api/reading/sessions/'+sid)
                assert response.status_code==200
                assert response.json()['id']==sid
        with ThreadPoolExecutor(max_workers=4) as pool:
            for future in [pool.submit(poll) for _ in range(4)]:future.result()
        assert wait_answer(client,sid)['messages'][-1]['status']=='completed'


def test_agent_reads_pdf_and_persists_verified_evidence(config_path, monkeypatch):
    import fitz
    import json
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions

    def download(_self, paper, destination):
        document = fitz.open()
        document.new_page().insert_text((50, 60), 'Paired images and text are mapped to a shared embedding space.')
        document.save(destination)
        document.close()
        with open(destination, 'ab') as handle:
            handle.write(b'\n%' + b' ' * 11000)
        return destination

    def complete(_self, **kwargs):
        messages = kwargs['messages']
        last_tool = next((m for m in reversed(messages) if m['role'] == 'tool'), None)
        if not last_tool:
            name, arguments = 'read_pages', {'start': 1, 'end': 1}
        elif 'shared embedding' in last_tool['content'] and not 'citation_id' in last_tool['content']:
            name, arguments = 'cite', {'page': 1, 'quote': 'Paired images and text are mapped to a shared embedding space.'}
        else:
            cid = json.loads(last_tool['content'])['citation_id']
            return iter([NS(choices=[NS(delta=NS(content=f'将配对的图文映射到共同空间。[[来源:{cid}]]', tool_calls=None), finish_reason='stop')])])
        call = NS(index=0, id='call-1', function=NS(name=name, arguments=json.dumps(arguments)))
        return iter([NS(choices=[NS(delta=NS(content=None, tool_calls=[call]), finish_reason='tool_calls')])])

    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    monkeypatch.setattr(ArxivClient, 'download_pdf', download)
    monkeypatch.setattr(Completions, 'create', complete)
    with TestClient(create_app(config_path)) as client:
        sid = new_chat(client)['id']
        body = {'text': '这里的对齐是什么意思？', 'request_id': 'first-question'}
        response = client.post(f'/api/reading/sessions/{sid}/messages', json=body)
        assert response.status_code == 200, response.text
        value = wait_answer(client, sid)
        answer = value['messages'][-1]
        assert answer['status'] == 'completed', answer
        assert answer['citations'][0]['page'] == 1
        assert 'shared embedding' in answer['citations'][0]['quote']
        assert '共同空间' in answer['text']
        assert client.get(answer['citations'][0]['url'].split('#')[0]).status_code == 200
        client.post(f'/api/reading/sessions/{sid}/messages', json=body)
        assert len(client.get(f'/api/reading/sessions/{sid}').json()['messages']) == 2
        citation_id = answer['citations'][0]['id']
        def followup(_self, **kwargs):
            return iter([NS(choices=[NS(delta=NS(content=f'继续解释。[[来源:{citation_id}]]', tool_calls=None), finish_reason='stop')])])
        monkeypatch.setattr(Completions, 'create', followup)
        client.post(f'/api/reading/sessions/{sid}/messages', json={'text':'再解释','request_id':'followup'})
        followup_answer = wait_answer(client, sid)['messages'][-1]
        assert followup_answer['citations'][0]['id'] == citation_id
        assert '引用未核实' not in followup_answer['html']


def test_reading_page_settings_and_entry_points(config_path):
    with TestClient(create_app(config_path)) as client:
        page = client.get('/reading?arxiv_id=2401.12345v1')
        assert page.status_code == 200
        assert 'reading-chat.js' in page.text
        assert 'reading-panel' in page.text
        assert '阅读对话' in client.get('/').text
        settings = client.put('/api/reading/settings', json={
            'independent': True, 'model': 'vision-reader', 'base_url': 'http://localhost:11434/v1',
            'api_key': 'fixture-secret', 'images': True, 'max_tools': 6, 'max_tokens': 2000})
        assert settings.status_code == 200, settings.text
        assert 'fixture-secret' not in settings.text
        assert settings.json()['model'] == 'vision-reader'
        assert settings.json()['key_configured'] is True
        assert client.put('/api/reading/settings', json={'max_tools': 0}).status_code == 422


def test_stop_delete_and_no_late_resurrection(config_path, monkeypatch):
    import threading
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    started, release = threading.Event(), threading.Event()
    def complete(_self, **kwargs):
        def chunks():
            yield NS(choices=[NS(delta=NS(content='部分回答', tool_calls=None), finish_reason=None)])
            started.set()
            release.wait(5)
            yield NS(choices=[NS(delta=NS(content='迟到内容', tool_calls=None), finish_reason='stop')])
        return chunks()
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    monkeypatch.setattr(Completions, 'create', complete)
    app = create_app(config_path)
    try:
        with TestClient(app) as client:
            sid = new_chat(client)['id']
            endpoint = f'/api/reading/sessions/{sid}'
            assert client.post(endpoint + '/messages', json={'text': '解释', 'request_id': 'one'}).status_code == 200
            assert started.wait(3)
            assert client.post(endpoint + '/messages', json={'text': '再解释', 'request_id': 'two'}).status_code == 409
            stopped = client.post(endpoint + '/stop').json()
            assert stopped['messages'][-1]['text'] == '部分回答'
            assert stopped['messages'][-1]['status'] == 'stopped'
            assert client.delete(endpoint).status_code == 200
            release.set()
            app.state.jobs.executor.shutdown(wait=True)
            assert client.get(endpoint).status_code == 404
    finally:
        release.set()


def test_missing_version_and_report_snapshot_are_not_silently_replaced(config_path, monkeypatch):
    from arxiv_ra.utils import write_json
    report = config_path.parent / 'run' / '2026-10-03' / 'reports' / 'example'
    report.mkdir(parents=True)
    paper = ArxivClient.get(None, '2401.12345v1').to_dict()
    write_json(report / 'metadata.json', {'paper': paper, 'profile_id': 'default'})
    (report / 'report.md').write_text('# Report\nContrastive learning', encoding='utf-8')
    (report / 'report.html').write_text('old', encoding='utf-8')
    rid = '2026-10-03/reports/example/report.html'
    with TestClient(create_app(config_path)) as client:
        response = client.post('/api/reading/sessions', json={'arxiv_id': '2401.12345', 'report_id': rid})
        assert response.json()['paper']['version'] == 1
        mismatch = client.post('/api/reading/sessions', json={'arxiv_id': '2401.12345v2', 'report_id': rid})
        assert mismatch.status_code == 400
        rendered = client.get('/artifacts/' + rid)
        assert 'reading-chat.js' in rendered.text
        def unavailable(*args):
            raise RuntimeError('offline')
        monkeypatch.setattr(ArxivClient, 'get', unavailable)
        failed = client.post('/api/reading/sessions', json={'arxiv_id': '2501.12345'})
        assert failed.status_code == 400
        assert client.get('/api/reading/sessions').json() == []


def test_budget_tool_scope_and_unverified_links(config_path, monkeypatch):
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    seen = []
    def complete(_self, **kwargs):
        seen.append(kwargs)
        if kwargs['tool_choice'] != 'none':
            call = NS(index=0, id='scope', function=NS(name='read_pages', arguments='{"start":1,"end":1,"path":"another.pdf"}'))
            return iter([NS(choices=[NS(delta=NS(content=None, tool_calls=[call]), finish_reason='tool_calls')])])
        assert any('越界' in m.get('content', '') for m in kwargs['messages'] if m['role'] == 'tool')
        return iter([NS(choices=[NS(delta=NS(content='证据不足。[[来源:invented]] [原文](https://arxiv.org/pdf/fake#page=99)', tool_calls=None), finish_reason='stop')])])
    monkeypatch.setattr(Completions, 'create', complete)
    with TestClient(create_app(config_path)) as client:
        assert client.put('/api/reading/settings', json={'max_tools': 1}).status_code == 200
        sid = new_chat(client)['id']
        client.post(f'/api/reading/sessions/{sid}/messages', json={'text':'核实', 'request_id':'bounded'})
        answer = wait_answer(client, sid)['messages'][-1]
        assert answer['limited'] is True
        assert answer['citations'] == []
        assert '引用未核实' in answer['html']
        assert 'href=' not in answer['html']
        assert len(seen) == 2
        assert client.get('/artifacts/.reading/' + sid + '.json').status_code == 404


def test_long_history_keeps_messages_and_model_attribution(config_path, monkeypatch):
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    captured = []
    def complete(_self, **kwargs):
        captured.append(kwargs)
        if not kwargs.get('stream'):
            return NS(choices=[NS(message=NS(content='问题 0 的概念解释与后续讨论。'))])
        return iter([NS(choices=[NS(delta=NS(content='通俗概念说明。', tool_calls=None), finish_reason='stop')])])
    monkeypatch.setattr(Completions, 'create', complete)
    with TestClient(create_app(config_path)) as client:
        sid = new_chat(client)['id']
        for index in range(9):
            assert client.post(f'/api/reading/sessions/{sid}/messages', json={
                'text': f'问题 {index}', 'request_id': str(index)}).status_code == 200
            value = wait_answer(client, sid)
        assert len(value['messages']) == 18
        assert value['messages'][-1]['summarized'] is True
        assert value['messages'][0]['text'] == '问题 0'
        assert any('较早讨论滚动摘要' in str(m['content']) for m in captured[-1]['messages'])
        client.put('/api/reading/settings', json={'independent':True,'model':'another-model',
            'api_key':'fixture-reader','max_tokens':1000})
        client.post(f'/api/reading/sessions/{sid}/messages', json={'text':'继续','request_id':'new-model'})
        value = wait_answer(client, sid)
        assert value['messages'][-1]['model'] == 'another-model'
        assert value['messages'][1]['model'] == 'test-reader'


def test_unknown_image_capability_fails_without_losing_history(config_path, monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    with TestClient(create_app(config_path)) as client:
        client.put('/api/reading/settings', json={'images':False})
        sid = new_chat(client)['id']
        response = client.post(f'/api/reading/sessions/{sid}/messages', json={
            'text':'看图','request_id':'image','images':['data:image/png;base64,AAAA']})
        assert response.status_code == 400
        assert '图片' in response.json()['detail']
        assert client.get(f'/api/reading/sessions/{sid}').json()['messages'] == []


def test_complete_snapshot_without_version_cannot_bypass_report_gate(config_path):
    from arxiv_ra.utils import write_json
    app = create_app(config_path)
    paper = ArxivClient.get(None, '2401.12345v1').to_dict()
    paper['version'] = None
    # Prepare an existing legacy report snapshot with no version.
    write_json(app.state.output_root / '2026-10-03/reports/legacy/metadata.json', {'paper':paper})
    with TestClient(app) as client:
        response = client.post('/api/reading/sessions', json={'arxiv_id':'2401.12345', 'report_id':'2026-10-03/reports/legacy/report.html'})
        assert response.status_code == 400


def test_image_page_reference_and_image_followup(config_path, monkeypatch):
    import fitz
    import json
    import base64
    from types import SimpleNamespace as NS
    from openai.resources.chat.completions import Completions
    monkeypatch.setenv('LLM_API_KEY','fixture')
    image_doc = fitz.open()
    image_doc.new_page(width=100,height=100).draw_rect((10,10,90,90),color=(1,0,0))
    picture = 'data:image/png;base64,' + base64.b64encode(image_doc[0].get_pixmap().tobytes('png')).decode()
    def download(_self, paper, destination):
        image_doc.save(destination)
        with open(destination,'ab') as handle:
            handle.write(b'\n%' + b' ' * 11000)
    def complete(_self, **kwargs):
        tool = next((m for m in reversed(kwargs['messages']) if m['role']=='tool'),None)
        if tool:
            cid=json.loads(tool['content'])['citation_id']
            return iter([NS(choices=[NS(delta=NS(content=f'图中显示方框。[[来源:{cid}]]',tool_calls=None),finish_reason='stop')])])
        call=NS(index=0,id='image-call',function=NS(name='page_image',arguments='{"page":1}'))
        return iter([NS(choices=[NS(delta=NS(content=None,tool_calls=[call]),finish_reason='tool_calls')])])
    monkeypatch.setattr(ArxivClient,'download_pdf',download)
    monkeypatch.setattr(Completions,'create',complete)
    with TestClient(create_app(config_path)) as client:
        sid=new_chat(client)['id']
        client.post(f'/api/reading/sessions/{sid}/messages',json={'text':'解释图','request_id':'image','images':[picture]})
        answer=wait_answer(client,sid)['messages'][-1]
        assert answer['status']=='completed',answer
        assert answer['citations'][0]['kind']=='page_image'
        assert answer['citations'][0]['page']==1
        captured=[]
        def followup(_self,**kwargs):
            captured.extend(kwargs['messages'])
            return iter([NS(choices=[NS(delta=NS(content='继续解释图片。',tool_calls=None),finish_reason='stop')])])
        monkeypatch.setattr(Completions,'create',followup)
        client.post(f'/api/reading/sessions/{sid}/messages',json={'text':'方框是什么','request_id':'followup-image'})
        wait_answer(client,sid)
        assert any(isinstance(m['content'],list) and any(p['type']=='image_url' for p in m['content']) for m in captured)
    image_doc.close()


def test_process_exit_preserves_partial_answer_without_replay(config_path, monkeypatch):
    import subprocess
    import sys
    from openai.resources.chat.completions import Completions
    with TestClient(create_app(config_path)) as client:
        sid = new_chat(client)['id']
    code = '''
import os,sys,time
from types import SimpleNamespace as NS
from fastapi.testclient import TestClient
from openai.resources.chat.completions import Completions
from arxiv_ra.web import create_app
os.environ['LLM_API_KEY']='fixture'
def create(_self,**kwargs):
    def chunks():
        yield NS(choices=[NS(delta=NS(content='Saved partial answer',tool_calls=None),finish_reason=None)])
        os._exit(77)
    return chunks()
Completions.create=create
with TestClient(create_app(sys.argv[1])) as client:
    client.post('/api/reading/sessions/'+sys.argv[2]+'/messages',json={'text':'Explain','request_id':'crash'})
    time.sleep(5)
'''
    result = subprocess.run([sys.executable, '-c', code, str(config_path), sid], timeout=15, capture_output=True)
    assert result.returncode == 77, result.stderr.decode(errors='replace')
    def forbidden(*args, **kwargs):
        pytest.fail('restarting must not replay model calls')
    monkeypatch.setattr(Completions, 'create', forbidden)
    with TestClient(create_app(config_path)) as client:
        answer = client.get('/api/reading/sessions/' + sid).json()['messages'][-1]
        assert answer['status'] == 'interrupted'
        assert answer['text'] == 'Saved partial answer'


def test_history_excludes_empty_sessions_until_first_message(config_path, monkeypatch):
    from arxiv_ra.reading_chat import ReadingService
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    # Keep the accepted message queued without calling an external model.
    monkeypatch.setattr(ReadingService, 'generate', lambda *args: None)
    with TestClient(create_app(config_path)) as client:
        empty = new_chat(client)
        sid = empty['id']
        assert client.get('/api/reading/sessions').json() == []
        assert new_chat(client)['id'] == sid
        assert client.get('/api/reading/sessions/' + sid).json()['messages'] == []
        response = client.post('/api/reading/sessions/' + sid + '/messages',
                               json={'text': 'Explain the method', 'request_id': 'first-message'})
        assert response.status_code == 200
        assert [row['id'] for row in client.get('/api/reading/sessions').json()] == [sid]
        new_chat(client, new=True)
        assert [row['id'] for row in client.get('/api/reading/sessions').json()] == [sid]
        assert [row['id'] for row in client.get('/api/reading/sessions',
                params={'arxiv_id': '2401.12345', 'q': 'Explain'}).json()] == [sid]
