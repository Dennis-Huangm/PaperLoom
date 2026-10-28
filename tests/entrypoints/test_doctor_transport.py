import httpx
import pytest

from arxiv_ra.cli import doctor


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('output_dir: run\n', encoding='utf-8')
    monkeypatch.setattr('arxiv_ra.cli._has_module', lambda name: True)
    return path


@pytest.mark.parametrize('error', [
    ImportError('Using SOCKS proxy, but socksio is not installed'),
    ValueError('Unknown scheme for proxy URL socks://private-user:private-secret@example.test'),
])
def test_doctor_fails_when_network_client_cannot_initialize(config, monkeypatch, capsys, error):
    def broken_client(*args, **kwargs):
        raise error
    monkeypatch.setattr(httpx, 'Client', broken_client)
    assert doctor(config) == 1
    output = capsys.readouterr().out
    assert 'HTTP 客户端与代理配置' in output
    assert 'private-secret' not in output
    assert 'private-user' not in output
    if isinstance(error, ImportError):
        assert 'httpx[socks]' in output


def test_doctor_initializes_and_closes_transport_without_network(config, monkeypatch, capsys):
    closed = []
    real_close = httpx.Client.close
    def close(client):
        closed.append(True)
        return real_close(client)
    def unexpected_request(*args, **kwargs):
        pytest.fail('doctor must not make external requests')
    monkeypatch.setattr(httpx.Client, 'close', close)
    monkeypatch.setattr(httpx.Client, 'send', unexpected_request)
    assert doctor(config) == 0
    assert closed == [True]
    assert 'HTTP 客户端与代理配置' in capsys.readouterr().out
