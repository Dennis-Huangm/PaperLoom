import httpx
import pytest

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


def test_arxiv_client_retries_transient_connection_reset(monkeypatch) -> None:
    client = ArxivClient(max_retries=3, retry_base_delay=1.0)
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
    client = ArxivClient(max_retries=2, retry_base_delay=0)
    request = httpx.Request("GET", "https://export.arxiv.org/api/query")

    def always_reset(_url: str):
        raise httpx.ConnectError("connection reset", request=request)

    monkeypatch.setattr(client.client, "get", always_reset)
    monkeypatch.setattr("arxiv_ra.arxiv_client.time.sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="已自动重试 2 次"):
        client.get("2608.01234")


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
