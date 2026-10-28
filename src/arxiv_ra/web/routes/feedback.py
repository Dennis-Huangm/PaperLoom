"""feedback page adapters."""
from __future__ import annotations

from ...config import AppConfig
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Callable
import re
from datetime import datetime
from typing import Any
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from ...papers.feedback import FeedbackStore, VERDICTS
from ...papers.library import PaperLibraryStore
from ...papers.reading_state import ReadingConflict
from ...papers.paper_data import base_id
from ...integrations.obsidian import ObsidianError, ObsidianExporter
from ...utils import read_json

from ..validation import ARXIV_ID_RE


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    output_root: Path,
    project_root: Path,
    selected_paper: Callable[..., dict[str, Any]],
    templates: Jinja2Templates,
) -> None:
    @app.post("/api/feedback", response_class=JSONResponse)
    async def save_feedback(
        arxiv_id: str = Form(...), verdict: str = Form(...), origin: str = Form(""),
        source_date: str = Form(""), scope: str = Form("paper"), terms: str = Form("")
    ) -> JSONResponse:
        arxiv_id = arxiv_id.strip()
        if verdict not in VERDICTS:
            raise HTTPException(status_code=400, detail="无效的阅读反馈")
        if scope != "topic":
            raise HTTPException(status_code=400, detail="单篇排除功能已移除；请加入文献库表达兴趣，或填写主题词进行屏蔽")
        current = current_config()
        item = selected_paper(arxiv_id, current, origin=origin, source_date=source_date)
        store = FeedbackStore(output_root, current.profile_id)
        try:
            entry = store.set(item.get("paper") or {}, verdict, scope=scope,
                              terms=re.split(r"[,，\n]+", terms))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        obsidian_synced = False
        obsidian_warning = ""
        if (
            current.obsidian.enabled
            and current.obsidian.auto_sync
            and current.obsidian.sync_feedback
        ):
            try:
                with ObsidianExporter(current, project_root) as exporter:
                    await run_in_threadpool(
                        exporter.sync_feedback,
                        item.get("paper") or {},
                        entry,
                        item.get("verified") or {},
                    )
                obsidian_synced = True
            except ObsidianError as exc:
                obsidian_warning = str(exc)
        return JSONResponse(
            {
                "feedback": entry,
                "in_library": False,
                "learned_term_count": len(
                    store.learned_terms(
                        PaperLibraryStore(output_root, current.profile_id).all()
                    )
                ),
                "message": "已排除此论文，并屏蔽命中所填主题词的论文，可在反馈记录中撤销",
                "obsidian_synced": obsidian_synced,
                "obsidian_warning": obsidian_warning,
            }
        )

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback_page(request: Request, audit: str = ""):
        current = current_config()
        entries = FeedbackStore(output_root, current.profile_id).all()
        name_pattern = re.compile(re.escape(f"selection-{current.profile_id or 'default'}-") + r"[0-9a-f]{32}\.json")
        paths = sorted((p for p in output_root.glob("????-??-??/selection-*.json") if name_pattern.fullmatch(p.name)),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        runs = []
        payload: dict[str, Any] = {}
        for path in paths:
            key = path.relative_to(output_root).as_posix()
            runs.append({"id": key, "label": f"{path.parent.name} · {datetime.fromtimestamp(path.stat().st_mtime).strftime('%H:%M:%S')} · {path.stem[-6:]}"})
        selected = audit or (runs[0]["id"] if runs else "")
        if selected:
            if selected not in {r["id"] for r in runs}:
                raise HTTPException(status_code=404, detail="筛选记录不存在")
            payload = read_json(output_root / selected, {}) or {}
            if payload.get("profile_id", "") != current.profile_id:
                raise HTTPException(status_code=404, detail="筛选记录不属于当前方向")
        return templates.TemplateResponse(request, "feedback.html", context(
            request, "dashboard", feedback_entries=sorted(entries.values(), key=lambda e: e.get("updated_at", ""), reverse=True),
            audit_runs=runs, selected_audit=selected, selection_audit=payload))

    @app.post("/api/feedback/undo", response_class=JSONResponse)
    def undo_feedback(arxiv_id: str = Form(...), expected_updated_at: str = Form("")):
        current = current_config()
        if not ARXIV_ID_RE.fullmatch(arxiv_id):
            raise HTTPException(status_code=400, detail="无效的 arXiv ID")
        try:
            saved = FeedbackStore(output_root, current.profile_id).state.undo_feedback(base_id(arxiv_id), expected_updated_at)
        except ReadingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"feedback": None, "in_library": saved,
                             "message": "反馈已撤销" + ("，原收藏已恢复" if saved else "") + "；下一次推荐按当前规则重新筛选"})
