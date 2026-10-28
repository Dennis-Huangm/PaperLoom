import os

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from paperloom.cli import _load_dotenv
from paperloom.papers.backup import BackupService
from paperloom.config import load_config
from paperloom.runtime.model_access.model_presets import ModelPresets
from paperloom.runtime.model_access.llm import LLMClient
from paperloom.web import create_app
from tests.web.test_web import _write_config


@pytest.fixture
def connection(tmp_path, monkeypatch):
    # Preset writes update the process environment as well as the isolated .env.
    monkeypatch.setattr(os, 'environ', os.environ.copy())
    path = tmp_path / 'config.yaml'
    _write_config(path)
    payload = yaml.safe_load(path.read_text(encoding='utf-8'))
    payload['llm'] = {'model': 'original-model', 'api_key_env': 'PRESET_TEST_KEY',
                      'base_url_env': 'PRESET_TEST_URL', 'temperature': 0.4}
    path.write_text(yaml.safe_dump(payload), encoding='utf-8')
    monkeypatch.setenv('PRESET_TEST_KEY', 'original-secret')
    monkeypatch.setenv('PRESET_TEST_URL', 'https://original.example/v1')
    with TestClient(create_app(path)) as client:
        yield path, client


def saved_entries(path):
    return yaml.safe_load(path.read_text(encoding='utf-8')).get('model_presets', [])


def test_named_connections_roundtrip_and_preserve_snapshots(connection):
    path, client = connection
    assert not saved_entries(path)
    # Capture an existing setup without revealing or resubmitting its secrets.
    response = client.post('/settings/model-presets', data={'model_preset_name': '原有服务'})
    assert response.status_code == 200
    original = saved_entries(path)[0]['id']
    response = client.post('/settings/model-presets', data={
        'model_preset_name': '另一个服务', 'llm_model': 'other-model',
        'llm_api_key': 'other-secret', 'llm_base_url': 'https://other.example/v1/',
    })
    assert response.status_code == 200
    other = saved_entries(path)[1]['id']
    current = load_config(path).llm
    assert current.model == 'other-model'
    assert current.temperature == 0.4 and current.api_key_env == 'PRESET_TEST_KEY'
    assert os.environ['PRESET_TEST_URL'] == 'https://other.example/v1'
    # Editing the live connection must not overwrite either saved snapshot.
    client.post('/settings/credentials', data={'llm_model': 'temporary-model', 'llm_api_key': 'temporary-secret'})
    for preset_id, model, key, url in [
        (original, 'original-model', 'original-secret', 'https://original.example/v1'),
        (other, 'other-model', 'other-secret', 'https://other.example/v1'),
    ]:
        response = client.post('/settings/model-presets/activate', data={'model_preset_id': preset_id})
        assert response.status_code == 200
        assert load_config(path).llm.model == model
        assert os.environ['PRESET_TEST_KEY'] == key
        assert os.environ['PRESET_TEST_URL'] == url
        page = client.get('/settings')
        assert page.headers['cache-control'] == 'no-store'
        assert key not in page.text and url not in page.text
        selected = BeautifulSoup(page.text, 'html.parser').select_one('#model-preset-select option[selected]')
        assert selected['value'] == preset_id
    # Simulate a fresh process loading persisted configuration and credentials.
    for key in list(os.environ):
        if key.startswith(('PAPERLOOM_MODEL_', 'PRESET_TEST_')):
            del os.environ[key]
    _load_dotenv(path.parent / '.env')
    assert ModelPresets(path).list(load_config(path).llm)[1]['active']
    client.post('/settings/model-presets/activate', data={'model_preset_id': original})
    assert os.environ['PRESET_TEST_KEY'] == 'original-secret'
    config_text = path.read_text(encoding='utf-8')
    assert 'original-secret' not in config_text and 'other-secret' not in config_text


def test_switch_clears_credentials_for_official_and_keyless_services(connection):
    path, client = connection
    for name, form in [
        ('官方接口', {'llm_api_key': 'official-secret', 'clear_llm_base_url': 'true'}),
        ('本地接口', {'clear_llm_api_key': 'true', 'llm_base_url': 'http://localhost:11434/v1'}),
    ]:
        client.post('/settings/model-presets', data={'model_preset_name': name, **form})
    official, local = [entry['id'] for entry in saved_entries(path)]
    client.post('/settings/model-presets/activate', data={'model_preset_id': official})
    assert os.environ['PRESET_TEST_KEY'] == 'official-secret'
    assert 'PRESET_TEST_URL' not in os.environ
    client.post('/settings/model-presets/activate', data={'model_preset_id': local})
    assert 'PRESET_TEST_KEY' not in os.environ
    assert os.environ['PRESET_TEST_URL'] == 'http://localhost:11434/v1'


def test_official_preset_uses_default_address_in_new_model_client(connection, monkeypatch):
    path, client = connection
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    client.post('/settings/model-presets', data={
        'model_preset_name': '官方接口', 'llm_api_key': 'official-secret',
        'clear_llm_base_url': 'true',
    })
    preset_id = saved_entries(path)[0]['id']
    client.post('/settings/credentials', data={
        'llm_model': 'local-model', 'llm_base_url': 'http://localhost:11434/v1',
        'clear_llm_api_key': 'true',
    })
    response = client.post('/settings/model-presets/activate', data={'model_preset_id': preset_id})
    assert response.status_code == 200
    model_client = LLMClient(load_config(path).llm)
    try:
        assert str(model_client.client.base_url) == 'https://api.openai.com/v1/'
    finally:
        model_client.client.close()


def test_delete_keeps_live_connection_and_unrelated_credentials(connection):
    path, client = connection
    client.post('/settings/model-presets', data={'model_preset_name': '可删除配置'})
    preset_id = saved_entries(path)[0]['id']
    response = client.post('/settings/model-presets/delete', data={'model_preset_id': preset_id})
    assert response.status_code == 200
    assert not saved_entries(path)
    assert os.environ['PRESET_TEST_KEY'] == 'original-secret'
    assert load_config(path).llm.model == 'original-model'
    assert preset_id.upper() not in (path.parent / '.env').read_text(encoding='utf-8')
    assert all(preset_id.upper() not in key for key in os.environ)


@pytest.mark.parametrize('form', [
    {'model_preset_name': ''}, {'model_preset_name': 'x' * 81},
    {'model_preset_name': 'bad\nname'}, {'model_preset_name': 'name', 'llm_model': ''},
    {'model_preset_name': 'name', 'llm_base_url': 'invalid-url'},
    {'model_preset_name': 'name', 'llm_api_key': 'bad\nsecret'},
])
def test_invalid_save_does_not_mutate_any_configuration(connection, form):
    path, client = connection
    before = path.read_bytes()
    response = client.post('/settings/model-presets', data=form)
    assert response.status_code == 400
    assert path.read_bytes() == before and not (path.parent / '.env').exists()
    assert os.environ['PRESET_TEST_KEY'] == 'original-secret'


def test_duplicate_missing_and_cross_site_requests(connection):
    path, client = connection
    client.post('/settings/model-presets', data={'model_preset_name': 'Service'})
    before = path.read_bytes(), (path.parent / '.env').read_bytes()
    assert client.post('/settings/model-presets', data={'model_preset_name': ' service '}).status_code == 400
    for action in ('activate', 'delete'):
        url = '/settings/model-presets/' + action
        assert client.post(url, data={'model_preset_id': '../unknown'}).status_code == 400
        assert client.post(url, data={'model_preset_id': saved_entries(path)[0]['id']},
                           headers={'Origin': 'https://foreign.example'}).status_code == 403
    assert (path.read_bytes(), (path.parent / '.env').read_bytes()) == before


def test_missing_restored_credentials_cannot_inherit_current_service(connection):
    path, client = connection
    client.post('/settings/model-presets', data={'model_preset_name': '待恢复配置'})
    preset_id = saved_entries(path)[0]['id']
    for key in list(os.environ):
        if key.startswith('PAPERLOOM_MODEL_'):
            del os.environ[key]
    page = client.get('/settings')
    assert '凭据缺失' in page.text
    before = path.read_bytes(), (path.parent / '.env').read_bytes()
    response = client.post('/settings/model-presets/activate', data={'model_preset_id': preset_id})
    assert response.status_code == 400 and '凭据缺失' in response.json()['detail']
    assert (path.read_bytes(), (path.parent / '.env').read_bytes()) == before


@pytest.mark.parametrize('secrets', [False, True])
def test_presets_follow_existing_backup_credentials_option(connection, secrets):
    path, client = connection
    client.post('/settings/model-presets', data={'model_preset_name': '备份服务'})
    service = BackupService(path)
    archive = service.create(secrets=secrets)
    preview = service.preview(archive, 'preset-copy')
    service.restore(archive, 'preset-copy', token=preview['token'])
    restored = service.restores / 'preset-copy' / 'config.yaml'
    for key in list(os.environ):
        if key.startswith(('PAPERLOOM_MODEL_', 'PRESET_TEST_')):
            del os.environ[key]
    _load_dotenv(restored.parent / '.env')
    presets = ModelPresets(restored)
    config = load_config(restored).llm
    entry = presets.list(config)[0]
    assert entry['name'] == '备份服务' and entry['available'] is secrets
    if secrets:
        presets.activate(entry['id'], config)
        assert os.environ['PRESET_TEST_KEY'] == 'original-secret'
    else:
        with pytest.raises(ValueError, match='凭据缺失'):
            presets.activate(entry['id'], config)
