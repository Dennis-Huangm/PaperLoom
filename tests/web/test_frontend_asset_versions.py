"""Updated templates must not reuse cached application scripts or styles."""
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from paperloom.web import create_app
import pytest


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


@pytest.mark.parametrize(('route', 'script', 'other'), [
    ('/settings', 'settings.js', 'conferences.js'),
    ('/conferences', 'conferences.js', 'settings.js'),
])
def test_page_scripts_are_scoped_and_content_versioned(tmp_path, monkeypatch, route, script, other):
    config = tmp_path / 'config.yaml'
    config.write_text('output_dir: run\n', encoding='utf-8')
    with TestClient(create_app(config)) as client:
        def scripts():
            response = client.get(route)
            assert response.status_code == 200
            soup = BeautifulSoup(response.text, 'html.parser')
            return [tag['src'] for tag in soup.select('script[src]')]

        initial = scripts()
        page_url = next(value for value in initial if urlsplit(value).path == f'/static/{script}')
        assert all(urlsplit(value).path != f'/static/{other}' for value in initial)
        assert initial.index(page_url) > next(i for i, value in enumerate(initial)
                                              if urlsplit(value).path == '/static/app.js')
        assert client.get(page_url).status_code == 200
        original = Path.read_bytes
        monkeypatch.setattr(Path, 'read_bytes', lambda path: original(path) +
                            (b'\n/* page changed */' if path.name == script else b''))
        updated = scripts()
        assert page_url not in updated
        assert [value for value in initial if value != page_url] == [
            value for value in updated if urlsplit(value).path != f'/static/{script}']
