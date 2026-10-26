"""Isolated settings preview; never contacts a model provider."""
import tempfile
from pathlib import Path

import uvicorn
import yaml
from arxiv_ra import web

root = Path(tempfile.mkdtemp(prefix='paperloom-settings-'))
config = root / 'config.yaml'
config.write_text(yaml.safe_dump({'output_dir': str(root / 'output')}), encoding='utf-8')
web.probe_connection = lambda *args: {'ok': True, 'message': '连接成功，模型已返回有效回复。', 'elapsed_ms': 42}
app = web.create_app(config)
if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8774)
