"""Web entry points for explicit paper collection."""
from pathlib import Path

from fastapi import Form, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .collaboration import PaperCollection
from .paper_data import base_id
from .web_catalog import report_library


LABELS = {'succeeded': '已完成', 'failed': '失败，可重试', 'partial': '部分完成',
          'running': '执行中', 'pending': '待处理', 'interrupted': '已中断，可重试',
          'needs_attention': '需要处理', 'unlinked': '未关联', 'verified': '已验证',
          'unconfigured': '尚未配置', 'unverified': '待验证的历史记录'}


def register_collection_routes(app, current_config, selected_paper, project_root, pdf_loader, templates, context):
    def resolve(arxiv_id, config, origin, source_date, report_id):
        item = selected_paper(arxiv_id, config, origin=origin, source_date=source_date, report_id=report_id)
        report_path = None
        pdf_path = None
        if origin == 'report':
            output = PaperCollection(config, project_root).output
            report = next((r for r in report_library(output) if r['report_id'] == report_id
                           and r['arxiv_id'] == base_id(arxiv_id)), None)
            if not report:
                raise HTTPException(404, '报告不存在')
            report_path = Path(report['report_path']).with_suffix('.md')
            if report['pdf_path'].is_file():
                pdf_path = report['pdf_path']
        return item, report_path, pdf_path

    @app.get('/collection')
    def page(request: Request, arxiv_id: str, origin: str = '', source_date: str = '', report_id: str = ''):
        config = current_config()
        state = PaperCollection(config, project_root).status(arxiv_id)
        try:
            item, report_path, pdf = resolve(arxiv_id, config, origin, source_date, report_id)
        except HTTPException:
            if not state['operations']:
                raise
            item, report_path, pdf = None, None, None
        return templates.TemplateResponse(request, 'collection.html', context(request, 'library',
            arxiv_id=arxiv_id, origin=origin, source_date=source_date, report_id=report_id,
            item=item, report_path=report_path, state=state, labels=LABELS))

    @app.get('/api/collection')
    def collection_status(arxiv_id: str):
        return PaperCollection(current_config(), project_root).status(arxiv_id)

    @app.post('/api/collection')
    async def collect(arxiv_id: str = Form(...), origin: str = Form(''),
                      source_date: str = Form(''), report_id: str = Form(''),
                      zotero: bool = Form(False), obsidian: bool = Form(False),
                      collection_key: str = Form('')):
        config = current_config()
        item, report_path, pdf_path = resolve(arxiv_id, config, origin, source_date, report_id)
        try:
            return await run_in_threadpool(PaperCollection(config, project_root).collect,
                item, zotero=zotero, obsidian=obsidian, report_path=report_path, pdf_path=pdf_path,
                collection_key=collection_key, pdf_loader=pdf_loader)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/collection/retry')
    async def retry(operation_id: str = Form(...), selected_key: str = Form(''), use_original: bool = Form(False)):
        try:
            return await run_in_threadpool(PaperCollection(current_config(), project_root).retry,
                operation_id, pdf_loader=pdf_loader, selected_key=selected_key, use_original=use_original)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/collection/check')
    async def check(arxiv_id: str = Form(...), selected_path: str = Form(''), selected_key: str = Form('')):
        return await run_in_threadpool(PaperCollection(current_config(), project_root).check,
                                      arxiv_id, selected_path=selected_path, selected_key=selected_key)

    @app.post('/api/collection/recollect')
    async def recollect(operation_id: str = Form(...), target: str = Form(...)):
        try:
            return await run_in_threadpool(PaperCollection(current_config(), project_root).recollect,
                operation_id, target, pdf_loader=pdf_loader)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
