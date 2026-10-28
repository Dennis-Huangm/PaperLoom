"""Isolated catalog latency check: python tests/benchmarks/report_catalog.py.

A generous interactive budget, not a timing assertion in the regular test suite.
No user configuration, external accounts or model calls are used.
"""
import json
import statistics
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from paperloom.profiles.profiles import ProfileManager
from paperloom.utils import write_json
from paperloom.web import create_app

with tempfile.TemporaryDirectory(prefix='paperloom-catalog-benchmark-') as directory:
    root = Path(directory)
    config = root / 'config.yaml'
    config.write_text('output_dir: run\n', encoding='utf-8')
    manager = ProfileManager(root)
    manager.save({'id': 'fixture', 'name': 'Fixture',
                  'discovery': {'interest_description': 'Fixture'},
                  'source': {'reference_papers': [
                      {'arxiv_id': f'2609.{index:05d}', 'title': f'Fixture paper {index}',
                       'notes': 'Example reference used to reproduce a large saved research direction.'}
                      for index in range(1800)]}})
    manager.active_path.write_text('fixture', encoding='utf-8')
    for index in range(13):
        folder = root / 'run' / '2026-10-08' / 'reports' / str(index)
        write_json(folder / 'metadata.json', {'profile_id': 'fixture', 'paper': {
            'arxiv_id': f'2609.{index:05d}', 'version': 1, 'title': f'Fixture {index}', 'authors': []}})
        (folder / 'report.html').write_text('fixture', encoding='utf-8')
    with TestClient(create_app(config)) as client:
        durations = []
        for _ in range(5):
            started = time.perf_counter()
            response = client.get('/api/reports')
            durations.append(time.perf_counter() - started)
            assert response.status_code == 200 and len(response.json()['reports']) == 13
        print(json.dumps({'reports': 13, 'reference_papers': 1800,
                          'request_seconds': [round(value, 4) for value in durations],
                          'median_seconds': round(statistics.median(durations), 4)}))
        assert statistics.median(durations) < .3, 'catalog exceeds the 300 ms interactive budget'
