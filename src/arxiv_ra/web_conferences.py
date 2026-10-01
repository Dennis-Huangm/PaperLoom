"""Profile-scoped, read-only conference searches and explicit collection."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import re
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .conference_scope import scope_from_form
from .discovery import DiscoveryService, DiscoveryError
from .library import PaperLibraryStore
from .models import Paper
from .plan_selection import evaluate_plan, selectable
from .ranker import rank_papers
from .research_clients import ResearchClients
from .task_runtime import task_checkpoint, task_warning
from .utils import read_json, write_json
from .web_jobs import JobContext


def register_conference_routes(app, templates, context, current_config, jobs, output_root, project_root):
    def result_folder(profile_id):
        return output_root / '.conference-search' / hashlib.sha256(profile_id.encode()).hexdigest()

    def read_result(profile_id, search_id):
        if not re.fullmatch(r'[a-f0-9]{32}', search_id):
            raise HTTPException(400, '无效的检索记录')
        result = read_json(result_folder(profile_id) / f'{search_id}.json', {})
        if not result or result.get('profile_id') != profile_id:
            raise HTTPException(404, '当前研究方向没有这次检索记录')
        return result

    @app.get('/conferences')
    def conference_page(request: Request, search_id: str = '', profile_id: str | None = None):
        current = current_config()
        if profile_id is not None and profile_id != current.profile_id:
            raise HTTPException(409, '研究方向已切换，请重新打开会议检索')
        result = None
        if search_id:
            result = read_result(current.profile_id, search_id)
        else:
            paths = sorted(result_folder(current.profile_id).glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            if paths:
                result = read_result(current.profile_id, paths[0].stem)
        scope = asdict(current.discovery)
        scope['mode'] = 'conference'
        return templates.TemplateResponse(request, 'conferences.html', context(request, 'conferences',
            scope=scope, result=result, saved=PaperLibraryStore(output_root, current.profile_id).all()))

    @app.post('/api/jobs/conferences')
    async def search_conferences(request: Request):
        form = await request.form()
        task = JobContext.capture(current_config(), project_root)
        try:
            scope = scope_from_form(form)
            if scope['mode'] != 'conference':
                raise ValueError('手动会议检索仅使用会议论文模式')
            topic_mode = str(form.get('topic_mode', 'direction'))
            if topic_mode not in {'direction', 'custom', 'browse'}:
                raise ValueError('无效的主题选项')
            topic = str(form.get('topic', '')).strip()
            if len(topic) > 4000 or topic_mode == 'custom' and not topic:
                raise ValueError('临时主题必须为 1–4000 字')
            budget = int(str(form.get('max_candidates', '30')))
            if not 1 <= budget <= 5000:
                raise ValueError('关联预算必须为 1–5000')
            discovery = replace(task.config.discovery, **scope, max_candidates=budget)
            if topic_mode != 'direction':
                discovery = replace(discovery, search_plan={}, concept_groups=[], minimum_concept_groups=0,
                    positive_keywords=topic.split() if topic_mode == 'custom' else [], negative_keywords=[],
                    interest_description=topic if topic_mode == 'custom' else '', arxiv_query_terms=[],
                    recent_library_enabled=False)
            task_config = replace(task.config, discovery=discovery)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        search_id = uuid.uuid4().hex
        refresh = 'refresh' in form
        def run():
            with ResearchClients(task_config) as clients:
                service = DiscoveryService(discovery, clients.arxiv, clients.alphaxiv, output_root,
                    conferences=clients.conferences, refresh_conferences=refresh)
                error = ''
                try:
                    result = service.discover()
                except DiscoveryError as exc:
                    result, error = exc.discovery_result, str(exc)
                ranked = rank_papers(result.papers, discovery, task_config.ranking)
                if topic_mode == 'browse':
                    selected, rejected = ranked, []
                else:
                    accepted, rejected = evaluate_plan(ranked, task_config, clients.llm)
                    selected = [p for p in accepted if selectable(p, task_config)]
                for name, state in result.sources.items():
                    if state['status'] in {'failed', 'partial', 'truncated', 'unpublished'}:
                        task_warning(name, state.get('error') or '检索范围部分完成，请查看来源统计')
                payload = {'search_id': search_id, 'profile_id': task.config.profile_id,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'scope': scope, 'error': error,
                    'topic_mode': topic_mode, 'topic': discovery.interest_description,
                    'sources': result.sources, 'papers': [p.to_dict() for p in selected],
                    'filtered_count': len(ranked) - len(selected), 'rejected': rejected}
                task_checkpoint()
                write_json(result_folder(task.config.profile_id) / f'{search_id}.json', payload)
        job = jobs.submit('conferences', f'{task.config.profile_name} · {scope["conference_year_from"]}–{scope["conference_year_to"]}',
            run, identity=task.identity('conferences', scope=scope, topic_mode=topic_mode, topic=topic,
                                    budget=budget, refresh=refresh), profile_id=task.config.profile_id)
        return JSONResponse(asdict(job), status_code=202)

    @app.post('/conferences/save')
    async def save_conference_paper(request: Request):
        form = await request.form()
        current = current_config()
        result = read_result(current.profile_id, str(form.get('search_id', '')))
        aid = str(form.get('arxiv_id', ''))
        snapshot = next((p for p in result['papers'] if p['arxiv_id'] == aid), None)
        if snapshot is None:
            raise HTTPException(404, '该论文不在本次检索结果中')
        paper = Paper.from_dict(snapshot)
        PaperLibraryStore(output_root, current.profile_id).add({'paper': paper.to_dict()}, current.profile_name)
        return RedirectResponse(f'/conferences?search_id={result["search_id"]}&profile_id={current.profile_id}', status_code=303)
