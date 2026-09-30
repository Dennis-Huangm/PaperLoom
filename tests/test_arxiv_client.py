import httpx
import pytest
import ssl

from arxiv_ra.arxiv_client import ArxivClient, parse_feed


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2608.01234v2</id>
    <updated>2026-08-16T01:02:03Z</updated>
    <published>2026-08-15T01:02:03Z</published>
    <title> A Test   Paper </title>
    <summary>We test parsing.</summary>
    <author><name>Ada Example</name></author>
    <category term="cs.AI"/><arxiv:primary_category term="cs.AI"/>
    <link title="pdf" href="https://arxiv.org/pdf/2608.01234v2"/>
    <arxiv:doi>10.0000/example</arxiv:doi>
  </entry>
</feed>"""


def test_parse_feed() -> None:
    papers = parse_feed(ATOM)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.arxiv_id == "2608.01234"
    assert paper.title == "A Test Paper"
    assert paper.primary_category == "cs.AI"
    assert paper.version == 2
    assert paper.doi == "10.0000/example"


def test_search_advertises_standard_https_tls_capability_without_changing_query(monkeypatch):
    """Model the observed cold-query rejection at the client construction seam."""
    factory = httpx.Client
    requests = []
    def create_client(**kwargs):
        context = kwargs.get('verify')
        if context is None:
            context = httpx.create_ssl_context()
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        def server(request):
            requests.append(request)
            # Real 406/cache-MISS and 200/cache-MISS probes are recorded separately.
            return httpx.Response(200, text=ATOM) if context.post_handshake_auth else httpx.Response(406)
        return factory(**kwargs, transport=httpx.MockTransport(server))
    monkeypatch.setattr(httpx, 'Client', create_client)
    client = ArxivClient(min_interval=0)
    try:
        papers = client.search(['cs.CV', 'cs.AI'], 45, 2, ['image generation', 'SVG'])
    finally:
        client.client.close()
    assert len(papers) == 1
    assert len(requests) == 1
    query = requests[0].url.params['search_query']
    assert '(cat:cs.CV OR cat:cs.AI) AND submittedDate:' in query
    assert '(all:"image generation" OR all:"SVG")' in query
    assert requests[0].url.params['max_results'] == '2'


def test_arxiv_tls_still_rejects_invalid_custom_ca_file(tmp_path, monkeypatch):
    path = tmp_path / 'invalid-ca.pem'
    path.write_text('not a certificate', encoding='utf-8')
    monkeypatch.setenv('SSL_CERT_FILE', str(path))
    with pytest.raises(ssl.SSLError):
        ArxivClient()


def test_arxiv_client_retries_transient_connection_reset(monkeypatch) -> None:
    client = ArxivClient(max_retries=3, retry_base_delay=1.0, min_interval=0)
    request = httpx.Request("GET", "https://export.arxiv.org/api/query")
    responses = [
        httpx.ConnectError("connection reset", request=request),
        httpx.ConnectError("connection reset", request=request),
        httpx.Response(200, text=ATOM, request=request),
    ]
    delays: list[float] = []

    def fake_get(_url: str):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(client.client, "get", fake_get)
    monkeypatch.setattr("arxiv_ra.arxiv_client.time.sleep", delays.append)

    paper = client.get("2608.01234")

    assert paper.title == "A Test Paper"
    assert delays == [1.0, 2.0]


def test_arxiv_client_reports_clear_error_after_retries(monkeypatch) -> None:
    client = ArxivClient(max_retries=2, retry_base_delay=0, min_interval=0)
    request = httpx.Request("GET", "https://export.arxiv.org/api/query")

    def always_reset(_url: str):
        raise httpx.ConnectError("connection reset", request=request)

    monkeypatch.setattr(client.client, "get", always_reset)
    monkeypatch.setattr("arxiv_ra.arxiv_client.time.sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="已自动重试 2 次"):
        client.get("2608.01234")


def test_arxiv_client_backoffs_and_reports_rate_limit(monkeypatch) -> None:
    client = ArxivClient(
        max_retries=1,
        retry_base_delay=0,
        min_interval=0,
        rate_limit_key="test-arxiv-429",
    )
    request = httpx.Request("GET", "https://export.arxiv.org/api/query")
    responses = [
        httpx.Response(429, text="Rate exceeded", request=request),
        httpx.Response(429, text="Rate exceeded", request=request),
    ]
    delays: list[float] = []

    monkeypatch.setattr(client.client, "get", lambda _url: responses.pop(0))
    # Both the request backoff and the shared API cooldown use the same
    # process-wide time module, so the spy observes both waits.
    monkeypatch.setattr("time.sleep", delays.append)

    with pytest.raises(RuntimeError, match="当前限流（HTTP 429）"):
        client.get("2608.01234")

    assert delays[0] == 10.0
    assert 9.9 <= delays[1] <= 10.0


def test_pdf_download_replaces_only_after_complete_response(tmp_path) -> None:
    paper = parse_feed(ATOM)[0]
    payload = b"%PDF-1.7\n" + b"x" * (11 * 1024)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    client = ArxivClient()
    client.client.close()
    client.client = httpx.Client(transport=transport)
    destination = tmp_path / "paper.pdf"

    client.download_pdf(paper, destination)

    assert destination.read_bytes() == payload
    assert list(tmp_path.glob("*.part")) == []


def test_too_small_pdf_does_not_replace_existing_file(tmp_path) -> None:
    paper = parse_feed(ATOM)[0]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"not a real PDF")
    )
    client = ArxivClient()
    client.client.close()
    client.client = httpx.Client(transport=transport)
    destination = tmp_path / "paper.pdf"
    destination.write_bytes(b"previous complete PDF")

    with pytest.raises(ValueError, match="响应过小"):
        client.download_pdf(paper, destination)

    assert destination.read_bytes() == b"previous complete PDF"
    assert list(tmp_path.glob("*.part")) == []


class InterruptedPDFStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"%PDF-1.7\n" + b"partial" * 4096
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(received 17825792 bytes, expected 21235990)"
        )


@pytest.mark.parametrize("version_download", [False, True])
def test_pdf_retries_interrupted_body_from_start(tmp_path, monkeypatch, version_download):
    paper = parse_feed(ATOM)[0]
    destination = tmp_path / "paper.pdf"
    payload = b"%PDF-1.7\n" + b"complete" * 2048
    requests = []
    delays = []

    def handler(request):
        requests.append(request)
        assert not destination.exists()
        assert list(tmp_path.glob("*.part")) == []
        if len(requests) < 3:
            return httpx.Response(200, stream=InterruptedPDFStream())
        assert request.headers.get("Range") == "bytes=0-"
        return httpx.Response(206, content=payload, headers={
            "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
        })

    monkeypatch.setattr("arxiv_ra.arxiv_client.time.sleep", delays.append)
    client = ArxivClient(max_retries=3, retry_base_delay=1, min_interval=0)
    client.client.close()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client.client = transport
        if version_download:
            client.download_version(paper.arxiv_id, paper.version, destination)
        else:
            client.download_pdf(paper, destination)

    assert destination.read_bytes() == payload
    assert len(requests) == 3
    assert all(str(request.url) == paper.pdf_url for request in requests)
    assert requests[0].headers.get("Range") is None
    assert delays[:2] == [1, 2]
    assert list(tmp_path.glob("*.part")) == []


@pytest.mark.parametrize("version_download", [False, True])
def test_pdf_exhausted_retries_preserve_existing_file(tmp_path, monkeypatch, version_download):
    paper = parse_feed(ATOM)[0]
    destination = tmp_path / "paper.pdf"
    previous = b"previous complete PDF" if not version_download else b"old small file"
    destination.write_bytes(previous)
    requests = []
    delays = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=InterruptedPDFStream())

    monkeypatch.setattr("arxiv_ra.arxiv_client.time.sleep", delays.append)
    client = ArxivClient(max_retries=2, retry_base_delay=1, min_interval=0)
    client.client.close()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client.client = transport
        with pytest.raises(RuntimeError, match="PDF.*已自动重试 2 次") as error:
            if version_download:
                client.download_version(paper.arxiv_id, paper.version, destination)
            else:
                client.download_pdf(paper, destination)

    assert isinstance(error.value.__cause__, httpx.RemoteProtocolError)
    assert "17825792" in str(error.value)
    assert len(requests) == 3
    assert delays == [1, 2]
    assert destination.read_bytes() == previous
    assert list(tmp_path.glob("*.part")) == []


def test_pdf_does_not_retry_permanent_http_error(tmp_path, monkeypatch):
    requests = []
    delays = []

    def handler(request):
        requests.append(request)
        return httpx.Response(404)

    monkeypatch.setattr("arxiv_ra.arxiv_client.time.sleep", delays.append)
    client = ArxivClient(min_interval=0)
    client.client.close()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client.client = transport
        with pytest.raises(httpx.HTTPStatusError):
            client.download_pdf(parse_feed(ATOM)[0], tmp_path / "paper.pdf")
    assert len(requests) == 1
    assert delays == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("content_range", [
    None, "bytes 100-20000/20001", "bytes 0-12000/24000", "bytes 0-24000/24001",
])
def test_pdf_rejects_incomplete_range_response(tmp_path, monkeypatch, content_range):
    payload = b"%PDF-1.7\n" + b"x" * 16384
    headers = {"Content-Range": content_range} if content_range else {}
    monkeypatch.setattr("arxiv_ra.arxiv_client.time.sleep", lambda _: None)
    client = ArxivClient(max_retries=0, min_interval=0)
    client.client.close()
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(206, content=payload, headers=headers)
    )) as transport:
        client.client = transport
        with pytest.raises(RuntimeError, match="PDF.*已自动重试 0 次"):
            client.download_pdf(parse_feed(ATOM)[0], tmp_path / "paper.pdf")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("date_header", [False, True])
def test_retry_after_long_cooldown_is_not_shortened(monkeypatch, date_header):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    clock = [1000.0]
    calls = []
    fixed_now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now
    monkeypatch.setattr("arxiv_ra.arxiv_client.datetime", FixedDatetime)
    header = format_datetime(fixed_now + timedelta(seconds=120), usegmt=True) if date_header else "120"
    def handler(request):
        calls.append(clock[0])
        return httpx.Response(429, headers={"Retry-After": header}) if len(calls) == 1 else httpx.Response(200)
    client = ArxivClient(max_retries=1, rate_limit_key=f"long-cooldown-{date_header}")
    client.client.close()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client.client = transport
        monkeypatch.setattr("time.monotonic", lambda: clock[0])
        monkeypatch.setattr("time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
        client._get("https://export.arxiv.org/api/query")
    assert 119 <= calls[1] - calls[0] <= 120


def test_get_many_batches_without_truncating_ids():
    from urllib.parse import parse_qs
    batches = []
    def handler(request):
        batches.append(parse_qs(request.url.query.decode())["id_list"][0].split(","))
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
    client = ArxivClient(min_interval=0)
    client.client.close()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client.client = transport
        ids = [f"2609.{i:05}" for i in range(101)]
        client.get_many(ids)
    assert [len(batch) for batch in batches] == [50, 50, 1]
    assert [aid for batch in batches for aid in batch] == ids
