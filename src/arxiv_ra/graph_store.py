"""Versioned graph publications: one pointer commits a complete immutable snapshot."""
from __future__ import annotations

import copy
import math
import re
import uuid
from pathlib import Path

from .task_runtime import task_checkpoint, task_commit
from .utils import read_json, write_json


def active_graph_path(folder: Path) -> Path:
    pointer = read_json(folder / 'active.json', None)
    if pointer is None:
        return folder / 'graph.json'
    if not isinstance(pointer, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(pointer.get('snapshot_id', ''))):
        raise ValueError('图谱快照指针损坏')
    target = folder / 'snapshots' / pointer['snapshot_id'] / 'graph.json'
    if not target.is_file() or not target.with_name('index.html').is_file():
        raise ValueError('图谱快照不完整')
    if not target.resolve().is_relative_to(folder.resolve()):
        raise ValueError('图谱快照路径无效')
    return target


def normalize_paper(node: dict) -> dict:
    result = dict(node)
    for field in ('title', 'abstract', 'venue', 'url'):
        if not isinstance(result.get(field), str):
            result[field] = ''
    result['authors'] = [a for a in result.get('authors', []) if isinstance(a, dict) and isinstance(a.get('name'), str)] if isinstance(result.get('authors'), list) else []
    external = result.get('externalIds')
    result['externalIds'] = {k: v for k, v in external.items() if isinstance(v, str)} if isinstance(external, dict) else {}
    for field in ('year', 'citationCount'):
        value = result.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            result[field] = None
        elif field == 'year' and (value != int(value) or value < 1000 or value > 3000):
            result[field] = None
    return result


def read_graph(path: Path) -> dict:
    raw = read_json(path, None)
    if not isinstance(raw, dict) or not isinstance(raw.get('seed'), dict) or not raw['seed'].get('paperId'):
        raise ValueError('图谱数据损坏或缺少起点')
    version = raw.get('schema_version', 1)
    if version not in (1, 2):
        raise ValueError('此图谱来自不支持的版本，请更新应用')
    graph = copy.deepcopy(raw)
    if version == 1:
        graph['status'] = 'legacy'
        graph['sources'] = {name: {'status': 'unknown', 'fetched_at': '', 'truncated': False}
                            for name in ('seed', 'references', 'citations', 'similar', 'cross')}
    nodes = graph.get('nodes')
    if not isinstance(nodes, list) or not nodes:
        nodes = [{**graph['seed'], 'roles': ['seed']}]
        for key, role in (('references', 'reference'), ('citations', 'citation'), ('similar', 'similar')):
            nodes.extend({**n, 'roles': [role]} for n in graph.get(key, []) if isinstance(n, dict))
    valid = {}
    for node in nodes:
        if isinstance(node, dict) and node.get('paperId'):
            node = normalize_paper(node)
            node['paperId'] = str(node['paperId'])
            node['roles'] = [r for r in node.get('roles', []) if r in {'seed', 'reference', 'citation', 'similar'}] if isinstance(node.get('roles'), list) else []
            valid.setdefault(node['paperId'], node)
    seed_id = str(graph['seed']['paperId'])
    valid.setdefault(seed_id, {**normalize_paper(graph['seed']), 'paperId': seed_id, 'roles': ['seed']})
    if 'seed' not in valid[seed_id]['roles']:
        valid[seed_id]['roles'].append('seed')
    graph['nodes'] = list(valid.values())
    graph['seed'] = valid[seed_id]
    def endpoints(edge):
        return (isinstance(edge, dict) and edge.get('source') in valid and edge.get('target') in valid
                and edge['source'] != edge['target'])
    graph['citation_edges'] = [e for e in graph.get('citation_edges', [])
                               if endpoints(e) and e.get('kind') in {'reference', 'citation', 'cross-citation'}]
    graph['edges'] = [e for e in graph.get('edges', []) if endpoints(e)
                      and e.get('kind') == 'similarity' and isinstance(e.get('score'), (int, float))
                      and math.isfinite(e['score']) and e['score'] >= .025]
    return graph


def publish_graph(folder: Path, graph: dict, render) -> Path:
    snapshot_id = uuid.uuid4().hex
    graph['snapshot_id'] = snapshot_id
    destination = folder / 'snapshots' / snapshot_id
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / 'graph.json', graph)
    (destination / 'index.html').write_text(render(graph), encoding='utf-8')
    # Both browser loading modes use the very same local, versioned assets.
    assets = Path(__file__).parent / 'static'
    for name in ('graph.js', 'graph.css'):
        if (assets / name).exists():
            (destination / name).write_bytes((assets / name).read_bytes())
    task_checkpoint()
    with task_commit():
        write_json(folder / 'active.json', {'snapshot_id': snapshot_id})
    return destination / 'index.html'
