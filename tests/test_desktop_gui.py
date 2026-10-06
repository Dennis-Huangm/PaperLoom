"""Exercise the actual windowless process and cooperative shutdown on Windows."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows windowless launcher')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def desktop(tmp_path):
    project = tmp_path / 'project with spaces'
    (project / 'scripts').mkdir(parents=True)
    shutil.copy2(ROOT / 'scripts/gui_background.pyw', project / 'scripts/gui_background.pyw')
    (project / 'config.yaml').write_text('output_dir: run\n', encoding='utf-8')
    stub = tmp_path / 'stub/arxiv_ra'
    stub.mkdir(parents=True)
    (stub / '__init__.py').write_text(f'__path__.append({str(ROOT / "src/arxiv_ra")!r})\n', encoding='utf-8')
    (stub / 'cli.py').write_text('def _load_dotenv(path): pass\n', encoding='utf-8')
    (stub / 'web.py').write_text(
        'from fastapi import FastAPI\nimport sys\n'
        'def create_app(config):\n'
        '    assert sys.stdout is not None and sys.stderr is not None and sys.stdin is not None\n'
        '    print("windowless app initialized")\n'
        '    app = FastAPI()\n'
        '    return app\n', encoding='utf-8')
    env = {**os.environ, 'PYTHONPATH': str(stub.parent)}
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]

    def invoke(action, config=None):
        return subprocess.run([sys.executable, '-m', 'arxiv_ra.desktop_gui', action,
                               '--config', str(config or project / 'config.yaml'), '--port', str(port),
                               '--launcher', str(project / 'scripts/gui_background.pyw'), '--timeout', '10'],
                              env=env, capture_output=True, text=True, timeout=20)
    yield project, port, invoke
    invoke('stop')


def test_windowless_start_duplicate_start_restart_and_graceful_stop(desktop):
    project, port, invoke = desktop
    result = invoke('start')
    assert result.returncode == 0, result.stdout + result.stderr
    state_path = project / f'run/.desktop-gui/gui-{port}.json'
    first = json.loads(state_path.read_text())
    assert invoke('start').returncode == 0
    assert json.loads(state_path.read_text())['pid'] == first['pid']
    assert json.loads(invoke('status').stdout)['running']
    assert invoke('restart').returncode == 0
    second = json.loads(state_path.read_text())
    assert second['nonce'] != first['nonce']
    result = invoke('stop')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not json.loads(invoke('status').stdout)['running']
    assert not state_path.exists()
    log = (project / f'run/.desktop-gui/gui-{port}.log').read_text(encoding='utf-8')
    assert 'windowless app initialized' in log and 'Application shutdown complete' in log


def test_unrelated_port_listener_is_neither_success_nor_shutdown_target(desktop):
    project, port, invoke = desktop
    with socket.socket() as other:
        other.bind(('127.0.0.1', port))
        other.listen()
        result = invoke('start')
        assert result.returncode == 1
        assert 'port may already be occupied' in result.stdout
        assert invoke('stop').returncode == 0
        assert other.getsockname()[1] == port
    assert not (project / f'run/.desktop-gui/gui-{port}.json').exists()


def test_stop_with_a_different_config_cannot_stop_the_running_gui(desktop):
    project, port, invoke = desktop
    alternate = project / 'other.yaml'
    alternate.write_text('output_dir: other\n', encoding='utf-8')
    assert invoke('start').returncode == 0
    result = invoke('stop', alternate)
    assert result.returncode == 1
    assert json.loads(invoke('status').stdout)['running']


def test_stop_waits_for_background_workers_before_releasing_instance(desktop, tmp_path):
    project, port, invoke = desktop
    # Exercise the real CLI/process boundary with a worker still unwinding after
    # ASGI shutdown, the same gap that leaves the scheduled parent Running.
    stub = tmp_path / 'stub/arxiv_ra/web.py'
    stub.write_text('''from fastapi import FastAPI
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading, time

def create_app(config):
    pool = ThreadPoolExecutor(1)
    release = threading.Event()
    def work():
        release.wait()
        time.sleep(1.2)
        config.with_suffix('.drained').write_text('done')
    pool.submit(work)
    @asynccontextmanager
    async def lifespan(app):
        yield
        release.set()
        pool.shutdown(wait=False)
    app = FastAPI(lifespan=lifespan)
    app.state.jobs = SimpleNamespace(executor=pool, close=release.set)
    return app
''', encoding='utf-8')
    assert invoke('start').returncode == 0
    result = invoke('stop')
    assert result.returncode == 0, result.stdout + result.stderr
    assert (project / 'config.drained').is_file(), 'stop returned before background workers exited'
