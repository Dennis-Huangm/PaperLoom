from pathlib import Path
import threading
import time

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from arxiv_ra.utils import write_json
from arxiv_ra.config import AppConfig
from arxiv_ra.job_requests import capture_request
from arxiv_ra.web import create_app
from arxiv_ra.web_catalog import grouped_report_library, report_library


def add_report(root: Path, date: str, folder: str, *, arxiv_id: str = "2407.05600",
               version: int = 1, profile_id: str = "alpha", profile_name: str = "方向 A",
               quality: str = "abstract") -> Path:
    path = root / date / "reports" / folder
    write_json(path / "metadata.json", {
        "profile_id": profile_id,
        "profile_name": profile_name,
        "report_quality": quality,
        "paper": {"arxiv_id": arxiv_id, "version": version, "title": f"Paper {arxiv_id}", "authors": []},
        "verified": {},
    })
    (path / "report.html").write_text("report", encoding="utf-8")
    (path / "paper.pdf").write_bytes(b"pdf")
    return path


def test_report_catalog_has_one_row_per_id_and_all_directions(tmp_path):
    root = tmp_path / "run"
    add_report(root, "2026-09-01", "old", profile_id="alpha", quality="full")
    newer = add_report(root, "2026-09-02", "new", version=2, profile_id="beta", profile_name="方向 B")
    add_report(root, "2026-09-02", "other", arxiv_id="2407.05601")

    grouped = grouped_report_library(report_library(root), {"alpha": "方向 A", "beta": "方向 B"})
    assert len(grouped) == 2
    paper = next(item for item in grouped if item["arxiv_id"] == "2407.05600")
    assert paper["version"] == 2 and paper["report_path"] == newer / "report.html"
    assert paper["directions"] == [{"id": "alpha", "name": "方向 A"}, {"id": "beta", "name": "方向 B"}]


def test_reports_page_filters_by_direction_and_deletes_all_attempts(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: fixture\n", encoding="utf-8")
    root = tmp_path / "run"
    old = add_report(root, "2026-09-01", "old", profile_id="alpha")
    new = add_report(root, "2026-09-02", "new", version=2, profile_id="beta", profile_name="方向 B")
    other = add_report(root, "2026-09-02", "other", arxiv_id="2407.05601")
    app = create_app(config)

    with TestClient(app) as client:
        completed = app.state.jobs.submit("report", "saved report", lambda: new / "report.html")
        deadline = time.monotonic() + 2
        while app.state.jobs.get(completed.id).status in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(.01)
        assert app.state.jobs.get(completed.id).result_url
        page = BeautifulSoup(client.get("/reports").text, "html.parser")
        assert len(page.select(".report-row")) == 2
        assert {x.get_text(strip=True) for x in page.select(".report-direction")} >= {"方向 A", "方向 B"}
        filtered = BeautifulSoup(client.get("/reports?direction=beta").text, "html.parser")
        assert len(filtered.select(".report-row")) == 1
        assert filtered.select_one('.report-row .report-id').get_text().startswith("2407.05600")
        selected = next(item for item in grouped_report_library(report_library(root)) if item["arxiv_id"] == "2407.05600")
        stale = client.post("/reports/delete", data={"arxiv_id": "2407.05600", "report_id": "2026-09-01/reports/old/report.html"}, follow_redirects=False)
        assert stale.status_code == 409
        deleted = client.post("/reports/delete", data={"arxiv_id": "2407.05600", "report_id": selected["report_id"]}, follow_redirects=False)
        assert deleted.status_code == 303
        assert not old.exists() and not new.exists() and other.exists()
        assert app.state.jobs.get(completed.id).result_url is None
        assert len(BeautifulSoup(client.get("/reports").text, "html.parser").select(".report-row")) == 1


def test_report_delete_rejects_invalid_id(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: fixture\n", encoding="utf-8")
    app = create_app(config)
    with TestClient(app) as client:
        response = client.post("/reports/delete", data={"arxiv_id": "../outside", "report_id": "ignored"})
        assert response.status_code == 400


@pytest.mark.parametrize("batch", [False, True])
def test_report_delete_waits_for_active_generation(tmp_path, batch):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: fixture\n", encoding="utf-8")
    root = tmp_path / "run"
    existing = add_report(root, "2026-09-02", "current")
    other = add_report(root, "2026-09-02", "other", arxiv_id="2407.05601")
    app = create_app(config)
    started, release = threading.Event(), threading.Event()
    def holding():
        started.set()
        release.wait(5)
        return existing / "report.html"
    request = capture_request("report", AppConfig(profile_id="fixture", output_dir="run"), tmp_path,
                              arxiv_id="2407.05600v1", snapshot=None)
    with TestClient(app) as client:
        job = app.state.jobs.submit("report", "fixture", holding, request=request, profile_id="fixture")
        try:
            assert started.wait(2)
            catalog = {item["arxiv_id"]: item for item in grouped_report_library(report_library(root))}
            current = catalog["2407.05600"]
            if batch:
                response = client.post("/reports/delete-batch", data={"report_ids": [
                    catalog["2407.05601"]["report_id"], current["report_id"]]})
            else:
                response = client.post("/reports/delete", data={"arxiv_id": "2407.05600", "report_id": current["report_id"]})
            assert response.status_code == 409
            assert existing.exists() and other.exists()
        finally:
            release.set()


def test_batch_delete_selected_reports_and_keep_filters(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: fixture\n", encoding="utf-8")
    root = tmp_path / "run"
    old = add_report(root, "2026-09-01", "old")
    current = add_report(root, "2026-09-02", "current", version=2, profile_id="beta")
    second = add_report(root, "2026-09-02", "second", arxiv_id="2407.05601")
    unselected = add_report(root, "2026-09-02", "unselected", arxiv_id="2407.05602")
    (old / "method-figure.png").write_bytes(b"figure")
    app = create_app(config)
    with TestClient(app) as client:
        job = app.state.jobs.submit("report", "fixture", lambda: current / "report.html")
        deadline = time.monotonic() + 2
        while app.state.jobs.get(job.id).status in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(.01)
        assert app.state.jobs.get(job.id).result_url
        page = BeautifulSoup(client.get("/reports?direction=beta").text, "html.parser")
        choices = page.select('.report-selection')
        assert len(choices) == 1
        assert choices[0]["form"] == "report-bulk-delete-form"
        assert page.select_one('#report-bulk-delete-form button[type="submit"]').has_attr("disabled")
        catalog = {item["arxiv_id"]: item for item in grouped_report_library(report_library(root))}
        first_id = catalog["2407.05600"]["report_id"]
        response = client.post("/reports/delete-batch", data={
            "report_ids": [first_id, catalog["2407.05601"]["report_id"], first_id],
            "q": "Paper", "direction": "beta",
        }, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/reports?q=Paper&direction=beta"
        assert not old.exists() and not current.exists() and not second.exists()
        assert unselected.exists()
        assert app.state.jobs.get(job.id).result_url is None


@pytest.mark.parametrize("invalid", ["2026-09-01/reports/old/report.html", "../outside"])
def test_batch_delete_rejects_stale_or_unknown_selection_before_deleting(tmp_path, invalid):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: fixture\n", encoding="utf-8")
    root = tmp_path / "run"
    old = add_report(root, "2026-09-01", "old")
    current = add_report(root, "2026-09-02", "current", version=2)
    app = create_app(config)
    with TestClient(app) as client:
        report_id = grouped_report_library(report_library(root))[0]["report_id"]
        response = client.post("/reports/delete-batch", data={"report_ids": [report_id, invalid]})
        assert response.status_code == 409
        assert old.exists() and current.exists()
        assert client.post("/reports/delete-batch", data={}).status_code == 422


@pytest.mark.parametrize("batch", [False, True])
def test_delete_report_without_paper_identity(tmp_path, batch):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: fixture\n", encoding="utf-8")
    root = tmp_path / "run"
    orphan = add_report(root, "2026-10-01", "draft-orphan")
    write_json(orphan / "metadata.json", {"evidence": {}})
    other_orphan = add_report(root, "2026-10-01", "other-orphan")
    write_json(other_orphan / "metadata.json", {})
    valid = add_report(root, "2026-10-01", "valid")
    report_id = (orphan / "report.html").relative_to(root).as_posix()
    with TestClient(create_app(config)) as client:
        if batch:
            response = client.post("/reports/delete-batch", data={"report_ids": [report_id]}, follow_redirects=False)
        else:
            response = client.post("/reports/delete", data={"arxiv_id": "", "report_id": report_id}, follow_redirects=False)
        assert response.status_code == 303, response.text
        assert not orphan.exists()
        assert other_orphan.exists() and valid.exists()
