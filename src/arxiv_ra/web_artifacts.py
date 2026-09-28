"""Safe local report presentation, including artifacts made by older releases."""
from pathlib import Path
import html
import stat

from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .render import report_document
from .markdown_rendering import ReportRenderError
from .utils import read_json


class ReportStaticFiles(StaticFiles):
    def _report(self, path: str) -> str | None:
        # Use StaticFiles' containment and symlink checks for every source file.
        markdown_name = Path(path).with_suffix('.md').as_posix()
        source, info = self.lookup_path(markdown_name)
        if not info or not stat.S_ISREG(info.st_mode):
            return None
        metadata_name = Path(path).with_name('metadata.json').as_posix()
        metadata, metadata_info = self.lookup_path(metadata_name)
        payload = {}
        if metadata_info and stat.S_ISREG(metadata_info.st_mode):
            try:
                payload = read_json(Path(metadata), {}) or {}
            except (OSError, ValueError):
                pass
        if not isinstance(payload, dict):
            payload = {}
        paper = payload.get('paper') or {}
        if not isinstance(paper, dict):
            paper = {}
        # Legacy unscoped artifacts can be saved through the report catalog.
        arxiv_id = str(paper.get('arxiv_id') or '') if payload.get('profile_id') else ''
        return report_document(Path(source).read_text(encoding='utf-8'),
                               str(payload.get('title') or paper.get('title') or Path(path).parent.name), arxiv_id,
                               profile_id=str(payload.get('profile_id') or ''), report_id=Path(path).as_posix())

    async def get_response(self, path, scope):
        if any(part.casefold() in {".jobs", ".search", "backups", "restored", "schedule-config.json", "schedule-state.json"} for part in Path(path).parts):
            from starlette.exceptions import HTTPException
            raise HTTPException(status_code=404)
        if Path(path).name == 'index.html' and Path(path).parts and Path(path).parts[0] == 'citations':
            from .graph_store import read_graph
            from .graph_render import render_graph
            graph_name = Path(path).with_name('graph.json').as_posix()
            graph_path, info = self.lookup_path(graph_name)
            if info and stat.S_ISREG(info.st_mode):
                try:
                    graph = await run_in_threadpool(read_graph, Path(graph_path))
                    document = await run_in_threadpool(render_graph, graph, '/static/')
                except (ValueError, TypeError, KeyError, OSError):
                    return HTMLResponse('<h1>图谱暂时无法读取</h1><p>数据损坏或版本不受支持，请重新生成或更新应用。</p>', status_code=422)
                return HTMLResponse(document, headers={'Cache-Control': 'no-cache'})
        response = await super().get_response(path, scope)
        if scope['method'] in {'GET', 'HEAD'} and Path(path).name == 'report.html':
            try:
                document = await run_in_threadpool(self._report, path)
            except ReportRenderError as exc:
                return HTMLResponse(
                    '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<title>报告排版检查未通过</title><h1>报告排版检查未通过</h1>'
                    f'<p>{html.escape(str(exc))}</p><p><a href="report.md">查看原始 Markdown</a></p></html>',
                    status_code=422, headers={'Cache-Control': 'no-cache'})
            if document is not None:
                return HTMLResponse(document, headers={'Cache-Control': 'no-cache'})
        return response
