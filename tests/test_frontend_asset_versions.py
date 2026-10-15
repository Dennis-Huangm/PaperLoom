"""Updated templates must not reuse cached application scripts or styles."""
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from arxiv_ra.web import create_app


def test_application_asset_urls_change_with_content(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: preview\n", encoding="utf-8")
    app = create_app(config)

    def assets(client):
        response = client.get("/")
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        return {urlsplit(value).path: value for tag in soup.select('script[src], link[href]')
                if (value := tag.get('src') or tag.get('href'))
                and urlsplit(value).path in {'/static/app.js', '/static/app.css'}}

    with TestClient(app) as client:
        initial = assets(client)
        assert assets(client) == initial
        original = Path.read_bytes
        for changed in ['app.js', 'app.css']:
            with monkeypatch.context() as patch:
                patch.setattr(Path, 'read_bytes', lambda path: original(path) +
                              (b'\n/* changed asset */' if path.name == changed else b''))
                updated = assets(client)
                assert updated[f'/static/{changed}'] != initial[f'/static/{changed}']
                for other in {'app.js', 'app.css'} - {changed}:
                    assert updated[f'/static/{other}'] == initial[f'/static/{other}']
