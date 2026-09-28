"""Local, CSP-compatible graph document rendering."""
import html
import json
from pathlib import Path


def render_graph(graph: dict, asset_prefix: str = '') -> str:
    template = (Path(__file__).parent / 'templates' / 'graph.html').read_text(encoding='utf-8')
    data = json.dumps(graph, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    # Insert JSON last so a title or abstract cannot substitute template markers.
    before, after = template.split('__GRAPH_DATA__')
    title = html.escape(str(graph['seed'].get('title') or graph.get('arxiv_id', '相关工作地图')))
    return before.replace('__TITLE__', title).replace('__ASSET_PREFIX__', html.escape(asset_prefix, quote=True)) + data + after
