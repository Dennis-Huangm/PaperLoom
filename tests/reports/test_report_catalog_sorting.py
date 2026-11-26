"""Report ordering uses original addition and publication dates, never repair mtimes."""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import pytest

from paperloom.utils import write_json
from paperloom.web import create_app


def seed(root: Path, name: str, aid: str, day: int, published: str, *,
         version: int = 1, generated: str = "", profile: str = "alpha") -> Path:
    folder = root / "run" / f"2026-10-{day:02}" / "reports" / name
    write_json(folder / "metadata.json", {
        "profile_id": profile, "profile_name": profile, "generated_at": generated,
        "paper": {"arxiv_id": aid, "title": f"Paper {aid}", "version": version,
                  "published": published, "authors": []},
    })
    (folder / "report.html").write_text("report", encoding="utf-8")
    return folder


@pytest.fixture
def catalog(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\n", encoding="utf-8")
    # A regenerated report must keep its original place in the addition order.
    oldest = seed(tmp_path, "a-old", "2407.00001", 1, "2024-07-03T00:30:00+08:00")
    seed(tmp_path, "a-new", "2407.00001", 20, "", version=2, profile="beta")
    second = seed(tmp_path, "b", "2407.00002", 2, "2024-07-02T23:30:00Z",
                  generated="2026-10-02T22:00:00+08:00")
    seed(tmp_path, "c", "2407.00003", 2, "2024-07-01",
         generated="2026-10-02T15:00:00Z")
    seed(tmp_path, "d", "2407.00004", 4, "invalid-date", generated="invalid-date")
    # Simulate later repairs or copying old files; addition dates still come from metadata.
    for path in (oldest, second):
        os.utime(path / "report.html", (1900000000, 1900000000))
    with TestClient(create_app(config)) as client:
        yield client


@pytest.mark.parametrize(("order", "expected"), [
    ("added_asc", [1, 2, 3, 4]),
    ("added_desc", [4, 3, 2, 1]),
    ("published_asc", [3, 1, 2, 4]),
    ("published_desc", [2, 1, 3, 4]),
    ("opened_desc", [4, 3, 2, 1]),
    ("invalid", [4, 3, 2, 1]),
])
def test_ordering_keeps_directory_and_cards_in_sync(catalog, order, expected):
    response = catalog.get("/reports", params={"sort": order})
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    cards = page.select(".report-row")
    actual = [int(card.select_one(".report-id").get_text().split("·")[0].strip().split(".")[1])
              for card in cards]
    assert actual == expected
    links = page.select(".report-directory-link")
    assert [link.select_one("strong").get_text() for link in links] == [
        card.select_one("h2").get_text() for card in cards]
    for link, card in zip(links, cards):
        assert link["href"] == f"#{card['id']}"
    regenerated = next(card for card in cards if "2407.00001" in card.select_one(".report-id").get_text())
    assert "a-new" in regenerated.select_one("h2 a")["href"]
    assert "添加：2026-10-01" in regenerated.get_text()
    assert "发表：2024-07-03" in regenerated.get_text()


def test_sort_is_retained_in_search_clear_filters_and_delete_forms(catalog):
    page = BeautifulSoup(catalog.get("/reports?q=Paper&direction=alpha&sort=published_asc").text, "html.parser")
    assert page.select_one("#report-sort option[selected]")["value"] == "published_asc"
    clear = page.select_one('[aria-label="清除搜索"]')["href"]
    assert parse_qs(urlsplit(clear).query) == {"direction": ["alpha"], "sort": ["published_asc"]}
    cleared = BeautifulSoup(catalog.get(clear).text, "html.parser")
    assert [card.select_one(".report-id").get_text().strip().split(" ·")[0]
            for card in cleared.select(".report-row")] == ["2407.00003", "2407.00001", "2407.00002", "2407.00004"]
    for form in page.select(".report-delete-form, #report-bulk-delete-form"):
        assert form.select_one('[name="sort"]')["value"] == "published_asc"


@pytest.mark.parametrize("batch", [False, True])
def test_deletion_redirect_keeps_sort_and_filters(catalog, batch):
    page = BeautifulSoup(catalog.get("/reports?sort=published_asc").text, "html.parser")
    form = page.select_one(".report-delete-form")
    data = {"q": "Paper", "direction": "alpha", "sort": "published_asc"}
    data["delete_tokens"] = form.select_one('[name="delete_tokens"]')["value"]
    if batch:
        data["report_ids"] = [form.select_one('[name="report_id"]')["value"]]
    else:
        data.update({key: form.select_one(f'[name="{key}"]')["value"] for key in ("arxiv_id", "report_id")})
    response = catalog.post("/reports/delete-batch" if batch else "/reports/delete", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query) == {
        "q": ["Paper"], "direction": ["alpha"], "sort": ["published_asc"]}


def paper_order(client, query="sort=opened_desc"):
    page = BeautifulSoup(client.get(f"/reports?{query}").text, "html.parser")
    cards = page.select(".report-row")
    assert [link.select_one("strong").get_text() for link in page.select(".report-directory-link")] == [
        card.select_one("h2").get_text() for card in cards]
    return [int(card.select_one(".report-id").get_text().split("·")[0].strip().split(".")[1])
            for card in cards]


def test_recent_opens_reorder_papers_and_survive_new_versions(catalog, tmp_path):
    # Opening an old, non-representative version still updates the shared paper.
    old = "/artifacts/2026-10-01/reports/a-old/report.html"
    second = "/artifacts/2026-10-02/reports/b/report.html"
    assert catalog.get(old).status_code == 200
    assert paper_order(catalog) == [1, 4, 3, 2]
    assert catalog.get(second).status_code == 200
    assert paper_order(catalog) == [2, 1, 4, 3]
    # Refreshing the same report makes it most recently opened again.
    assert catalog.get(old).status_code == 200
    assert paper_order(catalog) == [1, 2, 4, 3]
    # Missing/invalid sorting defaults to recent opens; explicit choices still work.
    assert paper_order(catalog, "") == [1, 2, 4, 3]
    assert paper_order(catalog, "sort=invalid") == [1, 2, 4, 3]
    assert paper_order(catalog, "sort=") == [1, 2, 4, 3]
    assert paper_order(catalog, "sort=added_desc") == [4, 3, 2, 1]
    assert paper_order(catalog, "sort=opened_desc&direction=beta") == [1]
    seed(tmp_path, "a-newest", "2407.00001v3", 21, "", version=3, profile="gamma")
    assert paper_order(catalog) == [1, 2, 4, 3]
    page = BeautifulSoup(catalog.get("/reports?sort=opened_desc").text, "html.parser")
    assert "a-newest" in page.select_one(".report-row h2 a")["href"]
    page = BeautifulSoup(catalog.get("/reports?q=Paper").text, "html.parser")
    assert page.select_one("#report-sort option[selected]")["value"] == "opened_desc"
    assert all(form.select_one('[name="sort"]')["value"] == "opened_desc"
               for form in page.select(".report-delete-form, #report-bulk-delete-form"))


def test_report_open_history_survives_application_restart(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\n", encoding="utf-8")
    seed(tmp_path, "first", "2407.00001", 1, "")
    seed(tmp_path, "second", "2407.00002", 2, "")
    with TestClient(create_app(config)) as first:
        assert paper_order(first) == [2, 1]
        assert first.get("/artifacts/2026-10-01/reports/first/report.html").status_code == 200
        assert paper_order(first) == [1, 2]
    with TestClient(create_app(config)) as restarted:
        assert paper_order(restarted) == [1, 2]


def test_only_successful_report_body_gets_are_recorded(catalog, tmp_path, monkeypatch):
    from paperloom.reports.markdown_rendering import ReportRenderError
    from paperloom.reports.report_visits import ReportVisitStore
    from paperloom.web.web_artifacts import ReportStaticFiles

    url = "/artifacts/2026-10-02/reports/b/report.html"
    store = ReportVisitStore(tmp_path / "run")
    assert catalog.head(url).status_code == 200
    assert catalog.get("/artifacts/2026-10-02/reports/missing/report.html").status_code == 404
    folder = tmp_path / "run" / "2026-10-02" / "reports" / "b"
    (folder / "figure.png").write_bytes(b"image")
    assert catalog.get(url.replace("report.html", "figure.png")).status_code == 200
    assert catalog.get(url.replace("report.html", "metadata.json")).status_code == 200
    assert catalog.get("/api/reports").status_code == 200
    assert paper_order(catalog) == [4, 3, 2, 1]
    assert store.snapshot() == {}

    def fail_render(self, path):
        raise ReportRenderError("invalid report")

    with monkeypatch.context() as patch:
        patch.setattr(ReportStaticFiles, "_report", fail_render)
        assert catalog.get(url).status_code == 422
    assert store.snapshot() == {}
    first = catalog.get(url)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    assert set(store.snapshot()) == {"paper:2407.00002"}
    # A cached HTML-only report is still a genuine repeat open.
    previous = store.snapshot()["paper:2407.00002"]
    assert catalog.get(url, headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    assert store.snapshot()["paper:2407.00002"] > previous
    assert catalog.get("/artifacts/.report-visits/history.json").status_code == 404


def test_rendered_markdown_body_opens_are_recorded(catalog, tmp_path):
    folder = tmp_path / "run" / "2026-10-02" / "reports" / "b"
    (folder / "report.md").write_text("# Paper\n\nA report.", encoding="utf-8")
    url = "/artifacts/2026-10-02/reports/b/report.html"
    response = catalog.get(url)
    assert response.status_code == 200
    assert "A report." in response.text
    assert paper_order(catalog) == [2, 4, 3, 1]


def test_recent_sort_uses_addition_order_for_equal_or_missing_open_times():
    from paperloom.web.web_catalog import sort_report_library

    reports = [
        {"report_id": "a", "added_at": "2026-10-01", "last_opened_at": "2026-11-14T08:00:00+08:00"},
        {"report_id": "b", "added_at": "2026-10-02", "last_opened_at": "2026-11-14T00:00:00Z"},
        {"report_id": "c", "added_at": "2026-10-03", "last_opened_at": "invalid"},
        {"report_id": "d", "added_at": "2026-10-04"},
        {"report_id": "e", "added_at": "invalid"},
    ]
    assert [item["report_id"] for item in sort_report_library(reports, "opened_desc")] == ["b", "a", "d", "c", "e"]
