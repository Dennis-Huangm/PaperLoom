"""Launch the actual PowerShell entry points, without registering real tasks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest


pytestmark = pytest.mark.skipif(sys.platform != 'win32' or not shutil.which('powershell'),
                                reason='Windows PowerShell entry points')
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project with spaces'
    (root / 'scripts').mkdir(parents=True)
    for name in ('run.ps1', 'start_gui.ps1', 'install_windows_task.ps1'):
        shutil.copy2(SCRIPTS / name, root / 'scripts' / name)
    shutil.copy2(SCRIPTS.parent / 'Start-PaperLoom.cmd', root / 'Start-PaperLoom.cmd')
    (root / 'config.yaml').write_text('output_dir: run\n', encoding='utf-8')
    venv.EnvBuilder(with_pip=False).create(root / '.venv')
    stub = tmp_path / 'stub'
    (stub / 'arxiv_ra').mkdir(parents=True)
    (stub / 'arxiv_ra/__init__.py').write_text('', encoding='utf-8')
    (stub / 'arxiv_ra/__main__.py').write_text(
        'import json,os,sys\nfrom pathlib import Path\n'
        'Path(os.environ["CAPTURE"]).write_text(json.dumps({"python":sys.executable, '
        '"args":sys.argv[1:],"cwd":os.getcwd()}),encoding="utf-8")\n'
        'sys.exit(int(os.environ.get("CHILD_EXIT", "0")))\n', encoding='utf-8')
    env = {**os.environ, 'PYTHONPATH': str(stub), 'CAPTURE': str(tmp_path / 'invocation.json')}
    return root, env


def ps(command, cwd, env):
    command = "$ErrorActionPreference = 'Stop'\n" + command + "\nexit $LASTEXITCODE"
    return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=30)


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def test_gui_and_cli_use_project_python_from_another_directory(project, tmp_path):
    root, env = project
    result = ps(f"& {quote(root / 'scripts/start_gui.ps1')} -Port 8769 -NoBrowser", tmp_path, env)
    assert result.returncode == 0, result.stderr
    captured = json.loads(Path(env['CAPTURE']).read_text(encoding='utf-8'))
    assert Path(captured['python']) == root / '.venv/Scripts/python.exe'
    assert Path(captured['cwd']) == root
    assert captured['args'] == ['--config', str(root / 'config.yaml'), 'gui', '--port', '8769', '--no-browser']
    env['CHILD_EXIT'] = '7'
    result = ps(f"& {quote(root / 'scripts/run.ps1')} doctor", tmp_path, env)
    assert result.returncode == 7


def test_missing_venv_fails_instead_of_falling_back_to_path_python(tmp_path):
    root = tmp_path / 'missing venv'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(SCRIPTS / 'run.ps1', root / 'scripts/run.ps1')
    result = ps(f"& {quote(root / 'scripts/run.ps1')} doctor", tmp_path, os.environ)
    assert result.returncode != 0
    assert 'setup_environment.ps1' in result.stderr


def test_double_click_launcher_preserves_arguments_and_project_directory(project, tmp_path):
    root, env = project
    result = ps(f"& {quote(root / 'Start-PaperLoom.cmd')} -Port 8772 -NoBrowser", tmp_path, env)
    assert result.returncode == 0, result.stderr
    captured = json.loads(Path(env['CAPTURE']).read_text(encoding='utf-8'))
    assert Path(captured['python']) == root / '.venv/Scripts/python.exe'
    assert Path(captured['cwd']) == root
    assert captured['args'] == ['--config', str(root / 'config.yaml'), 'gui', '--port', '8772', '--no-browser']


def test_task_preflight_uses_same_python_and_failure_prevents_registration(project, tmp_path):
    root, env = project
    env['CHILD_EXIT'] = '1'
    registered = tmp_path / 'registered.json'
    # Replacing the registration cmdlet guarantees this test cannot create an OS task.
    command = (f"function Register-ScheduledTask {{ 'unexpected' | Set-Content {quote(registered)} }}\n"
               f"& {quote(root / 'scripts/install_windows_task.ps1')} -ProjectDir {quote(root)}")
    result = ps(command, tmp_path, env)
    assert result.returncode != 0
    assert not registered.exists()
    captured = json.loads(Path(env['CAPTURE']).read_text(encoding='utf-8'))
    assert Path(captured['python']) == root / '.venv/Scripts/python.exe'
    assert captured['args'] == ['--config', str(root / 'config.yaml'), 'doctor']
