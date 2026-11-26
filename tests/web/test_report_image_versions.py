"""Replacing a report illustration must invalidate an already cached image URL."""
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from paperloom.reports.render import render_report
from paperloom.web.web_artifacts import ReportStaticFiles


@pytest.mark.parametrize('http_view', [False, True])
def test_report_image_url_changes_when_illustration_is_replaced(tmp_path, http_view):
    source = '![Figure](figure.png?download=1#panel)'
    (tmp_path / 'report.md').write_text(source, encoding='utf-8')
    image = tmp_path / 'figure.png'
    image.write_bytes(b'old illustration')
    destination = tmp_path / 'report.html'
    app = FastAPI()
    app.mount('/artifacts', ReportStaticFiles(directory=tmp_path))
    with TestClient(app) as client:
        def image_url():
            render_report(source, destination, 'Paper')
            document = client.get('/artifacts/report.html').text if http_view else destination.read_text(encoding='utf-8')
            return BeautifulSoup(document, 'html.parser').select_one('article img')['src']

        initial = image_url()
        assert image_url() == initial
        image.write_bytes(b'new illustration')  # Same size: version the content, not just its size.
        updated = image_url()
        assert updated != initial
        assert urlsplit(updated).path == 'figure.png'
        assert parse_qs(urlsplit(updated).query)['download'] == ['1']
        assert urlsplit(updated).fragment == 'panel'
        assert client.get('/artifacts/' + updated).content == b'new illustration'


def test_report_image_versioning_leaves_unavailable_and_external_sources_alone(tmp_path):
    destination = tmp_path / 'report.html'
    sources = ['missing.png', '../outside.png', 'https://example.org/figure.png', '/static/icon.png']
    (tmp_path.parent / 'outside.png').write_bytes(b'outside the report')
    render_report('\n\n'.join(f'![Figure]({src})' for src in sources), destination, 'Paper')
    tree = BeautifulSoup(destination.read_text(encoding='utf-8'), 'html.parser')
    assert [image['src'] for image in tree.select('article img')] == sources
