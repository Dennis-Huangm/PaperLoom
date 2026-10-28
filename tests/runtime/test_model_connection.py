import httpx
import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from openai import OpenAI

from paperloom.runtime.model_access import model_connection
from paperloom import web
from tests.web.test_web import _write_config


@pytest.mark.parametrize('status, text', [(200, '连接成功'), (401, '鉴权失败'), (403, '访问被拒绝'),
                                         (404, '找不到接口或模型'), (429, '限流'), (500, 'HTTP 500')])
def test_probe_provider_responses(monkeypatch, status, text):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={
            'choices': [{'message': {'content': 'OK'}, 'index': 0, 'finish_reason': 'stop'}]
        } if status == 200 else {'error': {'message': 'private-provider-body'}})
    monkeypatch.setattr(model_connection, 'OpenAI', lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond))))
    result = model_connection.probe_connection('example-model', 'fixture-key', 'https://example.test/v1')
    assert text in result['message']
    assert result['ok'] is (status == 200)
    assert len(calls) == 1
    assert calls[0].url.path == '/v1/chat/completions'
    assert 'private-provider-body' not in str(result)
    assert 'fixture-key' not in str(result)


@pytest.mark.parametrize('failure, text', [(httpx.ReadTimeout, '超时'), (httpx.ConnectError, '无法连接')])
def test_probe_network_failures(monkeypatch, failure, text):
    def respond(request):
        raise failure('private-network-message', request=request)
    monkeypatch.setattr(model_connection, 'OpenAI', lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond))))
    result = model_connection.probe_connection('model', 'key', 'https://example.test/v1')
    assert not result['ok'] and text in result['message']
    assert 'private-network-message' not in str(result)


def test_settings_test_and_save_share_current_form_and_custom_mapping(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    _write_config(path)
    config = yaml.safe_load(path.read_text(encoding='utf-8'))
    config['llm'] = {'model': 'old-model', 'api_key_env': 'CUSTOM_KEY', 'base_url_env': 'CUSTOM_URL', 'temperature': 0.3}
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    monkeypatch.setenv('CUSTOM_KEY', 'saved-key')
    monkeypatch.setenv('CUSTOM_URL', 'https://saved.example/v1')
    calls = []
    def probe(*values):
        calls.append(values)
        return {'ok': True, 'message': '连接成功', 'elapsed_ms': 12}
    from paperloom.web.routes import settings as settings_routes
    monkeypatch.setattr(settings_routes, 'probe_connection', probe)
    with TestClient(web.create_app(path)) as client:
        page = BeautifulSoup(client.get('/settings').text, 'html.parser')
        form = page.select_one('#model-connection-form')
        assert form.select_one('[name=llm_model]')
        assert form.select_one('[name=llm_api_key]')
        assert form.select_one('#test-model-connection')
        assert page.select_one('[name=llm_api_key_env]')['type'] == 'hidden'
        assert len(page.select('[name=llm_model]')) == 1
        before = path.read_bytes()
        response = client.post('/settings/connection-test', data={'llm_model': 'new-model', 'llm_api_key': 'draft-key'})
        assert response.json()['ok']
        assert response.headers['cache-control'] == 'no-store'
        assert calls[-1] == ('new-model', 'draft-key', 'https://saved.example/v1')
        assert path.read_bytes() == before and not (tmp_path / '.env').exists()
        client.post('/settings/connection-test', data={'clear_llm_api_key': 'true', 'clear_llm_base_url': 'true'})
        assert calls[-1] == ('old-model', '', '')
        response = client.post('/settings/connection-test', data={'llm_base_url': 'not-a-url'})
        assert response.status_code == 400
        response = client.post('/settings/connection-test', headers={'Origin': 'https://foreign.example'}, data={})
        assert response.status_code == 403
        response = client.post('/settings/credentials', data={'llm_model': 'new-model', 'llm_api_key': 'new-key'}, follow_redirects=False)
        assert response.status_code == 303
    saved = yaml.safe_load(path.read_text(encoding='utf-8'))['llm']
    assert saved['model'] == 'new-model' and saved['temperature'] == 0.3
    assert saved['api_key_env'] == 'CUSTOM_KEY'
    assert 'CUSTOM_KEY=new-key' in (tmp_path / '.env').read_text(encoding='utf-8')


def test_missing_configuration():
    assert not model_connection.probe_connection('model', '', '')['ok']
