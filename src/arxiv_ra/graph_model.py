"""Deterministic selection and evidence-bearing relations for a research map."""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from .graph_store import normalize_paper
from .ranker import _plan_phrase_count


def matches(word, text):
    return _plan_phrase_count(text, word) > 0

STOPWORDS = set('the a an and or of to in on for with from by via using based we this that our is are be as at it can new paper method model models approach framework system results task tasks'.split())


def terms(node):
    def tokens(text):
        normalized = re.sub('[‐‑–—]', '-', str(text or '').casefold())
        words = [t for t in re.findall(r'[a-z][a-z0-9-]+', normalized) if len(t) > 2 and t not in STOPWORDS]
        return words + [' '.join(pair) for pair in zip(words, words[1:])]
    return Counter(tokens(node.get('title')) * 3 + tokens(node.get('abstract')))


class Similarity:
    def __init__(self, nodes):
        counts = {n['paperId']: terms(n) for n in nodes}
        frequency = Counter(t for counts_by_term in counts.values() for t in counts_by_term)
        self.vectors = {}
        for pid, values in counts.items():
            vector = {t: (1 + math.log(c)) * (math.log((1 + len(nodes)) / (1 + frequency[t])) + 1) for t, c in values.items()}
            norm = math.sqrt(sum(v * v for v in vector.values())) or 1
            self.vectors[pid] = {t: v / norm for t, v in vector.items()}
        self.cache = {}

    def compare(self, left, right):
        key = tuple(sorted((left, right)))
        if key not in self.cache:
            a, b = self.vectors[left], self.vectors[right]
            shared = [(t, value * b[t]) for t, value in a.items() if t in b]
            shared.sort(key=lambda item: (-item[1], item[0]))
            self.cache[key] = (sum(value for _, value in shared), [t for t, _ in shared[:6]])
        return self.cache[key]

    def edges(self, nodes):
        chosen = {}
        for node in nodes:
            pid = node['paperId']
            pairs = [(self.compare(pid, n['paperId'])[0], n['paperId']) for n in nodes if n['paperId'] != pid]
            for score, other in sorted(pairs, key=lambda pair: (-pair[0], pair[1]))[:4]:
                if score < .025:
                    continue
                left, right = sorted((pid, other))
                chosen[(left, right)] = {'source': left, 'target': right, 'kind': 'similarity',
                                        'score': round(score, 5), 'terms': self.compare(left, right)[1]}
        return list(chosen.values())


def combine(graph):
    """Merge trustworthy IDs, never near titles, retaining every source role."""
    items = [(graph['seed'], 'seed')]
    for key, role in (('references', 'reference'), ('citations', 'citation'), ('similar', 'similar')):
        items.extend((n, role) for n in graph.get(key, []))
    parents = {}
    def root(pid):
        while parents[pid] != pid:
            pid = parents[pid]
        return pid
    aliases = {}
    for item, _ in items:
        pid = str(item.get('paperId') or '')
        if not pid:
            continue
        parents.setdefault(pid, pid)
        for kind in ('ArXiv', 'DOI'):
            value = (item.get('externalIds') or {}).get(kind)
            if value:
                value = re.sub(r'v\d+$', '', str(value)) if kind == 'ArXiv' else str(value).casefold()
                alias = (kind, value)
                if alias in aliases:
                    parents[root(pid)] = root(aliases[alias])
                else:
                    aliases[alias] = pid
    groups = {}
    for item, role in items:
        pid = str(item.get('paperId') or '')
        if pid:
            groups.setdefault(root(pid), []).append((item, role))
    nodes, identities = [], {}
    for group in groups.values():
        seed = next((item for item, role in group if role == 'seed'), None)
        chosen_id = str(seed['paperId']) if seed else min(str(item['paperId']) for item, _ in group)
        merged = {'paperId': chosen_id, 'roles': sorted({role for _, role in group}), 'externalIds': {}}
        for item, _ in sorted(group, key=lambda pair: str(pair[0]['paperId'])):
            identities[str(item['paperId'])] = chosen_id
            merged['externalIds'].update(item.get('externalIds') or {})
            for key, value in item.items():
                if key in {'paperId', 'roles', 'externalIds'}:
                    continue
                if value is not None and (merged.get(key) is None or len(str(value)) > len(str(merged[key]))):
                    merged[key] = value
        nodes.append(normalize_paper(merged))
    return sorted(nodes, key=lambda n: ('seed' not in n['roles'], n['paperId'])), identities


def select_nodes(nodes, config, reading, outcomes=None):
    seed = next(n for n in nodes if 'seed' in n['roles'])
    eligible, pending, excluded = [], [], []
    outcomes = outcomes or {}
    for node in nodes:
        aid = re.sub(r'v\d+$', '', str(node.get('externalIds', {}).get('ArXiv', '')))
        dismissed = aid in reading.get('feedback', {})
        node['saved'] = aid in reading.get('library', {})
        node['text_status'] = 'unsupported' if not terms(node) else 'title_only' if not node.get('abstract') else 'short_abstract' if len(str(node['abstract'])) < 120 else 'full'
        outcome = outcomes.get(node['paperId'], {})
        node['condition_checks'] = outcome.get('checks', [])
        if outcome.get('note'):
            node['eligibility_note'] = outcome['note']
        if dismissed:
            node['eligibility_note'] = '当前研究方向已标记不相关'
        if node is seed:
            eligible.append(node)
        elif dismissed or outcome.get('state') == 'excluded':
            excluded.append(node)
        elif node.get('eligibility_note'):
            pending.append(node)
        else:
            eligible.append(node)
    similarity = Similarity(eligible)
    conditions = [c for c in config.discovery.search_plan.get('conditions', []) if c['confirmed']]
    positives = [a for c in conditions if c['kind'] in {'topic', 'prefer'} for a in c['aliases']] or config.discovery.positive_keywords
    negatives = [a for c in conditions if c['kind'] == 'demote' for a in c['aliases']] or config.discovery.negative_keywords
    for node in eligible:
        text = str(node.get('title') or '') + '\n' + str(node.get('abstract') or '')
        hits = [word for word in positives if matches(word, text)]
        demotions = sum(matches(word, text) for word in negatives)
        score, shared = similarity.compare(seed['paperId'], node['paperId'])
        node['relevance'] = round(max(0, score + min(.3, .06 * len(hits)) - min(.2, .04 * demotions)), 5)
        node['selection_reason'] = ('方向线索：' + '、'.join(hits[:3])) if hits else ('与起点共同词：' + '、'.join(shared[:3])) if shared else '来自论文关系或推荐来源；内容相似依据不足'
    selected = [seed]
    pool = [n for n in eligible if n is not seed]
    count = min(config.citations.max_nodes - 1, len(pool))
    year = datetime.now(timezone.utc).year
    recent = [n for n in pool if isinstance(n.get('year'), int) and year - 2 <= n['year'] <= year and n['relevance'] >= .025]
    reserve = min(len(recent), math.floor(count * .2))
    def pick(candidates):
        return min(candidates, key=lambda n: (-(n['relevance'] - .15 * max((similarity.compare(n['paperId'], s['paperId'])[0] for s in selected[1:]), default=0)), n['paperId']))
    for _ in range(reserve):
        node = pick(recent)
        recent.remove(node)
        pool.remove(node)
        selected.append(node)
    while len(selected) <= count and pool:
        node = pick(pool)
        pool.remove(node)
        selected.append(node)
    if positives and not any(matches(word, str(seed.get('title', '')) + '\n' + str(seed.get('abstract') or '')) for word in positives):
        seed.setdefault('eligibility_note', '起点未命中方向主题线索；词汇不足不能证明偏离主题')
    return selected, similarity, pending, excluded, len(eligible)


def citation_edges(graph, identities):
    visible = {n['paperId'] for n in graph['nodes']}
    edges = {}
    def add(source, target, kind, status):
        source, target = identities.get(source, source), identities.get(target, target)
        if source != target and source in visible and target in visible:
            edges.setdefault((source, target), {'source': source, 'target': target, 'kind': kind,
                                               'fetched_at': status.get('fetched_at', ''), 'source_status': status.get('status', 'unknown')})
    seed_id = graph['seed']['paperId']
    for key, kind in (('references', 'reference'), ('citations', 'citation')):
        for node in graph[key]:
            pair = (seed_id, node['paperId']) if key == 'references' else (node['paperId'], seed_id)
            add(*pair, kind, graph['sources'][key])
    for source, refs in graph['reference_lists'].items():
        for ref in refs:
            add(source, ref['paperId'], 'cross-citation', graph['reference_records'][source])
    return list(edges.values())


def attach_shared_references(graph):
    lists = {pid: {r['paperId']: r.get('title') or r['paperId'] for r in refs} for pid, refs in graph['reference_lists'].items()}
    for edge in graph['edges']:
        a, b = lists.get(edge['source'], {}), lists.get(edge['target'], {})
        shared = sorted(a.keys() & b.keys())
        edge['shared_references'] = [{'paperId': pid, 'title': a[pid]} for pid in shared[:8]]
        edge['shared_reference_count'] = len(shared)
