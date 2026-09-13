from datetime import timezone
import json

import httpx
import pytest

from arxiv_ra.alphaxiv import AlphaXivClient, AlphaXivUnavailable


def test_discover_papers_uses_mcp_and_normalizes_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
                headers={"Mcp-Session-Id": "session-1"},
            )
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
    assert len(requests) == 2
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
