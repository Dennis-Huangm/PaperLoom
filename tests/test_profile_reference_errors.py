from types import SimpleNamespace
import ssl

import httpx
import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.profiles import ProfileGenerator
from arxiv_ra.research_clients import ResearchClients


def generate_with_failure(error):
    def fail(_):
        raise error

    config = AppConfig()
    with ResearchClients(config, arxiv=SimpleNamespace(get=fail),
                         alphaxiv=SimpleNamespace(enabled=False),
                         llm=SimpleNamespace(enabled=False)) as clients:
        return ProfileGenerator(config, clients=clients).generate(
            'reference-error', 'SVG', ['svg'], [], ['2506.03139'])


def test_reference_tls_failure_is_distinguished_from_http_rejection():
    error = httpx.ConnectError('private proxy detail')
    error.__cause__ = ssl.SSLEOFError('handshake ended')
    draft = generate_with_failure(error)
    diagnostic = ' '.join(draft['diagnostics'])
    assert 'TLS' in diagnostic
    assert '尚未收到 HTTP 响应' in diagnostic
    assert '406' not in diagnostic
    assert 'private proxy detail' not in str(draft)
    assert draft['status'] == 'ready'  # User topic is still usable.


@pytest.mark.parametrize('status', [406, 429, 503])
def test_reference_http_error_retains_only_safe_status(status):
    request = httpx.Request('GET', 'https://user:secret@example.test/?api_key=private')
    error = httpx.HTTPStatusError('private response', request=request,
                                 response=httpx.Response(status, request=request))
    draft = generate_with_failure(error)
    assert f'HTTP {status}' in ' '.join(draft['diagnostics'])
    assert 'secret' not in str(draft)
    assert 'private' not in str(draft)
    assert draft['references'][0]['abstract'] == ''


@pytest.mark.parametrize('error, expected', [
    (httpx.ConnectTimeout('secret'), '超时'),
    (httpx.ProxyError('secret'), '代理连接失败'),
    (ImportError('secret'), '依赖'),
    (LookupError('secret'), 'ID 或版本'),
    (ValueError('secret'), '解析'),
    (RuntimeError('secret'), 'RuntimeError'),
])
def test_reference_other_errors_remain_actionable_without_raw_exception(error, expected):
    draft = generate_with_failure(error)
    assert expected in ' '.join(draft['diagnostics'])
    assert 'secret' not in str(draft)
