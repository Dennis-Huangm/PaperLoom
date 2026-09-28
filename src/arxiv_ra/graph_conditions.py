"""Reuse grounded direction judgments, within the graph's total request budget."""
from __future__ import annotations

import math
from types import SimpleNamespace

from .arxiv_categories import ARXIV_CATEGORIES
from .model_budget import model_request_budget
from .models import Paper
from .plan_selection import evaluate_plan
from .research_clients import ResearchClients
from .task_runtime import task_checkpoint


def assess_conditions(nodes, config, source):
    hard = [c for c in config.discovery.search_plan.get('conditions', [])
            if c.get('kind') in {'required', 'exclude'} and c.get('confirmed')]
    if not hard:
        return {}, 0, None
    cross_reserve = math.ceil(min(len(nodes), config.citations.max_nodes) / 50)
    categories, category_state = {}, None
    if any(c['text'] in ARXIV_CATEGORIES for c in hard):
        categories, category_state = source.categories(nodes, cross_reserve)
    semantic = any(c['text'] not in ARXIV_CATEGORIES for c in hard)
    available = max(0, source.limit - source.used - cross_reserve)
    outcomes = {}
    failures = 0
    def count_request(_count):
        task_checkpoint()
        source.used += 1
    with ResearchClients(config) as clients, model_request_budget(available, on_request=count_request) as budget:
        llm = clients.llm if semantic and config.ranking.llm_rerank and available else SimpleNamespace(enabled=False)
        model_client = getattr(llm, 'client', None)
        if model_client is not None:
            # Match the graph transport's finite timeout and disable hidden SDK retries.
            llm.client = model_client.with_options(timeout=30, max_retries=0)
        for start in range(0, len(nodes), 20):
            task_checkpoint()
            batch = nodes[start:start + 20]
            papers = [Paper(n['paperId'], str(n.get('title', '')), [], str(n.get('abstract') or ''),
                            categories.get(n['paperId'], []), '', None, None, '', '',
                            metadata_status='complete' if n.get('abstract') or categories.get(n['paperId']) else 'partial') for n in batch]
            evaluate_plan(papers, config, llm)
            for paper in papers:
                failures += bool(paper.ranking_explanation.get('condition_error'))
                checks = paper.ranking_explanation.get('conditions', [])
                conflict = any(c['kind'] == 'required' and c['verdict'] == 'not_satisfied'
                               or c['kind'] == 'exclude' and c['verdict'] == 'satisfied' for c in checks)
                unknown = any(c['verdict'] == 'unknown' for c in checks)
                state = 'excluded' if conflict else 'pending' if unknown else 'eligible'
                outcomes[paper.arxiv_id] = {'state': state, 'checks': checks,
                                           'note': '不符合已核实的方向条件' if conflict else '方向条件缺少可验证依据，待判断' if unknown else ''}
        used = budget.used if budget else 0
    uncertain = sum(o['state'] == 'pending' for o in outcomes.values())
    status = ('failed' if failures and failures == len(outcomes) else 'partial' if failures
              or 0 < uncertain < len(outcomes) else 'unknown' if uncertain else 'ok')
    state = source.status(status,
                          fetched_at='', count=len(outcomes), model_requests=used,
                          message=f'{uncertain} 篇条件待判断' if uncertain else '条件均已完成判断')
    if category_state:
        state['categories'] = category_state
        state['truncated'] = category_state['truncated']
        if category_state['status'] in {'failed', 'partial'}:
            state['status'] = 'partial' if categories else 'failed'
            state['message'] += '；部分类别元数据未取得' if categories else '；类别元数据未取得'
    return outcomes, used, state
