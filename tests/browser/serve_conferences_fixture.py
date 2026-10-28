"""Isolated conference history preview, with no network discovery requests."""
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import uvicorn

from arxiv_ra.profiles.profiles import ProfileManager
from arxiv_ra.runtime.jobs.task_runtime import task_warning
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.web import create_app

directory = TemporaryDirectory(prefix='paperloom-conferences-preview-')
root = Path(directory.name)
config = root / 'config.yaml'
config.write_text('output_dir: run\n', encoding='utf-8')
app = create_app(config)
active = ProfileManager(root).active_id()
seed = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'discovery/test_conference_history.py'))['seed_result']
seed(root / 'run', active, 'a' * 32)
success = seed(root / 'run', active, 'b' * 32, failed=False)
payload = read_json(success)
payload['papers'] = [{'arxiv_id': '2407.05600', 'title': 'Visual Agents: Learning to Generate and Refine Vector Graphics',
    'abs_url': 'https://arxiv.org/abs/2407.05600', 'authors': [{'name': 'Ada Researcher'}],
    'abstract': 'An agent framework for generating and refining scalable vector graphics with visual feedback.',
    'source_label': 'NeurIPS 2025', 'conference_publications': []}]
payload['sources']['neurips:2026'] = {'status': 'ok', 'directory_count': 128, 'matched': 96,
    'unmatched': 24, 'uncertain': 8, 'source_url': 'https://proceedings.neurips.cc/',
    'cached': True, 'updated_at': '2026-10-03T09:54:18+00:00'}
write_json(success, payload)

def incomplete():
    task_warning('neurips:2026', "Client error '404 Not Found' for url 'https://proceedings.neurips.cc/paper_files/paper/2026' " * 3)

app.state.jobs.submit('conferences', '当前方向 · 2025–2026', incomplete, profile_id=active)
app.state.jobs.submit('conferences', '之前失败的检索',
    lambda: (_ for _ in ()).throw(RuntimeError('Source unavailable')), profile_id=active)
time.sleep(.1)

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8775, log_level='warning')
