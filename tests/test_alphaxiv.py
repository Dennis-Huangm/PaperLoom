from datetime import timezone
import json

import httpx
import pytest

from arxiv_ra.alphaxiv import AlphaXivClient, AlphaXivUnavailable, AlphaXivError


def test_discover_papers_uses_mcp_and_normalizes_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}, "protocolVersion": "2025-03-26"}},
                headers={"Mcp-Session-Id": "session-1"},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"papers":[{"arxiv_id":"2608.04436v2","title":"ToolArtist","authors":[{"name":"Researcher"}],"abstract_preview":"Unified tool use.","published":"2026-08-05"},{"arxiv_id":"2608.04436","title":"Duplicate"}]}',
                        }
                    ]
                },
            },
            headers={"Mcp-Session-Id": "session-1"},
        )

    client = AlphaXivClient(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    papers = client.discover(
        keywords=["Agent", "T2I"],
        question="Agentic text-to-image generation",
        published_after="2026-07-01",
        limit=10,
    )

    assert len(papers) == 1
    assert papers[0].arxiv_id == "2608.04436"
    assert papers[0].authors[0].name == "Researcher"
    assert papers[0].published.tzinfo == timezone.utc
    assert papers[0].version == 2
    assert papers[0].metadata_status == "partial"
    assert len(requests) == 3
    assert json.loads(requests[1].content)["method"] == "notifications/initialized"
    assert requests[1].headers["Mcp-Session-Id"] == "session-1"
    assert requests[1].headers["Authorization"] == "Bearer test-key"


def test_discover_requires_api_key() -> None:
    client = AlphaXivClient()

    with pytest.raises(AlphaXivUnavailable, match="未配置 API Key"):
        client.discover(
            keywords=["agent"],
            question="agent research",
            published_after="2026-01-01",
        )


def test_numbered_markdown_results_are_normalized() -> None:
    payload = {
        "text": (
            "1. [ID=2609.05171] **WeAgent-MMGenEdit: A Full-Stack Recipe** "
            "(https://www.alphaxiv.org/abs/2609.05171). Published 2026-09-04 "
            "by Tencent · 14 votes · 59 views: A multimodal agent for image generation.\n"
            "2. [ID=2608.04436] **ToolArtist: Tool-Using Unified Multimodal Models** "
            "(https://www.alphaxiv.org/abs/2608.04436). Published 2026-08-05 "
            "· 37 votes · 350 views: A unified tool-use policy."
        )
    }

    papers = AlphaXivClient._normalize_papers(payload)

    assert [paper.arxiv_id for paper in papers] == ["2609.05171", "2608.04436"]
    assert papers[0].title.startswith("WeAgent-MMGenEdit")
    assert papers[0].abstract == "A multimodal agent for image generation."
    assert papers[0].published.date().isoformat() == "2026-09-04"


def test_discover_excludes_already_recommended_ids() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "1. [ID=2608.04436] **Old Paper** (https://www.alphaxiv.org/abs/2608.04436). Published 2026-08-05 · 1 views: Old.\n2. [ID=2609.09999] **New Paper** (https://www.alphaxiv.org/abs/2609.09999). Published 2026-09-09 · 1 views: New.",
                        }
                    ]
                },
            },
        )

    client = AlphaXivClient(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    papers = client.discover(
        keywords=["agent", "image generation"],
        question="New agent papers",
        published_after="2026-08-01",
        excluded_ids={"2608.04436"},
    )

    assert [paper.arxiv_id for paper in papers] == ["2609.09999"]
    arguments = calls[2]["params"]["arguments"]
    assert "2608.04436" in arguments["question"]


def test_unknown_dates_are_partial_and_old_results_are_filtered(monkeypatch):
    client = AlphaXivClient(api_key="test")
    monkeypatch.setattr(client, "_call_tool", lambda *_args: {"papers": [
        {"arxiv_id": "1706.03762v7", "title": "Unknown date"},
        {"arxiv_id": "1706.03763", "title": "Old", "published": "2017-06-12"},
    ]})
    papers = client.discover(keywords=["agent"], question="test", published_after="2026-09-01")
    assert len(papers) == 1
    assert papers[0].published is None and papers[0].updated is None
    assert papers[0].version == 7 and papers[0].abstract_kind == "preview"
    assert papers[0].to_dict()["published"] == ""


def test_mcp_sse_matches_response_id_and_joins_multiline_data():
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
        'data: {"jsonrpc":"2.0", "id":2,\n'
        'data: "result":{"content":[]}}\n\n'
        'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    ))
    assert AlphaXivClient._response_json(response, 2)["result"] == {"content": []}
    with pytest.raises(AlphaXivError):
        AlphaXivClient._response_json(response, 3)


def test_mcp_rejects_unsupported_protocol_before_tool_call():
    methods = []
    def handler(request):
        methods.append(json.loads(request.content)["method"])
        return httpx.Response(200, json={"id": 1, "result": {"protocolVersion": "unknown"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = AlphaXivClient(api_key="test", client=transport)
        with pytest.raises(AlphaXivError, match="协议版本"):
            client._call_tool("discover_papers", {})
    assert methods == ["initialize"]


def test_unrecognized_result_is_an_error_not_an_empty_search(monkeypatch):
    client = AlphaXivClient(api_key="test")
    monkeypatch.setattr(client, "_call_tool", lambda *_args: {"text": "unexpected server output"})
    with pytest.raises(AlphaXivError, match="格式无法识别"):
        client.discover(keywords=["agent"], question="test", published_after="2026-09-01")
