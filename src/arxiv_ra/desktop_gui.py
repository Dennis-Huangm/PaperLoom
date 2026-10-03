"""Manage a windowless, current-user GUI with cooperative shutdown on Windows."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid


@contextmanager
def instance_lock(path):
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
        else:
            try:
                yield True
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def state_at(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def busy(lock):
    with instance_lock(lock) as acquired:
        return not acquired


def serve(config, port, directory, lock, state):
    with instance_lock(lock) as acquired:
        if not acquired:
            return 0
        log = directory / f'gui-{port}.log'
        if log.exists() and log.stat().st_size > 10 * 1024 * 1024:
            log.replace(log.with_suffix('.previous.log'))
        with log.open('a', encoding='utf-8', buffering=1) as output:
            sys.stdout = sys.stderr = output
            sys.stdin = open(os.devnull, encoding='utf-8')
            nonce = uuid.uuid4().hex
            request = directory / f'{nonce}.stop'
            finished = threading.Event()
            watcher = None
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # Reserve the actual port before initializing application jobs.
                # Never treat an unrelated listener as this managed instance.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(('127.0.0.1', port))
                from .cli import _load_dotenv
                from .web import create_app
                import uvicorn

                _load_dotenv(config.parent / '.env')
                server = uvicorn.Server(uvicorn.Config(create_app(config), host='127.0.0.1', port=port,
                                                       log_level='info', timeout_graceful_shutdown=30))

                def monitor():
                    published = False
                    while not finished.wait(0.1):
                        if server.started and not published:
                            temporary = state.with_suffix('.tmp')
                            temporary.write_text(json.dumps({'pid': os.getpid(), 'nonce': nonce,
                                                             'config': str(config), 'port': port}), encoding='utf-8')
                            temporary.replace(state)
                            published = True
                        if request.exists():
                            server.should_exit = True

                watcher = threading.Thread(target=monitor, daemon=True)
                watcher.start()
                server.run(sockets=[sock])
                return 0 if server.started else 1
            except Exception:
                import traceback
                traceback.print_exc()
                return 1
            finally:
                finished.set()
                if watcher is not None:
                    watcher.join(timeout=2)
                sock.close()
                if state_at(state).get('nonce') == nonce:
                    state.unlink(missing_ok=True)
                request.unlink(missing_ok=True)


def stop(config, directory, lock, state, timeout):
    if not busy(lock):
        print('PaperLoom background GUI is stopped.')
        return 0
    current = state_at(state)
    if current.get('config') != str(config) or not current.get('nonce'):
        print('GUI is starting; retry after startup completes. No process was terminated.')
        return 1
    request = directory / f"{uuid.UUID(hex=current['nonce']).hex}.stop"
    request.touch()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not busy(lock):
            print('PaperLoom background GUI stopped.')
            return 0
        time.sleep(0.2)
    print('Shutdown is still in progress. Inspect the GUI log before retrying.')
    return 1


def start(config, port, directory, lock, state, launcher, timeout):
    if busy(lock):
        current = state_at(state)
        if current.get('config') == str(config):
            print(f'PaperLoom already runs at http://127.0.0.1:{port}')
            return 0
        print('Another managed instance is starting or uses a different configuration.')
        return 1
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise FileNotFoundError(f'Windowless project Python not found: {pythonw}')
    previous_nonce = state_at(state).get('nonce')
    with open(os.devnull, 'r') as stdin, open(os.devnull, 'a') as output:
        child = subprocess.Popen([str(pythonw), str(launcher), 'run', '--config', str(config), '--port', str(port)],
                                 cwd=config.parent, stdin=stdin, stdout=output, stderr=output,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = state_at(state)
        # Windows venv pythonw may use a redirector process, so Popen's PID
        # need not be the server's PID. Confirm a newly published instance.
        if current.get('nonce') and current['nonce'] != previous_nonce and current.get('config') == str(config) and busy(lock):
            print(f'PaperLoom started at http://127.0.0.1:{port}')
            return 0
        if child.poll() is not None:
            print(f'GUI did not start. Check {directory / f"gui-{port}.log"}; the port may already be occupied.')
            return 1
        time.sleep(0.2)
    print(f'Startup is still in progress. Check {directory / f"gui-{port}.log"}.')
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'start', 'stop', 'restart', 'status'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--launcher', type=Path)
    parser.add_argument('--timeout', type=float, default=45)
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('This launcher requires Windows.')
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    config = args.config.resolve(strict=True)
    directory = config.parent / 'run' / '.desktop-gui'
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / f'gui-{args.port}.lock'
    state = directory / f'gui-{args.port}.json'
    if args.action == 'run':
        return serve(config, args.port, directory, lock, state)
    if args.action == 'status':
        current = state_at(state)
        print(json.dumps({'running': busy(lock), 'instance': current,
                          'log': str(directory / f'gui-{args.port}.log')}, ensure_ascii=False))
        return 0
    if args.action in {'stop', 'restart'}:
        result = stop(config, directory, lock, state, args.timeout)
        if result or args.action == 'stop':
            return result
    launcher = args.launcher or config.parent / 'scripts' / 'gui_background.pyw'
    return start(config, args.port, directory, lock, state, launcher.resolve(strict=True), args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
