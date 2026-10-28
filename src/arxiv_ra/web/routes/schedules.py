"""schedules page adapters."""
from __future__ import annotations

from ...profiles.profiles import ProfileManager
from ...runtime.jobs.scheduler import ProfileScheduler
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from typing import Any, Callable
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from ...runtime.jobs.scheduler import WEEKDAYS, STATUS_LABELS as SCHEDULE_STATUS_LABELS


def register_routes(
    app: FastAPI,
    context: Callable[..., dict[str, Any]],
    profiles: ProfileManager,
    scheduler: ProfileScheduler,
    templates: Jinja2Templates,
) -> None:
    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request, saved: str = ""):
        try:
            schedule = scheduler.view()
            error = ""
        except (OSError, ValueError, TypeError, KeyError) as exc:
            schedule = {"enabled": False, "plans": [], "history": [], "errors": {}}
            error = str(exc)
        return templates.TemplateResponse(request, "schedules.html", context(request, "profiles",
            schedule=schedule, schedule_error=error, profiles=profiles.list(), saved=saved,
            plans_by_id={item["profile_id"]: item for item in schedule["plans"]},
            weekdays=WEEKDAYS, schedule_statuses=SCHEDULE_STATUS_LABELS))

    @app.post("/schedules/enabled")
    def set_schedule_enabled(profile_id: str = Form(...), enabled: bool = Form(False)):
        try:
            scheduler.set_enabled(enabled)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/schedules?saved=enabled", status_code=303)

    @app.post("/schedules/profile/{target_profile}")
    def save_schedule(target_profile: str, profile_id: str = Form(...), enabled: bool = Form(False),
                      time: str = Form(...), timezone: str = Form(...), weekdays: list[int] = Form(...),
                      send_email: bool = Form(False)):
        try:
            scheduler.save_profile(target_profile, {"enabled": enabled, "time": time, "timezone": timezone,
                "weekdays": weekdays, "send_email": send_email})
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/schedules?saved=profile", status_code=303)
