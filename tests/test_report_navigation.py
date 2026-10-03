"""Report-reader navigation through the public HTTP and HTML boundaries."""
import os

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from arxiv_ra.utils import write_json
from arxiv_ra.web import create_app


def test_report_catalog_has_one_entry_per_paper_and_only_public_fields(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\n", encoding="utf-8")
    root = tmp_path / "run"

    def report(folder, aid, version, profile, updated, date="2026-10-01"):
        directory = root / date / "reports" / folder
        directory.mkdir(parents=True)
        write_json(directory / "metadata.json", {
            "profile_id": profile, "profile_name": profile,
            "paper": {"arxiv_id": aid, "version": version,
                      "title": f"Paper {aid}", "authors": [{"name": "Ada"}]},
        })
        (directory / "report.md").write_text(f"# Paper {aid}\n\n## Summary\nText", encoding="utf-8")
        (directory / "report.html").write_text("legacy HTML", encoding="utf-8")
        os.utime(directory / "report.html", (updated, updated))
        return directory

    report("old", "2608.00001", 1, "alpha", 100)
    selected = report("selected", "2608.00001", 2, "beta", 200)
    report("recent", "2608.00002", 1, "alpha", 210, date="2026-09-30")
    with TestClient(create_app(config)) as client:
        response = client.get("/api/reports")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        entries = response.json()["reports"]
        assert [entry["arxiv_id"] for entry in entries] == ["2608.00002", "2608.00001"]
        chosen = entries[1]
        assert chosen["version"] == 2
        assert chosen["authors"] == ["Ada"]
        assert {part["id"] for part in chosen["directions"]} == {"alpha", "beta"}
        assert set(chosen) == {"title", "authors", "arxiv_id", "version", "directions",
                               "date", "updated_at", "report_url", "report_id"}
        assert chosen["report_url"].endswith("selected/report.html")
        # A historical report acquires navigation without regenerating its files.
        page = client.get("/artifacts/" + (selected / "report.html").relative_to(root).as_posix())
        tree = BeautifulSoup(page.text, "html.parser")
        panel = tree.select_one("#report-papers")
        assert panel["data-arxiv-id"] == "2608.00001"
        assert tree.select_one("#report-papers-toggle")["aria-controls"] == "report-papers"
        assert "Text" in tree.select_one(".report-article").get_text()


def test_legacy_report_can_highlight_its_paper_without_a_library_action(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\n", encoding="utf-8")
    directory = tmp_path / "run" / "2026-10-01" / "reports" / "legacy"
    directory.mkdir(parents=True)
    write_json(directory / "metadata.json", {"paper": {"arxiv_id": "2608.00001v1", "title": "Legacy"}})
    (directory / "report.md").write_text("# Legacy\n\nText", encoding="utf-8")
    (directory / "report.html").write_text("old", encoding="utf-8")
    with TestClient(create_app(config)) as client:
        page = client.get("/artifacts/2026-10-01/reports/legacy/report.html")
        assert page.status_code == 200
        tree = BeautifulSoup(page.text, "html.parser")
        assert tree.select_one("#report-papers")["data-arxiv-id"] == "2608.00001v1"
        assert tree.select_one("#report-library-add") is None


def test_empty_report_catalog(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\n", encoding="utf-8")
    with TestClient(create_app(config)) as client:
        assert client.get("/api/reports").json() == {"reports": []}
