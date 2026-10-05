"""Resolve reading materials exclusively from the local report library."""
import hashlib
import uuid

from .paper_data import base_id, requested_version
from .report_store import html_reports, quality_rank


class ReadingReferences:
    def __init__(self, root):
        self.root = root

    def catalog(self):
        chosen = {}
        for record in html_reports(self.root):
            paper = record.paper
            aid, version = paper.get('arxiv_id'), paper.get('version')
            if not aid or type(version) is not int or version < 1:
                continue
            previous = chosen.get(aid)
            rank = lambda r: (r.paper['version'], quality_rank(r.metadata), r.html_path.stat().st_mtime_ns)
            if previous is None or rank(record) > rank(previous):
                chosen[aid] = record
        return [self.describe(record) for record in chosen.values()]

    def describe(self, record):
        return {'report_id': record.report_id, 'paper': record.paper,
                'missing': not record.markdown_path.is_file()}

    def index(self):
        return {r.report_id: r for r in html_reports(self.root)}

    def resolve(self, report_ids, *, records=None):
        records = self.index() if records is None else records
        result, papers = [], set()
        for rid in dict.fromkeys(report_ids):
            record = records.get(rid)
            if not record or not record.markdown_path.is_file():
                raise ValueError('报告缺失或不可读，请恢复报告或移除该引用')
            paper = record.paper
            aid, version = paper.get('arxiv_id'), paper.get('version')
            if not aid or type(version) is not int or version < 1:
                raise ValueError('报告未记录明确论文版本，不能引用')
            if aid in papers:
                raise ValueError('同一会话不能同时引用同一论文的不同报告版本')
            papers.add(aid)
            result.append(self.describe(record))
        return result

    def for_paper(self, aid):
        version = requested_version(aid)
        matches = [r for r in html_reports(self.root)
                   if r.paper.get('arxiv_id') == base_id(aid)
                   and (not version or r.paper.get('version') == version)
                   and type(r.paper.get('version')) is int and r.markdown_path.is_file()]
        if not matches:
            raise ValueError('生成对应版本的报告后才可阅读对话')
        chosen = max(matches, key=lambda r: (r.paper['version'], quality_rank(r.metadata), r.html_path.stat().st_mtime_ns))
        return self.resolve([chosen.report_id])

    def references(self, value):
        if 'references' in value:
            return value['references']
        paper = value['paper']
        try:
            return self.for_paper(f"{paper['arxiv_id']}v{paper['version']}")
        except ValueError:
            return [{'report_id': '', 'paper': paper, 'missing': True}]

    def validate(self, references, *, records=None):
        resolved = self.resolve([r['report_id'] for r in references], records=records)
        if len(resolved) != len(references):
            raise ValueError('引用范围无效，请重新选择论文')
        for old, new in zip(references, resolved):
            if any(old['paper'].get(k) != new['paper'].get(k) for k in ('arxiv_id', 'version')):
                raise ValueError('报告版本已改变，请移除引用后重新选择')
        return resolved

    def present(self, references, *, records=None):
        records = self.index() if records is None else records
        result = []
        for ref in references:
            try:
                self.validate([ref], records=records)
                missing = False
            except ValueError:
                missing = True
            result.append({'report_id': ref['report_id'], 'paper': ref['paper'], 'missing': missing})
        return result

    def snapshot(self, references):
        records = self.index()
        validated = self.validate(references, records=records)
        if not validated:
            raise ValueError('请先引用至少一篇已有报告的论文')
        result = []
        for ref in validated:
            record = records[ref['report_id']]
            try:
                text = record.markdown_path.read_text(encoding='utf-8')
            except (OSError, UnicodeError) as exc:
                raise ValueError('报告缺失或不可读，请恢复报告或移除该引用') from exc
            if not text.strip():
                raise ValueError('报告内容为空，请重新生成报告')
            material = {'id': ref['report_id'], 'text': text}
            if record.pdf_path.is_file():
                data = record.pdf_path.read_bytes()
                source_id = hashlib.sha256(data).hexdigest()
                cache = self.root / '.reading' / 'sources'
                cache.mkdir(parents=True, exist_ok=True)
                target = cache / f'{source_id}.pdf'
                if not target.exists():
                    temporary = cache / f'{source_id}-{uuid.uuid4().hex}.tmp'
                    try:
                        temporary.write_bytes(data)
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                material['source_id'] = source_id
            result.append({**ref, 'report': material})
        return result
