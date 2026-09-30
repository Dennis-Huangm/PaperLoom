"""Web entry points for explicit paper collection."""
from pathlib import Path
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Form, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .collaboration import PaperCollection


LABELS = {'succeeded': '已完成', 'failed': '失败，可重试', 'partial': '部分完成',
          'running': '执行中', 'pending': '待处理', 'interrupted': '已中断，可重试',
          'needs_attention': '需要处理', 'unlinked': '未关联', 'verified': '已验证',
          'unconfigured': '尚未配置', 'unverified': '待验证的历史记录'}

TOOL_NAMES = {'zotero': 'Zotero', 'obsidian': 'Obsidian', 'obsidian_link': '笔记中的文献链接'}


def collection_targets(config, state, report_path):
    """Translate tool settings and associations into readable destinations."""
    vault = config.obsidian.vault_path.strip()
    folder = config.obsidian.root_folder.strip()
    root_label = '知识库根目录' if folder in {'', '.', './', '.\\'} else folder
    descriptions = {
        'verified': '已有可靠关联，可以直接打开。',
        'unlinked': '尚未确认这篇论文的关联，可检查已有资料或进行收录。',
        'unconfigured': '请先在设置中启用并配置此工具。',
        'unverified': '历史关联等待验证，请先检查关联。',
        'needs_attention': '关联需要处理，请检查已有资料。',
    }
    result = []
    for name in ('zotero', 'obsidian'):
        settings = getattr(config, name)
        association = state['associations'].get(name, {'status': 'unlinked' if settings.enabled else 'unconfigured'})
        status = association['status']
        destination = (config.zotero.collection_name or '我的文库') if name == 'zotero' else (
            f'{Path(vault).name} / {root_label}' if vault else '尚未选择知识库')
        materials = ['论文信息'] if name == 'zotero' else ['论文笔记']
        pdf_enabled = settings.attach_pdf if name == 'zotero' else settings.copy_pdf
        if pdf_enabled:
            materials.append('保存 PDF' if name == 'zotero' else '复制 PDF')
        if report_path and (name == 'obsidian' or settings.attach_report):
            materials.append('链接所选报告' if name == 'zotero' else '所选报告正文')
        result.append({'name': name, 'title': TOOL_NAMES[name], 'enabled': settings.enabled,
            'destination': destination, 'detail': config.zotero.base_url if name == 'zotero' else vault,
            'materials': materials, 'association': association, 'link': state['links'].get(name, ''),
            'status_label': '已关联' if status == 'verified' else LABELS.get(status, '待验证'),
            'description': association.get('error') or descriptions.get(status, '请检查关联状态。')})
    return result


def collection_time(value, timezone):
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo(timezone)).strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return value


def register_collection_routes(app, current_config, selected_paper, project_root, pdf_loader, templates, context,
                               *, selected_report):
    def resolve(arxiv_id, config, origin, source_date, report_id):
        if origin == 'report':
            report = selected_report(arxiv_id, report_id)
            return (report.source_item(), report.markdown_path,
                    report.pdf_path if report.pdf_path.is_file() else None)
        item = selected_paper(arxiv_id, config, origin=origin, source_date=source_date, report_id=report_id)
        return item, None, None

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
        settings = {name: asdict(getattr(config, name)) for name in ('zotero', 'obsidian')}
        for operation in state['operations']:
            operation['config_changed'] = operation['request'].get('settings', settings) != settings
        return templates.TemplateResponse(request, 'collection.html', context(request, 'library',
            arxiv_id=arxiv_id, origin=origin, source_date=source_date, report_id=report_id,
            item=item, report_path=report_path, state=state, labels=LABELS,
            targets=collection_targets(config, state, report_path), tool_names=TOOL_NAMES,
            receipt_time=lambda value: collection_time(value, config.timezone),
            receipt_report=lambda path: Path(path).parent.name if path else '',
            back_url={'report': '/reports', 'recommendation': '/', 'library': '/library'}.get(origin, '/library')))

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
