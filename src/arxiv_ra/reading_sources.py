"""Read-only tools confined to a conversation's paper and selected report."""
import base64
import hashlib

import fitz

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
    tool('search', '在当前 PDF 搜索文字，返回页码和片段', {'query': STRING}, ['query']),
    tool('read_pages', '按物理页读取当前 PDF，每次最多五页', {'start': INTEGER, 'end': INTEGER}, ['start', 'end']),
    tool('page_image', '读取当前 PDF 单页图像；用于图表和扫描页', {'page': INTEGER}, ['page']),
    tool('cite', '验证已读取页的短原文摘录，取得可用于回答的 [[来源:ID]]',
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
                        found.append({'page': index + 1, 'text': text[max(0, position - 300):position + 1200]})
                        self.read.add(index + 1)
                        if len(found) == 12:
                            break
                result = {'matches': found, 'searched_through_page': index + 1 if limit else 0,
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
                    cid = hashlib.sha256(f'{self.source_id}:{start}:{quote}'.encode()).hexdigest()[:16]
                    citation = {'id': cid, 'page': start, 'quote': quote, 'source_id': self.source_id,
                                'source_note': self.source_note,
                                'url': f'/api/reading/sources/{self.source_id}#page={start}'}
                    if not any(c['id'] == cid for c in self.citations):
                        self.citations.append(citation)
                    self.update(citations=self.citations)
                    return {'citation_id': cid, **citation}
                self.read.update(range(start, end + 1))
                if name == 'page_image':
                    data = doc[start - 1].get_pixmap(matrix=fitz.Matrix(1.3, 1.3)).tobytes('png')
                    cid = hashlib.sha256(f'{self.source_id}:{start}:image'.encode()).hexdigest()[:16]
                    citation = {'id': cid, 'page': start, 'quote': '已读取的原文页面图像（不是逐字文字摘录）',
                                'kind': 'page_image', 'source_id': self.source_id, 'source_note': self.source_note,
                                'url': f'/api/reading/sources/{self.source_id}#page={start}'}
                    if not any(c['id'] == cid for c in self.citations):
                        self.citations.append(citation)
                    self.update(citations=self.citations)
                    result = {'page': start, 'citation_id': cid, 'source_note': self.source_note,
                              '_image': 'data:image/png;base64,' + base64.b64encode(data).decode()}
                else:
                    result = {'pages': [{'page': p, 'text': doc[p - 1].get_text()[:18000]} for p in range(start, end + 1)],
                              'note': '页面文字可能截断；不可辨认的图表可读取页面图像'}
            self.update(material={'status': 'partial', 'total_pages': len(doc),
                                  'read_pages': sorted(self.read), 'source_note': self.source_note})
            return result
