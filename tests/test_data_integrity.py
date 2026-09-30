"""Regressions for retained user data and exact report/revision selection."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from arxiv_ra.abstracts import localize_abstracts
from arxiv_ra.config import AppConfig, ObsidianConfig
from arxiv_ra.models import Author, Paper, VerifiedMetadata
from arxiv_ra.obsidian import MANAGED_START, MANAGED_END, ObsidianExporter
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.storage import read_recommendations
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.web import create_app
from arxiv_ra.weekly import WeeklySynthesizer
from arxiv_ra.zotero import ZoteroClient
from arxiv_ra.config import ZoteroConfig


def paper(version=2, aid="2609.00001"):
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    return Paper(aid, "Shared Paper", [Author("Author")], "Source abstract",
                 ["cs.AI"], "cs.AI", now, now,
                 f"https://arxiv.org/abs/{aid}v{version}",
                 f"https://arxiv.org/pdf/{aid}v{version}", version=version)


def config(tmp_path, profile="a"):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return AppConfig(output_dir=str(tmp_path / "run"), profile_id=profile,
                     profile_name=profile, obsidian=ObsidianConfig(
                         enabled=True, vault_path=str(vault), copy_pdf=True))


def exporter(tmp_path, profile="a"):
    return ObsidianExporter(config(tmp_path, profile), tmp_path,
                            clients=SimpleNamespace(llm=SimpleNamespace(enabled=False)))


def report(tmp_path, profile="a", version=2, text="RIGHT", day="2026-09-23"):
    folder = tmp_path / "run" / day / "reports" / f"2609.00001-v{version}-{profile}-shared"
    write_json(folder / "metadata.json", {"profile_id": profile,
               "profile_name": profile, "paper": paper(version).to_dict(), "verified": {}})
    (folder / "report.md").write_text(
        f"# Shared Paper\n\n## 一句话总结\n\n{text}\n\n## 为什么值得阅读\n\n{text}\n", encoding="utf-8")
    (folder / "report.html").write_text(text, encoding="utf-8")
    (folder / "paper.pdf").write_bytes(f"PDF-{text}".encode())
    return folder / "report.md"


def test_obsidian_preserves_both_sides_and_custom_frontmatter(tmp_path):
    exp = exporter(tmp_path)
    note = exp.sync_feedback(paper().to_dict(), {"label": "saved"})
    text = note.read_text(encoding="utf-8")
    text = text.replace("---\n", "---\nuser_property: keep\n", 1)
    text = text.replace(MANAGED_START, "USER_PREFIX\n" + MANAGED_START) + "\nUSER_TAIL\n"
    note.write_text(text, encoding="utf-8")
    exp.sync_feedback(paper().to_dict(), {"label": "changed"})
    updated = note.read_text(encoding="utf-8")
    assert "USER_PREFIX" in updated
    assert "USER_TAIL" in updated
    assert "user_property: keep" in updated
    assert "changed" in updated


def test_obsidian_keeps_directions_and_legacy_manifest_separate(tmp_path):
    a, b = exporter(tmp_path, "a"), exporter(tmp_path, "b")
    first = a.sync_feedback(paper().to_dict(), {"label": "saved"})
    # Seed the original manifest format to exercise upgrade compatibility.
    manifest = a._manifest()
    entry = next(iter(manifest["papers"].values()))
    entry.pop("profile_id", None)
    manifest["papers"] = {paper().arxiv_id: entry}
    a._save_manifest(manifest)
    before = first.read_text(encoding="utf-8")
    second = b.sync_feedback(paper().to_dict(), {"label": "dismissed"})
    assert first != second
    assert first.read_text(encoding="utf-8") == before
    assert a.sync_feedback(paper().to_dict(), {"label": "saved"}) == first
    assert len(a._manifest()["papers"]) == 2


def test_obsidian_full_sync_keeps_own_reports_and_weekly(tmp_path):
    exp = exporter(tmp_path)
    report(tmp_path, "a", text="OWN_REPORT")
    report(tmp_path, "z", text="OTHER_REPORT")
    for profile in ("a", "z"):
        folder = tmp_path / "run/weekly" / f"2026-W39-{profile}"
        write_json(folder / "metadata.json", {"profile_id": profile,
                   "profile_name": profile, "week_id": "2026-W39"})
        (folder / "report.md").write_text(f"WEEKLY_{profile}", encoding="utf-8")
    exp.sync_all()
    note = next((tmp_path / "vault").glob("**/Papers/a/*.md"))
    assert "OWN_REPORT" in note.read_text(encoding="utf-8")
    assert "OTHER_REPORT" not in note.read_text(encoding="utf-8")
    weekly = next((tmp_path / "vault").glob("**/2026-W39 - a.md"))
    assert "WEEKLY_a" in weekly.read_text(encoding="utf-8")
    assert "WEEKLY_z" not in weekly.read_text(encoding="utf-8")


def test_obsidian_attachments_do_not_overwrite_other_revisions(tmp_path):
    exp = exporter(tmp_path)
    old = report(tmp_path, version=1, text="OLD")
    new = report(tmp_path, version=2, text="NEW")
    exp.sync_report(paper(1).to_dict(), {}, old)
    original = next((tmp_path / "vault").glob("**/paper.pdf"))
    original_bytes = original.read_bytes()
    exp.sync_report(paper(2).to_dict(), {}, new)
    assert original.read_bytes() == original_bytes
    assert {p.read_bytes() for p in (tmp_path / "vault").glob("**/paper.pdf")} == {b"PDF-OLD", b"PDF-NEW"}


def test_abstract_and_weekly_evidence_matches_profile_and_revision(tmp_path):
    report(tmp_path, "a", 2, "RIGHT")
    report(tmp_path, "a", 9, "WRONG_REVISION")
    report(tmp_path, "z", 2, "WRONG_PROFILE")
    cfg = config(tmp_path)
    item = {"paper": paper(2).to_dict()}
    localize_abstracts(cfg, tmp_path / "run", [item], generate=False)
    assert item["paper"]["abstract_zh"] == "RIGHT"
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value="report"))
    weekly = WeeklySynthesizer(cfg, tmp_path, clients=SimpleNamespace(llm=llm))
    weekly._generate_markdown("2026-W39", [item], {}, {})
    prompt = llm.chat.call_args.args[1]
    assert "RIGHT" in prompt
    assert "WRONG_REVISION" not in prompt and "WRONG_PROFILE" not in prompt


def test_abstract_cache_does_not_reuse_another_revision(tmp_path, monkeypatch):
    class LLM:
        enabled = True
        def __init__(self, _config):
            self.client = SimpleNamespace(close=lambda: None)
        def library_identity(self):
            return "test-library/users/0"
        def chat(self, *args, **kwargs):
            return '{"papers":[{"id":"2609.00001","abstract_zh":"V1_SUMMARY","recommendation_detail":"V1_REASON"}]}'
    monkeypatch.setattr("arxiv_ra.abstracts.LLMClient", LLM)
    cfg = config(tmp_path)
    localize_abstracts(cfg, tmp_path / "run", [{"paper": paper(1).to_dict()}])
    report(tmp_path, version=2, text="V2 REPORT")
    newer = {"paper": paper(2).to_dict()}
    localize_abstracts(cfg, tmp_path / "run", [newer], generate=False)
    assert newer["paper"]["abstract_zh"] == "V2 REPORT"


def test_daily_publication_retains_earlier_batches_and_versions(tmp_path):
    cfg = config(tmp_path)
    pipeline = DailyPipeline(cfg, tmp_path, clients=SimpleNamespace())
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    day = tmp_path / "run/2026-09-23"
    day.mkdir(parents=True)
    for p in (paper(1), paper(2, "2609.00002"), paper(2)):
        pipeline._publish_digest(day, day.name, [p], {p.arxiv_id: VerifiedMetadata()},
            [{"profile_id": "a", "paper": p.to_dict(), "verified": {}}],
            {p.arxiv_id}, tmp_path / "run/state-a.json", now, demo=False, deliver=False)
    items = read_recommendations(tmp_path / "run", day.name, "a")
    assert {(item["paper"]["arxiv_id"], item["paper"]["version"]) for item in items} == {
        ("2609.00001", 1), ("2609.00001", 2), ("2609.00002", 2)}
    assert set(read_json(tmp_path / "run/state-a.json")["processed"]) == {"2609.00001", "2609.00002"}
    assert "2609.00002" in (day / "index-a.html").read_text(encoding="utf-8")
    weekly = WeeklySynthesizer(cfg, tmp_path, clients=SimpleNamespace())
    collected = weekly._collect(now)
    assert next(item for item in collected if item["paper"]["arxiv_id"] == "2609.00001")["paper"]["version"] == 2


def web_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"output_dir": "run", "discovery": {"interest_description": "a"}}), encoding="utf-8")
    return path


def test_zotero_export_uses_selected_report_and_rejects_ambiguous_id(tmp_path, monkeypatch):
    old = report(tmp_path, "a", 1, "OLD", "2026-09-22")
    report(tmp_path, "b", 2, "NEW")
    saved = []
    class Zotero:
        def __init__(self, _config):
            self.client = SimpleNamespace(close=lambda: None)
        def library_identity(self):
            return "test-library/users/0"
        def save_paper(self, p, verified, profile, **kwargs):
            saved.append((p, profile, kwargs))
            return SimpleNamespace(to_dict=lambda: {"attachments_added": 1, "created": True})
    monkeypatch.setattr("arxiv_ra.web.ZoteroClient", Zotero)
    with TestClient(create_app(web_config(tmp_path))) as client:
        selected = old.with_suffix(".html").relative_to(tmp_path / "run").as_posix()
        response = client.post("/api/zotero/save-report", data={"arxiv_id": paper().arxiv_id, "report_id": selected})
        assert response.status_code == 200
        assert saved[0][0]["version"] == 1
        assert saved[0][2]["report_path"] == old.with_suffix(".html")
        ambiguous = client.post("/api/zotero/save-report", data={"arxiv_id": paper().arxiv_id})
        assert ambiguous.status_code == 409
        invalid = client.post("/api/zotero/save-report", data={"arxiv_id": paper().arxiv_id, "report_id": "../outside.html"})
        assert invalid.status_code in (400, 404)


def test_zotero_pdf_cache_pins_revision(tmp_path, monkeypatch):
    saved = []
    class Zotero:
        def __init__(self, _config):
            self.client = SimpleNamespace(close=lambda: None)
        def library_identity(self):
            return "test-library/users/0"
        def status(self):
            return {"ready": True}
        def save_paper(self, p, verified, profile, **kwargs):
            saved.append(kwargs["pdf_path"].read_bytes())
            return SimpleNamespace(to_dict=lambda: {"attachments_added": 1, "created": True})
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "test-only")
    monkeypatch.setattr("arxiv_ra.web.ZoteroClient", Zotero)
    monkeypatch.setattr("arxiv_ra.web.localize_abstracts", lambda *args, **kwargs: None)
    downloads = []
    @contextmanager
    def stream(method, url, **kwargs):
        downloads.append(url)
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=str(req.url).encode()))) as transport:
            with transport.stream(method, url) as response:
                yield response
    monkeypatch.setattr("arxiv_ra.web.httpx.stream", stream)
    with TestClient(create_app(web_config(tmp_path))) as client:
        for version in (1, 2):
            p = paper(version).to_dict()
            # Deliberately use an unversioned legacy URL with a known revision.
            p["pdf_url"] = "https://arxiv.org/pdf/2609.00001"
            write_json(tmp_path / "run/2026-09-23/recommendations-a.json", [{"profile_id": "a", "paper": p}])
            response = client.post("/api/zotero/save-paper", data={"arxiv_id": p["arxiv_id"]})
            assert response.status_code == 200
    assert downloads == ["https://arxiv.org/pdf/2609.00001v1", "https://arxiv.org/pdf/2609.00001v2"]
    assert saved[0] != saved[1]


def test_zotero_imports_new_pdf_when_old_revision_exists(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"new revision")
    posts = []
    def handler(request):
        if request.url.path.endswith("/children"):
            return httpx.Response(200, json=[{"data": {"contentType": "application/pdf", "md5": "old-digest"}}])
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(200, json={"exists": 1})
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = ZoteroClient(ZoteroConfig(), client=http)
        monkeypatch.setattr(client, "_headers", lambda **kwargs: {})
        monkeypatch.setattr(client, "_template", lambda *args, **kwargs: {})
        monkeypatch.setattr(client, "_post_objects", lambda *args: "NEWPDF")
        assert client._create_imported_attachment("PAPER", pdf, "PDF v2", "application/pdf")
        assert posts


@pytest.mark.parametrize("damage", ["missing_end", "reversed", "duplicate", "invalid_yaml"])
def test_obsidian_refuses_to_replace_malformed_managed_notes(tmp_path, damage):
    exp = exporter(tmp_path)
    note = exp.sync_feedback(paper().to_dict(), {})
    text = note.read_text(encoding="utf-8")
    if damage == "missing_end":
        text = text.replace(MANAGED_END, "")
    elif damage == "reversed":
        text = MANAGED_END + text.replace(MANAGED_END, "")
    elif damage == "duplicate":
        text += MANAGED_START
    else:
        text = text.replace("---\n", "---\ninvalid: [\n", 1)
    note.write_text(text, encoding="utf-8")
    from arxiv_ra.obsidian import ObsidianError
    with pytest.raises(ObsidianError):
        exp.sync_feedback(paper().to_dict(), {})
    assert note.read_text(encoding="utf-8") == text


def test_report_evidence_does_not_substitute_when_exact_version_missing(tmp_path):
    from arxiv_ra.report_store import matching_report
    report(tmp_path, "a", 3, "OTHER VERSION")
    report(tmp_path, "b", 2, "OTHER PROFILE")
    assert matching_report(tmp_path / "run", "a", paper(2).to_dict()) is None
    assert matching_report(tmp_path / "run", "a", {"arxiv_id": paper().arxiv_id}) is None


def test_obsidian_same_display_name_does_not_share_paper_notes(tmp_path):
    a, b = exporter(tmp_path, "a"), exporter(tmp_path, "b")
    b.config.profile_name = "a"
    first = a.sync_feedback(paper().to_dict(), {})
    before = first.read_bytes()
    second = b.sync_feedback(paper().to_dict(), {"label": "other"})
    assert first != second
    assert first.read_bytes() == before


def test_obsidian_full_sync_prefers_numeric_latest_revision(tmp_path):
    exp = exporter(tmp_path)
    report(tmp_path, version=9, text="REVISION_NINE")
    report(tmp_path, version=10, text="REVISION_TEN")
    exp.sync_all()
    note = next((tmp_path / "vault").glob("**/Papers/a/*.md"))
    assert "REVISION_TEN" in note.read_text(encoding="utf-8")
    assert "REVISION_NINE" not in note.read_text(encoding="utf-8")


def test_zotero_export_uses_historical_recommendation_snapshot(tmp_path, monkeypatch):
    captured = []
    for day, version in (("2026-09-21", 1), ("2026-09-23", 2)):
        write_json(tmp_path / "run" / day / "recommendations-a.json",
                   [{"profile_id": "a", "paper": paper(version).to_dict()}])
    class Zotero:
        def __init__(self, _config):
            self.client = SimpleNamespace(close=lambda: None)
        def library_identity(self):
            return "test-library/users/0"
        def status(self):
            return {"ready": True}
        def save_paper(self, p, verified, profile, **kwargs):
            captured.append(p)
            return SimpleNamespace(to_dict=lambda: {"created": True, "attachments_added": 0})
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "test-only")
    monkeypatch.setattr("arxiv_ra.web.ZoteroClient", Zotero)
    path = web_config(tmp_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["zotero"] = {"attach_pdf": False}
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with TestClient(create_app(path)) as client:
        response = client.post("/api/zotero/save-paper", data={
            "arxiv_id": "2609.00001v1", "origin": "recommendation", "source_date": "2026-09-21"})
        assert response.status_code == 200
        assert captured[0]["version"] == 1
        missing = client.post("/api/zotero/save-paper", data={
            "arxiv_id": "2609.00001v2", "origin": "recommendation", "source_date": "2026-09-21"})
        assert missing.status_code == 404


def test_version_tracking_does_not_import_another_profiles_reports(tmp_path):
    from arxiv_ra.version_tracker import VersionTracker
    report(tmp_path, "b")
    cfg = config(tmp_path)
    cfg.version_tracking.include_feedback = cfg.version_tracking.include_zotero = False
    tracker = VersionTracker(cfg, tmp_path, clients=SimpleNamespace())
    assert tracker.tracked_sources() == {}
    report(tmp_path, "a")
    assert set(tracker.tracked_sources()) == {paper().arxiv_id}


def test_daily_publication_keeps_immutable_batches_on_delivery_failure(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    pipeline = DailyPipeline(cfg, tmp_path, clients=SimpleNamespace())
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    day = tmp_path / "run/2026-09-23"
    day.mkdir(parents=True)
    state = tmp_path / "run/state-a.json"
    def publish(p, deliver):
        return pipeline._publish_digest(day, day.name, [p], {p.arxiv_id: VerifiedMetadata()},
            [{"profile_id": "a", "paper": p.to_dict(), "verified": {}}],
            {p.arxiv_id}, state, now, demo=False, deliver=deliver)
    publish(paper(1), False)
    batch = next(day.glob("batches/a/*/recommendations.json"))
    original = batch.read_bytes()
    monkeypatch.setattr("arxiv_ra.pipeline.send_digest", Mock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError):
        publish(paper(2, "2609.00002"), True)
    assert batch.read_bytes() == original
    assert len(read_recommendations(tmp_path / "run", day.name, "a")) == 2
    assert read_json(state)["processed"] == ["2609.00001"]
    publish(paper(2, "2609.00002"), False)
    assert len(read_recommendations(tmp_path / "run", day.name, "a")) == 2


def test_obsidian_daily_aggregate_keeps_latest_revision(tmp_path):
    exp = exporter(tmp_path)
    exp.sync_daily("2026-09-23", [
        {"profile_id": "a", "paper": paper(2).to_dict()},
        {"profile_id": "a", "paper": paper(1).to_dict()},
    ])
    assert exp._manifest()["papers"]["a::2609.00001"]["version"] == 2
