"""Bounded Semantic Scholar acquisition with per-source freshness."""
from __future__ import annotations

import os
import math
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from . import __version__
from .graph_store import normalize_paper
from .graph_model import combine
from .rate_limit import shared_rate_limit
from .task_runtime import task_checkpoint, task_warning

FIELDS = 'paperId,title,abstract,year,venue,citationCount,externalIds,url,authors'
BASE = 'https://api.semanticscholar.org/graph/v1/paper/'


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class RequestBudgetExceeded(RuntimeError):
    pass


class GraphSource:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.used = 0
        self.limit = config.citations.max_requests

    def request(self, method, url, params, payload=None, reserve=0):
        headers = {'User-Agent': f'PaperLoom/{__version__}'}
        key = os.getenv(self.config.metadata.semantic_scholar_api_key_env)
        if key:
            headers['x-api-key'] = key
        for attempt in range(self.config.citations.max_retries + 1):
            task_checkpoint()
            if self.used >= self.limit - reserve:
                raise RequestBudgetExceeded('已达到本次请求额度')
            self.used += 1
            delay = min(2 ** attempt, 30)
            try:
                # Release the shared service lock before backoff or cancellation.
                with shared_rate_limit('semantic-scholar', self.config.citations.min_interval, task_checkpoint):
                    task_checkpoint()
                    response = self.client.request(method, url, params=params, json=payload, headers=headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise
                try:
                    delay = min(30, max(0, float(exc.response.headers.get('Retry-After', delay))))
                except ValueError:
                    pass
                if attempt == self.config.citations.max_retries:
                    raise
            except httpx.TransportError:
                if attempt == self.config.citations.max_retries:
                    raise
            if self.used >= self.limit - reserve:
                raise RequestBudgetExceeded('已达到本次请求额度')
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                task_checkpoint()
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
        raise RuntimeError('请求未完成')

    @staticmethod
    def status(status='ok', **values):
        return {'status': status, 'attempted_at': now(), 'fetched_at': '', 'truncated': False, **values}

    def seed(self, arxiv_id, existing):
        try:
            value = self.request('GET', BASE + quote('ARXIV:' + arxiv_id, safe=':'), {'fields': FIELDS})
            if not isinstance(value, dict) or not value.get('paperId') or not value.get('title'):
                raise ValueError('来源未返回有效起点')
            return normalize_paper(value), self.status(fetched_at=now(), count=1)
        except (httpx.HTTPError, ValueError, RequestBudgetExceeded) as exc:
            cached = existing.get('seed')
            if not cached:
                raise LookupError('起点论文查询失败，且没有可用缓存') from exc
            task_warning('起点论文', '查询失败，使用上次快照；数据时间未更新')
            previous = existing.get('sources', {}).get('seed', {})
            return cached, self.status('cached', fetched_at=previous.get('fetched_at', ''), message='查询失败，使用缓存')

    def candidates(self, seed, existing):
        seed_id = seed['paperId']
        limits = {'references': self.config.citations.max_references,
                  'citations': self.config.citations.max_citations, 'similar': self.config.citations.max_similar}
        values = {key: [] for key in limits}
        states = {key: self.status(scope='recent' if key == 'similar' else 'paper-relations') for key in limits}
        offsets = {key: 0 for key in limits}
        active = set(limits)
        visited = {key: set() for key in limits}
        def identities():
            combined, mapping = combine({'seed': seed, **values})
            return {n['paperId'] for n in combined if 'seed' not in n['roles']}, mapping

        cross_reserve = math.ceil(self.config.citations.max_nodes / 50)
        hard = any(c.get('kind') in {'required', 'exclude'} and c.get('confirmed')
                   for c in self.config.discovery.search_plan.get('conditions', []))
        model_reserve = min(4, math.ceil((self.config.citations.max_candidates + 1) / 20)) if hard and self.config.ranking.llm_rerank else 0
        model_reserve = min(model_reserve, max(0, self.limit - 4 - cross_reserve))
        round_number = 0
        # First pages get a fair opportunity, then the global identity cap stops paging.
        while active:
            for key in limits:
                if key not in active:
                    continue
                total_ids, mapping = identities()
                count = len({mapping[p['paperId']] for p in values[key]})
                remaining = limits[key] - count
                if remaining <= 0 or round_number > 0 and len(total_ids) >= self.config.citations.max_candidates:
                    states[key]['truncated'] = True
                    active.remove(key)
                    continue
                first = offsets[key] == 0
                reserve = cross_reserve + model_reserve + (sum(other in active and offsets[other] == 0 for other in limits if other != key) if first else 0)
                params = {'fields': FIELDS, 'limit': min(50, remaining, self.config.citations.max_candidates)}
                if key == 'similar':
                    params['limit'] = min(500, remaining, self.config.citations.max_candidates)
                    params['from'] = 'recent'
                    url = f'https://api.semanticscholar.org/recommendations/v1/papers/forpaper/{seed_id}'
                else:
                    params['offset'] = offsets[key]
                    url = BASE + seed_id + '/' + key
                try:
                    payload = self.request('GET', url, params, reserve=reserve)
                    if not isinstance(payload, dict):
                        raise ValueError('无效数据格式')
                    rows = payload.get('recommendedPapers' if key == 'similar' else 'data')
                    if not isinstance(rows, list):
                        raise ValueError('来源缺少论文列表')
                    papers = rows if key == 'similar' else [row.get('citedPaper' if key == 'references' else 'citingPaper')
                                                          for row in rows if isinstance(row, dict)]
                    known = {p['paperId'] for p in values[key]}
                    for paper in papers:
                        if not isinstance(paper, dict) or not paper.get('paperId') or not paper.get('title'):
                            continue
                        pid = paper['paperId']
                        if pid == seed_id or pid in known:
                            continue
                        values[key].append(normalize_paper(paper))
                        known.add(pid)
                    states[key].update(fetched_at=now(), status='ok' if values[key] else 'empty')
                    next_page = payload.get('next')
                    visited[key].add(offsets[key])
                    if key == 'similar':
                        states[key]['truncated'] |= len(rows) >= params['limit']
                        active.remove(key)
                    elif type(next_page) is int and next_page >= 0 and next_page not in visited[key]:
                        offsets[key] = next_page
                    else:
                        states[key]['truncated'] |= next_page is not None
                        active.remove(key)
                except (httpx.HTTPError, ValueError, RequestBudgetExceeded) as exc:
                    active.remove(key)
                    cached = existing.get(key, [])
                    # all-cs recommendations from old snapshots cannot masquerade as recent.
                    if key == 'similar' and existing.get('sources', {}).get(key, {}).get('scope') != 'recent':
                        cached = []
                    if values[key]:
                        states[key].update(status='partial', truncated=True)
                    elif cached:
                        values[key] = [normalize_paper(p) for p in cached if isinstance(p, dict) and p.get('paperId') and p.get('title')][:limits[key]]
                        previous = existing.get('sources', {}).get(key, {})
                        states[key].update(status='cached', fetched_at=previous.get('fetched_at', ''), truncated=previous.get('truncated', True))
                    else:
                        states[key]['status'] = 'failed'
                    states[key]['message'] = '请求额度已用尽' if isinstance(exc, RequestBudgetExceeded) else '来源暂时不可用'
                    states[key]['truncated'] |= isinstance(exc, RequestBudgetExceeded)
                    task_warning(key, states[key]['message'])
                states[key]['count'] = len(values[key])
            round_number += 1
        # Canonical IDs count once, even when the source supplied different paper IDs.
        _, mapping = identities()
        per_source = {}
        for key in limits:
            unique = list(dict.fromkeys(mapping[p['paperId']] for p in values[key]))
            states[key]['truncated'] |= len(unique) > limits[key]
            per_source[key] = unique[:limits[key]]
        selected = set()
        for index in range(max((len(v) for v in per_source.values()), default=0)):
            for key in limits:
                if index < len(per_source[key]) and len(selected) < self.config.citations.max_candidates:
                    selected.add(per_source[key][index])
        for key in limits:
            allowed = selected.intersection(per_source[key])
            states[key]['truncated'] |= len(allowed) < len(per_source[key])
            values[key] = [p for p in values[key] if mapping[p['paperId']] in allowed]
            states[key]['count'] = len(allowed)
        return values, states

    def cross_references(self, nodes, existing):
        ids = sorted(n['paperId'] for n in nodes)
        references, records = {}, {}
        previous = existing.get('reference_records', {})
        for start in range(0, len(ids), 50):
            batch_ids = ids[start:start + 50]
            returned = {}
            try:
                batch = self.request('POST', BASE + 'batch', {'fields': 'paperId,references.paperId,references.title'}, {'ids': batch_ids})
                if not isinstance(batch, list):
                    raise ValueError('无效批量引用数据')
                returned = {item['paperId']: item['references'] for item in batch
                            if isinstance(item, dict) and item.get('paperId') in batch_ids and isinstance(item.get('references'), list)}
            except (httpx.HTTPError, ValueError, RequestBudgetExceeded):
                task_warning('交叉引用', '部分交叉引用无法补全，保留可用记录')
            for pid in batch_ids:
                if pid in returned:
                    references[pid] = [r for r in returned[pid] if isinstance(r, dict) and r.get('paperId')]
                    records[pid] = self.status(fetched_at=now(), truncated=len(references[pid]) >= 9999)
                elif pid in previous and pid in existing.get('reference_lists', {}):
                    references[pid] = existing['reference_lists'][pid]
                    records[pid] = self.status('cached', fetched_at=previous[pid].get('fetched_at', ''), truncated=previous[pid].get('truncated', True))
                else:
                    references[pid] = []
                    records[pid] = self.status('failed')
        all_ok = all(r['status'] == 'ok' for r in records.values())
        fetched = [r['fetched_at'] for r in records.values() if r['fetched_at']]
        any_available = any(r['status'] in {'ok', 'cached'} for r in records.values())
        state = self.status('ok' if all_ok else 'partial' if any_available else 'failed', fetched_at=min(fetched, default=''),
                            truncated=any(r['truncated'] for r in records.values()), count=len(records))
        return references, records, state
