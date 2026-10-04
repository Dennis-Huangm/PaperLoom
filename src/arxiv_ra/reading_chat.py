"""Version-bound reading conversations, independent of report lifecycle."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
import uuid
import base64
import os
import hashlib

from .models import Paper
from .paper_data import PaperResolver, base_id, local_paper_item, requested_version
from .report_store import explicit_report
from .research_clients import ResearchClients
from .utils import read_json, write_json
from .task_runtime import TaskCancelled, task_checkpoint
from .reading_sources import ReadingSources
from .reading_model import run_reading, create_completion, failure_message


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class ReadingService:
    def __init__(self, root: Path, jobs):
        self.root, self.jobs = root, jobs
        self.folder = root / '.reading'
        self.folder.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        for path in self.folder.glob('*.json'):
            value = read_json(path, {})
            changed = False
            for message in value.get('messages', []):
                if message.get('status') in {'queued', 'running'}:
                    message.update(status='interrupted', detail='服务重启，回答已中断；可重试')
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
            value['updated_at'] = timestamp()
            write_json(self.path(value['id']), value)

    def present(self, sid):
        from .render import markdown_with_math
        value = self.get(sid)
        for message in value['messages']:
            report = message.get('report')
            if report:
                report['missing'] = explicit_report(self.root, value['paper']['arxiv_id'], report['id']) is None
                report.pop('text', None)
            if message['role'] != 'assistant':
                continue
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
                return f"[原文第 {item['page']} 页]({item['url']})" if exists else f"（原文第 {item['page']} 页，文件已缺失）"
            text = re.sub(r'\[\[来源:([a-zA-Z0-9_-]+)\]\]', citation, text)
            for item in citations.values():
                item['available'] = (self.folder / 'sources' / f"{item['source_id']}.pdf").exists()
            message['html'] = markdown_with_math(text)[0]
        return value

    def list(self, aid='', q=''):
        with self.lock:
            values = [read_json(path, {}) for path in self.folder.glob('*.json')]
            return sorted([{k: v for k, v in row.items() if k != 'messages'} for row in values
                           if (not aid or row['paper']['arxiv_id'] == base_id(aid))
                           and q.casefold() in row['title'].casefold()],
                          key=lambda row: row['updated_at'], reverse=True)

    def report(self, aid, rid):
        report = explicit_report(self.root, base_id(aid), rid)
        if not report:
            raise LookupError('指定报告不存在')
        if requested_version(aid) and report.paper.get('version') != requested_version(aid):
            raise ValueError('报告修订版不匹配')
        return report

    def create(self, config, aid, *, report_id='', new=False, origin='', source_date=''):
        if report_id:
            item = self.report(aid, report_id).source_item()
        else:
            item = local_paper_item(self.root, config.profile_id, aid, origin=origin, source_date=source_date)
        snapshot = Paper.from_dict(item['paper']) if item else None
        with ResearchClients(config) as clients:
            resolver = PaperResolver(config.discovery, clients.arxiv, None, self.root)
            paper = resolver.resolve(aid, snapshot=snapshot if snapshot and snapshot.version else None)
        if not paper.version:
            raise ValueError('尚未确认论文版本，请稍后重试；问题草稿可保留')
        with self.lock:
            if not new:
                existing = next((row for row in self.list(aid)
                                 if row['paper']['version'] == paper.version and not row.get('archived')), None)
                if existing:
                    return self.get(existing['id'])
            value = {'format': 1, 'id': uuid.uuid4().hex, 'paper': paper.to_dict(),
                     'title': paper.title, 'created_at': timestamp(), 'messages': []}
            self.save(value)
            return value

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
                # Retrying uses its saved materials even when the original report was removed.
                text, selection, validated = original['text'], original['selection'], original['images']
            if any(m['status'] in {'queued', 'running'} for m in value['messages']):
                raise RuntimeError('当前会话正在回答，请等待或停止后发送')
            report = None
            if retry_of:
                assert original is not None
                report = deepcopy(original.get('report'))
            elif report_id:
                stored = self.report(f"{value['paper']['arxiv_id']}v{value['paper']['version']}", report_id)
                report = {'id': report_id, 'text': stored.markdown_path.read_text(encoding='utf-8')}
                if stored.pdf_path.exists():
                    data = stored.pdf_path.read_bytes()
                    source_id = hashlib.sha256(data).hexdigest()
                    destination = self.folder / 'sources' / f'{source_id}.pdf'
                    destination.parent.mkdir(exist_ok=True)
                    if not destination.exists():
                        temporary = destination.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                        try:
                            temporary.write_bytes(data)
                            temporary.replace(destination)
                        finally:
                            temporary.unlink(missing_ok=True)
                    report['source_id'] = source_id
            if selection and not report:
                raise ValueError('提问选区需要指定来源报告')
            user = {'id': uuid.uuid4().hex, 'role': 'user', 'text': text, 'selection': selection,
                    'report': report, 'images': validated, 'status': 'completed',
                    'request_id': request_id, 'created_at': timestamp(), 'retry_of': retry_of}
            answer = {'id': uuid.uuid4().hex, 'role': 'assistant', 'text': '', 'status': 'queued',
                      'model': model, 'citations': [], 'created_at': timestamp(), 'detail': '排队中'}
            if not value['messages'] and value['title'] == value['paper']['title']:
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
                sources = ReadingSources(self.root, value, user, clients, update, settings['images'])
                known_citations = {c['id']: c for m in value['messages'][:-2] for c in m.get('citations', [])}
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
                used = set(re.findall(r'\[\[来源:([a-zA-Z0-9_-]+)\]\]', answer))
                update(text=answer, status='completed', limited=limited,
                       citations=[c for c in sources.citations if c['id'] in used],
                       detail='本轮已达上限，可发送“继续”' if limited else '回答完成')
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
