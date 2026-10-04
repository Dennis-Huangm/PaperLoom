"""HTTP boundary for paper reading conversations."""
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Literal

from .reading_chat import ReadingService
from .utils import write_json


class StartReading(BaseModel):
    arxiv_id: str = Field(min_length=1, max_length=100)
    report_id: str = ''
    new: bool = False
    origin: str = ''
    source_date: str = ''


class RenameReading(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class RollbackReading(BaseModel):
    message_id: str = Field(min_length=1, max_length=100)


class ReadingMessage(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    request_id: str = Field(min_length=1, max_length=100)
    selection: str = Field(default='', max_length=20000)
    report_id: str = ''
    images: list[str] = Field(default_factory=list, max_length=4)
    retry_of: str = ''


class ReadingSettings(BaseModel):
    independent: bool = False
    model: str = Field(default='', max_length=200)
    base_url: str = Field(default='', max_length=1000)
    api_key: str = Field(default='', max_length=2000)
    clear_key: bool = False
    images: bool = True
    reasoning_effort: Literal['', 'low', 'medium', 'high'] = 'high'
    max_tools: int = Field(default=10, ge=1, le=30)
    max_tokens: int = Field(default=4000, ge=256, le=32000)


def register_reading_routes(app, templates, context, current_config, jobs, output_root):
    service = ReadingService(output_root, jobs)
    app.state.reading = service

    @app.get('/reading')
    def page(request: Request, arxiv_id: str = '', report_id: str = '', origin: str = '', source_date: str = ''):
        return templates.TemplateResponse(request=request, name='reading.html',
            context=context(request, 'reading', arxiv_id=arxiv_id, report_id=report_id,
                            origin=origin, source_date=source_date))

    @app.get('/api/reading/settings')
    def settings():
        return {**service.settings(current_config()), 'profile_id': current_config().profile_id}

    @app.put('/api/reading/settings')
    def save_settings(body: ReadingSettings):
        from .web_settings import update_dotenv
        from urllib.parse import urlsplit
        if any(c in body.api_key + body.base_url + body.model for c in '\r\n'):
            raise HTTPException(400, '配置不能含换行')
        if body.independent and not body.model.strip():
            raise HTTPException(400, '请填写阅读模型名称')
        if body.base_url:
            parsed = urlsplit(body.base_url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(400, '请输入不含凭据的 HTTP 服务地址')
        updates = {'PAPERLOOM_READING_BASE_URL': body.base_url}
        if body.api_key:
            updates['PAPERLOOM_READING_API_KEY'] = body.api_key
        with service.lock:
            update_dotenv(app.state.project_root / '.env', updates,
                          {'PAPERLOOM_READING_API_KEY'} if body.clear_key else set())
            write_json(service.folder / 'settings' / 'config.json',
                       body.model_dump(exclude={'api_key', 'clear_key', 'base_url'}))
        return service.settings(current_config())

    def run(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/reading/sessions')
    def sessions(arxiv_id: str = '', q: str = ''):
        return service.list(arxiv_id, q)

    @app.post('/api/reading/sessions')
    def start(body: StartReading):
        return run(service.create, current_config(), body.arxiv_id, report_id=body.report_id,
                   new=body.new, origin=body.origin, source_date=body.source_date)

    @app.get('/api/reading/sessions/{sid}')
    def detail(sid: str):
        return run(service.present, sid)

    @app.patch('/api/reading/sessions/{sid}')
    def rename(sid: str, body: RenameReading):
        return run(service.rename, sid, body.title)

    @app.delete('/api/reading/sessions/{sid}')
    def delete(sid: str):
        run(service.delete, sid)
        return {'deleted': True}

    @app.post('/api/reading/sessions/{sid}/messages')
    def send(sid: str, body: ReadingMessage):
        return run(service.submit, sid, current_config(), **body.model_dump())

    @app.post('/api/reading/sessions/{sid}/stop')
    def stop(sid: str):
        return run(service.stop, sid)

    @app.post('/api/reading/sessions/{sid}/rollback')
    def rollback(sid: str, body: RollbackReading):
        return run(service.rollback, sid, body.message_id)

    @app.get('/api/reading/sources/{source_id}')
    def source(source_id: str):
        import re
        if not re.fullmatch(r'[a-f0-9]{64}', source_id):
            raise HTTPException(400, '无效来源')
        path = service.folder / 'sources' / f'{source_id}.pdf'
        if not path.exists():
            raise HTTPException(404, '原文文件已缺失；历史摘录仍可查看')
        return FileResponse(path, media_type='application/pdf')
