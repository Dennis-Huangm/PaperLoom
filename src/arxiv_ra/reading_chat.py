"""Version-bound reading conversations, independent of report lifecycle."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import unicodedata
import threading
import uuid
import base64
import os
from typing import Callable

from .paper_data import base_id, requested_version
from .report_store import explicit_report
from .research_clients import ResearchClients
from .utils import read_json, write_json
from .task_runtime import TaskCancelled, task_checkpoint
from .reading_sources import ReferencedSources
from .reading_references import ReadingReferences
from .reading_model import run_reading, create_completion, failure_message


SOURCE_MARKER = re.compile(r'\[\[来源:([^\]\r\n]*)\]\]')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def history_search(row, query):
    """Match visible conversation text, never hidden tool/report snapshots."""
    def normalize(text):
        return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())

    terms = normalize(query).split()
    if not terms:
        return {}
    fields = [('会话标题', row.get('title', ''))]

    def paper_fields(paper):
        fields.append(('论文标题', paper.get('title', '')))
        aid = paper.get('arxiv_id', '')
        fields.append(('论文 ID', aid + (f"v{paper['version']}" if paper.get('version') else '')))

    paper_fields(row.get('paper') or {})
    for ref in row.get('references', []):
        paper_fields(ref.get('paper') or {})
    for message in row.get('messages', []):
        for ref in message.get('references', []):
            paper_fields(ref.get('paper') or {})
        if message.get('role') in {'user', 'assistant'}:
            fields.append(('提问' if message['role'] == 'user' else '回答', message.get('text', '')))
    searchable = [(label, text, normalize(text)) for label, text in fields if text]
    if not all(any(term in normalized for _, _, normalized in searchable) for term in terms):
        return None
    # Prefer the field matching the most terms; ties keep title-first ordering.
    label, text, _ = max(searchable, key=lambda field: sum(term in field[2] for term in terms))
    text = ' '.join(unicodedata.normalize('NFKC', text).split())
    # Map case-folded offsets back to display text (casefold can expand characters).
    offsets = [i for i, char in enumerate(text) for _ in char.casefold()]
    folded = text.casefold()
    hits = [folded.find(term) for term in terms if term in folded]
    start = max(0, offsets[min(hits)] - 30) if hits else 0
    snippet = ('…' if start else '') + text[start:start + 150] + ('…' if start + 150 < len(text) else '')
    return {'label': label, 'snippet': snippet}


class ReadingService:
    def __init__(self, root: Path, jobs):
        self.root, self.jobs = root, jobs
        self.materials = ReadingReferences(root)
        self.folder = root / '.reading'
        self.folder.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.listeners: dict[str, set[Callable[[], None]]] = {}
        for path in self.folder.glob('*.json'):
            value = read_json(path, {})
            changed = False
            for message in value.get('messages', []):
                if message.get('status') in {'queued', 'running'}:
                    message.update(status='interrupted', detail='服务重启，回答已中断；可重试',
                                   finished_at=message.get('progress_at', value.get('updated_at')))
                    changed = True
            if changed:
                self.save(value)

    def path(self, sid):
        if not re.fullmatch(r'[a-f0-9]{32}', sid):
            raise ValueError('无效会话标识')
        return self.folder / f'{sid}.json'

    def get(self, sid):
        # Windows can reject opening a file while its atomic replacement completes.
        # Polling readers must share the writer lock, not just other writers.
        with self.lock:
            value = read_json(self.path(sid), None)
        if value is None:
            raise LookupError('阅读会话不存在')
        return value

    def save(self, value):
        with self.lock:
            now = timestamp()
            for message in value.get('messages', []):
                if message.get('role') != 'assistant':
                    continue
                if message.get('status') == 'running':
                    message.setdefault('started_at', now)
                    message['progress_at'] = now
                if message.get('started_at'):
                    if message.get('status') not in {'queued', 'running'}:
                        message.setdefault('finished_at', now)
                    end = message.get('finished_at') or now
                    message['elapsed_seconds'] = max(0, (datetime.fromisoformat(end) -
                        datetime.fromisoformat(message['started_at'])).total_seconds())
            value['updated_at'] = now
            write_json(self.path(value['id']), value)
            for notify in tuple(self.listeners.get(value['id'], ())):
                notify()

    def subscribe(self, sid, notify):
        with self.lock:
            self.get(sid)
            self.listeners.setdefault(sid, set()).add(notify)

    def unsubscribe(self, sid, notify):
        with self.lock:
            listeners = self.listeners.get(sid, set())
            listeners.discard(notify)
            if not listeners:
                self.listeners.pop(sid, None)

    def present(self, sid):
        from .render import markdown_with_math
        value = self.get(sid)
        records = self.materials.index()
        value['references'] = self.materials.present(self.materials.references(value), records=records)
        for message in value['messages']:
            if 'references' in message:
                message['references'] = self.materials.present(message['references'], records=records)
            report = message.get('report')
            if report:
                report['missing'] = report['id'] not in records
                report.pop('text', None)
            if message['role'] != 'assistant':
                continue
            if message.get('status') == 'running' and message.get('started_at'):
                message['elapsed_seconds'] = max(0, (datetime.now(timezone.utc) -
                    datetime.fromisoformat(message['started_at'])).total_seconds())
            citations = {c['id']: c for c in message.get('citations', [])}
            text = message['text']
            # Do not render provider-generated URLs as trustworthy source links.
            text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
            text = re.sub(r'<[^>]*>', '', text)
            def citation(match):
                item = citations.get(match[1])
                if not item:
                    return '（引用未核实）'
                exists = (self.folder / 'sources' / f"{item['source_id']}.pdf").exists()
                item['available'] = exists
                label = f"第{item['page']}页"
                title = (item.get('paper_title') or item.get('paper_id') or '原文').replace('"', '&quot;').replace('\n', ' ')
                return f'[{label}]({item["url"]} "{title}")' if exists else f"（{label}，文件已缺失）"
            text = SOURCE_MARKER.sub(citation, text)
            for item in citations.values():
                item['available'] = (self.folder / 'sources' / f"{item['source_id']}.pdf").exists()
            message['html'] = markdown_with_math(text)[0]
        return value

    def list(self, aid='', q='', *, include_empty=True, mode=''):
        with self.lock:
            values = [read_json(path, {}) for path in self.folder.glob('*.json')]
            results = []
            for row in values:
                if not include_empty and not row.get('messages'):
                    continue
                if mode and row.get('mode', 'report') != mode:
                    continue
                if aid and not any(r['paper'].get('arxiv_id') == base_id(aid) for r in self.materials.references(row)):
                    continue
                match = history_search(row, q)
                if match is None:
                    continue
                item = dict({k: v for k, v in row.items() if k != 'messages'},
                            message_count=len(row.get('messages', [])))
                if match:
                    item['search_match'] = match
                results.append(item)
            return sorted(results, key=lambda row: row['updated_at'], reverse=True)

    def report(self, aid, rid):
        report = explicit_report(self.root, base_id(aid), rid)
        if not report:
            raise LookupError('指定报告不存在')
        if requested_version(aid) and report.paper.get('version') != requested_version(aid):
            raise ValueError('报告修订版不匹配')
        return report

    def create(self, config, aid='', *, report_id='', report_ids=None, mode='report', new=False, origin='', source_date=''):
        references = (self.materials.resolve(report_ids) if report_ids is not None else
                      self.materials.resolve([report_id]) if report_id else
                      self.materials.for_paper(aid) if aid else [])
        if mode == 'report' and len(references) != 1:
            raise ValueError('报告侧栏只能引用当前报告对应的一篇论文')
        if aid and references and (base_id(aid) != references[0]['paper']['arxiv_id'] or
                requested_version(aid) and requested_version(aid) != references[0]['paper']['version']):
            raise ValueError('报告与指定论文版本不匹配')
        paper = references[0]['paper'] if references else {}
        with self.lock:
            if mode == 'report' and not new:
                existing = next((row for row in self.list(aid or paper['arxiv_id'])
                    if row.get('mode', 'report') == 'report' and row['paper']['version'] == paper['version']
                    and not row.get('archived')), None)
                if existing:
                    value = self.get(existing['id'])
                    value['references'] = references
                    self.save(value)
                    return value
            value = {'format': 2, 'id': uuid.uuid4().hex, 'paper': paper, 'mode': mode,
                     'references': references, 'title': paper.get('title') or '新阅读对话',
                     'created_at': timestamp(), 'messages': []}
            self.save(value)
            return value

    def set_references(self, sid, report_ids, *, workspace=False):
        with self.lock:
            value = self.get(sid)
            if value.get('archived'):
                raise ValueError('归档会话只读，请新建对话')
            if any(m['status'] in {'queued', 'running'} for m in value['messages']):
                raise RuntimeError('请先停止正在生成的回答，再调整引用')
            old = {r['report_id']: r for r in self.materials.references(value)}
            added = {r['report_id']: r for r in self.materials.resolve([rid for rid in report_ids if rid not in old])}
            references = [deepcopy(old[rid] if rid in old else added[rid]) for rid in dict.fromkeys(report_ids)]
            if len({r['paper']['arxiv_id'] for r in references}) != len(references):
                raise ValueError('同一会话不能同时引用同一论文的不同报告版本')
            if not workspace and value.get('mode', 'report') == 'report':
                if len(references) != 1 or references[0]['paper']['arxiv_id'] != value['paper']['arxiv_id'] or references[0]['paper']['version'] != value['paper']['version']:
                    raise ValueError('报告侧栏只能引用当前报告对应的论文版本')
            value.update(references=references, format=2)
            if workspace:
                value['mode'] = 'workspace'
            self.save(value)
            return self.present(sid)

    def rename(self, sid, title):
        if not title.strip() or len(title) > 200:
            raise ValueError('会话标题需为 1–200 个字符')
        with self.lock:
            value = self.get(sid)
            value['title'] = title.strip()
            self.save(value)
            return value

    def delete(self, sid):
        self.stop(sid)
        with self.lock:
            self.get(sid)
            self.path(sid).unlink()
            for notify in tuple(self.listeners.get(sid, ())):
                notify()

    def rollback(self, sid, mid):
        with self.lock:
            value = self.get(sid)
            if value.get('archived'):
                raise ValueError('归档会话只读，请新建对话')
            if any(m['status'] in {'queued', 'running'} for m in value['messages']):
                raise RuntimeError('请先停止正在生成的回答，再回退')
            index = next((i for i, m in enumerate(value['messages']) if m['id'] == mid), None)
            if index is None:
                raise ValueError('这轮对话已改变，请刷新后重试')
            if value['messages'][index]['role'] == 'assistant':
                index -= 1
            if index < 0 or value['messages'][index]['role'] != 'user':
                raise ValueError('找不到原问题')
            draft = deepcopy(value['messages'][index])
            archive = deepcopy(value)
            archive.update(id=uuid.uuid4().hex, archived=True, title=value['title'] + ' · 回退前归档',
                           archive_of=sid, created_at=timestamp())
            self.save(archive)
            value['messages'] = value['messages'][:index]
            value.pop('memory', None)
            self.save(value)
            return {'session': self.present(sid), 'draft': draft, 'archive_id': archive['id']}

    def settings(self, config):
        saved = read_json(self.folder / 'settings' / 'config.json', {})
        return {**{'independent': False, 'model': config.llm.model, 'images': True,
                   'reasoning_effort': 'high',
                   'max_tools': 10, 'max_tokens': 4000}, **saved,
                'key_configured': bool(os.getenv('PAPERLOOM_READING_API_KEY')),
                'base_url': os.getenv('PAPERLOOM_READING_BASE_URL', '')}

    def submit(self, sid, config, *, text, request_id, selection='', report_id='', images=None, retry_of=''):
        settings = self.settings(config)
        model = settings['model'] if settings['independent'] else config.llm.model
        key_name = 'PAPERLOOM_READING_API_KEY' if settings['independent'] else config.llm.api_key_env
        url_name = 'PAPERLOOM_READING_BASE_URL' if settings['independent'] else config.llm.base_url_env
        key, url = os.getenv(key_name), os.getenv(url_name)
        if not key and not url:
            raise ValueError('请先配置阅读模型的 API Key 或兼容服务地址')
        if images and not settings['images']:
            raise ValueError('当前模型未启用图片能力，请更换配置')
        validated = []
        for picture in images or []:
            if not re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+', picture):
                raise ValueError('只接受 PNG、JPEG 或 WebP 图片')
            raw = base64.b64decode(picture.split(',', 1)[1], validate=True)
            if len(raw) > 5 * 1024 * 1024:
                raise ValueError('单张图片不能超过 5 MiB')
            import fitz
            try:
                pixmap = fitz.Pixmap(raw)
                if pixmap.width * pixmap.height > 20000000:
                    raise ValueError('图片像素过大')
            except (RuntimeError, ValueError) as exc:
                raise ValueError('图片无法读取或过大') from exc
            validated.append(picture)
        with self.lock:
            value = self.get(sid)
            if value.get('archived'):
                raise ValueError('归档会话只读，请新建对话')
            if any(m.get('request_id') == request_id for m in value['messages']):
                return value
            if retry_of:
                original = next((m for m in value['messages'] if m['id'] == retry_of and m['role'] == 'user'), None)
                if not original:
                    raise ValueError('找不到需要重试的问题')
                # Retry the original question; report eligibility is rechecked below.
                text, selection, validated = original['text'], original['selection'], original['images']
            if any(m['status'] in {'queued', 'running'} for m in value['messages']):
                raise RuntimeError('当前会话正在回答，请等待或停止后发送')
            references = self.materials.references(value)
            if retry_of:
                references = original.get('references') or references
            materials = self.materials.snapshot(references)
            # A retry retains its exact earlier materials, but only after today's
            # report availability and version check has succeeded.
            if retry_of and original.get('references'):
                materials = deepcopy(original['references'])
            report = None
            if report_id:
                report = next((r['report'] for r in materials if r['report_id'] == report_id), None)
                if report is None:
                    raise ValueError('选区来源不在本轮引用范围中')
            elif retry_of:
                report = deepcopy(original.get('report'))
            elif value.get('mode', 'report') == 'report':
                report = materials[0]['report']
            if selection and not report:
                raise ValueError('提问选区需要指定来源报告')
            user = {'id': uuid.uuid4().hex, 'role': 'user', 'text': text, 'selection': selection,
                    'report': report, 'references': materials, 'images': validated, 'status': 'completed',
                    'request_id': request_id, 'created_at': timestamp(), 'retry_of': retry_of}
            answer = {'id': uuid.uuid4().hex, 'role': 'assistant', 'text': '', 'status': 'queued',
                      'model': model, 'citations': [], 'created_at': timestamp(), 'detail': '排队中'}
            if not value['messages'] and value['title'] in {value['paper'].get('title'), '新阅读对话'}:
                value['title'] = ' '.join(text.split())[:60]
            value['messages'].extend([user, answer])
            self.save(value)
            try:
                job = self.jobs.submit('reading', value['title'],
                    lambda: self.generate(sid, answer['id'], deepcopy(value), user, deepcopy(config),
                                          settings, model, key, url),
                    identity=f'reading:{sid}:{request_id}', profile_id=config.profile_id)
            except Exception:
                answer.update(status='failed', detail='任务提交失败，请重试')
                self.save(value)
                raise
            answer['job_id'] = job.id
            self.save(value)
            return value

    def update_answer(self, sid, mid, **fields):
        with self.lock:
            try:
                value = self.get(sid)
            except LookupError:
                raise TaskCancelled()
            message = next(m for m in value['messages'] if m['id'] == mid)
            if message['status'] not in {'queued', 'running'}:
                raise TaskCancelled()
            message.update(deepcopy(fields))
            self.save(value)

    def generate(self, sid, mid, value, user, config, settings, model, key, url):
        from openai import OpenAI
        update = lambda **fields: self.update_answer(sid, mid, **fields)
        def checkpoint():
            task_checkpoint()
            with self.lock:
                try:
                    current = self.get(sid)
                except LookupError:
                    raise TaskCancelled()
                if next(m for m in current['messages'] if m['id'] == mid)['status'] not in {'queued', 'running'}:
                    raise TaskCancelled()
        try:
            update(status='running', detail='正在阅读问题')
            with ResearchClients(config) as clients, OpenAI(api_key=key or 'local', base_url=url, max_retries=0) as client:
                sources = ReferencedSources(self.root, value, user, clients, update, settings['images'])
                allowed = set(sources.sources)
                known_citations = {c['id']: c for m in value['messages'][:-2]
                                   for c in m.get('citations', []) if c.get('paper_id') in allowed}
                sources.citations = list(known_citations.values())
                update(citations=sources.citations)
                from .reading_context import make_context
                output_budget = settings['max_tokens']
                def summarize(text):
                    nonlocal output_budget
                    checkpoint()
                    update(detail='正在整理较早讨论')
                    allowance = min(1000, settings['max_tokens'] // 3)
                    output_budget -= allowance
                    result = create_completion(client, update, checkpoint,
                        model=model, messages=[{'role': 'system', 'content':
                            '压缩阅读讨论，保留用户问题、已解释概念、未决问题和引用 ID。不要创造事实。摘要不是原文证据。'},
                            {'role': 'user', 'content': text}], max_tokens=allowance)
                    checkpoint()
                    summary = result.choices[0].message.content or ''
                    if not summary.strip():
                        raise RuntimeError('讨论摘要为空，请重试')
                    return summary
                messages, memory = make_context(value, user, images=settings['images'], summarize=summarize)
                if memory['text']:
                    with self.lock:
                        checkpoint()
                        stored = self.get(sid)
                        stored['memory'] = memory
                        self.save(stored)
                    update(summarized=True)
                answer, limited = run_reading(client, model, messages, sources, update, checkpoint,
                    max_tools=settings['max_tools'], max_tokens=output_budget,
                    reasoning_effort=settings['reasoning_effort'])
                known = {c['id'] for c in sources.citations}
                # A paper ID or invented marker cannot be repaired by guessing which
                # excerpt supports a claim. Keep the limitation visible in saved text.
                answer = SOURCE_MARKER.sub(lambda m: m[0] if m[1] in known else '（引用未核实）', answer)
                used = set(SOURCE_MARKER.findall(answer))
                update(text=answer, status='completed', limited=limited,
                       citations=[c for c in sources.citations if c['id'] in used],
                       detail='本轮查阅或输出已停止，可发送“继续”' if limited else '回答完成')
        except TaskCancelled:
            try:
                update(status='stopped', detail='回答已停止，保留部分内容')
            except TaskCancelled:
                pass
            raise
        except Exception as exc:
            # SDK exceptions may include request details; do not persist credentials or raw responses.
            message = failure_message(exc)
            try:
                update(status='failed', detail=message)
            except TaskCancelled:
                pass
            raise RuntimeError(message) from None

    def stop(self, sid):
        job_ids = []
        with self.lock:
            value = self.get(sid)
            for message in value['messages']:
                if message['status'] in {'queued', 'running'}:
                    message.update(status='stopped', detail='回答已停止，保留部分内容')
                    if message.get('job_id'):
                        job_ids.append(message['job_id'])
            self.save(value)
        for job_id in job_ids:
            self.jobs.cancel(job_id)
        return value
