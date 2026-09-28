from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

import httpx

from .config import AppConfig
from .graph_model import combine, select_nodes, citation_edges, attach_shared_references
from .graph_conditions import assess_conditions
from .graph_source import GraphSource, now
from .graph_store import active_graph_path, read_graph, publish_graph
from .reading_state import ReadingStateStore
from .task_runtime import task_progress, task_warning


class CitationExplorer:
    def __init__(self, config: AppConfig, project_root: Path) -> None:
        self.config = config
        output = Path(config.output_dir)
        self.output_root = output if output.is_absolute() else project_root / output
        self.client = httpx.Client(timeout=30.0, follow_redirects=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def generate(self, arxiv_id: str) -> Path:
        if not self.config.citations.enabled:
            raise ValueError("相关工作地图生成已关闭")
        if not re.fullmatch(r"(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", arxiv_id, re.I):
            raise ValueError("请输入有效的 arXiv ID")
        self.config.citations.__post_init__()
        task_progress("正在读取已有图谱快照…", 5)
        folder = self.output_root / "citations" / f"{arxiv_id.replace('/', '-')}-{self.config.profile_id or 'default'}"
        try:
            existing = read_graph(active_graph_path(folder))
        except (OSError, ValueError):
            existing = {}
        if existing and (existing.get('arxiv_id') != arxiv_id or existing.get('schema_version') == 2 and existing.get('profile_id', '') != self.config.profile_id):
            existing = {}
        source = GraphSource(self.client, self.config)
        task_progress("正在查询起点论文…", 12)
        seed, seed_status = source.seed(arxiv_id, existing)
        task_progress("正在收集参考、引用和近期推荐候选…", 25)
        candidates, statuses = source.candidates(seed, existing)
        graph = {'schema_version': 2, 'arxiv_id': arxiv_id, 'seed': seed, **candidates,
                 'profile_id': self.config.profile_id, 'profile_name': self.config.profile_name,
                 'generated_at': now(), 'sources': {'seed': seed_status, **statuses},
                 'condition_signature': hashlib.sha256(json.dumps(asdict(self.config.discovery), sort_keys=True).encode()).hexdigest(),
                 'algorithm': {'version': 'tfidf-v2', 'scope': 'eligible-candidates', 'threshold': .025,
                               'identity': 'paperId, exact ArXiv/DOI', 'missing_text': 'title-only or unavailable'}}
        task_progress("正在筛选并计算内容关联…", 60)
        combined, identities = combine(graph)
        reading = ReadingStateStore(self.output_root, self.config.profile_id).snapshot()
        outcomes, model_used, condition_state = assess_conditions(combined, self.config, source)
        nodes, similarity, pending, excluded, eligible_count = select_nodes(combined, self.config, reading, outcomes)
        if condition_state:
            graph['sources']['conditions'] = condition_state
        graph.update(nodes=nodes, pending=pending, excluded_count=len(excluded),
                     candidate_count=len(combined) - 1, eligible_count=eligible_count,
                     undisplayed_count=eligible_count - len(nodes))
        graph['seed'] = nodes[0]
        graph['algorithm']['corpus_count'] = eligible_count
        graph['edges'] = similarity.edges(nodes)
        task_progress("正在补全展示论文的引用依据…", 75)
        refs, records, cross_status = source.cross_references(nodes, existing)
        graph.update(reference_lists=refs, reference_records=records)
        graph['sources']['cross'] = cross_status
        graph['citation_edges'] = citation_edges(graph, identities)
        attach_shared_references(graph)
        graph['budget'] = {**asdict(self.config.citations), 'requests_used': source.used, 'model_requests_used': model_used}
        graph['status'] = 'partial' if any(s['status'] not in {'ok', 'empty'} or s['truncated'] for s in graph['sources'].values()) else 'complete'
        if pending:
            task_warning('方向条件', f'{len(pending)} 篇候选缺少可验证依据，已列入待判断列表')
        task_progress("正在准备并发布完整图谱快照…", 95)
        return publish_graph(folder, graph, self._render)

    @staticmethod
    def _render(graph: dict) -> str:
        from .graph_render import render_graph
        return render_graph(graph)
