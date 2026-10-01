import os
import subprocess
import sys

import pytest


def test_semantic_scholar_serializes_independent_processes(tmp_path):
    barrier = tmp_path / 'start'
    code = '''import sys,time
from pathlib import Path
from arxiv_ra.rate_limit import shared_rate_limit
while not Path(sys.argv[1]).exists(): time.sleep(.01)
with shared_rate_limit("semantic-scholar", 0): print(time.time(), flush=True)
'''
    env = {**os.environ, 'ARXIV_RA_RATE_LIMIT_DIR': str(tmp_path / 'limits')}
    children = [subprocess.Popen([sys.executable, '-c', code, str(barrier)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)]
    barrier.touch()
    try:
        times = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stderr
            times.append(float(stdout.strip()))
        assert abs(times[0] - times[1]) >= 1.15
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_semantic_retry_each_acquires_slot(monkeypatch):
    from contextlib import contextmanager
    from arxiv_ra.config import MetadataConfig
    from arxiv_ra.metadata import MetadataVerifier
    from types import SimpleNamespace
    slots = []

    @contextmanager
    def slot(key, interval):
        slots.append(key)
        yield

    class Client:
        def get(self, *_args, **kwargs):
            if len(slots) == 1:
                return SimpleNamespace(status_code=429, headers={'Retry-After': '0'})
            return SimpleNamespace(status_code=200, json=lambda: {'title': 'Test'})

    monkeypatch.setattr('arxiv_ra.metadata.shared_rate_limit', slot)
    monkeypatch.setattr('arxiv_ra.metadata.time.sleep', lambda _: None)
    monkeypatch.setattr('arxiv_ra.metadata.defer_rate_limit', lambda *_: None, raising=False)
    verifier = MetadataVerifier(MetadataConfig())
    verifier.client.close()
    verifier.client = Client()
    assert verifier._semantic_scholar(SimpleNamespace(arxiv_id='1706.03762')) == {'title': 'Test'}
    assert slots == ['semantic-scholar', 'semantic-scholar']


def test_semantic_cooldown_reaches_another_process(tmp_path, monkeypatch):
    import time
    from arxiv_ra.rate_limit import defer_rate_limit
    monkeypatch.setenv('ARXIV_RA_RATE_LIMIT_DIR', str(tmp_path))
    defer_rate_limit('semantic-scholar', 1.3)
    started = time.time()
    child = subprocess.run([sys.executable, '-c',
        'import time; from arxiv_ra.rate_limit import shared_rate_limit\n'
        'with shared_rate_limit("semantic-scholar", 0): print(time.time())'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    assert float(child.stdout.strip()) - started >= 1.2
