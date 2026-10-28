"""settings page adapters."""
from __future__ import annotations

from ...config import AppConfig
from ...profiles.profiles import ProfileManager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from typing import Any, Callable
from ...integrations import zotero as zotero_adapter
import os
from pathlib import Path
from urllib.parse import quote
from fastapi import Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from ...runtime.model_access.model_connection import connection_values, probe_connection
from ...integrations.obsidian import ObsidianExporter, discover_obsidian_vaults
from ...discovery.conference_scope import scope_from_form
from ...integrations.zotero import ZoteroClient
from ...runtime.model_access.settings import (
    build_config_update,
    credential_specs,
    save_config_settings,
    update_dotenv,
)

from ..status import service_status


def register_routes(
    app: FastAPI,
    config_path: Path,
    context: Callable[..., dict[str, Any]],
    current_config: Callable[[], AppConfig],
    output_root: Path,
    profiles: ProfileManager,
    project_root: Path,
    templates: Jinja2Templates,
) -> None:
    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, saved: str = "") -> HTMLResponse:
        current = current_config()
        zotero_connection = zotero_adapter.ZoteroClient(current.zotero, timeout=2.0).status()
        obsidian_connection = ObsidianExporter(current, project_root).status()
        obsidian_open_url = ""
        if obsidian_connection.get("ready"):
            vault_name = Path(str(obsidian_connection.get("vault") or "")).name
            root = current.obsidian.root_folder.strip("./\\")
            home = current.obsidian.home_folder.strip("./\\")
            file_path = "/".join(part for part in (root, home, "Research Hub") if part)
            obsidian_open_url = (
                f"obsidian://open?vault={quote(vault_name)}&file={quote(file_path)}"
            )
        return templates.TemplateResponse(
            request,
            "settings.html",
            context(
                request,
                "settings",
                saved=saved,
                credentials=credential_specs(current),
                zotero_connection=zotero_connection,
                obsidian_connection=obsidian_connection,
                obsidian_vaults=discover_obsidian_vaults(),
                obsidian_open_url=obsidian_open_url,
            ),
        )

    @app.post("/settings/config")
    async def update_settings(request: Request) -> RedirectResponse:
        form = dict(await request.form())
        current = current_config()
        form.setdefault("llm_model", current.llm.model)
        try:
            scope = scope_from_form(form)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        protected = ("interest_description", "arxiv_categories", "arxiv_query_terms", "positive_keywords",
                     "negative_keywords", "seed_papers", "concept_groups", "minimum_concept_groups")
        if current.discovery.search_plan.get("version") == 2:
            form = dict(form)
            for key in protected:
                value = getattr(current.discovery, key)
                form[key] = "\n".join(value) if isinstance(value, list) else str(value)
        try:
            values = build_config_update(form)
            discovery = values.pop("discovery")
            discovery.update(scope)
            ranking = values.pop("ranking")
            save_config_settings(config_path, values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if current.discovery.search_plan.get("version") == 2:
            discovery["search_plan"] = current.discovery.search_plan
            for key in protected:
                discovery[key] = getattr(current.discovery, key)
        if current.profile_id:
            profile = profiles.get(current.profile_id)
            profile.update(discovery=discovery, ranking=ranking)
            profiles.save(profile)
        else:
            save_config_settings(config_path, {"discovery": discovery, "ranking": ranking})
        return RedirectResponse("/settings?saved=config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/settings/credentials/reveal", response_class=JSONResponse)
    def reveal_credential(field: str = Form(...)) -> JSONResponse:
        item = next((item for item in credential_specs(current_config()) if item["field"] == field), None)
        if item is None:
            raise HTTPException(status_code=400, detail="未知的凭据字段")
        return JSONResponse({"value": os.getenv(item["env_name"], "")})

    @app.post("/settings/credentials")
    async def update_credentials(request: Request) -> RedirectResponse:
        form = await request.form()
        current = current_config()
        updates: dict[str, str] = {}
        clear: set[str] = set()
        for item in credential_specs(current):
            field = item["field"]
            env_name = item["env_name"]
            value = str(form.get(field, ""))
            if f"clear_{field}" in form:
                clear.add(env_name)
            elif value:
                updates[env_name] = value.strip() if not item["secret"] else value
        clear.difference_update(updates)
        try:
            if "llm_model" in form:
                model, key, url = connection_values(form, current.llm)
                for env_name, value in ((current.llm.api_key_env, key), (current.llm.base_url_env, url)):
                    if env_name in updates:
                        updates[env_name] = value
            update_dotenv(project_root / ".env", updates, clear)
            if "llm_model" in form:
                save_config_settings(config_path, {"llm": {"model": model}})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/settings?saved=credentials", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/settings/connection-test")
    async def test_model_connection(request: Request) -> JSONResponse:
        try:
            values = connection_values(await request.form(), current_config().llm)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse(await run_in_threadpool(probe_connection, *values))

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        current = current_config()
        return templates.TemplateResponse(
            request,
            "status.html",
            context(
                request,
                "status",
                services=service_status(current, config_path),
                output_root=str(output_root),
            ),
        )
