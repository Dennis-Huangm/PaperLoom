"""HTTP views for direction-owned overview artifacts."""
from dataclasses import asdict

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .direction_overview import OverviewStore, submit_overview
from .render import markdown_with_math


def register_overview_routes(app, profiles, current_config, templates, context, jobs, project_root):
    store = OverviewStore(jobs.output_root)

    def profile_for(profile_id, source):
        if source not in {"draft", "saved"}:
            raise HTTPException(400, "请选择草稿或已保存方向")
        try:
            return profiles.draft(profile_id) if source == "draft" else profiles.get(profile_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, "研究方向不存在") from exc

    def overview_for(profile):
        overview = store.read(profile)
        other_jobs = overview.pop("other_job_ids")
        job = jobs.get(overview["job_id"]) if overview["job_id"] else None
        if (not job or job.status not in {"queued", "running", "cancelling"}) and (not overview["article"] or overview["stale"]):
            for other_id in other_jobs:
                other = jobs.get(other_id)
                if other and other.status in {"queued", "running", "cancelling"}:
                    job = other
                    overview["generation_outdated"] = True
                    break
        if job:
            overview["job"] = asdict(job)
            if job.status in {"queued", "running", "cancelling"}:
                overview["status"] = job.status
            elif job.status in {"failed", "cancelled", "interrupted"}:
                overview["status"] = job.status
                overview["error"] = overview["error"] or job.detail
        elif overview["status"] == "running":
            overview.update(status="interrupted", error="任务已中断，可重新生成；已有文章仍然保留")
        return overview

    @app.get("/api/direction-overviews/{profile_id}")
    def overview_status(profile_id: str, source: str = "saved"):
        return overview_for(profile_for(profile_id, source))

    @app.post("/api/direction-overviews/{profile_id}")
    def generate_overview(profile_id: str, source: str = "saved"):
        profile = profile_for(profile_id, source)
        try:
            job = submit_overview(profile, current_config(), project_root, jobs)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(503, "综述任务提交失败，请稍后重试") from exc
        return JSONResponse(asdict(job), status_code=202)

    @app.post("/profiles/{profile_id}/overview")
    def generate_overview_form(profile_id: str, source: str = "saved"):
        generate_overview(profile_id, source)
        return RedirectResponse(f"/profiles/{profile_id}/overview?source={source}", status_code=303)

    @app.get("/profiles/{profile_id}/overview.md")
    def download_overview(profile_id: str, source: str = "saved"):
        overview = overview_for(profile_for(profile_id, source))
        if not overview["article"]:
            raise HTTPException(404, "方向小综述尚未生成")
        return Response(overview["markdown"], media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{profile_id}-overview.md"'})

    @app.get("/profiles/{profile_id}/overview")
    def read_overview(request: Request, profile_id: str, source: str = "saved"):
        profile = profile_for(profile_id, source)
        overview = overview_for(profile)
        body, _ = markdown_with_math(overview["markdown"])
        return templates.TemplateResponse(request, "direction_overview.html", context(request, "profiles",
            overview_profile=profile, overview=overview, overview_source=source, overview_body=body))

    return overview_for
