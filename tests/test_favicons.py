from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from arxiv_ra.graph_render import render_graph
from arxiv_ra.graph_store import publish_graph
from arxiv_ra.backup import allowed
from arxiv_ra.render import render_digest, render_recommendations, render_report
from arxiv_ra.web import create_app


def icon_path(document: str, page: Path) -> Path:
    href = BeautifulSoup(document, "html.parser").find("link", rel="icon")["href"]
    return Path(url2pathname(urlsplit(urljoin(page.as_uri(), href)).path))


def test_report_icon_works_as_file_and_for_saved_http_report(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    page = output / "report.html"
    (output / "report.md").write_text("# Example", encoding="utf-8")
    render_report("# Example", page, "Example")
    icon = icon_path(page.read_text(encoding="utf-8"), page)
    assert icon.is_file()

    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run", encoding="utf-8")
    with TestClient(create_app(config)) as client:
        response = client.get("/artifacts/report.html")
        fallback = client.get("/favicon.ico")
        head = client.head("/favicon.ico")
        assert response.status_code == 200
        assert BeautifulSoup(response.text, "html.parser").find("link", rel="icon")["href"] == "/static/app-icon.ico"
        assert client.get("/static/app-icon.ico").content == fallback.content == icon.read_bytes()
        assert head.status_code == 200 and head.headers["content-type"] == "image/x-icon"


def test_daily_pages_reference_a_local_icon(tmp_path):
    for name, render in (("digest.html", render_digest), ("recommendations.html", render_recommendations)):
        page = tmp_path / name
        document = (render([], page, "2026-09-29") if name == "digest.html"
                    else render([], {}, page, "2026-09-29"))
        assert icon_path(document, page).is_file()


def test_graph_icon_works_as_file_and_http_view(tmp_path):
    graph = {"seed": {"title": "Example"}}
    page = publish_graph(tmp_path / "citations" / "example", graph, render_graph)
    assert icon_path(page.read_text(encoding="utf-8"), page) == page.parent / "app-icon.ico"
    assert (page.parent / "app-icon.ico").is_file()
    assert allowed(f"data/citations/example/snapshots/{page.parent.name}/app-icon.ico")
    browser_document = render_graph(graph, "/static/")
    assert BeautifulSoup(browser_document, "html.parser").find("link", rel="icon")["href"] == "/static/app-icon.ico"
