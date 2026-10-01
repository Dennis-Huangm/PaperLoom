"""Official proceedings discovery with conservative arXiv association."""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
from typing import Any

from bs4 import BeautifulSoup
import httpx

from .models import Paper
from .paper_data import base_id
from .rate_limit import shared_rate_limit
from .task_runtime import task_checkpoint, task_progress
from .task_runtime import TaskCancelled
from .utils import read_json, write_json


@dataclass
class ConferenceRecord:
    conference: str
    year: int
    title: str
    authors: list[str]
    evidence_url: str
    abstract: str = ''
    arxiv_id: str = ''
    paper_type: str = 'long'


def normalized(value):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', value).casefold())


def author_names(values):
    return {normalized(v) for v in values if normalized(v)}


def matching_papers(record, papers):
    # Full author names, rather than common surnames, establish an independent
    # corroboration of title identity. Ambiguous candidates remain unresolved.
    names = author_names(record.authors)
    matches = {}
    for paper in papers:
        if (normalized(record.title) == normalized(paper.title) and names
                and names.intersection(author_names([a.name for a in paper.authors]))
                and (not record.arxiv_id or base_id(record.arxiv_id) == base_id(paper.arxiv_id))):
            matches[base_id(paper.arxiv_id)] = paper
    return list(matches.values())


class ConferenceClient:
    DIRECTORY_TTL = 24 * 3600
    ASSOCIATION_TTL = 7 * 24 * 3600

    def __init__(self, username_env='OPENREVIEW_USERNAME', password_env='OPENREVIEW_PASSWORD'):
        self.username_env, self.password_env = username_env, password_env
        self._openreview_token = None
        self.client = httpx.Client(timeout=30, follow_redirects=True,
                                   headers={'User-Agent': 'PaperLoom (personal research use)'})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def _get(self, url, **params):
        task_checkpoint()
        headers = self._openreview_headers() if urlparse(url).hostname in {'api.openreview.net', 'api2.openreview.net'} else {}
        with shared_rate_limit('conference:' + urlparse(url).hostname, 1):
            response = self.client.get(url, params=params or None, headers=headers)
        if response.status_code == 403 and 'openreview.net' in str(urlparse(url).hostname):
            try:
                challenge = response.json().get('name') == 'ChallengeRequiredError'
            except ValueError:
                challenge = False
            if challenge:
                raise ValueError('OpenReview 官方 API 要求访问验证（403 ChallengeRequiredError）；本次未取得目录，请稍后重试或在 OpenReview 完成访问验证。')
        response.raise_for_status()
        # UTF-8 proceedings sometimes omit the charset in their headers.
        response.encoding = 'utf-8'
        return response

    def _openreview_headers(self):
        username, password = os.getenv(self.username_env, ''), os.getenv(self.password_env, '')
        if not username and not password:
            return {}
        if not username or not password:
            raise ValueError('OpenReview 账号和密码需同时配置')
        if self._openreview_token is None:
            task_checkpoint()
            # Disable redirects so credentials can only be posted to the
            # explicitly named official login endpoint.
            response = self.client.post('https://api2.openreview.net/login',
                json={'id': username, 'password': password}, follow_redirects=False)
            if response.status_code != 200:
                raise ValueError(f'OpenReview 官方登录失败（HTTP {response.status_code}），请检查账号配置')
            data = response.json()
            if data.get('mfaPending'):
                raise ValueError('OpenReview 登录需要多因素认证；当前客户端暂不支持该认证步骤')
            token = data.get('token')
            if not isinstance(token, str) or not token:
                raise ValueError('OpenReview 登录未返回有效令牌')
            self._openreview_token = token
        return {'Authorization': 'Bearer ' + self._openreview_token}

    def _directory(self, conference, year):
        if conference == 'acl':
            collection = f'{year}.acl' if year >= 2020 else f'P{year % 100:02d}'
            url = f'https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{collection}.xml'
            root = ET.fromstring(self._get(url).text)
            records = []
            for volume in root.findall('volume'):
                meta = volume.find('meta')
                if meta is None or meta.findtext('year') != str(year) or meta.findtext('venue') != 'acl':
                    continue
                booktitle = meta.find('booktitle')
                title = ''.join(booktitle.itertext()) if booktitle is not None else ''
                if volume.get('id') != 'long' and 'long papers' not in title.casefold():
                    continue
                for p in volume.findall('paper'):
                    paper_title, abstract = p.find('title'), p.find('abstract')
                    paper_id = p.get('id', '')
                    if paper_id == '0' or not paper_id.isdecimal() or paper_title is None:
                        continue
                    ident = f'{collection}-{volume.get("id")}.{paper_id}' if year >= 2020 else f'{collection}-{volume.get("id")}{int(paper_id):03d}'
                    records.append(ConferenceRecord(conference, year, ''.join(paper_title.itertext()),
                        [f'{a.findtext("first", "")} {a.findtext("last", "")}'.strip() for a in p.findall('author')],
                        f'https://aclanthology.org/{ident}/',
                        ''.join(abstract.itertext()) if abstract is not None else ''))
            return records, url
        if conference == 'iclr':
            return self._iclr(year)
        if conference == 'icml':
            root_url = 'https://proceedings.mlr.press/'
            index = BeautifulSoup(self._get(root_url).text, 'html.parser')
            links = [a for a in index.select('a[href]') if re.search(rf'\bICML {year}\b', a.parent.get_text(' ', strip=True))
                     and re.fullmatch(r'Volume \d+', a.get_text(strip=True))
                     and re.search(rf'Proceedings of ICML {year}\b', a.parent.get_text(' ', strip=True))]
            if len(links) != 1:
                raise LookupError(f'ICML {year} 官方目录尚未公布或该历史届次不在 PMLR 中')
            url = urljoin(root_url, links[0]['href'])
            soup = BeautifulSoup(self._get(url).text, 'html.parser')
            records = []
            for p in soup.select('.paper'):
                title, authors, link = p.select_one('.title'), p.select_one('.authors'), p.select_one('.links a')
                if title and authors and link:
                    records.append(ConferenceRecord(conference, year, title.get_text(' ', strip=True),
                        [a.strip() for a in authors.get_text().split(',')], urljoin(url, link['href'])))
            return records, url
        if conference == 'neurips':
            url = f'https://proceedings.neurips.cc/paper_files/paper/{year}'
            soup = BeautifulSoup(self._get(url).text, 'html.parser')
            records = []
            for p in soup.select('.paper-list li'):
                track = p.get('data-track', '')
                if track and track != 'conference':
                    continue
                a = p.select_one('a[href*="Abstract"]')
                authors = p.select_one('.paper-authors') or p.select_one('i')
                if a and authors and ('-Conference.html' in a['href'] or year < 2022):
                    records.append(ConferenceRecord(conference, year, a.get_text(' ', strip=True),
                        [v.strip() for v in authors.get_text().split(',')], urljoin(url, a['href'])))
            return records, url
        url = f'https://openaccess.thecvf.com/CVPR{year}?day=all'
        soup = BeautifulSoup(self._get(url).text, 'html.parser')
        records = []
        for p in soup.select('dt.ptitle'):
            a = p.select_one('a[href]')
            authors = p.find_next_sibling('dd')
            if not a or not authors or not re.search(rf'/content/(?:CVPR{year}|cvpr_{year})/html/', a['href'], re.I):
                continue
            names = [v.get('value', '') for v in authors.select('input[name="query_author"]')]
            records.append(ConferenceRecord(conference, year, a.get_text(' ', strip=True), names, urljoin(url, a['href'])))
        return records, url

    def _iclr(self, year):
        # Prefer API 2 even for migrated historical venues. API 1 remains a
        # documented fallback for older venues that have no API 2 records.
        records, source = self._iclr_notes(year, legacy=False)
        if not records and year < 2024:
            return self._iclr_notes(year, legacy=True)
        return records, source

    def _iclr_notes(self, year, *, legacy):
        url = 'https://api.openreview.net/notes' if legacy else 'https://api2.openreview.net/notes'
        venue = f'ICLR.cc/{year}/Conference'
        records, offset, total = [], 0, None
        while total is None or offset < total:
            filters = {'invitation': f'{venue}/-/Blind_Submission', 'details': 'directReplies,original'} if legacy else {'content.venueid': venue}
            response = self._get(url, **filters, limit=1000, offset=offset, count='true')
            data = response.json()
            notes = data.get('notes')
            if not isinstance(notes, list):
                raise ValueError('OpenReview 没有返回有效的论文列表')
            if 'count' not in data and len(notes) == 1000:
                raise ValueError('OpenReview 没有提供分页总数，无法确认目录完整性')
            total = int(data.get('count', len(notes)))
            for note in notes:
                if legacy:
                    decisions = [r for r in note.get('details', {}).get('directReplies', [])
                        if str(r.get('invitation', '')).endswith('/Decision')]
                    if not any(str(r.get('content', {}).get('decision', '')).startswith('Accept') for r in decisions):
                        continue
                    original = note.get('details', {}).get('original')
                    if isinstance(original, dict):
                        note = {**note, 'content': original.get('content', note.get('content', {}))}
                content = note.get('content', {})
                def value(key):
                    v = content.get(key, '')
                    return v.get('value', '') if isinstance(v, dict) else v
                if not legacy and value('venueid') != venue:
                    continue
                authors = value('authors')
                if not isinstance(authors, list) or not note.get('id'):
                    continue
                records.append(ConferenceRecord('iclr', year, str(value('title')), authors,
                    f'https://openreview.net/forum?id={note["id"]}', str(value('abstract'))))
            if not notes:
                if offset < total:
                    raise ValueError('OpenReview 分页提前结束，目录不完整')
                break
            offset += len(notes)
        return records, f'https://openreview.net/group?id={venue}'

    @staticmethod
    def _read_fresh(path, ttl):
        if path and path.is_file():
            try:
                payload = read_json(path, {})
                if isinstance(payload, dict) and 0 <= datetime.now(timezone.utc).timestamp() - float(payload.get('timestamp', 0)) < ttl:
                    return payload
            except (OSError, ValueError, TypeError):
                pass
        return None

    def discover(self, config, arxiv, output_root, excluded_ids, *, refresh=False):
        cache = Path(output_root) / '.conference-cache' if output_root else None
        states: dict[str, dict[str, Any]] = {}
        directories: list[tuple[str, list[ConferenceRecord]]] = []
        current_year = datetime.now(timezone.utc).year
        for year in range(config.conference_year_to, config.conference_year_from - 1, -1):
            for conference in config.conferences:
                key = f'{conference}:{year}'
                state = states[key] = {'status': 'ok', 'count': 0, 'matched': 0, 'unmatched': 0,
                    'uncertain': 0, 'association_failed': 0, 'excluded': 0, 'not_examined': 0,
                    'directory_count': 0, 'cached': False}
                path = cache / f'{conference}-{year}.json' if cache else None
                task_progress(f'读取 {conference.upper()} {year} 主会论文目录')
                try:
                    payload = None if refresh else self._read_fresh(path, self.DIRECTORY_TTL)
                    if payload:
                        records = [ConferenceRecord(**r) for r in payload['records']]
                        url = payload['source_url']
                        state['cached'] = True
                    else:
                        records, url = self._directory(conference, year)
                        if not records:
                            raise LookupError('未取得已核验的主会长文目录，可能尚未公布或历史格式不受支持')
                        payload = {'timestamp': datetime.now(timezone.utc).timestamp(), 'source_url': url,
                                   'records': [asdict(r) for r in records]}
                        if path:
                            write_json(path, payload)
                    state.update(directory_count=len(records), source_url=url,
                                 updated_at=datetime.fromtimestamp(payload['timestamp'], timezone.utc).isoformat())
                    directories.append((key, records))
                except (httpx.HTTPError, ValueError, KeyError, TypeError, LookupError, ET.ParseError) as exc:
                    missing = isinstance(exc, LookupError) or isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404
                    state.update(status='unpublished' if missing and year == current_year else 'failed', error=str(exc))
        papers: list[Paper] = []
        attempts = 0
        excluded = {base_id(a) for a in excluded_ids}
        # Interleave venues and years so one volume cannot consume the budget.
        ranked = []
        terms = config.positive_keywords + config.arxiv_query_terms
        for key, records in directories:
            ranked.append((key, sorted(records, key=lambda r: sum(t.casefold() in (r.title + ' ' + r.abstract).casefold() for t in terms if t), reverse=True)))
        for index in range(max((len(r) for _, r in ranked), default=0)):
            for key, records in ranked:
                if index >= len(records):
                    continue
                task_checkpoint()
                record, state = records[index], states[key]
                digest = hashlib.sha256(json.dumps(asdict(record), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                path = cache / 'associations' / f'{digest}.json' if cache else None
                entry = None if refresh else self._read_fresh(path, self.ASSOCIATION_TTL)
                if entry:
                    candidate = Paper.from_dict(entry['paper']) if entry.get('paper') else None
                    status = entry['status']
                elif attempts >= config.max_candidates:
                    state['not_examined'] += 1
                    continue
                else:
                    attempts += 1
                    task_progress(f'关联 arXiv · {attempts}/{config.max_candidates} · {record.title[:70]}')
                    try:
                        candidates = arxiv.get_many([record.arxiv_id]) if record.arxiv_id else arxiv.find_by_title(record.title)
                        matches = matching_papers(record, candidates)
                        candidate = matches[0] if len(matches) == 1 else None
                        status = 'matched' if candidate else 'uncertain' if len(matches) > 1 else 'unmatched'
                        if path:
                            write_json(path, {'timestamp': datetime.now(timezone.utc).timestamp(), 'status': status,
                                              'paper': candidate.to_dict() if candidate else None})
                    except TaskCancelled:
                        raise
                    except Exception as exc:
                        state['association_failed'] += 1
                        state['error'] = str(exc)
                        # Do not repeat an exhausted network failure for every title.
                        attempts = config.max_candidates
                        continue
                state[status] += 1
                if candidate:
                    if base_id(candidate.arxiv_id) in excluded:
                        state['excluded'] += 1
                        continue
                    if len(papers) >= config.max_candidates:
                        state['not_examined'] += 1
                        continue
                    publication = {'conference': record.conference, 'year': record.year, 'paper_type': 'long',
                        'evidence_url': record.evidence_url, 'association_evidence': 'normalized title and full author name',
                        'verified_at': datetime.now(timezone.utc).isoformat()}
                    papers.append(replace(candidate, conference_publications=[publication]))
                    state['count'] += 1
        for state in states.values():
            if state['status'] == 'ok':
                if state['association_failed']:
                    state['status'] = 'partial'
                elif state['not_examined']:
                    state['status'] = 'truncated'
                elif not state['count']:
                    state['status'] = 'empty'
        return papers, states
