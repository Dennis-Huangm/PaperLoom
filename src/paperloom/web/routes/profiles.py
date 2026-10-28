"""profiles page adapters."""
from __future__ import annotations

from ...profiles.profiles import ProfileManager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from typing import Any, Awaitable, Callable
import uuid
from urllib.parse import quote
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from ...runtime.model_access.settings import _int_value, _list_field


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    generate_profile_draft: Callable[..., Awaitable[dict[str, Any]]],
    profiles: ProfileManager,
    templates: Jinja2Templates,
) -> None:
    @app.get("/profiles", response_class=HTMLResponse)
    def profile_page(request: Request, saved: str = "", switched: str = "", updated: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "profiles.html",
            context(
                request,
                "profiles",
                profiles=profiles.list(),
                drafts=profiles.drafts(),
                deleted_drafts=profiles.deleted_drafts(),
                draft_request_id=uuid.uuid4().hex,
                saved=saved,
                switched=switched,
                updated=updated,
            ),
        )

    @app.post("/profiles/create")
    async def create_profile(request: Request) -> RedirectResponse:
        form = await request.form()
        try:
            data: dict = {key: str(form.get(key, "")).strip() for key in ("name", "description")}
            data["request_id"] = str(form.get("request_id", ""))
            for key in ("keywords", "negative_keywords", "reference_ids", "required", "excluded"):
                data[key] = _list_field(str(form.get(key, "")))
            data["recommendation_count"] = _int_value(form, "recommendation_count", 1, 50)
            draft = await generate_profile_draft(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/profiles/drafts/{draft['id']}", status_code=303)

    @app.post("/profiles/{profile_id}/activate")
    def activate_profile(profile_id: str) -> RedirectResponse:
        try:
            profiles.activate(profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(
            f"/profiles?switched={quote(profile_id)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
