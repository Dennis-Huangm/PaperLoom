from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pymupdf
import pytest

from arxiv_ra.config import AppConfig, VersionTrackingConfig
from arxiv_ra.feedback import FeedbackStore
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.models import Author, Paper
from arxiv_ra.task_runtime import TaskCancelled
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.version_sync import PaperVersionSync
from arxiv_ra.version_tracker import VersionTracker
from arxiv_ra.web_catalog import version_tracking_data


def paper(version=3, aid="2501.00001"):
    return Paper(aid, "Revision-aware research", [Author("Researcher")], "An abstract.",
                 ["cs.AI"], "cs.AI", datetime(2025, 1, 1, tzinfo=timezone.utc),
                 datetime(2026, 9, 23, tzinfo=timezone.utc),
                 f"https://arxiv.org/abs/{aid}v{version}", f"https://arxiv.org/pdf/{aid}v{version}", version)


class Arxiv:
    def __init__(self):
        self.latest = 3
        self.queries = []
        self.downloads = []
        self.fail_download = False
        self.fail_lookup = False

    def get(self, aid):
        self.queries.append(aid)
        if self.fail_lookup:
            raise OSError("arXiv offline")
        base, _, version = aid.partition("v")
        return paper(int(version) if version else self.latest, base)

    def get_many(self, ids):
        return [self.get(aid) for aid in ids]

    def download_pdf(self, value, path):
        self.downloads.append((value.arxiv_id, value.version, value.pdf_url))
        if self.fail_download:
            path.write_bytes(b"partial")
            raise OSError("download interrupted")
        with pymupdf.open() as document:
            document.new_page().insert_text((40, 40), f"Revision {value.version}")
            document.save(path)


@pytest.fixture
def setup(tmp_path):
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="test", profile_name="Test",
                       version_tracking=VersionTrackingConfig(include_zotero=False))
    arxiv = Arxiv()
    clients = SimpleNamespace(arxiv=arxiv, alphaxiv=None)
    sync = PaperVersionSync(config, tmp_path, clients=clients)
    library = PaperLibraryStore(sync.output_root, "test")
    library.add({"paper": paper(1).to_dict()}, "Test", "2026-09-01")
    return sync, arxiv, library


def operation(sync, version=3):
    return read_json(sync.state_path, {})["operations"][str(version)]


def test_revision_sync_preserves_collected_conference_evidence(setup):
    sync, arxiv, library = setup
    saved = paper(1)
    saved.discovery_sources = ['conference']
    saved.conference_publications = [{'conference': 'icml', 'year': 2024, 'paper_type': 'long',
        'evidence_url': 'https://proceedings.mlr.press/example.html'}]
    library.add({'paper': saved.to_dict()}, 'Test')
    sync.sync(saved.arxiv_id)
    result = library.all()[saved.arxiv_id]['paper']
    assert result['version'] == 3
    assert result.get('conference_publications') == saved.conference_publications
    assert read_json(sync.root / 'v3/metadata.json')['paper']['conference_publications'] == saved.conference_publications


def test_latest_sync_preserves_history_and_pins_pdf(setup):
    sync, arxiv, library = setup
    before = deepcopy(library.all()[paper().arxiv_id])
    historical = sync.output_root / "2026-09-01" / "recommendations-test.json"
    write_json(historical, [{"paper": paper(1).to_dict()}])
    original = historical.read_bytes()
    sync.sync(paper().arxiv_id)
    assert operation(sync)["status"] == "succeeded"
    saved = library.all()[paper().arxiv_id]
    assert saved["paper"]["version"] == 3
    assert saved["saved_at"] == before["saved_at"]
    assert saved["source_date"] == "2026-09-01"
    assert read_json(sync.root / "v3/library-before.json", {}) == before
    assert historical.read_bytes() == original
    assert arxiv.downloads == [(paper().arxiv_id, 3, paper().pdf_url)]
    old_pdf = (sync.root / "v3/paper.pdf").read_bytes()
    arxiv.latest = 4
    sync.sync(paper().arxiv_id)
    assert (sync.root / "v3/paper.pdf").read_bytes() == old_pdf
    assert (sync.root / "v4/paper.pdf").is_file()


def test_download_failure_is_retryable_without_updating_saved_version(setup):
    sync, arxiv, library = setup
    arxiv.fail_download = True
    sync.sync(paper().arxiv_id)
    assert operation(sync)["status"] == "failed"
    assert library.all()[paper().arxiv_id]["paper"]["version"] == 1
    assert not (sync.root / "v3/metadata.json").exists()
    arxiv.fail_download = False
    arxiv.latest = 4
    sync.sync(paper().arxiv_id, target_version=3, retry=True)
    assert arxiv.queries[-1] == "2501.00001v3"
    assert library.all()[paper().arxiv_id]["paper"]["version"] == 3
    assert operation(sync)["status"] == "succeeded"


def test_latest_lookup_failure_never_uses_old_cached_revision(setup):
    sync, arxiv, library = setup
    sync.sync(paper().arxiv_id)
    arxiv.fail_lookup = True
    with pytest.raises(RuntimeError, match="未能确认目标版本"):
        sync.sync(paper().arxiv_id)
    assert "arXiv offline" in read_json(sync.state_path, {})["latest_error"]
    assert len(arxiv.downloads) == 1
    assert library.all()[paper().arxiv_id]["paper"]["version"] == 3


def test_retry_runs_only_failed_steps_and_remains_pinned(setup, monkeypatch):
    sync, arxiv, _ = setup
    calls = []

    def generate(value, folder):
        calls.append("report")
        path = sync.output_root / "2026-09-23/reports/test/report.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Full report", encoding="utf-8")
        path.with_suffix(".html").write_text("Full report", encoding="utf-8")
        return {"path": path.relative_to(sync.output_root).as_posix()}

    def zotero(*args):
        calls.append("zotero")
        raise OSError("Zotero offline")

    def obsidian(*args):
        calls.append("obsidian")
        path = sync.output_root / "note.md"
        path.write_text("User note", encoding="utf-8")
        return {"signature": args[-1], "path": str(path)}

    monkeypatch.setattr(sync, "_report", generate)
    monkeypatch.setattr(sync, "_zotero", zotero)
    monkeypatch.setattr(sync, "_obsidian", obsidian)
    sync.sync(paper().arxiv_id, report=True, zotero=True, obsidian=True)
    assert calls == ["report", "zotero", "obsidian"]
    assert operation(sync)["status"] == "partial"
    monkeypatch.setattr(sync, "_zotero", lambda *args: (calls.append("retry-zotero") or {"signature": args[-1]}))
    arxiv.fail_lookup = True
    sync.sync(paper().arxiv_id, target_version=3, retry=True)
    assert calls == ["report", "zotero", "obsidian", "retry-zotero", "obsidian"]
    assert len(arxiv.downloads) == 1
    assert operation(sync)["status"] == "succeeded"


def test_degraded_report_can_be_upgraded_without_downloading_again(setup, monkeypatch):
    sync, arxiv, _ = setup
    monkeypatch.setattr(sync, "_report", lambda *_: {"status": "degraded"})
    sync.sync(paper().arxiv_id, report=True)
    assert operation(sync)["status"] == "partial"
    monkeypatch.setattr(sync, "_report", lambda *_: {})
    sync.sync(paper().arxiv_id, target_version=3, retry=True)
    assert operation(sync)["steps"]["report"]["status"] == "succeeded"
    assert len(arxiv.downloads) == 1


def test_sync_does_not_restore_removed_library_entry(setup, monkeypatch):
    sync, arxiv, library = setup
    original = arxiv.download_pdf

    def download(value, path):
        original(value, path)
        library.remove(value.arxiv_id)

    monkeypatch.setattr(arxiv, "download_pdf", download)
    sync.sync(paper().arxiv_id)
    assert not library.contains(paper().arxiv_id)
    assert operation(sync)["steps"]["library"]["status"] == "skipped"


def test_cancellation_preserves_download_for_retry(setup, monkeypatch):
    sync, arxiv, _ = setup

    def cancel(*args):
        raise TaskCancelled()

    monkeypatch.setattr(sync, "_report", cancel)
    with pytest.raises(TaskCancelled):
        sync.sync(paper().arxiv_id, report=True)
    assert operation(sync)["status"] == "interrupted"
    assert operation(sync)["steps"]["download"]["status"] == "succeeded"
    assert operation(sync)["steps"]["report"]["status"] == "interrupted"
    monkeypatch.setattr(sync, "_report", lambda *_: {})
    sync.sync(paper().arxiv_id, target_version=3, retry=True)
    assert len(arxiv.downloads) == 1


def test_wrong_revision_is_rejected_before_download(setup, monkeypatch):
    sync, arxiv, _ = setup
    monkeypatch.setattr(arxiv, "get", lambda _: paper(4))
    with pytest.raises(RuntimeError, match="不匹配"):
        sync.sync(paper().arxiv_id, target_version=3)
    assert not arxiv.downloads


def test_sync_metadata_and_status_are_profile_scoped(setup):
    sync, arxiv, _ = setup
    sync.sync(paper().arxiv_id)
    other = deepcopy(sync.config)
    other.profile_id = "other"
    service = PaperVersionSync(other, sync.project_root, clients=sync.clients)
    arxiv.latest = 4
    service.sync(paper().arxiv_id)
    assert version_tracking_data(sync.output_root, "test")["items"][0]["local_version"] == 3
    assert version_tracking_data(sync.output_root, "other")["items"][0]["local_version"] == 4


def test_tracking_uses_saved_papers_excludes_dismissed_and_rotates(setup):
    sync, arxiv, library = setup
    for aid in ["2501.00002", "2501.00003", "2501.00004"]:
        library.add({"paper": paper(1, aid).to_dict()}, "Test")
    FeedbackStore(sync.output_root, "test").set(paper(1, "2501.00004").to_dict(), "not_relevant")
    # Even a historical report must not re-add a dismissed paper.
    write_json(sync.output_root / "2026-09-01/reports/dismissed/metadata.json",
               {"profile_id": "test", "paper": paper(1, "2501.00004").to_dict()})
    sync.config.version_tracking.max_tracked = 1
    tracker = VersionTracker(sync.config, sync.project_root, clients=sync.clients)
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    for _ in range(3):
        tracker.check(now)
    assert arxiv.queries == ["2501.00001", "2501.00002", "2501.00003"]
    assert read_json(tracker.state_path, {})["coverage"] == {"total": 3, "checked": 1, "pending": 2}


def test_tracking_batch_error_falls_back_per_paper(setup, monkeypatch):
    sync, _, library = setup
    library.add({"paper": paper(1, "2501.00002").to_dict()}, "Test")

    def get_many(ids):
        if len(ids) > 1 or ids == ["2501.00001"]:
            raise OSError("one paper unavailable")
        return [paper(3, ids[0])]

    monkeypatch.setattr(sync.clients.arxiv, "get_many", get_many)
    tracker = VersionTracker(sync.config, sync.project_root, clients=sync.clients)
    tracker.check()
    state = read_json(tracker.state_path, {})["items"]
    assert state["2501.00001"]["check_error"]
    assert state["2501.00002"]["latest_version"] == 3


def test_failed_diff_keeps_detection_and_retries_without_duplicate(setup, monkeypatch):
    sync, arxiv, _ = setup
    tracker = VersionTracker(sync.config, sync.project_root, clients=sync.clients)
    arxiv.latest = 1
    tracker.check()
    arxiv.latest = 3

    def failure(*args):
        raise OSError("PDF unavailable")

    monkeypatch.setattr(tracker, "_version_event", failure)
    tracker.check()
    item = read_json(tracker.state_path, {})["items"][paper().arxiv_id]
    assert item["latest_version"] == 3
    assert item["events"][0]["status"] == "failed"
    detected = item["events"][0]["detected_at"]
    monkeypatch.setattr(tracker, "_version_event", lambda *_: {"detected_at": "later"})
    tracker.check()
    events = read_json(tracker.state_path, {})["items"][paper().arxiv_id]["events"]
    assert len(events) == 1
    assert events[0]["status"] == "succeeded"
    assert events[0]["detected_at"] == detected


def test_sync_real_report_pipeline_reuses_pinned_pdf(setup):
    from arxiv_ra.models import VerifiedMetadata
    from arxiv_ra.pdf_pipeline import PDFParser
    from arxiv_ra.report import ReportGenerator

    sync, arxiv, _ = setup
    sync.config.pdf.parser = "pymupdf"
    sync.config.pdf.use_docling_if_available = False
    sync.clients.llm = SimpleNamespace(enabled=False)
    sync.clients.verifier = SimpleNamespace(verify=lambda _: VerifiedMetadata())
    sync.clients.parser = PDFParser(sync.config.pdf)
    sync.clients.arxiv_html = SimpleNamespace(fetch=lambda *_: [])
    sync.clients.reporter = ReportGenerator(sync.clients.llm, sync.config.llm)
    sync.sync(paper().arxiv_id, report=True)
    result = operation(sync)
    assert len(arxiv.downloads) == 1
    assert result["steps"]["report"]["status"] == "degraded"
    path = sync.output_root / result["steps"]["report"]["path"]
    metadata = read_json(path.with_name("metadata.json"), {})
    assert metadata["paper"]["version"] == 3
    assert metadata["report_quality"] == "abstract"
    assert path.with_suffix(".html").is_file()


def test_versions_page_shows_saved_and_local_versions_and_retry(setup, tmp_path):
    import yaml
    from fastapi.testclient import TestClient
    from arxiv_ra.web import create_app
    from arxiv_ra.profiles import ProfileManager

    sync, arxiv, _ = setup
    arxiv.fail_download = True
    sync.sync(paper().arxiv_id)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"profile_id": "test", "output_dir": str(sync.output_root)}), encoding="utf-8")
    profiles = ProfileManager(tmp_path)
    profiles.save({"id": "test", "name": "Test"})
    profiles.activate("test")
    with TestClient(create_app(config_path)) as client:
        response = client.get("/versions")
        assert response.status_code == 200
        assert "继续 v3 未完成步骤" in response.text
        assert "本地 PDF" in response.text
        assert "v1" in response.text and "v3" in response.text
        assert 'id="job-list"' in response.text
        assert "/api/jobs/version-sync" in response.text


def test_version_sync_api_validates_input_and_captures_options(tmp_path, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from arxiv_ra.web import create_app
    from arxiv_ra.profiles import ProfileManager

    config_path = tmp_path / "config.yaml"
    config_path.write_text("output_dir: run\nprofile_id: test\n", encoding="utf-8")
    profiles = ProfileManager(tmp_path)
    profiles.save({"id": "test", "name": "Test"})
    profiles.activate("test")
    seen = []
    done = threading.Event()

    def sync(self, aid, **options):
        seen.append((aid, self.config.profile_id, options))
        done.set()
        return tmp_path / "run/result.html"

    monkeypatch.setattr(PaperVersionSync, "sync", sync)
    with TestClient(create_app(config_path)) as client:
        for data in ({"arxiv_id": "../file"}, {"arxiv_id": "2501.00001v3"},
                     {"arxiv_id": "2501.00001", "retry": "true"},
                     {"arxiv_id": "2501.00001", "target_version": "0"}):
            assert client.post("/api/jobs/version-sync", data=data).status_code == 400
        response = client.post("/api/jobs/version-sync", data={"arxiv_id": "2501.00001", "report": "true", "obsidian": "true"})
        assert response.status_code == 202
        assert done.wait(3)
        assert seen == [("2501.00001", "test", {"target_version": None, "retry": False, "report": True,
                                              "zotero": False, "obsidian": True})]


def test_removed_artifacts_are_not_displayed_as_available(setup):
    sync, _, library = setup
    sync.sync(paper().arxiv_id)
    tracker = VersionTracker(sync.config, sync.project_root, clients=sync.clients)
    tracker.check()
    library.remove(paper().arxiv_id)
    (sync.root / "v3/paper.pdf").unlink()
    item = version_tracking_data(sync.output_root, "test")["items"][0]
    assert not item.get("saved_version")
    assert not item.get("local_version")


def test_sync_cli_exposes_optional_exports_and_pinned_retry():
    from arxiv_ra.cli import build_parser
    args = build_parser().parse_args(["sync", "2501.00001", "--target-version", "3", "--retry"])
    assert args.retry and args.target_version == 3
    assert not args.report and not args.zotero and not args.obsidian


def test_sync_obsidian_keeps_handwritten_content_and_old_attachment(setup, tmp_path):
    from arxiv_ra.config import ObsidianConfig
    sync, arxiv, _ = setup
    vault = tmp_path / "vault"
    vault.mkdir()
    sync.config.obsidian = ObsidianConfig(enabled=True, vault_path=str(vault), copy_pdf=True)
    sync.clients.llm = SimpleNamespace(enabled=False)
    arxiv.latest = 1
    sync.sync(paper().arxiv_id, obsidian=True)
    first = operation(sync, 1)["steps"]["obsidian"]
    assert first["status"] == "succeeded", first
    from pathlib import Path
    note = Path(first["path"])
    note.write_text(note.read_text(encoding="utf-8") + "\nMy handwritten research notes.\n", encoding="utf-8")
    old_pdf = next(vault.glob("**/v1/paper.pdf"))
    old_content = old_pdf.read_bytes()
    arxiv.latest = 3
    sync.sync(paper().arxiv_id, obsidian=True)
    assert operation(sync)["steps"]["obsidian"]["status"] == "succeeded"
    assert "My handwritten research notes." in note.read_text(encoding="utf-8")
    assert "version: 3" in note.read_text(encoding="utf-8")
    assert old_pdf.read_bytes() == old_content
    assert next(vault.glob("**/v3/paper.pdf")).is_file()


def test_sync_zotero_receives_exact_revision_files_and_closes_client(setup, monkeypatch):
    from unittest.mock import Mock
    import arxiv_ra.version_sync as module
    sync, _, _ = setup
    adapter = SimpleNamespace(client=SimpleNamespace(close=Mock()), library_identity=lambda: "test/users/0",
                              save_paper=Mock(return_value=SimpleNamespace(to_dict=lambda: {"item_key": "ITEM", "created": True})))
    monkeypatch.setattr(module, "ZoteroClient", lambda _: adapter)
    sync.sync(paper().arxiv_id, zotero=True)
    args, kwargs = adapter.save_paper.call_args
    assert args[0]["version"] == 3
    assert kwargs["pdf_path"] == sync.root / "v3/paper.pdf"
    assert kwargs["report_path"] is None
    adapter.client.close.assert_called_once()
    assert operation(sync)["steps"]["zotero"]["item_key"] == "ITEM"


def test_successful_sync_rechecks_current_library_state(setup):
    sync, arxiv, library = setup
    sync.sync(paper().arxiv_id)
    library.remove(paper().arxiv_id)
    library.add({'paper': paper(1).to_dict()}, 'Test')
    sync.sync(paper().arxiv_id)
    assert library.all()[paper().arxiv_id]['paper']['version'] == 3
    library.remove(paper().arxiv_id)
    sync.sync(paper().arxiv_id)
    assert not library.contains(paper().arxiv_id)
    assert len(arxiv.downloads) == 1


def test_successful_export_rechecks_external_item_on_version_retry(setup, monkeypatch):
    from unittest.mock import Mock
    from arxiv_ra.zotero import ZoteroConflict
    import arxiv_ra.version_sync as module
    sync, arxiv, _ = setup
    adapter = SimpleNamespace(client=SimpleNamespace(close=Mock()), library_identity=lambda: 'test/users/0',
        save_paper=Mock(side_effect=[SimpleNamespace(to_dict=lambda: {'item_key': 'ITEM', 'created': True}),
                                    ZoteroConflict('External item was deleted')]))
    monkeypatch.setattr(module, 'ZoteroClient', lambda _: adapter)
    sync.sync(paper().arxiv_id, zotero=True)
    assert operation(sync)['steps']['zotero']['status'] == 'succeeded'
    sync.sync(paper().arxiv_id, target_version=3, retry=True)
    assert operation(sync)['steps']['zotero']['status'] == 'failed'
    assert 'External item was deleted' in operation(sync)['steps']['zotero']['error']
    assert len(arxiv.downloads) == 1


def test_sync_repairs_nonempty_damaged_pdf(setup):
    sync, arxiv, _ = setup
    sync.sync(paper().arxiv_id)
    path = sync.root / 'v3/paper.pdf'
    path.write_bytes(b'bad pdf')
    sync.sync(paper().arxiv_id)
    assert len(arxiv.downloads) == 2
    with pymupdf.open(path) as document:
        assert document.page_count == 1
    assert operation(sync)['status'] == 'succeeded'


def test_version_export_exposes_shared_collection_receipt(setup, tmp_path):
    from arxiv_ra.config import ObsidianConfig
    sync, _, _ = setup
    vault = tmp_path / 'vault'
    vault.mkdir()
    sync.config.obsidian = ObsidianConfig(enabled=True, vault_path=str(vault))
    result = sync.sync(paper().arxiv_id, obsidian=True)
    assert '收录回执' in result.read_text(encoding='utf-8')
