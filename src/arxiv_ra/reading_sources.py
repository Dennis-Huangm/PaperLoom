"""Read-only tools confined to a conversation's paper and selected report."""
import base64
import hashlib

import fitz
import httpx

from .models import Paper
from .pdf_download import validate_pdf
from .paper_data import versioned


def tool(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': list(required), 'additionalProperties': False}}}


INTEGER = {'type': 'integer', 'minimum': 1}
STRING = {'type': 'string'}
TOOLS = [
    tool('metadata', '当前论文题录和材料范围', {}),
    tool('read_report', '读取本轮用户明确带入的报告段落；不是原文证据', {'offset': {'type': 'integer', 'minimum': 0}}),
    tool('search', '在当前 PDF 搜索文字，返回带 citation_id 的原文短片段，可直接引用，无需 cite', {'query': STRING}, ['query']),
    tool('read_pages', '按物理页读取当前 PDF，每次最多五页；返回带 citation_id 的连续短片段，可直接引用', {'start': INTEGER, 'end': INTEGER}, ['start', 'end']),
    tool('page_image', '读取当前 PDF 单页图像；用于图表和扫描页', {'page': INTEGER}, ['page']),
    tool('cite', '仅在已有片段不适用时核实额外的短原文摘录；已有 citation_id 不需再次核实',
         {'page': INTEGER, 'quote': STRING}, ['page', 'quote']),
]


class ReadingSources:
    def __init__(self, root, session, user, clients, update, images=True):
        self.root, self.session, self.user = root, session, user
        self.clients, self.update, self.images = clients, update, images
        self.paper = versioned(Paper.from_dict(session['paper']))
        self.read = set()
        self.citations = []
        self.pdf = None
        self.source_id = ''
        self.source_note = ''

    def ensure_pdf(self):
        if self.pdf:
            return self.pdf
        self.update(detail='正在获取原文', material={'status': 'fetching'})
        report = self.user.get('report') or {}
        cache = self.root / '.reading' / 'sources'
        cache.mkdir(parents=True, exist_ok=True)
        if report.get('source_id'):
            # Legacy report metadata cannot prove whether a PDF was imported.
            self.source_id = report['source_id']
            self.source_note = '报告附带原文，版本未做内容核验'
            target = cache / f'{self.source_id}.pdf'
        else:
            key = f'{self.paper.arxiv_id}v{self.paper.version}'
            self.source_id = hashlib.sha256(key.encode()).hexdigest()
            self.source_note = f'arXiv 指定版本 {key}；未做内文版本核验'
            target = cache / f'{self.source_id}.pdf'
            if not target.exists():
                import uuid
                temporary = cache / f'{self.source_id}-{uuid.uuid4().hex}.part'
                try:
                    self.clients.arxiv.download_pdf(self.paper, temporary)
                    validate_pdf(temporary, self.paper.arxiv_id)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        validate_pdf(target, self.paper.arxiv_id)
        with fitz.open(target) as doc:
            count = len(doc)
        self.pdf = target
        self.update(detail='原文已取得，按需读取页面', material={
            'status': 'partial', 'total_pages': count, 'read_pages': [], 'source_note': self.source_note})
        return target

    def _citation(self, page, quote):
        """Register text taken from this source; callers establish its provenance."""
        quote = ' '.join(quote.split())
        cid = hashlib.sha256(f'{self.paper.arxiv_id}v{self.paper.version}:{self.source_id}:{page}:{quote}'.encode()).hexdigest()[:16]
        citation = {'id': cid, 'page': page, 'quote': quote, 'source_id': self.source_id,
                    'source_note': self.source_note,
                    'url': f'/api/reading/sources/{self.source_id}#page={page}'}
        if not any(c['id'] == cid for c in self.citations):
            self.citations.append(citation)
        return citation

    def _excerpts(self, page, text):
        """Return the delivered text once, as bounded, directly citable excerpts."""
        pieces = []
        while text:
            end = min(800, len(text))
            if end < len(text):
                boundary = max(text.rfind('\n', 400, end), text.rfind(' ', 400, end))
                if boundary >= 0:
                    end = boundary + 1
            part, text = text[:end], text[end:]
            if not part.strip():
                continue
            citation = self._citation(page, part)
            pieces.append({'page': page, 'text': part, 'citation_id': citation['id']})
        return pieces

    def execute(self, name, args):
        definition = next((t['function'] for t in TOOLS if t['function']['name'] == name), None)
        if not definition or not isinstance(args, dict):
            raise ValueError('不支持的阅读工具')
        schema = definition['parameters']
        if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
            raise ValueError('工具参数越界或缺失')
        for key, value in args.items():
            shape = schema['properties'][key]
            if shape['type'] == 'integer' and (type(value) is not int or value < shape.get('minimum', 0)):
                raise ValueError('页码或偏移量无效')
            if shape['type'] == 'string' and (not isinstance(value, str) or not value.strip() or len(value) > 2000):
                raise ValueError('查询或摘录无效')
        if name == 'metadata':
            return {'paper': self.session['paper'], 'read_pages': sorted(self.read)}
        if name == 'read_report':
            text = (self.user.get('report') or {}).get('text', '')
            offset = args.get('offset', 0)
            return {'text': text[offset:offset + 12000], 'total_chars': len(text), 'source': '生成报告，非原文'}
        if name == 'page_image' and not self.images:
            raise ValueError('当前阅读模型未启用图片能力，请更换配置')
        with fitz.open(self.ensure_pdf()) as doc:
            if name == 'search':
                found = []
                query = args['query'].casefold()
                # Bound one search; explicit read_pages can access any page.
                limit = min(len(doc), 500)
                for index in range(limit):
                    text = doc[index].get_text()
                    position = text.casefold().find(query)
                    if position >= 0:
                        found.append((index + 1, text[max(0, position - 300):position + 1200]))
                        self.read.add(index + 1)
                        if len(found) == 12:
                            break
                result = {'matches': [piece for page, text in found for piece in self._excerpts(page, text)],
                          'matched_pages': len(found), 'searched_through_page': index + 1 if limit else 0,
                          'total_pages': len(doc), 'limited': limit < len(doc) or len(found) == 12}
            else:
                start, end = (args['start'], args['end']) if name == 'read_pages' else (args['page'], args['page'])
                if start > end or end > len(doc) or end - start >= 5:
                    raise ValueError('页码超出范围或一次读取超过五页')
                if name == 'cite':
                    if start not in self.read:
                        raise ValueError('引用页尚未读取')
                    quote = ' '.join(args['quote'].split())
                    text = ' '.join(doc[start - 1].get_text().split())
                    if len(quote) < 16 or text.count(quote) != 1:
                        raise ValueError('摘录无法在指定物理页唯一核实，不能生成引用')
                    citation = self._citation(start, quote)
                    self.update(citations=self.citations)
                    return {'citation_id': citation['id'], **citation}
                self.read.update(range(start, end + 1))
                if name == 'page_image':
                    data = doc[start - 1].get_pixmap(matrix=fitz.Matrix(1.3, 1.3)).tobytes('png')
                    cid = hashlib.sha256(f'{self.paper.arxiv_id}v{self.paper.version}:{self.source_id}:{start}:image'.encode()).hexdigest()[:16]
                    citation = {'id': cid, 'page': start, 'quote': '已读取的原文页面图像（不是逐字文字摘录）',
                                'kind': 'page_image', 'source_id': self.source_id, 'source_note': self.source_note,
                                'url': f'/api/reading/sources/{self.source_id}#page={start}'}
                    if not any(c['id'] == cid for c in self.citations):
                        self.citations.append(citation)
                    self.update(citations=self.citations)
                    result = {'page': start, 'citation_id': cid, 'source_note': self.source_note,
                              '_image': 'data:image/png;base64,' + base64.b64encode(data).decode()}
                else:
                    result = {'pages': [piece for p in range(start, end + 1)
                                        for piece in self._excerpts(p, doc[p - 1].get_text()[:18000])],
                              'read_pages': list(range(start, end + 1)),
                              'note': '页面文字可能截断；不可辨认的图表可读取页面图像'}
            if name in {'search', 'read_pages'}:
                self.update(citations=self.citations)
            self.update(material={'status': 'partial', 'total_pages': len(doc),
                                  'read_pages': sorted(self.read), 'source_note': self.source_note})
            return result


class ReferencedSources:
    """A turn-scoped allowlist; each paper owns independent PDF/read-page state."""
    def __init__(self, root, session, user, clients, update, images=True):
        from copy import deepcopy
        self.images, self.update = images, update
        self.citations = []
        self.sources = {}
        self.tools = deepcopy(TOOLS)
        for definition in self.tools:
            function = definition['function']
            function['description'] = function['description'].replace('当前', '所选')
            function['parameters']['properties']['paper_id'] = {
                'type': 'string', 'description': '本轮引用论文的 arXiv ID，包含 v 版本后缀'}
            if function['name'] != 'metadata':
                function['parameters']['required'].append('paper_id')
        for ref in user['references']:
            paper = ref['paper']
            pid = f"{paper['arxiv_id']}v{paper['version']}"
            def relay(pid=pid, paper=paper, **fields):
                if 'citations' in fields:
                    for citation in fields['citations']:
                        citation.update(paper_id=pid, paper_title=paper['title'])
                    known = {c['id']: c for c in self.citations}
                    known.update({c['id']: c for c in fields['citations']})
                    self.citations = list(known.values())
                    fields['citations'] = self.citations
                if 'material' in fields:
                    fields['material'] = {**fields['material'], 'paper_id': pid, 'paper_title': paper['title']}
                self.update(**fields)
            self.sources[pid] = ReadingSources(root, {'paper':paper}, {'report':ref['report']}, clients, relay, images)

    def execute(self, name, args):
        if not isinstance(args, dict):
            raise ValueError('工具参数必须是对象')
        args = dict(args)
        pid = args.pop('paper_id', None)
        if pid is not None and (not isinstance(pid, str) or not pid.strip()):
            raise ValueError('paper_id 必须是本轮引用论文的非空字符串标识')
        if name == 'metadata' and pid is None:
            if args:
                raise ValueError('工具参数越界')
            return {'papers':[{'paper_id':key, 'paper':s.session['paper']} for key,s in self.sources.items()]}
        # Older compatible providers may omit the target in single-paper turns.
        if pid is None and len(self.sources) == 1:
            pid = next(iter(self.sources))
        if pid not in self.sources:
            raise ValueError('论文不在本轮引用范围内，请使用 metadata 中的 paper_id')
        source = self.sources[pid]
        if name in {'search', 'read_pages', 'page_image', 'cite'}:
            try:
                source.ensure_pdf()
            except (OSError, RuntimeError, ValueError, httpx.HTTPError):
                return {'error':'该论文原文暂不可用；可用 read_report 查阅报告，原文未核实', 'paper_id':pid}
        result = source.execute(name, args)
        return {**result, 'paper_id':pid, 'paper_title':source.session['paper']['title']}
