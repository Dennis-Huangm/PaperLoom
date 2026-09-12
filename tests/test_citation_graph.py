from pathlib import Path

import httpx

from arxiv_ra.citation_graph import CitationExplorer
from arxiv_ra.config import AppConfig, CitationConfig
from arxiv_ra.utils import read_json


def test_citation_map_combines_three_relationship_types(tmp_path: Path) -> None:
    config = AppConfig(
        output_dir=str(tmp_path / "run"),
        citations=CitationConfig(
            max_references=2,
            max_citations=2,
            max_similar=2,
            min_interval=0,
            max_retries=0,
        ),
    )
    explorer = CitationExplorer(config, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paper = {
            "paperId": "S2SEED",
            "title": "Seed Paper",
            "externalIds": {"ArXiv": "2407.05600"},
            "authors": [],
        }
        if path.endswith("/paper/ARXIV:2407.05600"):
            return httpx.Response(200, json=paper)
        if path.endswith("/S2SEED/references"):
            return httpx.Response(200, json={"data": [{"citedPaper": {**paper, "paperId": "REF", "title": "Reference"}}]})
        if path.endswith("/S2SEED/citations"):
            return httpx.Response(200, json={"data": [{"citingPaper": {**paper, "paperId": "CITE", "title": "Citation"}}]})
        if "/forpaper/S2SEED" in path:
            return httpx.Response(200, json={"recommendedPapers": [{**paper, "paperId": "SIM", "title": "Similar"}]})
        if request.method == "POST" and path.endswith("/paper/batch"):
            return httpx.Response(
                200,
                json=[
                    {"paperId": "CITE", "references": [{"paperId": "REF"}]},
                    {"paperId": "REF", "references": []},
                    {"paperId": "SIM", "references": []},
                    {"paperId": "S2SEED", "references": [{"paperId": "REF"}]},
                ],
            )
        return httpx.Response(404)

    explorer.client = httpx.Client(transport=httpx.MockTransport(handler))

    report = explorer.generate("2407.05600")

    graph = read_json(report.parent / "graph.json", {})
    assert report.exists()
    assert graph["references"][0]["title"] == "Reference"
    assert graph["citations"][0]["title"] == "Citation"
    assert graph["similar"][0]["title"] == "Similar"
    assert any(
        edge["source"] == "CITE" and edge["target"] == "REF"
        for edge in graph["citation_edges"]
    )
    assert graph["edges"]
    assert all(edge["kind"] == "similarity" for edge in graph["edges"])
    assert all("score" in edge for edge in graph["edges"])
    assert "Seed Paper" in report.read_text(encoding="utf-8")
    assert 'id="graph"' in report.read_text(encoding="utf-8")
    assert "showLabels=true" in report.read_text(encoding="utf-8")
    assert "#c7650e" in report.read_text(encoding="utf-8")
    assert "如何阅读这张图" in report.read_text(encoding="utf-8")
    assert 'id="toggle-similarity"' in report.read_text(encoding="utf-8")
    assert 'id="toggle-citations"' in report.read_text(encoding="utf-8")
    assert "showSimilarity=true" in report.read_text(encoding="utf-8")

    explorer.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"message": "rate limited"})
        )
    )
    cached_report = explorer.generate("2407.05600")
    cached_graph = read_json(cached_report.parent / "graph.json", {})
    assert len(cached_graph["nodes"]) == len(graph["nodes"])
    assert any(
        edge["kind"] == "cross-citation"
        for edge in cached_graph["citation_edges"]
    )
