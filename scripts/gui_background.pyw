"""Windowless entry used directly by the current-user logon task."""
import os
from pathlib import Path
import sys
import subprocess
import traceback

project = Path(__file__).resolve().parents[1]
os.chdir(project)
try:
    # A base pythonw starts without a console; launch the venv redirector with
    # CREATE_NO_WINDOW as well (some Windows Python versions otherwise flash
    # a console). Wait so Task Scheduler tracks the entire server lifetime.
    pythonw = project / '.venv' / 'Scripts' / 'pythonw.exe'
    if pythonw.is_file() and Path(sys.prefix).resolve() != (project / '.venv').resolve():
        with open(os.devnull, 'r') as stdin, open(os.devnull, 'a') as output:
            child = subprocess.Popen([str(pythonw), str(Path(__file__).resolve()), *sys.argv[1:]],
                                     cwd=project, stdin=stdin, stdout=output, stderr=output,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            raise SystemExit(child.wait())
    from arxiv_ra.desktop_gui import main
    raise SystemExit(main())
except Exception:
    fallback = project / 'run' / '.desktop-gui'
    fallback.mkdir(parents=True, exist_ok=True)
    with (fallback / 'launcher-error.log').open('a', encoding='utf-8') as output:
        traceback.print_exc(file=output)
    raise SystemExit(1)
